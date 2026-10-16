use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Debug, Default, Deserialize, Serialize)]
pub struct State {
    pub binding: String,
    /// Pre-apply notices/deferrals are not execution intents or status receipts.
    #[serde(default)]
    pub deferred: BTreeMap<String, crate::control::Deferred>,
    pub cursor: Option<Value>,
    #[serde(default)]
    pub initialized: bool,
    pub deployments: BTreeMap<String, Outcome>,
    /// Components on this device by name, maintained by every deployment.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub components: BTreeMap<String, Component>,
    /// Resume point of the DeviceAddedToGroup scan, which starts at genesis
    /// whatever the deployment baseline (membership predates enrollment).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub membership_cursor: Option<Value>,
    /// Groups that ever added this device. Membership itself is re-read from
    /// each group object on every poll.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub groups: BTreeSet<String>,
    /// Component name → deployment desired-state reconciliation last acted on.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub managed: BTreeMap<String, String>,
    /// Component name → deployment this daemon last started for it, one-shot
    /// or desired, saved with that deployment's intent. Its outcome in
    /// `deployments` says whether it is what the component now runs.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub current: BTreeMap<String, String>,
    /// Fingerprint of the desired state last reconciled (reconcile.rs).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub desired: Option<String>,
    /// Desired-state step last started, saved before its intent. Until it has
    /// applied, later steps stay stopped, across restarts.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub step: Option<Step>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Step {
    pub deployment: String,
    /// Fingerprint of the desired state it was started for: that state stays
    /// stopped by it, newer ones only while it is in flight.
    pub desired: String,
}
/// An installed component. It is recorded unconfirmed before its install
/// starts and confirmed only once the install is known to have succeeded;
/// an unconfirmed entry is never reused to satisfy a dependency.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct Component {
    pub version: String,
    pub deployment_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub catalog_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version_id: Option<String>,
    pub recipe_sha256: String,
    pub artifact_sha256: String,
    #[serde(default)]
    pub dependencies: crate::deps::Dependencies,
    pub confirmed: bool,
}
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Outcome {
    pub status: Option<u8>,
    pub reported: bool,
    pub error: Option<String>,
    /// Firmware-helper request this deployment may have submitted, recorded
    /// before the update starts so a restart can still learn its outcome.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub firmware_request: Option<String>,
    /// The privileged update may or may not have been applied: never
    /// reported, never re-applied and never turned into Failed.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub indeterminate: bool,
}
pub struct Journal {
    path: PathBuf,
    _lock: File,
    pub state: State,
}
impl Journal {
    pub fn open(directory: &Path, binding: String) -> anyhow::Result<Self> {
        std::fs::create_dir_all(directory)?;
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(directory.join("daemon.lock"))?;
        lock.try_lock_exclusive()
            .map_err(|_| anyhow::anyhow!("another daemon owns this state directory"))?;
        let path = directory.join("state.json");
        let mut state: State = if path.exists() {
            serde_json::from_slice(&std::fs::read(&path)?)?
        } else {
            State::default()
        };
        anyhow::ensure!(
            state.binding.is_empty() || state.binding == binding,
            "state belongs to another network/package/device; use a separate state_dir"
        );
        state.binding = binding;
        let journal = Self {
            path,
            _lock: lock,
            state,
        };
        journal.save()?;
        Ok(journal)
    }
    /// Settle deployments an interrupted run left without a status. Those
    /// `pending` says can still learn their real outcome (and indeterminate
    /// ones) are kept; every other one becomes Failed and is never re-applied.
    pub fn settle_interrupted(
        &mut self,
        mut pending: impl FnMut(&str, &Outcome) -> bool,
    ) -> anyhow::Result<()> {
        for (id, outcome) in self.state.deployments.iter_mut() {
            if outcome.status.is_some() || outcome.indeterminate || pending(id, outcome) {
                continue;
            }
            outcome.status = Some(crate::protocol::STATUS_FAILED);
            outcome.error = Some(
                "daemon interrupted during apply; inspect device before explicitly redeploying"
                    .into(),
            );
        }
        self.save()
    }
    /// Confirm the root component a deployment installed once it is Applied.
    pub fn confirm_components(&mut self, deployment_id: &str) {
        for component in self.state.components.values_mut() {
            if component.deployment_id == deployment_id {
                component.confirmed = true;
            }
        }
    }
    pub fn save(&self) -> anyhow::Result<()> {
        let temporary = self.path.with_extension("json.tmp");
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(&temporary)?;
        file.write_all(&serde_json::to_vec_pretty(&self.state)?)?;
        file.sync_all()?;
        std::fs::rename(temporary, &self.path)?;
        File::open(self.path.parent().unwrap())?.sync_all()?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Reopen after the previous journal was dropped. A child forked by a
    /// concurrently running test can hold the inherited lock until it execs.
    fn reopen(path: &Path) -> Journal {
        for _ in 0..200 {
            if let Ok(journal) = Journal::open(path, "network/device".into()) {
                return journal;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        Journal::open(path, "network/device".into()).unwrap()
    }
    #[test]
    fn restart_retains_cursor_and_does_not_reapply_uncertain_update() {
        let path = std::env::temp_dir().join(format!(
            "suifleet-test-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        {
            let mut journal = Journal::open(&path, "network/device".into()).unwrap();
            assert!(Journal::open(&path, "network/device".into()).is_err());
            journal.state.cursor = Some(serde_json::json!({"txDigest":"digest","eventSeq":"0"}));
            journal.state.deployments.insert(
                "0x1".into(),
                Outcome::default(),
            );
            for (id, request, indeterminate) in [("0x2", true, false), ("0x3", true, true), ("0x4", true, false)] {
                journal.state.deployments.insert(
                    id.into(),
                    Outcome {
                        firmware_request: request.then(|| format!("request-{id}")),
                        indeterminate,
                        ..Outcome::default()
                    },
                );
            }
            journal.save().unwrap();
        }
        {
            let mut journal = reopen(&path);
            assert!(journal.state.cursor.is_some());
            assert_eq!(journal.state.deployments["0x1"].status, None, "open alone never settles");
            let mut asked = Vec::new();
            journal
                .settle_interrupted(|id, outcome| {
                    asked.push(id.to_owned());
                    outcome.firmware_request.as_deref() == Some("request-0x2")
                })
                .unwrap();
            asked.sort();
            assert_eq!(asked, ["0x1", "0x2", "0x4"], "indeterminate outcomes are never re-judged");
            let states: Vec<_> = ["0x1", "0x2", "0x3", "0x4"]
                .iter()
                .map(|id| journal.state.deployments[*id].status)
                .collect();
            assert_eq!(states, [Some(2), None, None, Some(2)]);
            assert!(!journal.state.deployments["0x1"].reported);
        }
        {
            let journal = reopen(&path);
            assert_eq!(journal.state.deployments["0x1"].status, Some(2), "settled outcome is durable");
            assert!(journal.state.deployments["0x3"].indeterminate);
        }
        assert!(Journal::open(&path, "other-device".into()).is_err());
        std::fs::remove_dir_all(path).unwrap();
    }
}
