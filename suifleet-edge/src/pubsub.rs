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
//!
//! A publication's copies are staged, synced and renamed into place, then the
//! persisted `seq` file commits them. Queue files above `seq` were never
//! committed and are removed on open, so a failure or crash part way through a
//! fanout delivers to nobody.
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

pub(crate) fn glob(pattern: &str, text: &str) -> bool {
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

/// Why a publication can never be accepted, whatever the queues hold.
pub fn invalid_publication(topic: &str, payload: &Value) -> Option<String> {
    if !valid_topic(topic) {
        return Some("invalid topic".into());
    }
    let size = serde_json::to_vec(payload).map_or(usize::MAX, |b| b.len());
    (size > MAX_PAYLOAD).then(|| format!("payload exceeds {MAX_PAYLOAD} bytes"))
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

/// Why a publication can never be accepted with the configured limits: its
/// share of some queue exceeds them even when that queue is empty.
#[derive(Debug)]
pub struct Unfittable(pub String);
impl std::fmt::Display for Unfittable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "publication exceeds the subscription limits even when empty: {}", self.0)
    }
}
impl std::error::Error for Unfittable {}

pub struct Broker {
    dir: PathBuf,
    limits: Limits,
    seq: u64,
    subs: BTreeMap<(String, String), Subscription>,
    /// Set when a failed publication could not be undone on disk. Publishing
    /// stays refused until a restart's `open` discards its uncommitted copies.
    poisoned: Option<String>,
}

fn queue_dir(root: &Path, principal: &str, filter: &str) -> PathBuf {
    let tag = hex::encode(&crate::identity::hash(format!("{principal}\0{filter}").as_bytes())[..12]);
    root.join("queues").join(tag)
}

pub(crate) fn write_synced(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let temporary = path.with_extension("tmp");
    write_file_synced(&temporary, bytes)?;
    std::fs::rename(&temporary, path)?;
    sync_dir(path.parent().unwrap_or(Path::new(".")))
}

fn write_file_synced(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let mut file = std::fs::File::create(path)?;
    file.write_all(bytes)?;
    file.sync_all()
}

fn sync_dir(dir: &Path) -> std::io::Result<()> {
    std::fs::File::open(dir)?.sync_all()
}

/// Remove a file; one that does not exist (or whose parent is not a directory)
/// is already absent.
fn remove_if_present(path: &Path) -> std::io::Result<()> {
    use std::io::ErrorKind::{NotADirectory, NotFound};
    match std::fs::remove_file(path) {
        Err(e) if !matches!(e.kind(), NotFound | NotADirectory) => Err(e),
        _ => Ok(()),
    }
}

impl Broker {
    pub fn open(dir: &Path, limits: Limits) -> anyhow::Result<Self> {
        std::fs::create_dir_all(dir.join("queues"))?;
        let records: Vec<Record> = match std::fs::read(dir.join("subscriptions.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(e) => return Err(e.into()),
        };
        // `seq` is the commit point: queue files above it are uncommitted. Only
        // a store that never committed has none; a marker that cannot be read
        // stops startup before any queue file is removed.
        let seq: u64 = match std::fs::read_to_string(dir.join("seq")) {
            Ok(text) => text.trim().parse().map_err(|e| anyhow::anyhow!("invalid pubsub commit marker {}: {e}", dir.join("seq").display()))?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
            Err(e) => return Err(anyhow::Error::new(e).context(format!("cannot read pubsub commit marker {}", dir.join("seq").display()))),
        };
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
                match id {
                    Some(id) if id > seq => {
                        std::fs::remove_file(&path)?;
                        sync_dir(&dir_q)?;
                    }
                    Some(id) => queued.push((id, std::fs::metadata(&path)?.len())),
                    None => {}
                }
            }
            queued.sort();
            subs.insert((record.principal.clone(), record.filter.clone()),
                Subscription { record, dir: dir_q, qos0: VecDeque::new(), queued: queued.into() });
        }
        Ok(Self { dir: dir.to_owned(), limits, seq, subs, poisoned: None })
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
        let mut results = self.publish_all(sender, &[(topic.to_owned(), payload.clone())], skip_principal)?;
        Ok(results.remove(0))
    }

    /// Publish several messages, in order, as one commit: either every QoS 1
    /// copy of every message is stored or none is. A queue that lacks room
    /// for its share of the batch refuses all of it with `Backpressure`; a
    /// queue whose share exceeds its limits even when empty returns
    /// `Unfittable`, since retrying can never succeed.
    pub fn publish_all(&mut self, sender: &str, publications: &[(String, Value)], skip_principal: Option<&str>) -> anyhow::Result<Vec<Value>> {
        if let Some(reason) = &self.poisoned {
            anyhow::bail!("messaging store needs a daemon restart: {reason}");
        }
        anyhow::ensure!(!publications.is_empty(), "nothing to publish");
        let now = crate::telemetry::now_ms();
        // Per publication: its message, stored bytes and matching subscriptions.
        let mut batch: Vec<(Message, Vec<u8>, Vec<(String, String)>)> = Vec::new();
        for (offset, (topic, payload)) in publications.iter().enumerate() {
            if let Some(reason) = invalid_publication(topic, payload) {
                anyhow::bail!(reason);
            }
            let targets: Vec<(String, String)> = self
                .subs
                .iter()
                .filter(|((principal, filter), _)| matches(filter, topic) && Some(principal.as_str()) != skip_principal)
                .map(|(k, _)| k.clone())
                .collect();
            // Ids are taken only if the batch is committed.
            let message = Message { id: self.seq + 1 + offset as u64, topic: topic.clone(), sender: sender.to_owned(), payload: payload.clone(), published_ms: now };
            // Bounds count the bytes actually stored, including escaping.
            let encoded = serde_json::to_vec(&message)?;
            batch.push((message, encoded, targets));
        }
        // What the batch adds to each durable queue: (messages, bytes).
        let mut added: BTreeMap<&(String, String), (usize, u64)> = BTreeMap::new();
        for (_, encoded, targets) in &batch {
            for key in targets.iter().filter(|k| self.subs[*k].record.qos == 1) {
                let entry = added.entry(key).or_default();
                entry.0 += 1;
                entry.1 += encoded.len() as u64;
            }
        }
        let unfittable: Vec<String> = added
            .iter()
            .filter(|(_, (count, bytes))| *count > self.limits.max_messages || *bytes > self.limits.max_bytes)
            .map(|((principal, filter), _)| format!("{principal} {filter}"))
            .collect();
        if !unfittable.is_empty() {
            return Err(Unfittable(unfittable.join(", ")).into());
        }
        let full: Vec<(String, String)> = added
            .iter()
            .filter(|(key, (count, bytes))| {
                let s = &self.subs[**key];
                s.queued.len() + count > self.limits.max_messages || s.queued.iter().map(|(_, b)| b).sum::<u64>() + bytes > self.limits.max_bytes
            })
            .map(|(key, _)| (*key).clone())
            .collect();
        if !full.is_empty() {
            for key in &full {
                self.subs.get_mut(key).unwrap().record.refused += 1;
            }
            self.save()?;
            return Err(Backpressure(full.iter().map(|(p, f)| format!("{p} {f}")).collect::<Vec<_>>().join(", ")).into());
        }
        let copies: Vec<(Vec<PathBuf>, u64, &[u8])> = batch
            .iter()
            .map(|(message, encoded, targets)| {
                let dirs = targets.iter().map(|k| &self.subs[k]).filter(|s| s.record.qos == 1).map(|s| s.dir.clone()).collect();
                (dirs, message.id, encoded.as_slice())
            })
            .collect();
        self.spool(&copies)?;
        self.seq = batch.last().unwrap().0.id;
        let mut results = Vec::new();
        let mut any_volatile = false;
        for (message, encoded, targets) in &batch {
            let (mut durable, mut volatile) = (0, 0);
            for key in targets {
                let sub = self.subs.get_mut(key).unwrap();
                if sub.record.qos == 1 {
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
            any_volatile |= volatile > 0;
            results.push(json!({ "id": message.id, "durable": durable, "volatile": volatile }));
        }
        // Committed: a counter that cannot be saved must not turn this into a refusal.
        if any_volatile {
            if let Err(error) = self.save() {
                tracing::warn!("cannot save pubsub counters: {error:#}");
            }
        }
        Ok(results)
    }

    /// Store each `(dirs, id, bytes)` copy in every durable queue in its
    /// `dirs`, or store nothing. Copies are staged and synced, renamed into
    /// place, then committed together by the synced `seq` file (the highest
    /// id). Any failure removes the copies again and returns `Backpressure`,
    /// so the publisher (or the bridge's broker) retries later.
    fn spool(&mut self, copies: &[(Vec<PathBuf>, u64, &[u8])]) -> anyhow::Result<()> {
        let Some(last) = copies.iter().map(|(_, id, _)| *id).max() else {
            return Ok(());
        };
        let name = |id: u64, extension: &str| format!("{id:020}.{extension}");
        // Every path this commit may have created, for the undo.
        let mut made: Vec<PathBuf> = Vec::new();
        let mut commit = || -> std::io::Result<()> {
            for (dirs, id, encoded) in copies {
                for d in dirs {
                    made.push(d.join(name(*id, "tmp")));
                    write_file_synced(made.last().unwrap(), encoded)?;
                }
            }
            for (dirs, id, _) in copies {
                for d in dirs {
                    let placed = d.join(name(*id, "json"));
                    std::fs::rename(d.join(name(*id, "tmp")), &placed)?;
                    made.push(placed);
                }
            }
            for d in copies.iter().flat_map(|(dirs, ..)| dirs) {
                sync_dir(d)?;
            }
            write_synced(&self.dir.join("seq"), last.to_string().as_bytes())
        };
        let Err(error) = commit() else {
            return Ok(());
        };
        let undo = || -> std::io::Result<()> {
            for path in &made {
                remove_if_present(path)?;
            }
            for d in copies.iter().flat_map(|(dirs, ..)| dirs) {
                sync_dir(d)?;
            }
            Ok(())
        };
        if let Err(undo_error) = undo() {
            // Uncommitted copies may remain; `open` removes them. Until then, no
            // later publication may commit an id at or above these.
            let reason = format!("message {last} was refused ({error}) but not removed ({undo_error})");
            self.poisoned = Some(reason.clone());
            anyhow::bail!(reason);
        }
        Err(Backpressure(format!("cannot spool message {last}: {error}")).into())
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
    fn byte_bounds_count_the_stored_envelope_including_escapes() {
        // Each U+0001 in a topic is stored as the six bytes `\u0001`.
        let topic = "\u{1}".repeat(MAX_TOPIC);
        let payload = json!("x".repeat(3600));
        let mut tiny = Broker::open(&dir("escaped"), Limits { max_messages: 100, max_bytes: 4096, qos0_messages: 1 }).unwrap();
        tiny.subscribe("b", "#", 1).unwrap();
        // Larger than the limit even for an empty queue: never retryable.
        assert!(tiny.publish("a", &topic, &payload, None).unwrap_err().downcast_ref::<Unfittable>().is_some());
        assert_eq!(tiny.stats(None)[0]["bytes"], 0);
        // Exactly at the bound is accepted; one byte less is refused.
        let mut sized = Broker::open(&dir("escaped-size"), Limits::default()).unwrap();
        sized.subscribe("b", "#", 1).unwrap();
        sized.publish("a", &topic, &json!("x".repeat(100)), None).unwrap();
        let size = sized.stats(None)[0]["bytes"].as_u64().unwrap();
        assert!(size > 6 * MAX_TOPIC as u64 + 100);
        for (max_bytes, accepted) in [(size, true), (size - 1, false)] {
            let mut b = Broker::open(&dir(&format!("escaped-{max_bytes}")), Limits { max_messages: 100, max_bytes, qos0_messages: 1 }).unwrap();
            b.subscribe("b", "#", 1).unwrap();
            assert_eq!(b.publish("a", &topic, &json!("x".repeat(100)), None).is_ok(), accepted, "{max_bytes}");
            assert!(b.stats(None)[0]["bytes"].as_u64().unwrap() <= max_bytes);
        }
    }

    fn assert_nothing_delivered(broker: &mut Broker) {
        for principal in ["a", "z"] {
            assert!(broker.receive(principal, "t").unwrap().is_none(), "{principal} received a refused publication");
            assert_eq!(broker.stats(Some(principal))[0]["bytes"], 0);
        }
    }

    #[test]
    fn a_batch_is_stored_whole_or_not_at_all() {
        let d = dir("batch");
        let mut broker = Broker::open(&d, Limits { max_messages: 3, max_bytes: 1 << 20, qos0_messages: 1 }).unwrap();
        broker.subscribe("s", "#", 1).unwrap();
        broker.subscribe("only-b", "b/#", 1).unwrap();
        broker.publish("p", "x", &json!(0), None).unwrap();
        let batch = [("a/t".to_owned(), json!(1)), ("b/t".to_owned(), json!(2))];
        let three = [("a/t".to_owned(), json!(1)), ("b/t".to_owned(), json!(2)), ("c/t".to_owned(), json!(3))];
        // Room for two of `s`'s three new copies: refused whole, and counted once.
        assert!(broker.publish_all("p", &three, None).unwrap_err().downcast_ref::<Backpressure>().is_some());
        assert_eq!(broker.stats(Some("s"))[0]["queued"], 1);
        assert_eq!(broker.stats(Some("s"))[0]["refused"], 1);
        assert!(broker.receive("only-b", "b/#").unwrap().is_none());
        // More than an empty queue holds: never retryable, nothing counted.
        let four: Vec<(String, Value)> = (0..4).map(|i| (format!("a/{i}"), json!(i))).collect();
        assert!(broker.publish_all("p", &four, None).unwrap_err().downcast_ref::<Unfittable>().is_some());
        // A batch that fits is stored in order with consecutive ids and survives a reopen.
        let results = broker.publish_all("p", &batch, None).unwrap();
        assert_eq!((results[0]["id"].as_u64(), results[1]["id"].as_u64()), (Some(2), Some(3)));
        assert_eq!(results[1]["durable"], 2);
        drop(broker);
        let mut broker = Broker::open(&d, Limits { max_messages: 3, max_bytes: 1 << 20, qos0_messages: 1 }).unwrap();
        let mut topics = Vec::new();
        while let Some(m) = broker.receive("s", "#").unwrap() {
            broker.ack("s", "#", m.id).unwrap();
            topics.push(m.topic);
        }
        assert_eq!(topics, ["x", "a/t", "b/t"]);
        assert_eq!(broker.receive("only-b", "b/#").unwrap().unwrap().topic, "b/t");
    }

    #[test]
    fn a_later_target_storage_failure_delivers_to_nobody() {
        let d = dir("fanout-write");
        let mut broker = Broker::open(&d, Limits::default()).unwrap();
        broker.subscribe("a", "t", 1).unwrap();
        broker.subscribe("z", "t", 1).unwrap();
        broker.subscribe("v", "t", 0).unwrap();
        // Obstruct only z's queue (it sorts after a), as a full or failing disk would.
        let z = queue_dir(&d, "z", "t");
        std::fs::remove_dir_all(&z).unwrap();
        std::fs::write(&z, b"").unwrap();
        let error = broker.publish("p", "t", &json!(1), None).unwrap_err();
        assert!(error.downcast_ref::<Backpressure>().is_some(), "{error}");
        std::fs::remove_file(&z).unwrap();
        std::fs::create_dir(&z).unwrap();
        assert_nothing_delivered(&mut broker);
        assert!(broker.receive("v", "t").unwrap().is_none(), "volatile copy of a refused publication");
        assert_eq!(std::fs::read_dir(queue_dir(&d, "a", "t")).unwrap().count(), 0, "staged copy left behind");
        drop(broker);
        let mut broker = Broker::open(&d, Limits::default()).unwrap();
        assert_nothing_delivered(&mut broker);
        // The refused publication did not take an id; the next one is delivered to both.
        assert_eq!(broker.publish("p", "t", &json!(2), None).unwrap()["id"], 1);
        assert_eq!(broker.receive("a", "t").unwrap().unwrap().payload, json!(2));
        assert_eq!(broker.receive("z", "t").unwrap().unwrap().payload, json!(2));
    }

    #[test]
    fn a_failed_rename_into_place_is_undone() {
        let d = dir("fanout-rename");
        let mut broker = Broker::open(&d, Limits::default()).unwrap();
        broker.subscribe("a", "t", 1).unwrap();
        broker.subscribe("z", "t", 1).unwrap();
        // a's copy is renamed into place first; z's rename then fails.
        let blocker = queue_dir(&d, "z", "t").join(format!("{:020}.json", 1));
        std::fs::create_dir(&blocker).unwrap();
        std::fs::write(blocker.join("keep"), b"").unwrap();
        assert!(broker.publish("p", "t", &json!(1), None).unwrap_err().downcast_ref::<Backpressure>().is_some());
        std::fs::remove_dir_all(&blocker).unwrap();
        assert_nothing_delivered(&mut broker);
        for principal in ["a", "z"] {
            assert_eq!(std::fs::read_dir(queue_dir(&d, principal, "t")).unwrap().count(), 0, "{principal} kept a copy");
        }
        assert_eq!(broker.publish("p", "t", &json!(2), None).unwrap()["durable"], 2);
    }

    #[test]
    fn open_discards_copies_a_crash_left_uncommitted() {
        let d = dir("fanout-crash");
        let mut broker = Broker::open(&d, Limits::default()).unwrap();
        broker.subscribe("a", "t", 1).unwrap();
        broker.subscribe("z", "t", 1).unwrap();
        broker.publish("p", "t", &json!("kept"), None).unwrap();
        drop(broker);
        // A crash after a's copy was renamed into place and z's only staged,
        // before `seq` committed message 2.
        let encoded = |id: u64| serde_json::to_vec(&Message { id, topic: "t".into(), sender: "p".into(), payload: json!("lost"), published_ms: 0 }).unwrap();
        std::fs::write(queue_dir(&d, "a", "t").join(format!("{:020}.json", 2)), encoded(2)).unwrap();
        std::fs::write(queue_dir(&d, "z", "t").join(format!("{:020}.tmp", 2)), encoded(2)).unwrap();
        let mut broker = Broker::open(&d, Limits::default()).unwrap();
        for principal in ["a", "z"] {
            let m = broker.receive(principal, "t").unwrap().unwrap();
            assert_eq!((m.id, m.payload), (1, json!("kept")));
            broker.ack(principal, "t", 1).unwrap();
            assert!(broker.receive(principal, "t").unwrap().is_none(), "{principal} got an uncommitted copy");
            assert_eq!(std::fs::read_dir(queue_dir(&d, principal, "t")).unwrap().count(), 0);
        }
        assert_eq!(broker.publish("p", "t", &json!("next"), None).unwrap()["id"], 2);
    }

    #[test]
    fn an_unreadable_commit_marker_stops_open_and_keeps_committed_messages() {
        let d = dir("bad-seq");
        let mut broker = Broker::open(&d, Limits::default()).unwrap();
        broker.subscribe("b", "t", 1).unwrap();
        broker.publish("p", "t", &json!("committed"), None).unwrap();
        drop(broker);
        let seq = d.join("seq");
        let saved = std::fs::read(&seq).unwrap();
        let queued = || std::fs::read_dir(queue_dir(&d, "b", "t")).unwrap().count();
        // Unreadable (EISDIR) and unparsable markers both fail open; the queue file stays.
        std::fs::remove_file(&seq).unwrap();
        std::fs::create_dir(&seq).unwrap();
        assert!(format!("{:#}", Broker::open(&d, Limits::default()).err().unwrap()).contains("cannot read pubsub commit marker"));
        assert_eq!(queued(), 1);
        std::fs::remove_dir(&seq).unwrap();
        for garbage in [&b"not a number"[..], b""] {
            std::fs::write(&seq, garbage).unwrap();
            assert!(format!("{:#}", Broker::open(&d, Limits::default()).err().unwrap()).contains("invalid pubsub commit marker"));
            assert_eq!(queued(), 1);
        }
        // With the marker restored the committed message is delivered.
        std::fs::write(&seq, saved).unwrap();
        let mut broker = Broker::open(&d, Limits::default()).unwrap();
        assert_eq!(broker.receive("b", "t").unwrap().unwrap().payload, json!("committed"));
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
