//! On-device service health in telemetry (SF-085; docs/TELEMETRY.md).
//!
//! Durable pub/sub (SF-032), its MQTT bridge, streams and their exports
//! (SF-034) and shadow synchronization (SF-033) keep counters that only local
//! IPC could read. Each heartbeat summarizes them in a bounded, versioned
//! `services` section of the signed snapshot, so a full queue, a lost broker
//! or a failing export is visible to the fleet and alerts on it.
//!
//! The section states facts and never content: no message payloads, no topics
//! or topic filters (a topic can itself be confidential), no export or sync
//! URLs, tokens or shadow documents, and no paths of conflicting shadow keys.
//! Error texts are the daemon's own, redacted with the log rules and bounded.
//!
//! Cumulative counters come with their increase since the previous snapshot
//! (`*_recent`), and conditions that matter only when they last carry the
//! time they began (`*_since_ms`), so one report suffices to judge them.
use crate::telemetry::{bounded, MAX_NAME, MAX_TEXT};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Arc, Mutex},
};

/// Version of the `services` section.
pub const VERSION: u64 = 1;
/// Entries per list (queues, streams, synchronized shadows).
pub const MAX_ENTRIES: usize = 32;
/// A queue or stream at least this full (percent) is near its limit.
pub const FULL_PERCENT: u8 = 90;
/// A condition lasting this many heartbeat intervals is sustained.
pub const SUSTAIN_INTERVALS: u64 = 2;
/// Consecutive failed export or sync attempts that make it failing.
pub const FAILURE_THRESHOLD: u32 = 3;

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Services {
    pub version: u64,
    pub messaging: Option<Messaging>,
    pub bridge: Option<BridgeHealth>,
    pub streams: Option<StreamsHealth>,
    pub shadows: Option<ShadowsHealth>,
}

/// Durable pub/sub queues, per subscriber principal.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Messaging {
    pub subscriptions: u64,
    pub queued: u64,
    /// Publications refused because a QoS 1 queue was full (persisted).
    pub refused: u64,
    /// QoS 0 messages dropped from a full in-memory queue (persisted).
    pub dropped: u64,
    /// Fullest first.
    pub queues: Vec<QueueHealth>,
    /// Principals beyond the list limit.
    pub omitted: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QueueHealth {
    /// A component name, `@bridge/<n>` or `@operator/<uid>`.
    pub principal: String,
    pub subscriptions: u64,
    pub queued: u64,
    /// Of its fullest subscription, by messages or bytes.
    pub fill_percent: u8,
    /// Since when it has been at least [`FULL_PERCENT`] full (this daemon run).
    pub full_since_ms: Option<u64>,
    pub refused: u64,
    pub refused_recent: u64,
    pub dropped: u64,
    pub dropped_recent: u64,
}

/// The pub/sub ↔ local MQTT broker bridge (counters since daemon start).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeHealth {
    pub connected: bool,
    /// When the current connection state began (this daemon run).
    pub since_ms: u64,
    pub connects: u64,
    /// Inbound messages left with the broker because a queue was full.
    pub refused: u64,
    pub refused_recent: u64,
    /// Inbound messages pub/sub can never accept, acknowledged and dropped.
    pub rejected: u64,
    pub rejected_recent: u64,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StreamsHealth {
    /// Fullest first.
    pub streams: Vec<StreamHealth>,
    pub omitted: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StreamHealth {
    pub name: String,
    pub bytes: u64,
    pub max_size: u64,
    pub fill_percent: u8,
    pub messages: u64,
    /// `OverwriteOldestData`: a full stream evicts instead of rejecting.
    pub overwrite: bool,
    /// Appends rejected because the stream was full (since daemon start).
    pub rejected: u64,
    pub rejected_recent: u64,
    pub export: Option<ExportHealth>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExportHealth {
    /// Retained messages not exported yet.
    pub lag: u64,
    /// Messages retired (evicted or expired) before they were exported.
    pub lost: u64,
    pub lost_recent: u64,
    pub batches: u64,
    pub failures: u64,
    pub consecutive_failures: u64,
    pub last_success_ms: Option<u64>,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowsHealth {
    pub shadows: u64,
    /// Shadow notifications pub/sub refused (since daemon start).
    pub notifications_refused: u64,
    pub notifications_refused_recent: u64,
    /// Synchronized shadows; most conflicts first.
    pub sync: Vec<ShadowSyncHealth>,
    pub omitted: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowSyncHealth {
    /// `""` is the classic shadow.
    pub name: String,
    /// Keys both sides changed, settled by the configured owner.
    pub conflicts: u64,
    pub conflicts_recent: u64,
    pub failures: u64,
    pub consecutive_failures: u64,
    pub last_success_ms: Option<u64>,
    pub last_error: Option<String>,
}

/// Facts about one stream, as its store knows them.
#[derive(Debug, Clone, Default)]
pub struct StreamFacts {
    pub name: String,
    pub bytes: u64,
    pub max_size: u64,
    pub messages: u64,
    pub overwrite: bool,
    pub rejected: u64,
    pub lost: u64,
    /// With an export: retained messages after the checkpoint, and the
    /// exporter's status (`batches`, `failures`, `consecutive_failures`,
    /// `last_success_ms`, `last_error`).
    pub export: Option<(u64, serde_json::Value)>,
}

/// The live services a snapshot summarizes; those not enabled are `None`.
#[derive(Default, Clone)]
pub struct Sources {
    pub broker: Option<Arc<Mutex<crate::pubsub::Broker>>>,
    pub bridge: Option<Arc<Mutex<crate::bridge::Stats>>>,
    pub streams: Option<Arc<Mutex<crate::streams::Store>>>,
    pub shadows: Option<Arc<Mutex<crate::shadows::Store>>>,
}

impl Sources {
    pub fn is_empty(&self) -> bool {
        self.broker.is_none() && self.bridge.is_none() && self.streams.is_none() && self.shadows.is_none()
    }
}

/// What the previous snapshot saw: counters (for `*_recent`) and when
/// conditions began. Only keys seen in the latest collection are kept.
#[derive(Debug, Default)]
pub struct Tracker {
    totals: BTreeMap<String, u64>,
    since: BTreeMap<String, u64>,
    seen: BTreeSet<String>,
    collected: bool,
}

impl Tracker {
    /// The increase since the previous snapshot. A counter that survives
    /// restarts (`persisted`) and exists at the first collection is its own
    /// baseline; any other counter (kept in memory, or new since) started at zero.
    pub fn recent(&mut self, key: String, total: u64, persisted: bool) -> u64 {
        self.seen.insert(key.clone());
        let baseline = if persisted && !self.collected { total } else { 0 };
        let previous = self.totals.insert(key, total).unwrap_or(baseline);
        total.saturating_sub(previous)
    }

    /// When `active` began: the first collection that saw it, or `first`
    /// for a condition already present then.
    pub fn since(&mut self, key: String, active: bool, now: u64) -> Option<u64> {
        let key = format!("since:{key}");
        if !active {
            self.since.remove(&key);
            return None;
        }
        self.seen.insert(key.clone());
        Some(*self.since.entry(key).or_insert(now))
    }

    fn finish(&mut self) {
        let seen = std::mem::take(&mut self.seen);
        self.totals.retain(|key, _| seen.contains(key));
        self.since.retain(|key, _| seen.contains(key));
        self.collected = true;
    }
}

fn percent(used: u64, limit: u64) -> u8 {
    if limit == 0 {
        return 100;
    }
    (used.saturating_mul(100) / limit).min(100) as u8
}

/// A daemon error text as it may leave the device: redacted, single-line, bounded.
pub fn error_text(text: &str) -> String {
    let line: String = text.chars().map(|c| if c.is_control() { ' ' } else { c }).collect();
    bounded(&crate::redact::redact(&line, &[]).0, MAX_TEXT)
}

fn number(status: &serde_json::Value, key: &str) -> u64 {
    status[key].as_u64().unwrap_or(0)
}

/// Summarize the enabled services; `None` when none is enabled. `first_ms`
/// is when a condition already present at the first collection is dated
/// (the daemon's start).
pub fn collect(sources: &Sources, tracker: &mut Tracker, first_ms: u64, now: u64) -> Option<Services> {
    if sources.is_empty() {
        return None;
    }
    let start = if tracker.collected { now } else { first_ms };
    let mut services = Services { version: VERSION, ..Services::default() };
    if let Some(broker) = &sources.broker {
        let (stats, limits) = {
            let broker = broker.lock().unwrap();
            (broker.stats(None), broker.limits())
        };
        let mut messaging = Messaging { subscriptions: stats.len() as u64, ..Messaging::default() };
        let mut queues: BTreeMap<String, QueueHealth> = BTreeMap::new();
        for s in &stats {
            let principal = s["principal"].as_str().unwrap_or_default().to_owned();
            let (queued, bytes) = (number(s, "queued"), number(s, "bytes"));
            let fill = if s["qos"].as_u64() == Some(1) {
                percent(queued, limits.max_messages as u64).max(percent(bytes, limits.max_bytes))
            } else {
                percent(queued, limits.qos0_messages as u64)
            };
            messaging.queued += queued;
            messaging.refused += number(s, "refused");
            messaging.dropped += number(s, "dropped");
            let queue = queues.entry(principal.clone()).or_insert_with(|| QueueHealth { principal, ..QueueHealth::default() });
            queue.subscriptions += 1;
            queue.queued += queued;
            queue.fill_percent = queue.fill_percent.max(fill);
            queue.refused += number(s, "refused");
            queue.dropped += number(s, "dropped");
        }
        let mut queues: Vec<QueueHealth> = queues
            .into_values()
            .map(|mut q| {
                let key = format!("queue:{}", q.principal);
                q.refused_recent = tracker.recent(format!("{key}:refused"), q.refused, true);
                q.dropped_recent = tracker.recent(format!("{key}:dropped"), q.dropped, true);
                q.full_since_ms = tracker.since(key, q.fill_percent >= FULL_PERCENT, start);
                q.principal = bounded(&q.principal, MAX_NAME);
                q
            })
            .collect();
        queues.sort_by(|a, b| (b.fill_percent, b.refused_recent + b.dropped_recent).cmp(&(a.fill_percent, a.refused_recent + a.dropped_recent)));
        messaging.omitted = queues.len().saturating_sub(MAX_ENTRIES) as u64;
        queues.truncate(MAX_ENTRIES);
        messaging.queues = queues;
        services.messaging = Some(messaging);
    }
    if let Some(bridge) = &sources.bridge {
        let stats = bridge.lock().unwrap().clone();
        let refused = stats.mappings.iter().map(|m| m.refused).sum();
        let rejected = stats.mappings.iter().map(|m| m.rejected).sum();
        services.bridge = Some(BridgeHealth {
            connected: stats.connected,
            // A state change starts a new period; the first one dates from start.
            since_ms: {
                tracker.since(format!("bridge:{}", !stats.connected), false, now);
                tracker.since(format!("bridge:{}", stats.connected), true, start).unwrap_or(now)
            },
            connects: stats.connects,
            refused,
            refused_recent: tracker.recent("bridge:refused".into(), refused, false),
            rejected,
            rejected_recent: tracker.recent("bridge:rejected".into(), rejected, false),
            last_error: stats.last_error.as_deref().map(error_text),
        });
    }
    if let Some(store) = &sources.streams {
        let facts = store.lock().unwrap().health(now);
        let mut streams: Vec<StreamHealth> = facts
            .into_iter()
            .map(|f| {
                let key = format!("stream:{}", f.name);
                StreamHealth {
                    fill_percent: percent(f.bytes, f.max_size),
                    rejected_recent: tracker.recent(format!("{key}:rejected"), f.rejected, false),
                    export: f.export.map(|(lag, status)| ExportHealth {
                        lag,
                        lost: f.lost,
                        lost_recent: tracker.recent(format!("{key}:lost"), f.lost, false),
                        batches: number(&status, "batches"),
                        failures: number(&status, "failures"),
                        consecutive_failures: number(&status, "consecutive_failures"),
                        last_success_ms: status["last_success_ms"].as_u64(),
                        last_error: status["last_error"].as_str().map(error_text),
                    }),
                    name: bounded(&f.name, MAX_NAME),
                    bytes: f.bytes,
                    max_size: f.max_size,
                    messages: f.messages,
                    overwrite: f.overwrite,
                    rejected: f.rejected,
                }
            })
            .collect();
        let lost = |s: &StreamHealth| s.export.as_ref().map_or(0, |e| e.lost_recent);
        streams.sort_by(|a, b| (lost(b), b.fill_percent).cmp(&(lost(a), a.fill_percent)));
        let omitted = streams.len().saturating_sub(MAX_ENTRIES) as u64;
        streams.truncate(MAX_ENTRIES);
        services.streams = Some(StreamsHealth { streams, omitted });
    }
    if let Some(store) = &sources.shadows {
        let (live, refused, statuses) = store.lock().unwrap().health();
        let mut sync: Vec<ShadowSyncHealth> = statuses
            .into_iter()
            .map(|(name, status)| {
                let conflicts = number(&status, "conflicts");
                ShadowSyncHealth {
                    conflicts_recent: tracker.recent(format!("shadow:{name}:conflicts"), conflicts, false),
                    name: bounded(&name, MAX_NAME),
                    conflicts,
                    failures: number(&status, "failures"),
                    consecutive_failures: number(&status, "consecutive_failures"),
                    last_success_ms: status["last_success_ms"].as_u64(),
                    last_error: status["last_error"].as_str().map(error_text),
                }
            })
            .collect();
        sync.sort_by(|a, b| (b.conflicts_recent, b.consecutive_failures).cmp(&(a.conflicts_recent, a.consecutive_failures)));
        let omitted = sync.len().saturating_sub(MAX_ENTRIES) as u64;
        sync.truncate(MAX_ENTRIES);
        services.shadows = Some(ShadowsHealth {
            shadows: live,
            notifications_refused: refused,
            notifications_refused_recent: tracker.recent("shadows:refused".into(), refused, false),
            sync,
            omitted,
        });
    }
    tracker.finish();
    Some(services)
}

/// How long a condition that began at `since` has lasted by `now`, if it
/// counts as sustained.
fn sustained(since: Option<u64>, interval_seconds: u64, now: u64) -> Option<u64> {
    let since = since?;
    let lasted = now.saturating_sub(since);
    (lasted >= SUSTAIN_INTERVALS * interval_seconds * 1000).then_some(lasted / 1000)
}

/// The device's own judgement: conditions that make it degraded. Transient
/// refusals, drops and settled shadow conflicts alert, but do not degrade.
pub fn reasons(services: &Services, interval_seconds: u64, now: u64) -> Vec<String> {
    let mut reasons = Vec::new();
    if let Some(messaging) = &services.messaging {
        for q in &messaging.queues {
            if let Some(seconds) = sustained(q.full_since_ms, interval_seconds, now) {
                reasons.push(format!("pub/sub queue of {} {}% full for {seconds} s", q.principal, q.fill_percent));
            }
        }
    }
    if let Some(bridge) = services.bridge.as_ref().filter(|b| !b.connected) {
        if let Some(seconds) = sustained(Some(bridge.since_ms), interval_seconds, now) {
            reasons.push(format!("MQTT bridge disconnected for {seconds} s: {}", bridge.last_error.as_deref().unwrap_or("not connected yet")));
        }
    }
    if let Some(streams) = &services.streams {
        for s in &streams.streams {
            let Some(export) = &s.export else { continue };
            if export.lost_recent > 0 {
                reasons.push(format!("stream {}: {} messages retired before export", s.name, export.lost_recent));
            }
            if export.consecutive_failures >= FAILURE_THRESHOLD as u64 {
                reasons.push(format!("stream {} export failing ({} consecutive): {}", s.name, export.consecutive_failures,
                                     export.last_error.as_deref().unwrap_or("unknown error")));
            }
        }
    }
    if let Some(shadows) = &services.shadows {
        for s in &shadows.sync {
            if s.consecutive_failures >= FAILURE_THRESHOLD as u64 {
                reasons.push(format!("shadow {:?} sync failing ({} consecutive): {}", s.name, s.consecutive_failures,
                                     s.last_error.as_deref().unwrap_or("unknown error")));
            }
        }
    }
    reasons
}

impl Services {
    /// Entries in all lists.
    #[cfg(test)]
    pub fn entries(&self) -> usize {
        self.messaging.as_ref().map_or(0, |m| m.queues.len())
            + self.streams.as_ref().map_or(0, |s| s.streams.len())
            + self.shadows.as_ref().map_or(0, |s| s.sync.len())
    }

    /// Drop the last entry of the longest list (counted as omitted).
    pub fn shed(&mut self) -> bool {
        let lengths = [
            self.messaging.as_ref().map_or(0, |m| m.queues.len()),
            self.streams.as_ref().map_or(0, |s| s.streams.len()),
            self.shadows.as_ref().map_or(0, |s| s.sync.len()),
        ];
        let longest = (0..3).max_by_key(|&i| (lengths[i], 3 - i)).unwrap();
        if lengths[longest] == 0 {
            return false;
        }
        match longest {
            0 => {
                let m = self.messaging.as_mut().unwrap();
                m.queues.pop();
                m.omitted += 1;
            }
            1 => {
                let s = self.streams.as_mut().unwrap();
                s.streams.pop();
                s.omitted += 1;
            }
            _ => {
                let s = self.shadows.as_mut().unwrap();
                s.sync.pop();
                s.omitted += 1;
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn temp(tag: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("suifleet-service-health-{tag}-{}", rand::random::<u64>()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn tracker_reports_increases_and_condition_start() {
        let mut t = Tracker::default();
        // A persisted counter's first sighting is its baseline; a counter
        // kept in memory started at zero with this daemon.
        assert_eq!(t.recent("p".into(), 7, true), 0);
        assert_eq!(t.recent("m".into(), 3, false), 3);
        assert_eq!(t.since("full".into(), true, 100), Some(100));
        t.finish();
        assert_eq!(t.recent("p".into(), 9, true), 2);
        assert_eq!(t.recent("m".into(), 3, false), 0);
        assert_eq!(t.since("full".into(), true, 200), Some(100), "still the same period");
        t.finish();
        assert_eq!(t.since("full".into(), false, 300), None);
        t.finish();
        // Unseen keys are forgotten: a recreated counter starts afresh, and
        // a persisted counter that appears later counted from zero.
        assert_eq!(t.recent("m".into(), 1, false), 1);
        assert_eq!(t.recent("new".into(), 2, true), 2);
        assert_eq!(t.since("full".into(), true, 400), Some(400));
    }

    #[test]
    fn collects_queue_fill_refusals_and_drops_without_topics() {
        let dir = temp("pubsub");
        let limits = crate::pubsub::Limits { max_messages: 4, max_bytes: 1 << 20, qos0_messages: 2 };
        let broker = Arc::new(Mutex::new(crate::pubsub::Broker::open(&dir, limits).unwrap()));
        {
            let mut b = broker.lock().unwrap();
            b.subscribe("slow", "secret/plant-7/valves", 1).unwrap();
            b.subscribe("lossy", "secret/plant-7/valves", 0).unwrap();
            for i in 0..4 {
                b.publish("pub", "secret/plant-7/valves", &json!({"setpoint": i}), None).unwrap();
            }
        }
        let sources = Sources { broker: Some(broker.clone()), ..Sources::default() };
        let mut tracker = Tracker::default();
        let queue = |s: &Services, principal: &str| s.messaging.as_ref().unwrap().queues.iter().find(|q| q.principal == principal).unwrap().clone();
        let s = collect(&sources, &mut tracker, 50, 1_000).unwrap();
        let m = s.messaging.as_ref().unwrap();
        assert_eq!((m.subscriptions, m.queued, m.refused, m.dropped), (2, 6, 0, 2));
        let slow = queue(&s, "slow");
        assert_eq!((slow.fill_percent, slow.full_since_ms), (100, Some(50)), "full since the daemon started");
        let lossy = queue(&s, "lossy");
        assert_eq!((lossy.fill_percent, lossy.dropped, lossy.dropped_recent), (100, 2, 0), "persisted drops are the baseline");
        // The next publication is refused for everyone; nothing is dropped.
        assert!(broker.lock().unwrap().publish("pub", "secret/plant-7/valves", &json!(5), None).is_err());
        let s = collect(&sources, &mut tracker, 50, 61_000).unwrap();
        let slow = queue(&s, "slow");
        assert_eq!((slow.refused, slow.refused_recent, slow.full_since_ms), (1, 1, Some(50)));
        assert_eq!(s.messaging.as_ref().unwrap().queues[0].principal, "slow", "the queue with new refusals first");
        let text = serde_json::to_string(&s).unwrap();
        assert!(!text.contains("secret") && !text.contains("setpoint"), "no topics or payloads: {text}");
        assert_eq!(reasons(&s, 30, 61_000), ["pub/sub queue of slow 100% full for 60 s", "pub/sub queue of lossy 100% full for 60 s"]);
        assert!(reasons(&s, 60, 61_000).is_empty(), "not yet two intervals");
        // Draining ends the condition; new drops are counted.
        {
            let mut b = broker.lock().unwrap();
            while let Some(message) = b.receive("slow", "secret/plant-7/valves").unwrap() {
                b.ack("slow", "secret/plant-7/valves", message.id).unwrap();
            }
            b.publish("pub", "secret/plant-7/valves", &json!(6), None).unwrap();
        }
        let s = collect(&sources, &mut tracker, 50, 62_000).unwrap();
        let (slow, lossy) = (queue(&s, "slow"), queue(&s, "lossy"));
        assert_eq!((slow.refused_recent, slow.full_since_ms, slow.queued), (0, None, 1));
        assert_eq!((lossy.dropped, lossy.dropped_recent), (3, 1));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn bridge_state_periods_and_redacted_errors() {
        let stats = Arc::new(Mutex::new(crate::bridge::Stats {
            connected: false, connects: 0,
            last_error: Some("connect 127.0.0.1:1883: refused; password=hunter2\nnext".into()),
            mappings: vec![crate::bridge::MappingStats { refused: 2, rejected: 1, ..Default::default() }],
        }));
        let sources = Sources { bridge: Some(stats.clone()), ..Sources::default() };
        let mut tracker = Tracker::default();
        let b = collect(&sources, &mut tracker, 10, 1_000).unwrap().bridge.unwrap();
        assert_eq!((b.connected, b.since_ms, b.refused_recent, b.rejected_recent), (false, 10, 2, 1));
        let error = b.last_error.unwrap();
        assert!(!error.contains("hunter2") && !error.contains('\n'), "{error}");
        {
            let mut s = stats.lock().unwrap();
            (s.connected, s.connects, s.last_error) = (true, 1, None);
        }
        let b = collect(&sources, &mut tracker, 10, 2_000).unwrap().bridge.unwrap();
        assert_eq!((b.connected, b.since_ms, b.refused_recent), (true, 2_000, 0));
        stats.lock().unwrap().connected = false;
        let s = collect(&sources, &mut tracker, 10, 3_000).unwrap();
        assert_eq!(s.bridge.as_ref().unwrap().since_ms, 3_000, "a new disconnected period");
        assert!(reasons(&s, 10, 22_000).is_empty());
        assert_eq!(reasons(&s, 10, 23_000), ["MQTT bridge disconnected for 20 s: not connected yet"]);
    }

    #[test]
    fn shedding_keeps_counts() {
        let mut s = Services {
            version: VERSION,
            messaging: Some(Messaging { queues: vec![QueueHealth::default(); 3], ..Messaging::default() }),
            streams: Some(StreamsHealth { streams: vec![StreamHealth::default(); 2], omitted: 0 }),
            ..Services::default()
        };
        assert!(s.shed());
        assert_eq!((s.messaging.as_ref().unwrap().queues.len(), s.messaging.as_ref().unwrap().omitted), (2, 1));
        while s.shed() {}
        assert_eq!(s.entries(), 0);
        assert_eq!((s.messaging.unwrap().omitted, s.streams.unwrap().omitted), (3, 2));
    }
}
