//! Pub/sub ↔ local MQTT broker bridge (SF-032; docs/MESSAGING.md).
//!
//! Mappings are `{topic, source, target, target_topic_prefix?}` with source and
//! target `LocalMqtt` or `Pubsub` (and different). A standard MQTT 3.1.1/5
//! broker on this device serves LAN clients; the bridge connects to it with a
//! persistent session.
//!
//! - **Pubsub → LocalMqtt:** each mapping owns a durable QoS 1 subscription
//!   (`@bridge/<n>`). That queue is the bridge's on-disk spool: bounded, kept
//!   across restarts, and when full it refuses publications (backpressure,
//!   counted). One message per mapping is in flight; it leaves the spool only
//!   after the broker's PUBACK, so delivery is at least once and in order per
//!   mapping (a reconnect resends the in-flight message with DUP).
//! - **LocalMqtt → Pubsub:** the bridge subscribes at QoS 1 and acknowledges a
//!   message only after it is durably queued for every matching subscriber. If
//!   a subscriber queue is full it does not acknowledge, counts the refusal and
//!   reconnects later, so the broker redelivers; nothing is silently lost.
//! - **No loops:** messages the bridge put into pub/sub are never forwarded
//!   back out, and configurations whose outbound topics could re-enter an
//!   inbound mapping are refused.
//!
//! Payloads: Pubsub → MQTT sends the JSON encoding of the payload. MQTT →
//! Pubsub takes a JSON payload as that value, other UTF-8 as a string, and
//! anything else as `{"base64": …}`.
use crate::{mqtt, pubsub};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tracing::{info, warn};

pub const BRIDGE_PRINCIPAL: &str = "@bridge";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
pub enum Endpoint {
    LocalMqtt,
    Pubsub,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Mapping {
    /// Topic filter at the source.
    pub topic: String,
    pub source: Endpoint,
    pub target: Endpoint,
    /// Prepended to the source topic at the target.
    #[serde(default)]
    pub target_topic_prefix: String,
}

fn default_client_id() -> String {
    "suifleet-bridge".into()
}
fn default_keep_alive() -> u16 {
    30
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeConfig {
    /// `host:port` of the local MQTT broker.
    pub broker: String,
    #[serde(default = "default_client_id")]
    pub client_id: String,
    #[serde(default)]
    pub username: Option<String>,
    /// File holding the broker password (keep it 0600).
    #[serde(default)]
    pub password_file: Option<PathBuf>,
    #[serde(default = "default_keep_alive")]
    pub keep_alive_seconds: u16,
    #[serde(default)]
    pub mappings: Vec<Mapping>,
}

/// Whether two valid filters can match a common topic.
pub fn overlap(a: &str, b: &str) -> bool {
    let (mut x, mut y) = (a.split('/'), b.split('/'));
    loop {
        match (x.next(), y.next()) {
            (Some("#"), _) | (_, Some("#")) => return true,
            (None, None) => return true,
            (None, _) | (_, None) => return false,
            (Some(p), Some(q)) if p == "+" || q == "+" || p == q => continue,
            _ => return false,
        }
    }
}

impl BridgeConfig {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(self.broker.rsplit_once(':').is_some_and(|(h, p)| !h.is_empty() && p.parse::<u16>().is_ok()), "bridge.broker must be host:port");
        anyhow::ensure!(!self.client_id.is_empty() && self.client_id.len() <= 23 && self.client_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'),
            "bridge.client_id must be 1-23 letters, digits, - or _");
        anyhow::ensure!((5..=600).contains(&self.keep_alive_seconds), "bridge.keep_alive_seconds must be 5..=600");
        anyhow::ensure!(!self.mappings.is_empty() && self.mappings.len() <= 64, "bridge needs 1-64 mappings");
        for m in &self.mappings {
            anyhow::ensure!(m.source != m.target, "a bridge mapping's source and target must differ");
            anyhow::ensure!(pubsub::valid_filter(&m.topic), "invalid bridge mapping topic filter {:?}", m.topic);
            anyhow::ensure!(!m.target_topic_prefix.contains(['+', '#', '\0']) && m.target_topic_prefix.len() <= 128, "invalid target_topic_prefix");
        }
        // An outbound mapping's topics (prefix + filter) must not re-enter any
        // inbound mapping, or messages would circulate.
        for out in self.mappings.iter().filter(|m| m.source == Endpoint::Pubsub) {
            let produced = format!("{}{}", out.target_topic_prefix, out.topic);
            for inbound in self.mappings.iter().filter(|m| m.source == Endpoint::LocalMqtt) {
                anyhow::ensure!(!pubsub::valid_filter(&produced) || !overlap(&produced, &inbound.topic),
                    "bridge loop: Pubsub→LocalMqtt topics {produced:?} could match LocalMqtt→Pubsub filter {:?}", inbound.topic);
            }
        }
        Ok(())
    }
}

fn principal(index: usize) -> String {
    format!("{BRIDGE_PRINCIPAL}/{index}")
}

#[derive(Debug, Default, Clone, Serialize)]
pub struct MappingStats {
    pub topic: String,
    pub source: Option<Endpoint>,
    pub target: Option<Endpoint>,
    pub forwarded: u64,
    /// Messages not accepted because a subscriber queue was full.
    pub refused: u64,
}

#[derive(Debug, Default, Clone, Serialize)]
pub struct Stats {
    pub connected: bool,
    pub connects: u64,
    pub last_error: Option<String>,
    pub mappings: Vec<MappingStats>,
}

pub struct Bridge {
    pub config: BridgeConfig,
    pub broker: Arc<Mutex<pubsub::Broker>>,
    pub stats: Arc<Mutex<Stats>>,
    pub stats_path: PathBuf,
}

/// Register the outbound mappings' durable subscriptions (and drop stale ones).
pub fn register(config: &BridgeConfig, broker: &mut pubsub::Broker) -> anyhow::Result<()> {
    let wanted: Vec<(String, String)> = config.mappings.iter().enumerate()
        .filter(|(_, m)| m.source == Endpoint::Pubsub)
        .map(|(i, m)| (principal(i), m.topic.clone()))
        .collect();
    for stale in broker.stats(None).into_iter().filter_map(|s| {
        let (p, f) = (s["principal"].as_str()?.to_owned(), s["filter"].as_str()?.to_owned());
        (p.starts_with(BRIDGE_PRINCIPAL) && !wanted.contains(&(p.clone(), f.clone()))).then_some((p, f))
    }) {
        broker.unsubscribe(&stale.0, &stale.1)?;
    }
    for (p, f) in wanted {
        broker.subscribe(&p, &f, 1)?;
    }
    Ok(())
}

fn to_value(payload: &[u8]) -> Value {
    if let Ok(value) = serde_json::from_slice::<Value>(payload) {
        return value;
    }
    match std::str::from_utf8(payload) {
        Ok(text) => Value::String(text.to_owned()),
        Err(_) => json!({ "base64": STANDARD.encode(payload) }),
    }
}

struct InFlight {
    message_id: u64,
    packet_id: u16,
    topic: String,
    payload: Vec<u8>,
}

impl Bridge {
    pub fn new(config: BridgeConfig, broker: Arc<Mutex<pubsub::Broker>>, stats_path: PathBuf) -> anyhow::Result<Self> {
        register(&config, &mut broker.lock().unwrap())?;
        let stats = Stats {
            mappings: config.mappings.iter().map(|m| MappingStats { topic: m.topic.clone(), source: Some(m.source), target: Some(m.target), ..Default::default() }).collect(),
            ..Default::default()
        };
        Ok(Self { config, broker, stats: Arc::new(Mutex::new(stats)), stats_path })
    }

    fn save_stats(&self) {
        let stats = self.stats.lock().unwrap().clone();
        if let Ok(bytes) = serde_json::to_vec_pretty(&stats) {
            let temporary = self.stats_path.with_extension("tmp");
            if std::fs::write(&temporary, bytes).is_ok() {
                let _ = std::fs::rename(&temporary, &self.stats_path);
            }
        }
    }

    pub async fn run(self) {
        let password = self.config.password_file.as_ref().and_then(|p| std::fs::read(p).ok()).map(|mut b| {
            while b.last().is_some_and(|c| *c == b'\n' || *c == b'\r') {
                b.pop();
            }
            b
        });
        let mut inflight: Vec<Option<InFlight>> = self.config.mappings.iter().map(|_| None).collect();
        let mut backoff = Duration::from_secs(1);
        info!(broker = %self.config.broker, mappings = self.config.mappings.len(), "Starting the MQTT bridge");
        loop {
            let started = Instant::now();
            let result = self.session(password.as_deref(), &mut inflight).await;
            {
                let mut stats = self.stats.lock().unwrap();
                stats.connected = false;
                stats.last_error = result.as_ref().err().map(|e| format!("{e:#}"));
            }
            self.save_stats();
            if let Err(error) = result {
                warn!(%error, "MQTT bridge disconnected; reconnecting");
            }
            if started.elapsed() > Duration::from_secs(60) {
                backoff = Duration::from_secs(1);
            }
            tokio::time::sleep(backoff).await;
            backoff = (backoff * 2).min(Duration::from_secs(60));
        }
    }

    async fn session(&self, password: Option<&[u8]>, inflight: &mut [Option<InFlight>]) -> anyhow::Result<()> {
        let keep_alive = self.config.keep_alive_seconds;
        let (mut session, _) = mqtt::Session::open(&self.config.broker, &self.config.client_id, self.config.username.as_deref(), password, keep_alive).await?;
        {
            let mut stats = self.stats.lock().unwrap();
            stats.connected = true;
            stats.connects += 1;
            stats.last_error = None;
        }
        self.save_stats();
        let inbound: Vec<(String, u8)> = self.config.mappings.iter().filter(|m| m.source == Endpoint::LocalMqtt).map(|m| (m.topic.clone(), 1)).collect();
        let mut next_id: u16 = 1;
        let mut take_id = || {
            let id = next_id;
            next_id = if next_id == u16::MAX { 1 } else { next_id + 1 };
            id
        };
        if !inbound.is_empty() {
            session.send(&mqtt::subscribe(take_id(), &inbound)).await?;
        }
        // Resend what was in flight when the last session ended.
        for slot in inflight.iter_mut().flatten() {
            slot.packet_id = take_id();
            session.send(&mqtt::publish(&slot.topic, &slot.payload, 1, Some(slot.packet_id), true)).await?;
        }
        let mut last_sent = Instant::now();
        let mut last_heard = Instant::now();
        let mut tick = tokio::time::interval(Duration::from_millis(100));
        loop {
            tokio::select! {
                packet = session.next() => {
                    last_heard = Instant::now();
                    match packet? {
                        mqtt::Packet::Publish { qos, packet_id, topic, payload, .. } => {
                            self.inbound(&topic, &payload)?;
                            if qos == 1 {
                                session.send(&mqtt::puback(packet_id.unwrap_or(0))).await?;
                                last_sent = Instant::now();
                            }
                        }
                        mqtt::Packet::PubAck(id) => {
                            if let Some(index) = inflight.iter().position(|s| s.as_ref().is_some_and(|f| f.packet_id == id)) {
                                let done = inflight[index].take().unwrap();
                                self.broker.lock().unwrap().ack(&principal(index), &self.config.mappings[index].topic, done.message_id)?;
                                self.stats.lock().unwrap().mappings[index].forwarded += 1;
                                self.save_stats();
                            }
                        }
                        mqtt::Packet::SubAck { codes, .. } => anyhow::ensure!(!codes.contains(&0x80), "broker refused a bridge subscription"),
                        _ => {}
                    }
                }
                _ = tick.tick() => {
                    for (index, mapping) in self.config.mappings.iter().enumerate() {
                        if mapping.source != Endpoint::Pubsub || inflight[index].is_some() {
                            continue;
                        }
                        let next = self.broker.lock().unwrap().receive(&principal(index), &mapping.topic)?;
                        let Some(message) = next else { continue };
                        if message.sender.starts_with(BRIDGE_PRINCIPAL) {
                            // Came in from MQTT: never sent back out.
                            self.broker.lock().unwrap().ack(&principal(index), &mapping.topic, message.id)?;
                            continue;
                        }
                        let topic = format!("{}{}", mapping.target_topic_prefix, message.topic);
                        let payload = serde_json::to_vec(&message.payload)?;
                        let packet_id = take_id();
                        session.send(&mqtt::publish(&topic, &payload, 1, Some(packet_id), false)).await?;
                        last_sent = Instant::now();
                        inflight[index] = Some(InFlight { message_id: message.id, packet_id, topic, payload });
                    }
                    if last_sent.elapsed() >= Duration::from_secs(keep_alive as u64 / 2) {
                        session.send(&mqtt::pingreq()).await?;
                        last_sent = Instant::now();
                    }
                    anyhow::ensure!(last_heard.elapsed() < Duration::from_secs(keep_alive as u64 * 3 / 2), "broker silent beyond the keep-alive");
                }
            }
        }
    }

    /// One message from the broker into every matching inbound mapping.
    fn inbound(&self, topic: &str, payload: &[u8]) -> anyhow::Result<()> {
        let value = to_value(payload);
        for (index, mapping) in self.config.mappings.iter().enumerate() {
            if mapping.source != Endpoint::LocalMqtt || !pubsub::matches(&mapping.topic, topic) {
                continue;
            }
            let target = format!("{}{}", mapping.target_topic_prefix, topic);
            let sender = format!("{BRIDGE_PRINCIPAL}/LocalMqtt");
            let result = self.broker.lock().unwrap().publish(&sender, &target, &value, Some(BRIDGE_PRINCIPAL));
            match result {
                Ok(_) => self.stats.lock().unwrap().mappings[index].forwarded += 1,
                Err(error) if error.downcast_ref::<pubsub::Backpressure>().is_some() => {
                    self.stats.lock().unwrap().mappings[index].refused += 1;
                    self.save_stats();
                    // Not acknowledged: reconnect so the broker redelivers it.
                    anyhow::bail!("subscriber queue full for {target}; message left with the broker for redelivery");
                }
                Err(error) => return Err(error),
            }
        }
        self.save_stats();
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mapping(topic: &str, source: Endpoint, target: Endpoint, prefix: &str) -> Mapping {
        Mapping { topic: topic.into(), source, target, target_topic_prefix: prefix.into() }
    }

    fn config(mappings: Vec<Mapping>) -> BridgeConfig {
        BridgeConfig { broker: "127.0.0.1:1883".into(), client_id: "suifleet-bridge".into(), username: None, password_file: None, keep_alive_seconds: 30, mappings }
    }

    #[test]
    fn filters_overlap_as_mqtt_matching_allows() {
        for (a, b, expected) in [("a/#", "a/b/c", true), ("a/+/c", "a/b/+", true), ("a/b", "a/c", false), ("a/+", "a/b/c", false),
                                 ("#", "x", true), ("a", "a/#", true), ("lan/x", "dev/#", false)] {
            assert_eq!(overlap(a, b), expected, "{a} {b}");
            assert_eq!(overlap(b, a), expected, "{b} {a}");
        }
    }

    #[test]
    fn configuration_rules() {
        config(vec![mapping("sensors/#", Endpoint::Pubsub, Endpoint::LocalMqtt, "device1/"),
                    mapping("commands/#", Endpoint::LocalMqtt, Endpoint::Pubsub, "")]).validate().unwrap();
        assert!(config(vec![mapping("a/#", Endpoint::Pubsub, Endpoint::Pubsub, "")]).validate().is_err(), "same endpoints");
        assert!(config(vec![mapping("a/#/b", Endpoint::Pubsub, Endpoint::LocalMqtt, "")]).validate().is_err(), "invalid filter");
        assert!(config(vec![mapping("a/#", Endpoint::Pubsub, Endpoint::LocalMqtt, "x/+")]).validate().is_err(), "wildcard prefix");
        let looped = config(vec![mapping("sensors/#", Endpoint::Pubsub, Endpoint::LocalMqtt, "lan/"), mapping("lan/#", Endpoint::LocalMqtt, Endpoint::Pubsub, "")]);
        assert!(format!("{:#}", looped.validate().unwrap_err()).contains("bridge loop"));
        let mut bad = config(vec![mapping("a", Endpoint::Pubsub, Endpoint::LocalMqtt, "")]);
        bad.broker = "localhost".into();
        assert!(bad.validate().is_err());
    }

    #[test]
    fn payloads_convert_both_ways() {
        assert_eq!(to_value(br#"{"v":1}"#), json!({ "v": 1 }));
        assert_eq!(to_value(b"plain text"), json!("plain text"));
        assert_eq!(to_value(&[0xff, 0x00]), json!({ "base64": "/wA=" }));
    }

    #[test]
    fn register_keeps_only_current_outbound_subscriptions() {
        let dir = std::env::temp_dir().join(format!("suifleet-bridge-register-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut broker = pubsub::Broker::open(&dir, pubsub::Limits::default()).unwrap();
        broker.subscribe("@bridge/5", "old/#", 1).unwrap();
        broker.subscribe("component", "x", 1).unwrap();
        register(&config(vec![mapping("a/#", Endpoint::Pubsub, Endpoint::LocalMqtt, ""), mapping("b", Endpoint::LocalMqtt, Endpoint::Pubsub, "")]), &mut broker).unwrap();
        let mut subs: Vec<String> = broker.stats(None).iter().map(|s| format!("{} {}", s["principal"].as_str().unwrap(), s["filter"].as_str().unwrap())).collect();
        subs.sort();
        assert_eq!(subs, ["@bridge/0 a/#", "component x"]);
    }
}
