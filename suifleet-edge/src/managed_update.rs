//! Private control channel inherited from the trusted portable supervisor.
//! Components never inherit this descriptor. Executable selection and rollback
//! remain in the supervisor, outside the daemon's state directory.
use std::{os::unix::net::UnixDatagram, sync::OnceLock, time::Duration};

static CHANNEL: OnceLock<UnixDatagram> = OnceLock::new();

#[cfg(target_os = "linux")]
pub fn attach(fd: std::os::fd::RawFd) -> anyhow::Result<()> {
    use std::os::fd::FromRawFd;
    anyhow::ensure!(fd > 2, "invalid managed update channel");
    // This entry point is used only by the gated portable child, before any
    // threads or component children can inherit the platform's descriptor.
    anyhow::ensure!(unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } == 0,
        "cannot protect managed update channel from inheritance");
    let channel = unsafe { UnixDatagram::from_raw_fd(fd) };
    channel.set_read_timeout(Some(Duration::from_secs(10)))?;
    channel.set_write_timeout(Some(Duration::from_secs(10)))?;
    CHANNEL.set(channel).map_err(|_| anyhow::anyhow!("duplicate managed update channel"))
}

pub fn attached() -> bool {
    CHANNEL.get().is_some()
}

#[derive(serde::Deserialize)]
pub struct ResultRecord {
    pub deployment: String,
    pub applied: bool,
}

pub fn ready() -> anyhow::Result<Option<ResultRecord>> {
    let channel = CHANNEL.get().ok_or_else(|| anyhow::anyhow!("managed update channel unavailable"))?;
    channel.send(b"ready")?;
    let mut bytes = [0; 1024];
    let len = channel.recv(&mut bytes)?;
    Ok(serde_json::from_slice(&bytes[..len])?)
}

pub fn request(deployment: &str, sha256: &str) -> anyhow::Result<()> {
    let channel = CHANNEL.get().ok_or_else(|| anyhow::anyhow!("managed update channel unavailable"))?;
    let message = serde_json::to_vec(&serde_json::json!({
        "deployment": deployment,
        "sha256": sha256.to_ascii_lowercase(),
    }))?;
    anyhow::ensure!(message.len() < 1024, "managed update request exceeds the channel limit");
    channel.send(&message)?;
    Ok(())
}
