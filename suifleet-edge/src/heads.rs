//! Device inbox and group heads (SF-125): which announced deployments a poll
//! must look at, and when the daemon must wake for a scheduled start.
//!
//! Announcements are hints. Every candidate still goes through the same
//! record, operator and membership checks as an event would, and through the
//! journal's at-most-once bookkeeping.
use crate::{
    state::{Journal, State},
    sui_client::Head,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
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

/// Start heads for a journal from before inboxes at `since_ms` in one
/// journal write, moving past pruned deployment history to `pruned_tip` when
/// given. The baseline comes from the old cursor, so a journal must never
/// hold a newer cursor without it: after an interruption the baseline would
/// come from the newer cursor and records created in between would be skipped.
pub fn begin(journal: &mut Journal, since_ms: u64, pruned_tip: Option<Option<Value>>) -> anyhow::Result<()> {
    if let Some(tip) = pruned_tip {
        skip_pruned_deployments(&mut journal.state, tip);
    }
    journal.state.heads = Some(Heads { since_ms, ..Default::default() });
    journal.save()
}

/// Resume the deployment event scan after pruned history, once heads cover
/// it. The combined discovery stream (SF-122) moves to `tip` as one cursor;
/// before its migration the deployment cursor stops at the merge fence, from
/// which the combined stream continues.
pub fn skip_pruned_deployments(state: &mut State, tip: Option<Value>) {
    if state.discovery_cursor.is_some() {
        state.cursor = tip.clone();
        state.discovery_cursor = tip.clone();
        state.membership_cursor = tip;
    } else {
        state.cursor = fence(state).or(tip);
    }
}

/// Resume the pre-migration membership scan after pruned history at the
/// merge fence, once the inbox lists the device's groups.
pub fn skip_pruned_memberships(state: &mut State, tip: Option<Value>) {
    state.membership_cursor = fence(state).or(tip);
}

fn fence(state: &State) -> Option<Value> {
    state.discovery_merge_checkpoint.map(|checkpoint| serde_json::json!({ "checkpoint": checkpoint }))
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
    fn a_moved_cursor_is_never_durable_without_its_baseline() {
        let dir = std::env::temp_dir().join(format!("sf125-begin-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut journal = Journal::open(&dir, "local".into()).unwrap();
        journal.state.initialized = true;
        journal.state.cursor = Some(serde_json::json!({"checkpoint": 10}));
        journal.save().unwrap();
        // The write fails after the snapshot is renamed into place, as a
        // crash right after it would leave the journal.
        crate::state::FAIL_DIRECTORY_SYNC.with(|fault| fault.set(true));
        let tip = Some(serde_json::json!({"checkpoint": 101}));
        assert!(begin(&mut journal, 9_000, Some(tip.clone())).is_err());
        let disk: crate::state::State =
            serde_json::from_slice(&std::fs::read(dir.join("state.json")).unwrap()).unwrap();
        assert_eq!(disk.cursor, tip);
        assert_eq!(disk.heads.map(|heads| heads.since_ms), Some(9_000), "the baseline from the old cursor");
        drop(journal);
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn pruned_history_resumes_at_the_fence_or_the_combined_tip() {
        let tip = Some(serde_json::json!({"checkpoint": 900}));
        let mut state = State {
            cursor: Some(serde_json::json!({"checkpoint": 10})),
            membership_cursor: Some(serde_json::json!({"checkpoint": 20})),
            discovery_merge_checkpoint: Some(500),
            ..Default::default()
        };
        // Before the merge: each pre-migration cursor stops at the fence on
        // its own; the other keeps scanning what is still retained.
        skip_pruned_deployments(&mut state, tip.clone());
        assert_eq!(state.cursor, Some(serde_json::json!({"checkpoint": 500})));
        assert_eq!(state.membership_cursor, Some(serde_json::json!({"checkpoint": 20})));
        skip_pruned_memberships(&mut state, tip.clone());
        assert_eq!(state.membership_cursor, Some(serde_json::json!({"checkpoint": 500})));
        assert_eq!(state.discovery_cursor, None);
        // After it: the one combined stream moves to the tip.
        state.discovery_cursor = state.cursor.clone();
        skip_pruned_deployments(&mut state, tip.clone());
        assert_eq!((&state.cursor, &state.discovery_cursor, &state.membership_cursor), (&tip, &tip, &tip));
    }

    #[test]
    fn a_scheduled_start_shortens_the_sleep() {
        assert_eq!(until_start(1_000, 4_000), Some(3_000));
        assert_eq!(until_start(1_000, 0), None, "no scheduled start");
        assert_eq!(until_start(1_000, 900), None, "a past start does not wake");
    }
}
