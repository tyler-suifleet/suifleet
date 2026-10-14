use crate::config::ApplyConfig;
use std::{path::Path, time::Duration};

pub struct ApplyRunner {
    config: ApplyConfig,
}
impl ApplyRunner {
    pub fn new(config: ApplyConfig) -> Self {
        Self { config }
    }
    pub async fn apply(&self, artifact: &Path, install: Option<&str>) -> anyhow::Result<()> {
        let mut command = if let Some(script) = install {
            anyhow::ensure!(
                self.config.allow_recipe_commands,
                "recipe commands disabled"
            );
            // Artifact substitutions are shell-quoted; operator-authored scripts are intentional code.
            let path = artifact
                .to_str()
                .ok_or_else(|| anyhow::anyhow!("non-UTF8 artifact path"))?;
            let parent = artifact
                .parent()
                .unwrap()
                .to_str()
                .ok_or_else(|| anyhow::anyhow!("non-UTF8 artifact directory"))?;
            let script = script
                .replace("{artifact}", &shell_words::quote(path))
                .replace("{artifacts}", &shell_words::quote(parent));
            let mut cmd = tokio::process::Command::new("/bin/sh");
            cmd.arg("-c").arg(script);
            cmd
        } else {
            let parts = shell_words::split(&self.config.command)?;
            let bin = parts
                .first()
                .ok_or_else(|| anyhow::anyhow!("apply.command is empty"))?;
            let mut cmd = tokio::process::Command::new(bin);
            cmd.args(&parts[1..]).arg(artifact);
            cmd
        };
        command.kill_on_drop(true);
        #[cfg(unix)]
        command.process_group(0);
        let mut child = command.spawn()?;
        #[cfg(unix)]
        let _group = ProcessGroup(
            child
                .id()
                .ok_or_else(|| anyhow::anyhow!("updater has no pid"))? as i32,
        );
        let status = tokio::time::timeout(
            Duration::from_secs(self.config.timeout_seconds),
            child.wait(),
        )
        .await
        .map_err(|_| anyhow::anyhow!("apply command timed out"))??;
        anyhow::ensure!(status.success(), "apply command failed: {status}");
        Ok(())
    }
}

/// Stop all updater descendants on timeout/cancellation, not just its shell.
#[cfg(unix)]
struct ProcessGroup(i32);
#[cfg(unix)]
impl Drop for ProcessGroup {
    fn drop(&mut self) {
        // The child was placed in a fresh process group whose ID is its PID.
        unsafe {
            libc::kill(-self.0, libc::SIGKILL);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn propagates_apply_failure() {
        let runner = ApplyRunner::new(ApplyConfig {
            command: "/bin/false".into(),
            timeout_seconds: 1,
            allow_recipe_commands: false,
        });
        assert!(runner
            .apply(Path::new("/tmp/artifact"), None)
            .await
            .is_err());
    }
    #[tokio::test]
    async fn times_out_hung_apply() {
        let runner = ApplyRunner::new(ApplyConfig {
            command: "/bin/true".into(),
            timeout_seconds: 1,
            allow_recipe_commands: true,
        });
        assert!(runner
            .apply(Path::new("/tmp/artifact"), Some("exec sleep 10"))
            .await
            .is_err());
    }
}
