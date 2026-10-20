//! Service health in telemetry (SF-085, docs/TELEMETRY.md): the daemon's
//! `services` section (suifleet-edge/src/service_health.rs), its bounds, and
//! the alert conditions derived from it.
//!
//! The section holds counters and states only: never payloads, topics,
//! export or sync URLs, credentials or shadow contents.
use crate::telemetry::{Snapshot, MAX_INTERVAL_SECONDS, MAX_NAME, MAX_TEXT, MIN_INTERVAL_SECONDS};
use serde::{Deserialize, Serialize};

pub const VERSION: u64 = 1;
pub const MAX_ENTRIES: usize = 32;
/// A queue or stream at least this full (percent) is near its limit.
pub const FULL_PERCENT: u8 = 90;
/// A condition lasting this many heartbeat intervals is sustained.
pub const SUSTAIN_INTERVALS: u64 = 2;
/// Consecutive failed export or sync attempts that make it failing.
pub const FAILURE_THRESHOLD: u64 = 3;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Services {
    pub version: u64,
    pub messaging: Option<Messaging>,
    pub bridge: Option<BridgeHealth>,
    pub streams: Option<StreamsHealth>,
    pub shadows: Option<ShadowsHealth>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Messaging {
    pub subscriptions: u64,
    pub queued: u64,
    pub refused: u64,
    pub dropped: u64,
    pub queues: Vec<QueueHealth>,
    pub omitted: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QueueHealth {
    pub principal: String,
    pub subscriptions: u64,
    pub queued: u64,
    pub fill_percent: u8,
    pub full_since_ms: Option<u64>,
    pub refused: u64,
    pub refused_recent: u64,
    pub dropped: u64,
    pub dropped_recent: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeHealth {
    pub connected: bool,
    pub since_ms: u64,
    pub connects: u64,
    pub refused: u64,
    pub refused_recent: u64,
    pub rejected: u64,
    pub rejected_recent: u64,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StreamsHealth {
    pub streams: Vec<StreamHealth>,
    pub omitted: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StreamHealth {
    pub name: String,
    pub bytes: u64,
    pub max_size: u64,
    pub fill_percent: u8,
    pub messages: u64,
    pub overwrite: bool,
    pub rejected: u64,
    pub rejected_recent: u64,
    pub export: Option<ExportHealth>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExportHealth {
    pub lag: u64,
    pub lost: u64,
    pub lost_recent: u64,
    pub batches: u64,
    pub failures: u64,
    pub consecutive_failures: u64,
    pub last_success_ms: Option<u64>,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowsHealth {
    pub shadows: u64,
    pub notifications_refused: u64,
    pub notifications_refused_recent: u64,
    pub sync: Vec<ShadowSyncHealth>,
    pub omitted: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowSyncHealth {
    pub name: String,
    pub conflicts: u64,
    pub conflicts_recent: u64,
    pub failures: u64,
    pub consecutive_failures: u64,
    pub last_success_ms: Option<u64>,
    pub last_error: Option<String>,
}

/// Bounds of a version-1 services section.
pub fn validate(services: &Services) -> Result<(), String> {
    if services.version != VERSION {
        return Err(format!("unsupported services section version {}", services.version));
    }
    let short = |text: &str, limit| text.len() <= limit;
    let error = |e: &Option<String>| e.as_deref().is_none_or(|e| short(e, MAX_TEXT));
    let percent = |p: u8| p <= 100;
    if let Some(m) = &services.messaging {
        if m.queues.len() > MAX_ENTRIES || !m.queues.iter().all(|q| short(&q.principal, MAX_NAME) && percent(q.fill_percent)) {
            return Err("services.messaging exceeds its bounds".into());
        }
    }
    if let Some(b) = &services.bridge {
        if !error(&b.last_error) {
            return Err("services.bridge exceeds its bounds".into());
        }
    }
    if let Some(s) = &services.streams {
        let ok = s.streams.len() <= MAX_ENTRIES
            && s.streams.iter().all(|s| short(&s.name, MAX_NAME) && percent(s.fill_percent) && s.export.as_ref().is_none_or(|e| error(&e.last_error)));
        if !ok {
            return Err("services.streams exceeds its bounds".into());
        }
    }
    if let Some(s) = &services.shadows {
        if s.sync.len() > MAX_ENTRIES || !s.sync.iter().all(|s| short(&s.name, MAX_NAME) && error(&s.last_error)) {
            return Err("services.shadows exceeds its bounds".into());
        }
    }
    Ok(())
}

/// One alert condition of a device's newest report.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Condition {
    pub kind: &'static str,
    pub severity: &'static str,
    /// Distinguishes alerts of one kind on one device (a principal, stream or shadow).
    pub subject: String,
    pub message: String,
    pub action: String,
    pub since_ms: Option<u64>,
}

/// Alert conditions in a snapshot's services section, judged at the time
/// it was taken. `*_recent` counts are increases since the previous report,
/// so such an alert resolves with the next report that has none.
pub fn conditions(snapshot: &Snapshot) -> Vec<Condition> {
    let Some(services) = &snapshot.services else { return Vec::new() };
    let now = snapshot.observed_ms;
    let interval = snapshot.interval_seconds.clamp(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS) * 1000;
    let sustained = |since: u64| now.saturating_sub(since) >= SUSTAIN_INTERVALS * interval;
    let mut found = Vec::new();
    let mut add = |kind, severity, subject: &str, message: String, action: String, since_ms| {
        found.push(Condition { kind, severity, subject: subject.into(), message, action, since_ms })
    };
    if let Some(m) = &services.messaging {
        for q in &m.queues {
            let p = &q.principal;
            if let Some(since) = q.full_since_ms.filter(|s| sustained(*s)) {
                add("messaging-backpressure", "warning", p,
                    format!("pub/sub queue of {p} has been {}% full for {} s ({} messages)", q.fill_percent, now.saturating_sub(since) / 1000, q.queued),
                    format!("Check that {p} runs and acknowledges its messages (local IPC `messaging` status); raise [pubsub] limits if the load is expected. \
                             Publications to its topics are refused while the queue is full."),
                    Some(since));
            }
            if q.refused_recent > 0 || q.dropped_recent > 0 {
                add("messaging-loss", "warning", p,
                    format!("{p}: {} publications refused (queue full) and {} QoS 0 messages dropped since the previous report",
                            q.refused_recent, q.dropped_recent),
                    format!("Refused publications were not delivered to anyone. Find out why {p} falls behind, or raise [pubsub] limits."),
                    Some(now));
            }
        }
    }
    if let Some(b) = &services.bridge {
        if !b.connected && sustained(b.since_ms) {
            add("bridge-disconnected", "warning", "",
                format!("MQTT bridge disconnected for {} s: {}", now.saturating_sub(b.since_ms) / 1000,
                        b.last_error.as_deref().unwrap_or("not connected yet")),
                "Check that the local MQTT broker runs and accepts the bridge's credentials. Outbound messages stay spooled \
                 in pub/sub meanwhile, and are refused once the spool is full.".into(),
                Some(b.since_ms));
        }
        if b.refused_recent > 0 || b.rejected_recent > 0 {
            add("messaging-loss", "warning", "@bridge",
                format!("MQTT bridge: {} inbound messages left with the broker (queue full) and {} rejected as unacceptable since the previous report",
                        b.refused_recent, b.rejected_recent),
                "Refused messages are redelivered by the broker once queues drain; rejected ones are dropped. Check the subscribers \
                 and the payload/topic limits of the LAN publishers.".into(),
                Some(now));
        }
    }
    if let Some(streams) = &services.streams {
        for s in &streams.streams {
            let name = &s.name;
            let at_risk = !s.overwrite || s.export.as_ref().is_some_and(|e| e.lag > 0);
            if (s.fill_percent >= FULL_PERCENT && at_risk) || s.rejected_recent > 0 {
                add("stream-near-limit", "warning", name,
                    format!("stream {name} is {}% full ({} of {} bytes); {} appends rejected since the previous report",
                            s.fill_percent, s.bytes, s.max_size, s.rejected_recent),
                    if s.overwrite {
                        "Unexported messages will be overwritten: fix the export or raise the stream's max_size.".into()
                    } else {
                        "New data is rejected when full: drain or export the stream, or raise its max_size.".into()
                    },
                    Some(now));
            }
            let Some(e) = &s.export else { continue };
            if e.lost_recent > 0 {
                add("stream-data-lost", "critical", name,
                    format!("stream {name}: {} messages were retired before export since the previous report ({} in total)", e.lost_recent, e.lost),
                    "Those messages are gone. Fix the export destination, then raise max_size or the time-to-live so the backlog fits.".into(),
                    Some(now));
            }
            if e.consecutive_failures >= FAILURE_THRESHOLD {
                add("export-failing", "critical", name,
                    format!("stream {name} export failed {} times in a row ({} messages waiting): {}", e.consecutive_failures, e.lag,
                            e.last_error.as_deref().unwrap_or("unknown error")),
                    "Check the export destination and its credentials from the device; the checkpoint resumes once it answers 2xx.".into(),
                    e.last_success_ms);
            }
        }
    }
    if let Some(shadows) = &services.shadows {
        for s in &shadows.sync {
            let name = &s.name;
            if s.consecutive_failures >= FAILURE_THRESHOLD {
                add("shadow-sync-failing", "warning", name,
                    format!("shadow {name:?} sync failed {} times in a row: {}", s.consecutive_failures, s.last_error.as_deref().unwrap_or("unknown error")),
                    "Check the shadow service and its credentials; local shadow changes are kept and pushed once it answers.".into(),
                    s.last_success_ms);
            }
            if s.conflicts_recent > 0 {
                add("shadow-conflict", "warning", name,
                    format!("shadow {name:?}: {} keys changed on both sides since the previous report, settled by the configured owner", s.conflicts_recent),
                    "Review the shadow: the losing side's values were overwritten. Make one side own each section if the writes are expected.".into(),
                    Some(now));
            }
        }
    }
    found
}

/// `suifleet-indexer telemetry-alerts FILE`: for each signed envelope in FILE
/// (a JSON array, or `{"messages": [...]}` as posted to the ingress), the
/// verified signer, the snapshot's own judgement, its services section and
/// the service-health alerts it raises. Offline: the signature and bounds
/// are checked, the signer's registry identity is not.
pub fn explain(envelopes: &[crate::telemetry::Envelope]) -> Vec<serde_json::Value> {
    envelopes
        .iter()
        .map(|envelope| {
            let checked = crate::telemetry::verify(envelope)
                .and_then(|(snapshot, signer)| crate::telemetry::validate(&snapshot).map(|()| (snapshot, signer)));
            match checked {
                Err(error) => serde_json::json!({ "error": error }),
                Ok((snapshot, signer)) => serde_json::json!({
                    "seq": snapshot.seq, "signer": signer, "schema": snapshot.schema, "observed_ms": snapshot.observed_ms,
                    "health": snapshot.health, "reasons": snapshot.reasons, "services": snapshot.services,
                    "alerts": conditions(&snapshot),
                }),
            }
        })
        .collect()
}

pub fn explain_file(path: &std::path::Path) -> anyhow::Result<()> {
    let value: serde_json::Value = serde_json::from_slice(&std::fs::read(path)?)?;
    let envelopes = value.get("messages").cloned().unwrap_or(value);
    let envelopes: Vec<crate::telemetry::Envelope> = serde_json::from_value(envelopes)?;
    println!("{}", serde_json::to_string_pretty(&explain(&envelopes))?);
    Ok(())
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// The alert kinds derived from the services section (as the dApp lists them).
    const KINDS: [&str; 8] = [
        "messaging-backpressure", "messaging-loss", "bridge-disconnected", "stream-near-limit",
        "stream-data-lost", "export-failing", "shadow-sync-failing", "shadow-conflict",
    ];

    pub(crate) fn services() -> Services {
        Services {
            version: 1,
            messaging: Some(Messaging {
                subscriptions: 2, queued: 10, refused: 4, dropped: 0, omitted: 0,
                queues: vec![QueueHealth {
                    principal: "app".into(), subscriptions: 1, queued: 10, fill_percent: 100, full_since_ms: Some(1_000_000),
                    refused: 4, refused_recent: 1, dropped: 0, dropped_recent: 0,
                }],
            }),
            bridge: Some(BridgeHealth {
                connected: false, since_ms: 1_000_000, connects: 1, refused: 0, refused_recent: 0, rejected: 0, rejected_recent: 0,
                last_error: Some("connection refused".into()),
            }),
            streams: Some(StreamsHealth {
                omitted: 0,
                streams: vec![StreamHealth {
                    name: "metrics".into(), bytes: 950, max_size: 1000, fill_percent: 95, messages: 9, overwrite: true,
                    rejected: 0, rejected_recent: 0,
                    export: Some(ExportHealth {
                        lag: 9, lost: 2, lost_recent: 2, batches: 0, failures: 5, consecutive_failures: 5, last_success_ms: None,
                        last_error: Some("destination answered 500 Internal Server Error".into()),
                    }),
                }],
            }),
            shadows: Some(ShadowsHealth {
                shadows: 1, notifications_refused: 0, notifications_refused_recent: 0, omitted: 0,
                sync: vec![ShadowSyncHealth {
                    name: "cfg".into(), conflicts: 1, conflicts_recent: 1, failures: 0, consecutive_failures: 0,
                    last_success_ms: Some(1_100_000), last_error: None,
                }],
            }),
        }
    }

    fn snapshot_at(observed_ms: u64) -> Snapshot {
        let mut s = crate::telemetry::tests::snapshot(observed_ms, observed_ms);
        (s.schema, s.services) = (2, Some(services()));
        s
    }

    #[test]
    fn conditions_follow_the_facts() {
        // 60 s interval: sustained after 120 s.
        let early: Vec<_> = conditions(&snapshot_at(1_100_000)).into_iter().map(|c| (c.kind, c.subject)).collect();
        assert_eq!(early, [("messaging-loss", "app".into()), ("stream-near-limit", "metrics".into()), ("stream-data-lost", "metrics".into()),
                           ("export-failing", "metrics".into()), ("shadow-conflict", "cfg".into())]);
        let late = conditions(&snapshot_at(1_120_000));
        let kinds: Vec<_> = late.iter().map(|c| c.kind).collect();
        assert_eq!(kinds, ["messaging-backpressure", "messaging-loss", "bridge-disconnected", "stream-near-limit", "stream-data-lost",
                           "export-failing", "shadow-conflict"]);
        assert!(late.iter().all(|c| KINDS.contains(&c.kind) && !c.action.is_empty()));
        assert_eq!(late[0].since_ms, Some(1_000_000));
        assert!(late[2].message.contains("connection refused"));
        // Quiet facts raise nothing; an overwrite stream without an export may run full.
        let mut quiet = snapshot_at(1_120_000);
        let s = quiet.services.as_mut().unwrap();
        s.messaging.as_mut().unwrap().queues[0] = QueueHealth { full_since_ms: None, fill_percent: 10, refused_recent: 0, ..s.messaging.as_ref().unwrap().queues[0].clone() };
        s.bridge.as_mut().unwrap().connected = true;
        s.streams.as_mut().unwrap().streams[0].export = None;
        s.shadows.as_mut().unwrap().sync[0].conflicts_recent = 0;
        assert!(conditions(&quiet).is_empty(), "{:?}", conditions(&quiet));
        // A rejecting stream near its limit alerts without an export.
        quiet.services.as_mut().unwrap().streams.as_mut().unwrap().streams[0].overwrite = false;
        assert_eq!(conditions(&quiet)[0].kind, "stream-near-limit");
        assert!(conditions(&crate::telemetry::tests::snapshot(1, 1)).is_empty(), "schema 1 has no services");
    }

    #[test]
    fn explains_signed_envelopes_offline() {
        use crate::telemetry::tests::{sign, KEY};
        let good = sign(&snapshot_at(1_120_000), &KEY);
        let mut forged = good.clone();
        forged.message = sign(&crate::telemetry::tests::snapshot(2, 2), &KEY).message;
        let explained = explain(&[good, forged]);
        assert_eq!(explained[0]["schema"], 2);
        assert_eq!(explained[0]["alerts"].as_array().unwrap().len(), 7);
        assert_eq!(explained[1]["error"], "signature does not verify");
    }

    #[test]
    fn shared_vector_alerts() {
        let cases: serde_json::Value = serde_json::from_str(include_str!("../../interfaces/telemetry-cases.json")).unwrap();
        let snapshot: Snapshot = serde_json::from_value(cases["signed_services"]["snapshot"].clone()).unwrap();
        assert!(validate(snapshot.services.as_ref().unwrap()).is_ok());
        let kinds: Vec<_> = conditions(&snapshot).into_iter().map(|c| (c.kind, c.subject)).collect();
        assert_eq!(kinds, [("messaging-backpressure", "svc".into()), ("messaging-loss", "svc".into()), ("bridge-disconnected", String::new()),
                           ("stream-near-limit", "metrics".into()), ("stream-data-lost", "metrics".into()), ("export-failing", "metrics".into()),
                           ("shadow-conflict", String::new())]);
    }

    #[test]
    fn bounds() {
        assert!(validate(&services()).is_ok());
        for change in [
            |s: &mut Services| s.version = 2,
            |s: &mut Services| s.messaging.as_mut().unwrap().queues[0].principal = "p".repeat(MAX_NAME + 1),
            |s: &mut Services| s.messaging.as_mut().unwrap().queues = vec![s.messaging.as_ref().unwrap().queues[0].clone(); MAX_ENTRIES + 1],
            |s: &mut Services| s.messaging.as_mut().unwrap().queues[0].fill_percent = 101,
            |s: &mut Services| s.bridge.as_mut().unwrap().last_error = Some("e".repeat(MAX_TEXT + 1)),
            |s: &mut Services| s.streams.as_mut().unwrap().streams[0].export.as_mut().unwrap().last_error = Some("e".repeat(MAX_TEXT + 1)),
            |s: &mut Services| s.shadows.as_mut().unwrap().sync[0].name = "n".repeat(MAX_NAME + 1),
        ] {
            let mut s = services();
            change(&mut s);
            assert!(validate(&s).is_err(), "{s:?}");
        }
    }
}
