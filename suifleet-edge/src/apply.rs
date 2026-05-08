use std::path::Path;
use tracing::{error, info};

pub struct ApplyRunner {
    command: String,
}

impl ApplyRunner {
    pub fn new(command: String) -> Self {
        Self { command }
    }

    pub async fn apply(&self, artifact: &Path) -> anyhow::Result<()> {
        let mut parts = self.command.split_whitespace();
        let bin = parts.next().ok_or_else(|| anyhow::anyhow!("apply.command is empty"))?;
        let mut cmd = tokio::process::Command::new(bin);
        cmd.args(parts).arg(artifact);

        info!("Applying artifact: {:?}", cmd);

        let status = cmd.status().await?;

        if status.success() {
            Ok(())
        } else {
            let code = status.code().unwrap_or(-1);
            error!("apply command exited with code {}", code);
            anyhow::bail!("apply command failed with exit code {}", code)
        }
    }
}
