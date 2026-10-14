use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs::{File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Debug, Default, Deserialize, Serialize)]
pub struct State {
    pub binding: String,
    pub cursor: Option<Value>,
    #[serde(default)]
    pub initialized: bool,
    pub deployments: BTreeMap<String, Outcome>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Outcome {
    pub status: Option<u8>,
    pub reported: bool,
    pub error: Option<String>,
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
        for outcome in state
            .deployments
            .values_mut()
            .filter(|outcome| outcome.status.is_none())
        {
            outcome.status = Some(2);
            outcome.error = Some(
                "daemon interrupted during apply; inspect device before explicitly redeploying"
                    .into(),
            );
        }
        let journal = Self {
            path,
            _lock: lock,
            state,
        };
        journal.save()?;
        Ok(journal)
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
                Outcome {
                    status: None,
                    reported: false,
                    error: None,
                },
            );
            journal.save().unwrap();
        }
        {
            let journal = Journal::open(&path, "network/device".into()).unwrap();
            assert!(journal.state.cursor.is_some());
            assert_eq!(journal.state.deployments["0x1"].status, Some(2));
            assert!(!journal.state.deployments["0x1"].reported);
        }
        assert!(Journal::open(&path, "other-device".into()).is_err());
        std::fs::remove_dir_all(path).unwrap();
    }
}
