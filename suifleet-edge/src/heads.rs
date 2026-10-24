//! Device inbox and group heads (SF-125): which announced deployments a poll
//! must look at, and when the daemon must wake for a scheduled start.
//!
//! Announcements are hints. Every candidate still goes through the same
//! record, operator and membership checks as an event would, and through the
//! journal's at-most-once bookkeeping.
use crate::sui_client::Head;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// What the daemon last acted on, per inbox or group.
#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
pub struct Heads {
    /// Chain time (UTC ms) from which announced deployments apply: the
    /// enrollment baseline, or the first poll that read heads. Earlier
    /// records belong to the event path, as before.
    pub since_ms: u64,
    /// Inbox or group ID → what was last acted on.
    #[serde(default)]
    pub seen: BTreeMap<String, Seen>,
    /// Device-clock time (UTC ms) of the last event safety-net scan.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_scan_ms: Option<u64>,
    /// Earliest announced scheduled start (UTC ms) in any head; 0 for none.
    #[serde(default)]
    pub next_start_ms: u64,
    /// Chain time (UTC ms) taken before the last completed poll read the
    /// inbox. A group the inbox lists later was joined after it, so its head
    /// applies records from then on, including ones created while offline.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub polled_ms: Option<u64>,
}

impl Heads {
    /// Where a head first seen in this poll starts applying records.
    pub fn baseline(&self) -> u64 {
        self.polled_ms.unwrap_or(self.since_ms)
    }
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
pub struct Seen {
    pub seq: u64,
    /// Records created before this chain time are not applied from this
    /// head: a group joined later starts from the last poll that did not
    /// list it, as the event path would.
    pub since_ms: u64,
    /// Component name → deployment, as last acted on.
    #[serde(default)]
    pub components: BTreeMap<String, String>,
}

/// Deployments `head` announces that `seen` has not acted on, by component
/// name. Nothing when the sequence is unchanged.
pub fn changed(head: &Head, seen: &Seen) -> Vec<String> {
    if head.seq == seen.seq {
        return Vec::new();
    }
    head.components
        .iter()
        .filter(|(name, id)| seen.components.get(*name) != Some(*id))
        .map(|(_, id)| id.clone())
        .collect()
}

/// Whether the event safety net is due. Every poll without heads; a clock
/// that moved back counts as due.
pub fn scan_due(last_ms: Option<u64>, now_ms: u64, interval_seconds: u64) -> bool {
    match last_ms {
        None => true,
        Some(last) => last > now_ms || now_ms - last >= interval_seconds.saturating_mul(1000),
    }
}

/// Milliseconds until the earliest announced scheduled start, when one is
/// still ahead; the daemon wakes then rather than a whole check interval late.
pub fn until_start(now_ms: u64, next_start_ms: u64) -> Option<u64> {
    (next_start_ms > now_ms).then(|| next_start_ms - now_ms)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn head(seq: u64, components: &[(&str, &str)]) -> Head {
        Head {
            seq,
            next_start_ms: 0,
            components: components.iter().map(|(n, d)| (n.to_string(), d.to_string())).collect(),
        }
    }

    #[test]
    fn only_changed_components_are_candidates() {
        let seen = Seen {
            seq: 2,
            since_ms: 0,
            components: [("app".into(), "0xa".into()), ("db".into(), "0xd".into())].into(),
        };
        assert!(changed(&head(2, &[("app", "0xb")]), &seen).is_empty(), "unchanged sequence");
        assert_eq!(changed(&head(3, &[("app", "0xb"), ("db", "0xd")]), &seen), vec!["0xb"]);
        assert_eq!(changed(&head(1, &[("new", "0xe")]), &Seen::default()), vec!["0xe"]);
        // A first sight with seq 0 has nothing announced yet.
        assert!(changed(&head(0, &[]), &Seen::default()).is_empty());
    }

    #[test]
    fn a_later_group_starts_from_the_last_poll() {
        let mut heads = Heads { since_ms: 1_000, ..Default::default() };
        assert_eq!(heads.baseline(), 1_000, "before the first poll: the enrollment baseline");
        heads.polled_ms = Some(5_000);
        assert_eq!(heads.baseline(), 5_000);
        // Older journals without the field still parse.
        let old: Heads = serde_json::from_str(r#"{"since_ms":7,"seen":{}}"#).unwrap();
        assert_eq!((old.polled_ms, old.baseline()), (None, 7));
    }

    #[test]
    fn the_event_scan_is_bounded_but_survives_clock_rollback() {
        assert!(scan_due(None, 10, 3600));
        assert!(!scan_due(Some(1_000), 3_599_999, 3600));
        assert!(scan_due(Some(1_000), 3_601_000, 3600));
        assert!(scan_due(Some(5_000), 4_000, 3600), "clock moved back");
        assert!(scan_due(Some(5_000), 5_000, 0), "0 scans every poll");
    }

    #[test]
    fn a_scheduled_start_shortens_the_sleep() {
        assert_eq!(until_start(1_000, 4_000), Some(3_000));
        assert_eq!(until_start(1_000, 0), None, "no scheduled start");
        assert_eq!(until_start(1_000, 900), None, "a past start does not wake");
    }
}
