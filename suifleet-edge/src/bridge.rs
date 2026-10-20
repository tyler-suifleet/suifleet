//! Pub/sub ↔ MQTT bridge (SF-032; docs/MESSAGING.md) with the local broker
//! and AWS IoT Core (SF-061; docs/AWS_IOT.md).
//!
//! Mappings are `{topic, source, target, target_topic_prefix?}`: one side is
//! `Pubsub`, the other `LocalMqtt` or `IotCore`. A standard MQTT 3.1.1/5
//! broker on this device serves LAN clients; the bridge connects to it with a
//! persistent session. `IotCore` mappings use the `[aws_iot]` mutual-TLS
//! connection, which also carries AWS Device Shadow synchronization.
//!
//! - **Pubsub → LocalMqtt:** each mapping owns a durable QoS 1 subscription
//!   (`@bridge/<n>`). That queue is the bridge's on-disk spool: bounded, kept
//!   across restarts, and when full it refuses publications (backpressure,
//!   counted). One message per mapping is in flight; it leaves the spool only
//!   after the broker's PUBACK, so delivery is at least once and in order per
//!   mapping (a reconnect resends the in-flight message with DUP).
//! - **LocalMqtt → Pubsub:** the bridge subscribes at QoS 1 and acknowledges a
//!   message only after it is durably queued for every matching subscriber.
//!   The copies for all matching mappings are one commit, so a redelivery
//!   never replays copies already stored. If a subscriber queue lacks room it
//!   does not acknowledge, counts the refusal and reconnects later, so the
//!   broker redelivers; nothing is silently lost. A
//!   message pub/sub can never accept (its payload or prefixed topic exceeds
//!   the limits) is rejected instead: acknowledged so the broker does not
//!   redeliver it forever, logged and counted in the mapping's `rejected`.
//! - **No loops:** messages the bridge put into pub/sub from one broker are
//!   never forwarded back to that broker (they may continue to the other),
//!   and configurations whose outbound topics could re-enter an inbound
//!   mapping of the same broker are refused.
//! - **IotCore:** one message is in flight at a time, so a refusal can be
//!   attributed. AWS IoT Core disconnects a client that publishes where its
//!   policy denies; a message the broker closes the connection on three
//!   times in a row is held in the spool and reported (a truncated
//!   connection looks the same), or rejected with
//!   `reject_denied_publications`.
//!
//! Payloads: Pubsub → MQTT sends the JSON encoding of the payload. MQTT →
//! Pubsub takes a JSON payload as that value, other UTF-8 as a string, and
//! anything else as `{"base64": …}`.
use crate::{iotcore, mqtt, pubsub};
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
    IotCore,
}

impl Endpoint {
    /// The pub/sub sender of messages bridged in from this broker.
    fn sender(self) -> String {
        format!("{BRIDGE_PRINCIPAL}/{self:?}")
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
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

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeConfig {
    /// `host:port` of the local MQTT broker; needed by LocalMqtt mappings.
    #[serde(default)]
    pub broker: Option<String>,
    #[serde(default = "default_client_id")]
    pub client_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub username: Option<String>,
    /// File holding the broker password (keep it 0600).
    #[serde(default, skip_serializing_if = "Option::is_none")]
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

/// Whether `filter` could match a topic of some thing's AWS shadow service.
/// Filters starting with a wildcard never match `$` topics (MQTT §4.7.2).
pub fn reaches_aws_shadows(filter: &str) -> bool {
    filter.starts_with('$') && overlap(filter, "$aws/things/+/shadow/#")
}

impl BridgeConfig {
    /// `aws_iot`: whether the `[aws_iot]` connection is configured, which
    /// IotCore mappings need and which allows a bridge without mappings.
    pub fn validate(&self, aws_iot: bool) -> anyhow::Result<()> {
        let uses = |link: Endpoint| self.mappings.iter().any(|m| m.source == link || m.target == link);
        match &self.broker {
            Some(broker) => anyhow::ensure!(broker.rsplit_once(':').is_some_and(|(h, p)| !h.is_empty() && p.parse::<u16>().is_ok()), "bridge.broker must be host:port"),
            None => anyhow::ensure!(!uses(Endpoint::LocalMqtt), "LocalMqtt bridge mappings need bridge.broker"),
        }
        anyhow::ensure!(!self.client_id.is_empty() && self.client_id.len() <= 23 && self.client_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'),
            "bridge.client_id must be 1-23 letters, digits, - or _");
        anyhow::ensure!((5..=600).contains(&self.keep_alive_seconds), "bridge.keep_alive_seconds must be 5..=600");
        anyhow::ensure!((aws_iot || !self.mappings.is_empty()) && self.mappings.len() <= 64, "bridge needs 1-64 mappings");
        anyhow::ensure!(aws_iot || !uses(Endpoint::IotCore), "IotCore bridge mappings need the [aws_iot] connection");
        for m in &self.mappings {
            anyhow::ensure!(m.source != m.target, "a bridge mapping's source and target must differ");
            anyhow::ensure!(m.source == Endpoint::Pubsub || m.target == Endpoint::Pubsub,
                "bridge mappings connect Pubsub with LocalMqtt or IotCore; route LocalMqtt and IotCore through Pubsub with two mappings");
            anyhow::ensure!(pubsub::valid_filter(&m.topic), "invalid bridge mapping topic filter {:?}", m.topic);
            anyhow::ensure!(!m.target_topic_prefix.contains(['+', '#', '\0']) && m.target_topic_prefix.len() <= 128, "invalid target_topic_prefix");
            if m.source == Endpoint::IotCore || m.target == Endpoint::IotCore {
                // The thing's shadow topics belong to shadow synchronization:
                // the bridge neither publishes there nor injects cloud
                // messages as local shadow notifications.
                let produced = format!("{}{}", m.target_topic_prefix, m.topic);
                let cloud_side = if m.source == Endpoint::IotCore { &m.topic } else { &produced };
                anyhow::ensure!(!reaches_aws_shadows(cloud_side) && !reaches_aws_shadows(&produced),
                    "IotCore mapping {:?} reaches AWS shadow topics, which belong to shadow synchronization ([shadows.aws_iot])", m.topic);
                anyhow::ensure!(iotcore::within_aws_limits(cloud_side), "IotCore mapping topic {cloud_side:?} exceeds AWS IoT Core topic limits (256 bytes, 7 slashes)");
            }
        }
        // An outbound mapping's topics (prefix + filter) must not re-enter an
        // inbound mapping from the same broker, or messages would circulate.
        for out in self.mappings.iter().filter(|m| m.source == Endpoint::Pubsub) {
            let produced = format!("{}{}", out.target_topic_prefix, out.topic);
            for inbound in self.mappings.iter().filter(|m| m.source == out.target) {
                anyhow::ensure!(!pubsub::valid_filter(&produced) || !overlap(&produced, &inbound.topic),
                    "bridge loop: Pubsub→{:?} topics {produced:?} could match {:?}→Pubsub filter {:?}", out.target, out.target, inbound.topic);
            }
        }
        Ok(())
    }

    /// The most pub/sub copies one MQTT message can become: the largest set of
    /// inbound mappings from one broker whose filters overlap one mapping's
    /// filter (every mapping matching a topic overlaps each other mapping
    /// matching it).
    pub fn max_inbound_copies(&self) -> usize {
        [Endpoint::LocalMqtt, Endpoint::IotCore].into_iter().map(|link| {
            let inbound: Vec<&Mapping> = self.mappings.iter().filter(|m| m.source == link).collect();
            inbound.iter().map(|m| inbound.iter().filter(|n| overlap(&m.topic, &n.topic)).count()).max().unwrap_or(0)
        }).max().unwrap_or(0)
    }

    /// Inbound filters from `link`.
    fn inbound_filters(&self, link: Endpoint) -> Vec<(String, u8)> {
        self.mappings.iter().filter(|m| m.source == link).map(|m| (m.topic.clone(), 1)).collect()
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
    /// Inbound messages dropped because pub/sub can never accept them.
    pub rejected: u64,
    pub last_rejected: Option<String>,
    /// The message AWS IoT Core keeps closing the connection on (a likely
    /// policy denial). It stays in the spool and is retried; cleared on PUBACK.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub held: Option<String>,
}

/// The AWS IoT Core connection's state. Holds no secret.
#[derive(Debug, Default, Clone, Serialize)]
pub struct IotStats {
    pub endpoint: String,
    pub client_id: String,
    pub connected: bool,
    pub connects: u64,
    pub last_error: Option<String>,
    /// ALPN protocol the endpoint accepted.
    pub alpn: Option<String>,
    /// SHA-256 of the thing certificate in use.
    pub certificate_sha256: Option<String>,
    /// Credential sets taken into use after the first.
    pub rotations: u64,
    /// Why the files on disk were not taken into use (the old set stays).
    pub credential_error: Option<String>,
    /// Filters the endpoint's policy refused (SUBACK 0x80).
    pub refused_subscriptions: Vec<String>,
}

#[derive(Debug, Default, Clone, Serialize)]
pub struct Stats {
    /// The local broker connection.
    pub connected: bool,
    pub connects: u64,
    pub last_error: Option<String>,
    pub mappings: Vec<MappingStats>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub iot_core: Option<IotStats>,
}

/// The `[aws_iot]` connection served by the bridge.
pub struct IotLink {
    pub config: iotcore::IotConfig,
    /// Present when shadows synchronize with AWS IoT Device Shadow.
    pub shadows: Option<iotcore::ShadowEndpoint>,
}

pub struct Bridge {
    pub config: BridgeConfig,
    pub broker: Arc<Mutex<pubsub::Broker>>,
    pub stats: Arc<Mutex<Stats>>,
    pub stats_path: PathBuf,
    pub iot: Option<IotLink>,
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

/// The single message in flight on the IotCore link.
enum IotInFlight {
    Bridge { index: usize, message: InFlight, sent: Instant },
    Shadow { packet_id: u16 },
}

/// IotCore state kept across sessions.
#[derive(Default)]
struct IotState {
    inflight: Option<IotInFlight>,
    /// The bridged message the broker last closed the connection on, and
    /// how many times in a row.
    suspect: Option<(usize, u64, u32)>,
    /// Whether the in-flight bridged message was sent in the current session.
    sent_in_session: bool,
    /// Round-robin start among outbound mappings.
    next_mapping: usize,
}

/// Times in a row the broker may close the connection on one message
/// before it is held (or, with `reject_denied_publications`, rejected).
const POISON_CLOSES: u32 = 3;

/// Why an IotCore session ended without a transport error.
#[derive(Debug)]
struct Rotated;
impl std::fmt::Display for Rotated {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("AWS IoT credentials rotated; reconnecting with the new set")
    }
}
impl std::error::Error for Rotated {}

impl Bridge {
    pub fn new(config: BridgeConfig, broker: Arc<Mutex<pubsub::Broker>>, stats_path: PathBuf) -> anyhow::Result<Self> {
        register(&config, &mut broker.lock().unwrap())?;
        let stats = Stats {
            mappings: config.mappings.iter().map(|m| MappingStats { topic: m.topic.clone(), source: Some(m.source), target: Some(m.target), ..Default::default() }).collect(),
            ..Default::default()
        };
        Ok(Self { config, broker, stats: Arc::new(Mutex::new(stats)), stats_path, iot: None })
    }

    /// Serve the AWS IoT Core connection too.
    pub fn with_iot_core(mut self, link: IotLink) -> Self {
        self.stats.lock().unwrap().iot_core = Some(IotStats { endpoint: link.config.address(), client_id: link.config.client_id().into(), ..Default::default() });
        self.iot = Some(link);
        self
    }

    fn uses(&self, link: Endpoint) -> bool {
        self.config.mappings.iter().any(|m| m.source == link || m.target == link)
    }

    fn iot_stats(&self, update: impl FnOnce(&mut IotStats)) {
        if let Some(stats) = self.stats.lock().unwrap().iot_core.as_mut() {
            update(stats);
        }
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
        let iot = self.iot.as_ref().map(|link| link.shadows.clone());
        let this = &self;
        tokio::join!(
            async move {
                if this.uses(Endpoint::LocalMqtt) {
                    this.run_local().await
                }
            },
            async move {
                if let Some(router) = iot {
                    this.run_iot(router).await
                }
            },
        );
    }

    async fn run_local(&self) {
        let password = self.config.password_file.as_ref().and_then(|p| std::fs::read(p).ok()).map(|mut b| {
            while b.last().is_some_and(|c| *c == b'\n' || *c == b'\r') {
                b.pop();
            }
            b
        });
        let mut inflight: Vec<Option<InFlight>> = self.config.mappings.iter().map(|_| None).collect();
        let mut backoff = Duration::from_secs(1);
        let broker = self.config.broker.clone().unwrap_or_default();
        info!(%broker, mappings = self.config.mappings.len(), "Starting the MQTT bridge");
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
        let broker = self.config.broker.as_deref().ok_or_else(|| anyhow::anyhow!("no local broker configured"))?;
        let (mut session, _) = mqtt::Session::open(broker, &self.config.client_id, self.config.username.as_deref(), password, keep_alive).await?;
        {
            let mut stats = self.stats.lock().unwrap();
            stats.connected = true;
            stats.connects += 1;
            stats.last_error = None;
        }
        self.save_stats();
        let inbound = self.config.inbound_filters(Endpoint::LocalMqtt);
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
                            self.inbound(Endpoint::LocalMqtt, &topic, &payload)?;
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
                        if mapping.source != Endpoint::Pubsub || mapping.target != Endpoint::LocalMqtt || inflight[index].is_some() {
                            continue;
                        }
                        let next = self.broker.lock().unwrap().receive(&principal(index), &mapping.topic)?;
                        let Some(message) = next else { continue };
                        if message.sender == Endpoint::LocalMqtt.sender() {
                            // Came in from this broker: never sent back to it.
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

    /// The AWS IoT Core connection: (re)connects with backoff, takes rotated
    /// credentials into use, and keeps the in-flight message across sessions.
    async fn run_iot(&self, shadows: Option<iotcore::ShadowEndpoint>) {
        let (mut shadows, mut requests) = match shadows {
            Some(endpoint) => {
                let (router, requests) = endpoint.attach().await;
                (Some(router), Some(requests))
            }
            None => (None, None),
        };
        let config = &self.iot.as_ref().expect("IotCore link configured").config;
        info!(endpoint = %config.address(), thing = %config.thing_name, "Starting the AWS IoT Core connection");
        let mut credentials: Option<iotcore::Credentials> = None;
        let mut state = IotState::default();
        let mut backoff = Duration::from_secs(1);
        loop {
            // Load the files on first use, after rotation, or after a failure to load.
            if credentials.as_ref().is_none_or(|c| c.rotated(config)) {
                match iotcore::Credentials::load(config) {
                    Ok(loaded) => {
                        let rotated = credentials.is_some();
                        self.iot_stats(|s| {
                            s.rotations += rotated as u64;
                            s.credential_error = None;
                        });
                        if rotated {
                            info!(certificate_sha256 = %loaded.certificate_sha256, "AWS IoT credentials rotated");
                        }
                        credentials = Some(loaded);
                    }
                    Err(error) => {
                        warn!(%error, "AWS IoT credentials not usable; keeping the current set");
                        self.iot_stats(|s| s.credential_error = Some(format!("{error:#}")));
                    }
                }
            }
            let started = Instant::now();
            let result = match &credentials {
                Some(credentials) => self.iot_session(config, credentials, shadows.as_mut(), requests.as_deref_mut(), &mut state).await,
                None => Err(anyhow::anyhow!("no usable AWS IoT credentials")),
            };
            if let Some(router) = shadows.as_mut() {
                router.set_connected(false);
                router.fail_all("the AWS IoT Core connection ended");
            }
            if matches!(state.inflight, Some(IotInFlight::Shadow { .. })) {
                state.inflight = None;
            }
            let rotated = result.as_ref().err().is_some_and(|e| e.downcast_ref::<Rotated>().is_some());
            if let Err(error) = &result {
                if error.downcast_ref::<mqtt::Closed>().is_some() {
                    self.closed_on_inflight(&mut state, config.reject_denied_publications);
                }
                if !rotated {
                    warn!(%error, "AWS IoT Core connection ended; reconnecting");
                }
            }
            self.iot_stats(|s| {
                s.connected = false;
                s.last_error = result.as_ref().err().filter(|_| !rotated).map(|e| format!("{e:#}"));
            });
            self.save_stats();
            if rotated {
                continue;
            }
            if started.elapsed() > Duration::from_secs(60) {
                backoff = Duration::from_secs(1);
            }
            // Wait out the backoff, but retry at once with newly installed credentials.
            let until = Instant::now() + backoff;
            backoff = (backoff * 2).min(Duration::from_secs(60));
            while Instant::now() < until {
                tokio::time::sleep((until - Instant::now()).min(Duration::from_secs(config.credential_check_seconds))).await;
                if credentials.as_ref().is_some_and(|c| c.rotated(config)) {
                    backoff = Duration::from_secs(1);
                    break;
                }
            }
        }
    }

    /// The broker acknowledged the in-flight bridged message.
    fn delivered(&self, state: &mut IotState) -> anyhow::Result<()> {
        if let Some(IotInFlight::Bridge { index, message, .. }) = state.inflight.take() {
            self.broker.lock().unwrap().ack(&principal(index), &self.config.mappings[index].topic, message.message_id)?;
            let mut stats = self.stats.lock().unwrap();
            stats.mappings[index].forwarded += 1;
            stats.mappings[index].held = None;
            drop(stats);
            state.suspect = None;
            self.save_stats();
        }
        Ok(())
    }

    /// The broker closed the connection with a bridged message in flight.
    /// Soon after sending it, that is how AWS IoT Core refuses a publication
    /// its policy denies, but a truncated connection looks the same. After
    /// `POISON_CLOSES` in a row the message is held: kept in the spool,
    /// reported and retried. Only with `reject` (the operator's
    /// `reject_denied_publications`) is it removed and counted as rejected.
    fn closed_on_inflight(&self, state: &mut IotState, reject: bool) {
        let Some(IotInFlight::Bridge { index, message, sent }) = &state.inflight else { return };
        if !state.sent_in_session || sent.elapsed() > Duration::from_secs(5) {
            return;
        }
        let count = match state.suspect {
            Some((i, id, n)) if i == *index && id == message.message_id => n.saturating_add(1),
            _ => 1,
        };
        state.suspect = Some((*index, message.message_id, count));
        if count < POISON_CLOSES {
            return;
        }
        let (index, id, topic) = (*index, message.message_id, message.topic.clone());
        let reason = format!("AWS IoT Core closed the connection each of {count} times it was sent (policy denial?)");
        if !reject {
            if count == POISON_CLOSES {
                warn!(%topic, mapping = index, %reason, "Holding a bridged message; it stays in the spool and is retried");
            }
            self.stats.lock().unwrap().mappings[index].held = Some(format!("{topic}: {reason}"));
            return;
        }
        warn!(%topic, mapping = index, %reason, "Rejecting a bridged message (reject_denied_publications)");
        if let Err(error) = self.broker.lock().unwrap().ack(&principal(index), &self.config.mappings[index].topic, id) {
            warn!(%error, "Could not remove the rejected message from the spool");
            return;
        }
        let mut stats = self.stats.lock().unwrap();
        stats.mappings[index].rejected += 1;
        stats.mappings[index].last_rejected = Some(format!("{topic}: {reason}"));
        stats.mappings[index].held = None;
        state.inflight = None;
        state.suspect = None;
    }

    async fn iot_session(&self, config: &iotcore::IotConfig, credentials: &iotcore::Credentials,
                         shadows: Option<&mut iotcore::ShadowRouter>, requests: Option<&mut iotcore::ShadowRequests>,
                         state: &mut IotState) -> anyhow::Result<()> {
        state.sent_in_session = false;
        let (stream, alpn) = iotcore::connect(config, credentials).await?;
        let (session, _) = mqtt::Session::start(stream, config.client_id(), None, None, config.keep_alive_seconds).await?;
        self.iot_stats(|s| {
            s.connected = true;
            s.connects += 1;
            s.last_error = None;
            s.alpn = alpn;
            s.certificate_sha256 = Some(credentials.certificate_sha256.clone());
            s.refused_subscriptions.clear();
        });
        self.save_stats();
        info!(endpoint = %config.address(), certificate_sha256 = %credentials.certificate_sha256, "Connected to AWS IoT Core");
        self.iot_exchange(config, session, Some(credentials), shadows, requests, state).await
    }

    /// A connected IotCore session until it ends: subscriptions, bridged
    /// messages both ways, shadow requests, keep-alive and, with
    /// `credentials`, the rotation check.
    async fn iot_exchange(&self, config: &iotcore::IotConfig, mut session: mqtt::Session, credentials: Option<&iotcore::Credentials>,
                          mut shadows: Option<&mut iotcore::ShadowRouter>, mut requests: Option<&mut iotcore::ShadowRequests>,
                          state: &mut IotState) -> anyhow::Result<()> {
        let keep_alive = config.keep_alive_seconds;
        let mut next_id: u16 = 1;
        let mut take_id = || {
            let id = next_id;
            next_id = if next_id == u16::MAX { 1 } else { next_id + 1 };
            id
        };
        // At most 8 filters per SUBSCRIBE (AWS IoT Core).
        let mut filters = self.config.inbound_filters(Endpoint::IotCore);
        filters.extend(shadows.as_ref().map(|r| r.filters.clone()).unwrap_or_default().into_iter().map(|f| (f, 1)));
        let mut awaiting: Vec<(u16, Vec<String>)> = Vec::new();
        for batch in filters.chunks(iotcore::SUBSCRIBE_BATCH) {
            let id = take_id();
            session.send(&mqtt::subscribe(id, batch)).await?;
            awaiting.push((id, batch.iter().map(|(f, _)| f.clone()).collect()));
        }
        if awaiting.is_empty() {
            if let Some(router) = shadows.as_mut() {
                router.set_connected(true);
            }
        }
        // Resend what was in flight when the last session ended.
        if let Some(IotInFlight::Bridge { message, sent, .. }) = state.inflight.as_mut() {
            message.packet_id = take_id();
            *sent = Instant::now();
            state.sent_in_session = true;
            session.send(&mqtt::publish(&message.topic, &message.payload, 1, Some(message.packet_id), true)).await?;
        }
        let outbound: Vec<usize> = self.config.mappings.iter().enumerate()
            .filter(|(_, m)| m.source == Endpoint::Pubsub && m.target == Endpoint::IotCore).map(|(i, _)| i).collect();
        let mut last_sent = Instant::now();
        let mut last_heard = Instant::now();
        let mut tick = tokio::time::interval(Duration::from_millis(100));
        let mut credential_check = tokio::time::interval(Duration::from_secs(config.credential_check_seconds));
        credential_check.tick().await;
        let mut refused_digest = None;
        loop {
            let ready = state.inflight.is_none() && awaiting.is_empty();
            let request = async {
                match requests.as_mut() {
                    Some(requests) if ready => requests.next().await,
                    _ => std::future::pending().await,
                }
            };
            tokio::select! {
                packet = session.next() => {
                    last_heard = Instant::now();
                    match packet? {
                        mqtt::Packet::Publish { qos, packet_id, topic, payload, .. } => {
                            match shadows.as_mut() {
                                Some(router) if router.owns(&topic) => router.deliver(&topic, &payload),
                                _ => self.inbound(Endpoint::IotCore, &topic, &payload)?,
                            }
                            if qos == 1 {
                                session.send(&mqtt::puback(packet_id.unwrap_or(0))).await?;
                                last_sent = Instant::now();
                            }
                        }
                        mqtt::Packet::PubAck(id) => match &state.inflight {
                            Some(IotInFlight::Bridge { message, .. }) if message.packet_id == id => self.delivered(state)?,
                            Some(IotInFlight::Shadow { packet_id }) if *packet_id == id => state.inflight = None,
                            _ => {}
                        },
                        mqtt::Packet::SubAck { packet_id, codes } => {
                            if let Some(at) = awaiting.iter().position(|(id, _)| *id == packet_id) {
                                let (_, batch) = awaiting.remove(at);
                                let refused: Vec<String> = batch.into_iter().zip(codes.iter().chain(std::iter::repeat(&0x80)))
                                    .filter(|(_, code)| **code == 0x80).map(|(f, _)| f).collect();
                                if !refused.is_empty() {
                                    warn!(?refused, "AWS IoT Core refused subscriptions (policy)");
                                    self.iot_stats(|s| s.refused_subscriptions.extend(refused));
                                    self.save_stats();
                                }
                                if awaiting.is_empty() {
                                    if let Some(router) = shadows.as_mut() {
                                        router.set_connected(true);
                                    }
                                }
                            }
                        }
                        _ => {}
                    }
                }
                Some(request) = request => {
                    let packet_id = take_id();
                    let (topic, payload, token) = (request.topic.clone(), request.payload.clone(), request.token.clone());
                    let router = shadows.as_mut().expect("requests come from the router");
                    router.sent(request);
                    if let Err(error) = session.send(&mqtt::publish(&topic, &payload, 1, Some(packet_id), false)).await {
                        router.fail(&token, "the AWS IoT Core connection failed");
                        return Err(error);
                    }
                    last_sent = Instant::now();
                    state.inflight = Some(IotInFlight::Shadow { packet_id });
                }
                _ = tick.tick() => {
                    if state.inflight.is_none() && awaiting.is_empty() && !outbound.is_empty() {
                        for step in 0..outbound.len() {
                            let index = outbound[(state.next_mapping + step) % outbound.len()];
                            let mapping = &self.config.mappings[index];
                            let next = self.broker.lock().unwrap().receive(&principal(index), &mapping.topic)?;
                            let Some(message) = next else { continue };
                            let topic = format!("{}{}", mapping.target_topic_prefix, message.topic);
                            if message.sender == Endpoint::IotCore.sender() || !iotcore::within_aws_limits(&topic) {
                                // From AWS IoT Core: never sent back. Beyond AWS limits: can never be accepted.
                                self.broker.lock().unwrap().ack(&principal(index), &mapping.topic, message.id)?;
                                if message.sender != Endpoint::IotCore.sender() {
                                    warn!(%topic, mapping = index, "IotCore bridge rejected a topic beyond AWS IoT Core limits");
                                    let mut stats = self.stats.lock().unwrap();
                                    stats.mappings[index].rejected += 1;
                                    stats.mappings[index].last_rejected = Some(format!("{topic}: exceeds AWS IoT Core topic limits"));
                                }
                                continue;
                            }
                            let payload = serde_json::to_vec(&message.payload)?;
                            let packet_id = take_id();
                            session.send(&mqtt::publish(&topic, &payload, 1, Some(packet_id), false)).await?;
                            last_sent = Instant::now();
                            state.inflight = Some(IotInFlight::Bridge { index, message: InFlight { message_id: message.id, packet_id, topic, payload }, sent: Instant::now() });
                            state.sent_in_session = true;
                            state.next_mapping = (state.next_mapping + step + 1) % outbound.len();
                            break;
                        }
                    }
                    if last_sent.elapsed() >= Duration::from_secs(keep_alive as u64 / 2) {
                        session.send(&mqtt::pingreq()).await?;
                        last_sent = Instant::now();
                    }
                    anyhow::ensure!(last_heard.elapsed() < Duration::from_secs(keep_alive as u64 * 3 / 2), "AWS IoT Core silent beyond the keep-alive");
                }
                _ = credential_check.tick() => {
                    // A complete new set ends the session; the caller reconnects with it.
                    if credentials.is_some_and(|c| c.rotated(config)) {
                        match iotcore::Credentials::load(config) {
                            Ok(_) => return Err(Rotated.into()),
                            Err(error) => {
                                let message = format!("{error:#}");
                                if refused_digest.as_ref() != Some(&message) {
                                    warn!(error = %message, "Rotated AWS IoT credentials are not usable yet; keeping the current set");
                                    self.iot_stats(|s| s.credential_error = Some(message.clone()));
                                    self.save_stats();
                                    refused_digest = Some(message);
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    /// One message from the broker into every matching inbound mapping, as
    /// one pub/sub commit: every mapping's copy is stored or none is, so a
    /// redelivery never replays copies that were already stored. An error
    /// leaves it unacknowledged for redelivery. A mapping whose target can
    /// never accept it rejects it; if the copies together can never fit the
    /// subscription limits, every mapping rejects it.
    fn inbound(&self, link: Endpoint, topic: &str, payload: &[u8]) -> anyhow::Result<()> {
        let value = to_value(payload);
        let sender = link.sender();
        let (mut targets, mut rejected) = (Vec::new(), Vec::new());
        for (index, mapping) in self.config.mappings.iter().enumerate() {
            if mapping.source != link || !pubsub::matches(&mapping.topic, topic) {
                continue;
            }
            let target = format!("{}{}", mapping.target_topic_prefix, topic);
            match pubsub::invalid_publication(&target, &value) {
                Some(reason) => rejected.push((index, reason)),
                None => targets.push((index, target)),
            }
        }
        if !targets.is_empty() {
            let publications: Vec<(String, Value)> = targets.iter().map(|(_, target)| (target.clone(), value.clone())).collect();
            let result = self.broker.lock().unwrap().publish_all(&sender, &publications, Some(BRIDGE_PRINCIPAL));
            match result {
                Ok(_) => {
                    let mut stats = self.stats.lock().unwrap();
                    for (index, _) in &targets {
                        stats.mappings[*index].forwarded += 1;
                    }
                }
                Err(error) if error.downcast_ref::<pubsub::Backpressure>().is_some() => {
                    {
                        let mut stats = self.stats.lock().unwrap();
                        for (index, _) in &targets {
                            stats.mappings[*index].refused += 1;
                        }
                    }
                    self.save_stats();
                    // Not acknowledged: reconnect so the broker redelivers it.
                    anyhow::bail!("{error}; message on {topic} left with the broker for redelivery");
                }
                Err(error) if error.downcast_ref::<pubsub::Unfittable>().is_some() => {
                    rejected.extend(targets.iter().map(|(index, _)| (*index, error.to_string())));
                }
                Err(error) => return Err(error),
            }
        }
        // Counted only once the message is acknowledged, not per redelivery.
        for (index, reason) in rejected {
            warn!(topic, mapping = index, %reason, "MQTT bridge rejected an inbound message pub/sub can never accept");
            let mut stats = self.stats.lock().unwrap();
            stats.mappings[index].rejected += 1;
            stats.mappings[index].last_rejected = Some(format!("{topic}: {reason}"));
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
        BridgeConfig { broker: Some("127.0.0.1:1883".into()), client_id: "suifleet-bridge".into(), username: None, password_file: None, keep_alive_seconds: 30, mappings }
    }

    /// A test MQTT peer standing in for AWS IoT Core.
    struct Peer {
        stream: tokio::net::TcpStream,
        buffer: Vec<u8>,
    }

    impl Peer {
        /// The next packet and its raw bytes, or None after `wait`.
        async fn next_within(&mut self, wait: Duration) -> Option<(mqtt::Packet, Vec<u8>)> {
            use tokio::io::AsyncReadExt;
            let deadline = Instant::now() + wait;
            loop {
                if let Some((packet, used)) = mqtt::decode(&self.buffer).unwrap() {
                    let raw = self.buffer.drain(..used).collect();
                    return Some((packet, raw));
                }
                let mut chunk = [0u8; 4096];
                let read = tokio::time::timeout(deadline.saturating_duration_since(Instant::now()), self.stream.read(&mut chunk)).await.ok()?.unwrap();
                assert!(read > 0, "the bridge closed the connection");
                self.buffer.extend_from_slice(&chunk[..read]);
            }
        }
        async fn next(&mut self) -> (mqtt::Packet, Vec<u8>) {
            self.next_within(Duration::from_secs(10)).await.expect("no packet from the bridge")
        }
        async fn send(&mut self, bytes: &[u8]) {
            use tokio::io::AsyncWriteExt;
            self.stream.write_all(bytes).await.unwrap();
        }
    }

    /// The packet id and filters of a raw SUBSCRIBE.
    fn subscribe_request(raw: &[u8]) -> (u16, Vec<String>) {
        let mut at = 1;
        while raw[at] & 0x80 != 0 {
            at += 1;
        }
        at += 1;
        let id = u16::from_be_bytes([raw[at], raw[at + 1]]);
        at += 2;
        let mut filters = Vec::new();
        while at < raw.len() {
            let length = u16::from_be_bytes([raw[at], raw[at + 1]]) as usize;
            filters.push(String::from_utf8(raw[at + 2..at + 2 + length].to_vec()).unwrap());
            at += 3 + length;
        }
        (id, filters)
    }

    /// The IotCore session over a loopback peer: a refused subscription is
    /// reported and the rest continue; a LAN message continues to AWS while
    /// one from AWS is never sent back; one message is in flight at a time;
    /// a shadow request is answered by its clientToken.
    #[tokio::test]
    async fn iot_core_session_routes_both_ways_and_answers_shadow_requests() {
        let dir = std::env::temp_dir().join(format!("suifleet-bridge-iot-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut store = pubsub::Broker::open(&dir.join("pubsub"), pubsub::Limits::default()).unwrap();
        store.subscribe("component", "cmd/#", 1).unwrap();
        let broker = Arc::new(Mutex::new(store));
        let mut cfg = config(vec![mapping("lan/#", Endpoint::LocalMqtt, Endpoint::Pubsub, ""), mapping("lan/#", Endpoint::Pubsub, Endpoint::IotCore, "dt/"),
                                  mapping("cmd/#", Endpoint::IotCore, Endpoint::Pubsub, ""), mapping("cmd/#", Endpoint::Pubsub, Endpoint::IotCore, "echo/"),
                                  mapping("denied/#", Endpoint::IotCore, Endpoint::Pubsub, "")]);
        cfg.broker = Some("127.0.0.1:1".into());
        cfg.validate(true).unwrap();
        let iot: iotcore::IotConfig = serde_json::from_value(json!({"endpoint": "localhost", "thing_name": "dev1", "ca_file": "/a",
            "certificate_file": "/b", "private_key_file": "/c", "keep_alive_seconds": 30})).unwrap();
        let (client, endpoint) = iotcore::shadow_channel("dev1", &["cfg".into()]);
        let (mut router, mut requests) = endpoint.attach().await;
        let bridge = Bridge::new(cfg, broker.clone(), dir.join("bridge.json")).unwrap().with_iot_core(IotLink { config: iot.clone(), shadows: None });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let accept = async {
            let (stream, _) = listener.accept().await.unwrap();
            let mut peer = Peer { stream, buffer: Vec::new() };
            assert_eq!(peer.next().await.0, mqtt::Packet::Other(1), "CONNECT");
            peer.send(&[0x20, 2, 0, 0]).await;
            peer
        };
        let connect = async {
            let tcp = mqtt::connect_tcp(&listener.local_addr().unwrap().to_string()).await.unwrap();
            mqtt::Session::start(Box::new(tcp), "dev1", None, None, 30).await.unwrap().0
        };
        let (mut peer, session) = tokio::join!(accept, connect);
        let received = |broker: &Arc<Mutex<pubsub::Broker>>| {
            let mut b = broker.lock().unwrap();
            let message = b.receive("component", "cmd/#").unwrap();
            if let Some(m) = &message {
                b.ack("component", "cmd/#", m.id).unwrap();
            }
            message.map(|m| (m.topic, m.payload, m.sender))
        };
        let script = async {
            let (packet, raw) = peer.next().await;
            assert_eq!(packet, mqtt::Packet::Other(8), "SUBSCRIBE");
            let (id, filters) = subscribe_request(&raw);
            assert_eq!(filters, ["cmd/#", "denied/#", "$aws/things/dev1/shadow/name/cfg/get/+",
                                 "$aws/things/dev1/shadow/name/cfg/update/+", "$aws/things/dev1/shadow/name/cfg/delete/+"]);
            let mut suback = id.to_be_bytes().to_vec();
            suback.extend(filters.iter().map(|f| if f == "denied/#" { 0x80 } else { 1 }));
            peer.send(&mqtt::packet(0x90, suback)).await;

            // LAN -> pub/sub -> AWS, one message in flight at a time.
            bridge.inbound(Endpoint::LocalMqtt, "lan/x", br#"{"v":1}"#).unwrap();
            bridge.inbound(Endpoint::LocalMqtt, "lan/y", br#"{"v":2}"#).unwrap();
            let (packet, _) = peer.next().await;
            let mqtt::Packet::Publish { topic, payload, packet_id: Some(first), qos: 1, .. } = packet else { panic!("{packet:?}") };
            assert_eq!((topic.as_str(), payload.as_slice()), ("dt/lan/x", br#"{"v":1}"#.as_slice()));
            assert!(peer.next_within(Duration::from_millis(500)).await.is_none(), "a second message was sent before the PUBACK");
            peer.send(&mqtt::puback(first)).await;
            let (packet, _) = peer.next().await;
            let mqtt::Packet::Publish { topic, packet_id: Some(second), .. } = packet else { panic!("{packet:?}") };
            assert_eq!(topic, "dt/lan/y");
            peer.send(&mqtt::puback(second)).await;

            // AWS -> pub/sub, acknowledged once stored, never echoed back to AWS.
            peer.send(&mqtt::publish("cmd/z", br#"{"c":1}"#, 1, Some(7), false)).await;
            assert_eq!(peer.next().await.0, mqtt::Packet::PubAck(7));
            assert_eq!(received(&broker), Some(("cmd/z".into(), json!({"c": 1}), "@bridge/IotCore".into())));
            assert!(peer.next_within(Duration::from_millis(500)).await.is_none(), "a message from AWS was sent back to AWS");

            // A shadow request and its answer.
            let call = tokio::spawn({
                let client = client.clone();
                async move { client.call("cfg", "get", serde_json::Map::new()).await }
            });
            let (packet, _) = peer.next().await;
            let mqtt::Packet::Publish { topic, payload, packet_id: Some(id), .. } = packet else { panic!("{packet:?}") };
            assert_eq!(topic, "$aws/things/dev1/shadow/name/cfg/get");
            let token = serde_json::from_slice::<Value>(&payload).unwrap()["clientToken"].clone();
            peer.send(&mqtt::puback(id)).await;
            let answer = serde_json::to_vec(&json!({"clientToken": token, "version": 3, "state": {}})).unwrap();
            peer.send(&mqtt::publish(&format!("{topic}/accepted"), &answer, 1, Some(9), false)).await;
            assert_eq!(peer.next().await.0, mqtt::Packet::PubAck(9));
            let (accepted, value) = call.await.unwrap().unwrap();
            assert!(accepted && value["version"] == 3);
        };
        let mut state = IotState::default();
        tokio::time::timeout(Duration::from_secs(30), async {
            tokio::select! {
                result = bridge.iot_exchange(&iot, session, None, Some(&mut router), Some(&mut *requests), &mut state) => panic!("session ended: {result:?}"),
                () = script => {}
            }
        }).await.unwrap();
        let stats = bridge.stats.lock().unwrap().clone();
        assert_eq!(stats.iot_core.unwrap().refused_subscriptions, ["denied/#"]);
        assert_eq!(stats.mappings.iter().map(|m| m.forwarded).collect::<Vec<_>>(), [2, 2, 1, 0, 0]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn aws_iot_cases() -> Value {
        serde_json::from_str(include_str!("../../interfaces/compat/aws-iot-cases.json")).unwrap()
    }

    /// interfaces/compat/aws-iot-cases.json: IotCore mapping rules.
    #[test]
    fn shared_aws_iot_mapping_cases() {
        let cases = aws_iot_cases();
        for case in cases["mappings"].as_array().unwrap() {
            let mut c = config(serde_json::from_value(case["mappings"].clone()).unwrap());
            c.broker = case["broker"].as_str().map(str::to_owned);
            let result = c.validate(case["aws_iot"].as_bool().unwrap());
            match case["expected"]["error"].as_str() {
                None => result.unwrap_or_else(|e| panic!("{}: {e:#}", case["id"])),
                Some(error) => {
                    let message = format!("{:#}", result.err().unwrap_or_else(|| panic!("{} was accepted", case["id"])));
                    assert!(message.contains(error), "{}: {message}", case["id"]);
                }
            }
        }
    }

    /// interfaces/compat/aws-iot-cases.json: a bridged message AWS IoT Core
    /// closes the connection on soon after it was sent, three times in a
    /// row, is held in the spool (rejected only with
    /// `reject_denied_publications`); other closes, transport errors and PUBACKs.
    #[test]
    fn shared_aws_iot_denied_publication_cases() {
        for case in aws_iot_cases()["denied_publication"].as_array().unwrap() {
            let dir = std::env::temp_dir().join(format!("suifleet-bridge-denied-{}-{}", std::process::id(), case["id"].as_str().unwrap()));
            let _ = std::fs::remove_dir_all(&dir);
            let broker = Arc::new(Mutex::new(pubsub::Broker::open(&dir.join("pubsub"), pubsub::Limits::default()).unwrap()));
            let bridge = Bridge::new(config(vec![mapping("t/#", Endpoint::Pubsub, Endpoint::IotCore, "")]), broker.clone(), dir.join("bridge.json")).unwrap();
            let reject = case["reject_denied_publications"].as_bool().unwrap();
            let mut state = IotState::default();
            let (mut rejected, mut forwarded) = (0, 0);
            for (n, step) in case["steps"].as_array().unwrap().iter().enumerate() {
                if state.inflight.is_none() {
                    broker.lock().unwrap().publish("component", &format!("t/{n}"), &json!(n), None).unwrap();
                    let message = broker.lock().unwrap().receive("@bridge/0", "t/#").unwrap().unwrap();
                    state.inflight = Some(IotInFlight::Bridge { index: 0, message: InFlight { message_id: message.id, packet_id: 1, topic: message.topic, payload: Vec::new() },
                        sent: Instant::now() });
                }
                let Some(IotInFlight::Bridge { sent, .. }) = state.inflight.as_mut() else { unreachable!() };
                match step["event"].as_str().unwrap() {
                    "closed-soon" => {
                        (*sent, state.sent_in_session) = (Instant::now(), true);
                        bridge.closed_on_inflight(&mut state, reject);
                    }
                    "closed-late" => {
                        (*sent, state.sent_in_session) = (Instant::now().checked_sub(Duration::from_secs(6)).unwrap(), true);
                        bridge.closed_on_inflight(&mut state, reject);
                    }
                    "closed-before-send" => {
                        state.sent_in_session = false;
                        bridge.closed_on_inflight(&mut state, reject);
                    }
                    "error" => {}
                    "puback" => bridge.delivered(&mut state).unwrap(),
                    other => panic!("{other}"),
                }
                let stats = bridge.stats.lock().unwrap().mappings[0].clone();
                let outcome = if stats.rejected > rejected { "rejected" } else if stats.forwarded > forwarded { "delivered" }
                    else if stats.held.is_some() { "held" } else { "retry" };
                (rejected, forwarded) = (stats.rejected, stats.forwarded);
                assert_eq!(outcome, step["expect"].as_str().unwrap(), "{} step {n}", case["id"]);
                // A rejected or delivered message has left the spool; a held one stays.
                assert_eq!(state.inflight.is_none(), outcome == "rejected" || outcome == "delivered", "{} step {n}", case["id"]);
            }
            let spool = broker.lock().unwrap().stats(Some("@bridge/0"));
            assert_eq!(spool[0]["queued"], json!(state.inflight.is_some() as u64), "{}", case["id"]);
            let _ = std::fs::remove_dir_all(&dir);
        }
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
                    mapping("commands/#", Endpoint::LocalMqtt, Endpoint::Pubsub, "")]).validate(false).unwrap();
        assert!(config(vec![mapping("a/#", Endpoint::Pubsub, Endpoint::Pubsub, "")]).validate(false).is_err(), "same endpoints");
        assert!(config(vec![mapping("a/#/b", Endpoint::Pubsub, Endpoint::LocalMqtt, "")]).validate(false).is_err(), "invalid filter");
        assert!(config(vec![mapping("a/#", Endpoint::Pubsub, Endpoint::LocalMqtt, "x/+")]).validate(false).is_err(), "wildcard prefix");
        let looped = config(vec![mapping("sensors/#", Endpoint::Pubsub, Endpoint::LocalMqtt, "lan/"), mapping("lan/#", Endpoint::LocalMqtt, Endpoint::Pubsub, "")]);
        assert!(format!("{:#}", looped.validate(false).unwrap_err()).contains("bridge loop"));
        let mut bad = config(vec![mapping("a", Endpoint::Pubsub, Endpoint::LocalMqtt, "")]);
        bad.broker = Some("localhost".into());
        assert!(bad.validate(false).is_err());
    }

    #[test]
    fn payloads_convert_both_ways() {
        assert_eq!(to_value(br#"{"v":1}"#), json!({ "v": 1 }));
        assert_eq!(to_value(b"plain text"), json!("plain text"));
        assert_eq!(to_value(&[0xff, 0x00]), json!({ "base64": "/wA=" }));
    }

    #[test]
    fn inbound_rejects_what_pubsub_can_never_accept_and_retries_full_queues() {
        let dir = std::env::temp_dir().join(format!("suifleet-bridge-inbound-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut store = pubsub::Broker::open(&dir.join("pubsub"), pubsub::Limits { max_messages: 3, max_bytes: 1 << 20, qos0_messages: 1 }).unwrap();
        store.subscribe("component", "#", 1).unwrap();
        let broker = Arc::new(Mutex::new(store));
        let prefix = "p/".repeat(60);
        let bridge = Bridge::new(config(vec![mapping("lan/#", Endpoint::LocalMqtt, Endpoint::Pubsub, ""), mapping("lan/#", Endpoint::LocalMqtt, Endpoint::Pubsub, &prefix)]),
            broker.clone(), dir.join("bridge.json")).unwrap();
        let mappings = |b: &Bridge| b.stats.lock().unwrap().mappings.iter().map(|m| (m.forwarded, m.refused, m.rejected)).collect::<Vec<_>>();
        // Too large for pub/sub after conversion: rejected by both mappings, acknowledged (Ok), nothing stored.
        bridge.inbound(Endpoint::LocalMqtt, "lan/big", &vec![b'x'; pubsub::MAX_PAYLOAD + 1]).unwrap();
        assert_eq!(mappings(&bridge), [(0, 0, 1), (0, 0, 1)]);
        assert!(bridge.stats.lock().unwrap().mappings[0].last_rejected.as_deref().unwrap().contains("payload exceeds"));
        assert!(broker.lock().unwrap().receive("component", "#").unwrap().is_none());
        // A topic only the prefixed mapping makes too long: that mapping rejects, the other forwards.
        let long = format!("lan/{}", "t".repeat(pubsub::MAX_TOPIC - 4));
        bridge.inbound(Endpoint::LocalMqtt, &long, br#"{"v":1}"#).unwrap();
        assert_eq!(mappings(&bridge), [(1, 0, 1), (0, 0, 2)]);
        assert!(bridge.stats.lock().unwrap().mappings[1].last_rejected.as_deref().unwrap().contains("invalid topic"));
        // Valid traffic still progresses.
        bridge.inbound(Endpoint::LocalMqtt, "lan/ok", br#"{"v":2}"#).unwrap();
        assert_eq!(mappings(&bridge), [(2, 0, 1), (1, 0, 2)]);
        // A full queue is retryable: an error (no PUBACK), counted as refused by every mapping, not rejected.
        assert!(bridge.inbound(Endpoint::LocalMqtt, "lan/more", br#"{"v":3}"#).is_err());
        assert_eq!(mappings(&bridge), [(2, 1, 1), (1, 1, 2)]);
        let saved: Value = serde_json::from_slice(&std::fs::read(dir.join("bridge.json")).unwrap()).unwrap();
        assert_eq!(saved["mappings"][1]["rejected"], 2);
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

    #[test]
    fn copies_one_message_can_become() {
        let c = config(vec![mapping("lan/#", Endpoint::LocalMqtt, Endpoint::Pubsub, "a/"), mapping("lan/x", Endpoint::LocalMqtt, Endpoint::Pubsub, "b/"),
                            mapping("other/#", Endpoint::LocalMqtt, Endpoint::Pubsub, ""), mapping("sensors/#", Endpoint::Pubsub, Endpoint::LocalMqtt, "")]);
        assert_eq!(c.max_inbound_copies(), 2);
        assert_eq!(config(vec![mapping("s/#", Endpoint::Pubsub, Endpoint::LocalMqtt, "")]).max_inbound_copies(), 0);
    }

    #[test]
    fn inbound_copies_that_can_never_fit_together_are_rejected_not_retried() {
        let dir = std::env::temp_dir().join(format!("suifleet-bridge-unfittable-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        // Configuration validation refuses this; the bridge must still not stall on it.
        let mut store = pubsub::Broker::open(&dir.join("pubsub"), pubsub::Limits { max_messages: 1, max_bytes: 1 << 20, qos0_messages: 1 }).unwrap();
        store.subscribe("component", "#", 1).unwrap();
        let broker = Arc::new(Mutex::new(store));
        let bridge = Bridge::new(config(vec![mapping("lan/#", Endpoint::LocalMqtt, Endpoint::Pubsub, "a/"), mapping("lan/#", Endpoint::LocalMqtt, Endpoint::Pubsub, "b/")]),
            broker.clone(), dir.join("bridge.json")).unwrap();
        bridge.inbound(Endpoint::LocalMqtt, "lan/x", b"1").unwrap();
        let stats = bridge.stats.lock().unwrap().clone();
        assert!(stats.mappings.iter().all(|m| (m.forwarded, m.refused, m.rejected) == (0, 0, 1)), "{stats:?}");
        assert!(stats.mappings[0].last_rejected.as_deref().unwrap().contains("even when empty"));
        assert!(broker.lock().unwrap().receive("component", "#").unwrap().is_none());
    }

    /// Overlapping inbound mappings share one bounded subscriber queue. A
    /// message refused for lack of room must, after draining, arrive once per
    /// mapping (no replay of earlier copies), be PUBACKed, and let the next
    /// message through. Uses the real session against a loopback MQTT peer.
    #[tokio::test]
    async fn overlapping_inbound_mappings_progress_across_redelivery() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let dir = std::env::temp_dir().join(format!("suifleet-bridge-redeliver-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut store = pubsub::Broker::open(&dir.join("pubsub"), pubsub::Limits { max_messages: 2, max_bytes: 1 << 20, qos0_messages: 1 }).unwrap();
        store.subscribe("component", "#", 1).unwrap();
        // One slot taken: the two copies of the first LAN message cannot both fit.
        store.publish("component", "local/pending", &json!(0), None).unwrap();
        let broker = Arc::new(Mutex::new(store));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let mut cfg = config(vec![mapping("lan/#", Endpoint::LocalMqtt, Endpoint::Pubsub, "a/"), mapping("lan/#", Endpoint::LocalMqtt, Endpoint::Pubsub, "b/")]);
        cfg.broker = Some(listener.local_addr().unwrap().to_string());
        assert_eq!(cfg.max_inbound_copies(), 2);
        let bridge = Bridge::new(cfg, broker.clone(), dir.join("bridge.json")).unwrap();
        let drain = |broker: &Arc<Mutex<pubsub::Broker>>| {
            let mut b = broker.lock().unwrap();
            let mut topics = Vec::new();
            while let Some(m) = b.receive("component", "#").unwrap() {
                b.ack("component", "#", m.id).unwrap();
                topics.push(m.topic);
            }
            topics
        };
        // Accept a connection, CONNACK it and SUBACK its subscription.
        async fn accept(listener: &tokio::net::TcpListener) -> tokio::net::TcpStream {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut buffer = Vec::new();
            for reply in [vec![0x20, 2, 0, 0], vec![0x90, 3, 0, 1, 1]] {
                read_packet(&mut stream, &mut buffer).await.unwrap();
                stream.write_all(&reply).await.unwrap();
            }
            stream
        }
        async fn read_packet(stream: &mut tokio::net::TcpStream, buffer: &mut Vec<u8>) -> Option<mqtt::Packet> {
            loop {
                if let Some((packet, used)) = mqtt::decode(buffer).unwrap() {
                    buffer.drain(..used);
                    return Some(packet);
                }
                let mut chunk = [0u8; 4096];
                let read = tokio::time::timeout(Duration::from_secs(10), stream.read(&mut chunk)).await.unwrap().unwrap();
                if read == 0 {
                    return None;
                }
                buffer.extend_from_slice(&chunk[..read]);
            }
        }
        let peer = {
            let broker = broker.clone();
            tokio::spawn(async move {
                let mut delivered = Vec::new();
                // First delivery: refused (queue lacks room for both copies), no PUBACK.
                let mut stream = accept(&listener).await;
                stream.write_all(&mqtt::publish("lan/x", b"1", 1, Some(7), false)).await.unwrap();
                assert_eq!(read_packet(&mut stream, &mut Vec::new()).await, None, "refused message must not be acknowledged");
                delivered.extend(drain(&broker));
                // Redelivery after draining: both copies stored, then PUBACK.
                let mut stream = accept(&listener).await;
                let mut buffer = Vec::new();
                stream.write_all(&mqtt::publish("lan/x", b"1", 1, Some(7), true)).await.unwrap();
                assert_eq!(read_packet(&mut stream, &mut buffer).await, Some(mqtt::Packet::PubAck(7)));
                delivered.extend(drain(&broker));
                // The following message progresses on the same connection.
                stream.write_all(&mqtt::publish("lan/y", b"2", 1, Some(8), false)).await.unwrap();
                assert_eq!(read_packet(&mut stream, &mut buffer).await, Some(mqtt::Packet::PubAck(8)));
                delivered.extend(drain(&broker));
                // Returned so the connection stays open until the session is dropped.
                (delivered, stream)
            })
        };
        let mut inflight: Vec<Option<InFlight>> = vec![None, None];
        let first = tokio::time::timeout(Duration::from_secs(20), bridge.session(None, &mut inflight)).await.unwrap();
        assert!(format!("{:#}", first.unwrap_err()).contains("redelivery"));
        let second = tokio::time::timeout(Duration::from_secs(20), async {
            tokio::select! {
                result = bridge.session(None, &mut inflight) => panic!("session ended early: {result:?}"),
                finished = peer => finished.unwrap().0,
            }
        }).await.unwrap();
        assert_eq!(second, ["local/pending", "a/lan/x", "b/lan/x", "a/lan/y", "b/lan/y"]);
        let stats = bridge.stats.lock().unwrap().clone();
        assert_eq!(stats.mappings.iter().map(|m| (m.forwarded, m.refused, m.rejected)).collect::<Vec<_>>(), [(2, 1, 0), (2, 1, 0)]);
    }
}
