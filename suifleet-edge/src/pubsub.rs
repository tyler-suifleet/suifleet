//! Durable local pub/sub (SF-032; docs/MESSAGING.md).
//!
//! Topics and filters follow MQTT 3.1.1/5 §4.7. Authorization mirrors AWS
//! `aws.greengrass.ipc.pubsub` policies: operations `aws.greengrass#PublishToTopic`,
//! `aws.greengrass#SubscribeToTopic` or `*`, and resources that are globs where
//! only `*` is special (`+` and `#` are literal). Both are tested on
//! `interfaces/compat/pubsub-cases.json`.
//!
//! A subscription (principal, filter) is durable. QoS 1 messages are written to
//! its own on-disk queue (one synced file per message) and stay until the
//! subscriber acknowledges them: delivery is at least once, in publication
//! order per subscription, also across daemon restarts. When a matching QoS 1
//! queue is full, the whole publication is refused (backpressure) and the
//! refusal counted; nothing is silently lost. QoS 0 queues are in memory: at
//! most once, the oldest dropped (and counted) when full, cleared on restart.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, VecDeque},
    io::Write,
    path::{Path, PathBuf},
};

pub const MAX_TOPIC: usize = 256;
pub const MAX_PAYLOAD: usize = 4096;
pub const MAX_SUBSCRIPTIONS: usize = 256;

/// A topic name: no wildcards, no NUL, 1..=256 bytes.
pub fn valid_topic(topic: &str) -> bool {
    !topic.is_empty() && topic.len() <= MAX_TOPIC && !topic.contains(['+', '#', '\0'])
}

/// A topic filter: `+` fills a whole level, `#` is a whole, last level.
pub fn valid_filter(filter: &str) -> bool {
    if filter.is_empty() || filter.len() > MAX_TOPIC || filter.contains('\0') {
        return false;
    }
    let levels: Vec<&str> = filter.split('/').collect();
    levels.iter().enumerate().all(|(i, level)| {
        if level.contains('#') {
            *level == "#" && i == levels.len() - 1
        } else if level.contains('+') {
            *level == "+"
        } else {
            true
        }
    })
}

/// Whether `filter` matches `topic` (both valid).
pub fn matches(filter: &str, topic: &str) -> bool {
    // A filter starting with a wildcard does not match `$` topics.
    if topic.starts_with('$') && (filter.starts_with('+') || filter.starts_with('#')) {
        return false;
    }
    let mut topic_levels = topic.split('/');
    for level in filter.split('/') {
        match level {
            "#" => return true,
            "+" => {
                if topic_levels.next().is_none() {
                    return false;
                }
            }
            exact => {
                if topic_levels.next() != Some(exact) {
                    return false;
                }
            }
        }
    }
    topic_levels.next().is_none()
}

/// One AWS-style pub/sub policy statement.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub operations: Vec<String>,
    pub resources: Vec<String>,
}

fn glob(pattern: &str, text: &str) -> bool {
    // Only `*` is special; it matches any run of characters, `/` included.
    let parts: Vec<&str> = pattern.split('*').collect();
    if parts.len() == 1 {
        return pattern == text;
    }
    let (first, last) = (parts[0], parts[parts.len() - 1]);
    if !text.starts_with(first) || !text[first.len()..].ends_with(last) || text.len() < first.len() + last.len() {
        return false;
    }
    let mut rest = &text[first.len()..text.len() - last.len()];
    for part in &parts[1..parts.len() - 1] {
        match rest.find(part) {
            Some(at) => rest = &rest[at + part.len()..],
            None => return false,
        }
    }
    true
}

/// `operation` is `PublishToTopic` or `SubscribeToTopic`; `topic` is the
/// topic (publish) or the literal filter (subscribe).
pub fn authorized(policies: &[Policy], operation: &str, topic: &str) -> bool {
    let qualified = format!("aws.greengrass#{operation}");
    policies.iter().any(|p| {
        p.operations.iter().any(|o| o == "*" || *o == qualified) && p.resources.iter().any(|r| glob(r, topic))
    })
}

pub fn validate_policies(policies: &[Policy]) -> anyhow::Result<()> {
    anyhow::ensure!(policies.len() <= 32, "too many pub/sub policies");
    for policy in policies {
        anyhow::ensure!(!policy.operations.is_empty() && policy.operations.len() <= 3 && !policy.resources.is_empty() && policy.resources.len() <= 64,
            "a pub/sub policy needs 1-3 operations and 1-64 resources");
        anyhow::ensure!(
            policy.operations.iter().all(|o| matches!(o.as_str(), "*" | "aws.greengrass#PublishToTopic" | "aws.greengrass#SubscribeToTopic")),
            "pub/sub operations are aws.greengrass#PublishToTopic, aws.greengrass#SubscribeToTopic or *"
        );
        anyhow::ensure!(policy.resources.iter().all(|r| !r.is_empty() && r.len() <= MAX_TOPIC && !r.contains('\0')), "invalid pub/sub resource");
    }
    Ok(())
}

// --- Durable broker ------------------------------------------------------------

#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
pub struct Limits {
    /// QoS 1 messages a subscription holds.
    pub max_messages: usize,
    /// QoS 1 bytes a subscription holds.
    pub max_bytes: u64,
    /// QoS 0 messages a subscription holds in memory.
    pub qos0_messages: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self { max_messages: 1000, max_bytes: 4 * 1024 * 1024, qos0_messages: 100 }
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct Message {
    pub id: u64,
    pub topic: String,
    pub sender: String,
    pub payload: Value,
    pub published_ms: u64,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
struct Record {
    principal: String,
    filter: String,
    qos: u8,
    /// QoS 0 messages dropped because the queue was full.
    #[serde(default)]
    dropped: u64,
    /// Publications refused because this QoS 1 queue was full.
    #[serde(default)]
    refused: u64,
}

struct Subscription {
    record: Record,
    dir: PathBuf,
    qos0: VecDeque<Message>,
    /// QoS 1 queue: (id, bytes), ascending.
    queued: VecDeque<(u64, u64)>,
}

/// Why a publication was refused.
#[derive(Debug)]
pub struct Backpressure(pub String);
impl std::fmt::Display for Backpressure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "subscriber queue full: {}", self.0)
    }
}
impl std::error::Error for Backpressure {}

pub struct Broker {
    dir: PathBuf,
    limits: Limits,
    seq: u64,
    subs: BTreeMap<(String, String), Subscription>,
}

fn queue_dir(root: &Path, principal: &str, filter: &str) -> PathBuf {
    let tag = hex::encode(&crate::identity::hash(format!("{principal}\0{filter}").as_bytes())[..12]);
    root.join("queues").join(tag)
}

fn write_synced(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let temporary = path.with_extension("tmp");
    let mut file = std::fs::File::create(&temporary)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    std::fs::rename(&temporary, path)?;
    std::fs::File::open(path.parent().unwrap_or(Path::new(".")))?.sync_all()
}

impl Broker {
    pub fn open(dir: &Path, limits: Limits) -> anyhow::Result<Self> {
        std::fs::create_dir_all(dir.join("queues"))?;
        let records: Vec<Record> = match std::fs::read(dir.join("subscriptions.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(e) => return Err(e.into()),
        };
        let mut seq = 0;
        let mut subs = BTreeMap::new();
        for record in records {
            let dir_q = queue_dir(dir, &record.principal, &record.filter);
            std::fs::create_dir_all(&dir_q)?;
            let mut queued = Vec::new();
            for entry in std::fs::read_dir(&dir_q)? {
                let path = entry?.path();
                if path.extension().is_some_and(|e| e == "tmp") {
                    let _ = std::fs::remove_file(&path);
                    continue;
                }
                let id = path.file_stem().and_then(|s| s.to_str()).and_then(|s| s.parse::<u64>().ok());
                if let Some(id) = id {
                    queued.push((id, std::fs::metadata(&path)?.len()));
                    seq = seq.max(id);
                }
            }
            queued.sort();
            subs.insert((record.principal.clone(), record.filter.clone()),
                Subscription { record, dir: dir_q, qos0: VecDeque::new(), queued: queued.into() });
        }
        let saved: u64 = std::fs::read_to_string(dir.join("seq")).ok().and_then(|s| s.trim().parse().ok()).unwrap_or(0);
        Ok(Self { dir: dir.to_owned(), limits, seq: seq.max(saved), subs })
    }

    fn save(&self) -> anyhow::Result<()> {
        let records: Vec<&Record> = self.subs.values().map(|s| &s.record).collect();
        write_synced(&self.dir.join("subscriptions.json"), &serde_json::to_vec_pretty(&records)?)?;
        Ok(())
    }

    pub fn subscribe(&mut self, principal: &str, filter: &str, qos: u8) -> anyhow::Result<()> {
        anyhow::ensure!(valid_filter(filter), "invalid topic filter");
        anyhow::ensure!(qos <= 1, "supported QoS are 0 and 1");
        let key = (principal.to_owned(), filter.to_owned());
        if let Some(existing) = self.subs.get_mut(&key) {
            anyhow::ensure!(existing.record.qos == qos || (existing.queued.is_empty() && existing.qos0.is_empty()),
                "change a subscription's QoS only when its queue is empty");
            existing.record.qos = qos;
        } else {
            anyhow::ensure!(self.subs.len() < MAX_SUBSCRIPTIONS, "too many subscriptions");
            let dir = queue_dir(&self.dir, principal, filter);
            std::fs::create_dir_all(&dir)?;
            self.subs.insert(key, Subscription {
                record: Record { principal: principal.to_owned(), filter: filter.to_owned(), qos, ..Record::default() },
                dir, qos0: VecDeque::new(), queued: VecDeque::new(),
            });
        }
        self.save()
    }

    pub fn unsubscribe(&mut self, principal: &str, filter: &str) -> anyhow::Result<bool> {
        let Some(sub) = self.subs.remove(&(principal.to_owned(), filter.to_owned())) else {
            return Ok(false);
        };
        self.save()?;
        let _ = std::fs::remove_dir_all(&sub.dir);
        Ok(true)
    }

    pub fn has(&self, principal: &str, filter: &str) -> bool {
        self.subs.contains_key(&(principal.to_owned(), filter.to_owned()))
    }

    /// Fan a publication out to every matching subscription, except those of
    /// `skip_principal` (the bridge's loop breaker). All or nothing for QoS 1.
    pub fn publish(&mut self, sender: &str, topic: &str, payload: &Value, skip_principal: Option<&str>) -> anyhow::Result<Value> {
        anyhow::ensure!(valid_topic(topic), "invalid topic");
        let bytes = serde_json::to_vec(payload)?;
        anyhow::ensure!(bytes.len() <= MAX_PAYLOAD, "payload exceeds {MAX_PAYLOAD} bytes");
        let targets: Vec<(String, String)> = self
            .subs
            .iter()
            .filter(|((principal, filter), _)| matches(filter, topic) && Some(principal.as_str()) != skip_principal)
            .map(|(k, _)| k.clone())
            .collect();
        let size = bytes.len() as u64 + topic.len() as u64 + sender.len() as u64 + 128;
        let full: Vec<String> = targets
            .iter()
            .filter(|k| {
                let s = &self.subs[*k];
                s.record.qos == 1
                    && (s.queued.len() >= self.limits.max_messages
                        || s.queued.iter().map(|(_, b)| b).sum::<u64>() + size > self.limits.max_bytes)
            })
            .map(|(principal, filter)| format!("{principal} {filter}"))
            .collect();
        if !full.is_empty() {
            for (principal, filter) in &targets {
                if full.contains(&format!("{principal} {filter}")) {
                    self.subs.get_mut(&(principal.clone(), filter.clone())).unwrap().record.refused += 1;
                }
            }
            self.save()?;
            return Err(Backpressure(full.join(", ")).into());
        }
        self.seq += 1;
        let message = Message { id: self.seq, topic: topic.to_owned(), sender: sender.to_owned(), payload: payload.clone(), published_ms: crate::telemetry::now_ms() };
        std::fs::write(self.dir.join("seq"), self.seq.to_string())?;
        let encoded = serde_json::to_vec(&message)?;
        let (mut durable, mut volatile) = (0, 0);
        for key in &targets {
            let sub = self.subs.get_mut(key).unwrap();
            if sub.record.qos == 1 {
                write_synced(&sub.dir.join(format!("{:020}.json", message.id)), &encoded)
                    .map_err(|e| Backpressure(format!("cannot spool for {} {}: {e}", key.0, key.1)))?;
                sub.queued.push_back((message.id, encoded.len() as u64));
                durable += 1;
            } else {
                if sub.qos0.len() >= self.limits.qos0_messages {
                    sub.qos0.pop_front();
                    sub.record.dropped += 1;
                }
                sub.qos0.push_back(message.clone());
                volatile += 1;
            }
        }
        if volatile > 0 {
            self.save()?;
        }
        Ok(json!({ "id": message.id, "durable": durable, "volatile": volatile }))
    }

    /// The next message of a subscription. QoS 1 stays queued until acked
    /// (receive returns the same head again); QoS 0 is removed.
    pub fn receive(&mut self, principal: &str, filter: &str) -> anyhow::Result<Option<Message>> {
        let sub = self.subs.get_mut(&(principal.to_owned(), filter.to_owned())).ok_or_else(|| anyhow::anyhow!("no such subscription"))?;
        if sub.record.qos == 0 {
            return Ok(sub.qos0.pop_front());
        }
        match sub.queued.front() {
            Some((id, _)) => Ok(Some(serde_json::from_slice(&std::fs::read(sub.dir.join(format!("{id:020}.json")))?)?)),
            None => Ok(None),
        }
    }

    /// Remove an acknowledged QoS 1 message (the head or any queued id).
    pub fn ack(&mut self, principal: &str, filter: &str, id: u64) -> anyhow::Result<bool> {
        let sub = self.subs.get_mut(&(principal.to_owned(), filter.to_owned())).ok_or_else(|| anyhow::anyhow!("no such subscription"))?;
        let Some(position) = sub.queued.iter().position(|(i, _)| *i == id) else {
            return Ok(false);
        };
        std::fs::remove_file(sub.dir.join(format!("{id:020}.json")))?;
        sub.queued.remove(position);
        Ok(true)
    }

    /// Counters per subscription (all, or one principal's).
    pub fn stats(&self, principal: Option<&str>) -> Vec<Value> {
        self.subs
            .values()
            .filter(|s| principal.is_none_or(|p| s.record.principal == p))
            .map(|s| json!({
                "principal": s.record.principal, "filter": s.record.filter, "qos": s.record.qos,
                "queued": if s.record.qos == 1 { s.queued.len() } else { s.qos0.len() },
                "bytes": s.queued.iter().map(|(_, b)| b).sum::<u64>(),
                "dropped": s.record.dropped, "refused": s.record.refused,
            }))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_topic_filter_match_and_authorization_cases() {
        let cases: Value = serde_json::from_str(include_str!("../../interfaces/compat/pubsub-cases.json")).unwrap();
        let mut checked = 0;
        for case in cases["filters"].as_array().unwrap() {
            assert_eq!(valid_filter(case["filter"].as_str().unwrap()), case["valid"].as_bool().unwrap(), "{}", case["id"]);
            checked += 1;
        }
        for case in cases["topics"].as_array().unwrap() {
            assert_eq!(valid_topic(case["topic"].as_str().unwrap()), case["valid"].as_bool().unwrap(), "{}", case["id"]);
            checked += 1;
        }
        for case in cases["matches"].as_array().unwrap() {
            assert_eq!(matches(case["filter"].as_str().unwrap(), case["topic"].as_str().unwrap()), case["matches"].as_bool().unwrap(), "{}", case["id"]);
            checked += 1;
        }
        for case in cases["authorization"].as_array().unwrap() {
            let policies: Vec<Policy> = serde_json::from_value(case["policies"].clone()).unwrap();
            let allowed = authorized(&policies, case["operation"].as_str().unwrap(), case["topic"].as_str().unwrap());
            assert_eq!(allowed, case["allowed"].as_bool().unwrap(), "{}", case["id"]);
            checked += 1;
        }
        assert_eq!(checked, 53, "the fixture's 53 checks");
    }

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("suifleet-pubsub-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn qos1_is_durable_ordered_and_at_least_once_across_restarts() {
        let d = dir("durable");
        let mut broker = Broker::open(&d, Limits::default()).unwrap();
        broker.subscribe("b", "sensors/+/temp", 1).unwrap();
        broker.subscribe("c", "sensors/#", 0).unwrap();
        for i in 0..3 {
            let out = broker.publish("a", &format!("sensors/s{i}/temp"), &json!({ "v": i }), None).unwrap();
            assert_eq!((out["durable"].as_u64(), out["volatile"].as_u64()), (Some(1), Some(1)));
        }
        assert!(broker.publish("a", "other/x", &json!(1), None).unwrap()["durable"] == 0);
        let first = broker.receive("b", "sensors/+/temp").unwrap().unwrap();
        assert_eq!(first.payload, json!({ "v": 0 }));
        // Unacked: the same head again (at least once).
        assert_eq!(broker.receive("b", "sensors/+/temp").unwrap().unwrap().id, first.id);
        assert!(broker.ack("b", "sensors/+/temp", first.id).unwrap());
        drop(broker);
        // Restart: QoS 1 kept in order, QoS 0 cleared, subscriptions kept.
        let mut broker = Broker::open(&d, Limits::default()).unwrap();
        assert!(broker.has("c", "sensors/#") && broker.receive("c", "sensors/#").unwrap().is_none());
        let next = broker.receive("b", "sensors/+/temp").unwrap().unwrap();
        assert_eq!(next.payload, json!({ "v": 1 }));
        broker.ack("b", "sensors/+/temp", next.id).unwrap();
        assert_eq!(broker.receive("b", "sensors/+/temp").unwrap().unwrap().payload, json!({ "v": 2 }));
        // New ids continue above the persisted ones.
        let out = broker.publish("a", "sensors/s9/temp", &json!(9), None).unwrap();
        assert!(out["id"].as_u64().unwrap() > next.id);
    }

    #[test]
    fn full_qos1_queues_refuse_whole_publications_and_qos0_drops_oldest() {
        let d = dir("limits");
        let mut broker = Broker::open(&d, Limits { max_messages: 2, max_bytes: 1 << 20, qos0_messages: 2 }).unwrap();
        broker.subscribe("slow", "t", 1).unwrap();
        broker.subscribe("fast", "t", 1).unwrap();
        broker.subscribe("lossy", "t", 0).unwrap();
        broker.publish("a", "t", &json!(1), None).unwrap();
        broker.publish("a", "t", &json!(2), None).unwrap();
        // Drain `fast`, not `slow`: the next publication is refused for everyone.
        for _ in 0..2 {
            let m = broker.receive("fast", "t").unwrap().unwrap();
            broker.ack("fast", "t", m.id).unwrap();
        }
        let error = broker.publish("a", "t", &json!(3), None).unwrap_err();
        assert!(error.downcast_ref::<Backpressure>().is_some() && error.to_string().contains("slow t"), "{error}");
        assert!(broker.receive("fast", "t").unwrap().is_none(), "partially delivered");
        let stats = broker.stats(Some("slow"));
        assert_eq!(stats[0]["refused"], 1);
        // QoS 0 kept the two newest and counted one drop.
        let lossy = broker.stats(Some("lossy"));
        assert_eq!((lossy[0]["queued"].as_u64(), lossy[0]["dropped"].as_u64()), (Some(2), Some(0)));
        let m = broker.receive("slow", "t").unwrap().unwrap();
        broker.ack("slow", "t", m.id).unwrap();
        broker.publish("a", "t", &json!(3), None).unwrap();
        assert_eq!(broker.stats(Some("lossy"))[0]["dropped"], 1);
        // Byte limit too.
        let mut tiny = Broker::open(&dir("bytes"), Limits { max_messages: 100, max_bytes: 300, qos0_messages: 1 }).unwrap();
        tiny.subscribe("b", "t", 1).unwrap();
        tiny.publish("a", "t", &json!("x".repeat(100)), None).unwrap();
        assert!(tiny.publish("a", "t", &json!("x".repeat(100)), None).is_err());
    }

    #[test]
    fn invalid_input_and_loop_breaker() {
        let mut broker = Broker::open(&dir("invalid"), Limits::default()).unwrap();
        assert!(broker.subscribe("b", "a/#/b", 1).is_err());
        assert!(broker.subscribe("b", "a/b", 2).is_err());
        assert!(broker.publish("a", "a/+", &json!(1), None).is_err());
        assert!(broker.publish("a", "t", &json!("x".repeat(MAX_PAYLOAD)), None).is_err());
        broker.subscribe("@bridge", "t", 1).unwrap();
        assert_eq!(broker.publish("@bridge", "t", &json!(1), Some("@bridge")).unwrap()["durable"], 0);
        assert!(broker.unsubscribe("@bridge", "t").unwrap());
        assert!(broker.receive("@bridge", "t").is_err());
    }
}
