use std::path::Path;
use tracing::{error, info};

pub struct SwupdateRunner {
    binary: std::path::PathBuf,
    dry_run: bool,
}

impl SwupdateRunner {
    pub fn new(binary: std::path::PathBuf, dry_run: bool) -> Self {
        Self { binary, dry_run }
    }

    /// Invoke swupdate with the given firmware image. Returns Ok(()) on success.
    pub async fn apply(&self, swu_path: &Path) -> anyhow::Result<()> {
        let mut cmd = tokio::process::Command::new(&self.binary);
        cmd.arg("-i").arg(swu_path);

        if self.dry_run {
            cmd.arg("-n"); // swupdate dry-run flag
        }

        info!("Invoking swupdate: {:?}", cmd);

        let status = cmd.status().await?;

        if status.success() {
            info!("swupdate finished successfully");
            Ok(())
        } else {
            let code = status.code().unwrap_or(-1);
            error!("swupdate exited with code {}", code);
            anyhow::bail!("swupdate failed with exit code {}", code)
        }
    }
}
