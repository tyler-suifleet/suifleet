//! Explicit destination-only AWS tunnel adapter. Runs under a dedicated UID,
//! outside the device daemon/component trust domain (docs/SECURE_TUNNELING.md).
use crate::{iotcore, mqtt};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    net::SocketAddrV4,
    os::unix::{fs::PermissionsExt, process::CommandExt},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

const MAX_NOTIFICATION: usize = 16 * 1024;
const MAX_SEEN: usize = 4096;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub enabled: bool,
    /// Separate IoT client id/certificate policy; never share the daemon's id.
    pub aws_iot: iotcore::IotConfig,
    pub region: String,
    pub proxy: PathBuf,
    pub proxy_sha256: String,
    /// A root-owned empty directory: upstream otherwise searches ./config.
    pub working_directory: PathBuf,
    pub proxy_settings: PathBuf,
    pub destinations: BTreeMap<String, SocketAddrV4>,
    pub max_concurrent_tunnels: usize,
    pub tunnel_timeout_seconds: u64,
}

fn service_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 32
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
}

impl Settings {
    pub fn topic(&self) -> String {
        format!("$aws/things/{}/tunnels/notify", self.aws_iot.thing_name)
    }
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(
            self.enabled,
            "secure tunneling requires explicit enabled=true"
        );
        self.aws_iot.validate()?;
        anyhow::ensure!(
            !self.region.is_empty()
                && self.region.len() <= 32
                && self
                    .region
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'),
            "invalid tunnel region"
        );
        anyhow::ensure!(
            self.proxy.is_absolute()
                && self.working_directory.is_absolute()
                && self.proxy_settings.is_absolute(),
            "tunnel paths must be absolute"
        );
        anyhow::ensure!(
            self.proxy_sha256.len() == 64
                && self.proxy_sha256.bytes().all(|b| b.is_ascii_hexdigit()),
            "proxy_sha256 must be a SHA-256 hex digest"
        );
        anyhow::ensure!(
            (1..=16).contains(&self.destinations.len())
                && self.destinations.iter().all(|(id, addr)| service_id(id)
                    && addr.ip().is_loopback()
                    && addr.port() != 0),
            "destinations require 1-16 named IPv4 loopback services"
        );
        anyhow::ensure!(
            (1..=16).contains(&self.max_concurrent_tunnels),
            "max_concurrent_tunnels must be 1-16"
        );
        anyhow::ensure!(
            (1..=43200).contains(&self.tunnel_timeout_seconds),
            "tunnel_timeout_seconds must be 1-43200"
        );
        Ok(())
    }
    fn preflight(&self, path: &Path) -> anyhow::Result<()> {
        self.validate()?;
        // Root is the installer, never the service identity. Operators must
        // reserve this UID exclusively for the adapter and its local proxies.
        anyhow::ensure!(
            unsafe { libc::geteuid() } != 0,
            "run tunneling as its dedicated unprivileged service user"
        );
        trusted_path(path)?;
        trusted_path(&self.proxy)?;
        trusted_path(&self.working_directory)?;
        trusted_path(&self.proxy_settings)?;
        let policy: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&self.proxy_settings)?)?;
        let expected: serde_json::Value =
            serde_json::from_str(include_str!("../packaging/tunneling/proxy-settings.json"))?;
        anyhow::ensure!(
            policy == expected,
            "proxy settings must match the packaged connection/retry limits"
        );
        anyhow::ensure!(
            std::fs::read_dir(&self.working_directory)?.next().is_none(),
            "tunnel working_directory must be empty"
        );
        let bytes = std::fs::read(&self.proxy)?;
        anyhow::ensure!(
            hex::encode(Sha256::digest(bytes)).eq_ignore_ascii_case(&self.proxy_sha256),
            "localproxy digest mismatch"
        );
        Ok(())
    }
}

/// Refuse symlinks and every writable/non-root path component. This keeps
/// configuration and executable replacement in the administrator trust domain.
fn trusted_path(path: &Path) -> anyhow::Result<()> {
    use std::os::unix::fs::MetadataExt;
    anyhow::ensure!(
        path.is_absolute()
            && !path
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir)),
        "trusted paths must be absolute without parent traversal"
    );
    for part in path.ancestors() {
        let m = std::fs::symlink_metadata(part)?;
        anyhow::ensure!(
            !m.file_type().is_symlink() && m.uid() == 0 && m.permissions().mode() & 0o022 == 0,
            "tunnel path must be root-owned, non-writable and not a symlink"
        );
    }
    Ok(())
}

// Intentionally no Debug or Serialize: parse failures never echo a payload.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Notification {
    client_access_token: String,
    client_mode: String,
    region: String,
    services: Vec<String>,
}
impl Drop for Notification {
    fn drop(&mut self) {
        zeroize::Zeroize::zeroize(&mut self.client_access_token);
    }
}

struct Running {
    child: Child,
    deadline: Instant,
}
impl Drop for Running {
    fn drop(&mut self) {
        // The proxy has a new session/process group. Always kill descendants,
        // even when the direct child already exited. systemd seals the cgroup.
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

struct Manager {
    settings: Settings,
    runs: Vec<Running>,
    // Only token digests; never tokens. Bounded, fail closed when exhausted.
    seen: BTreeSet<[u8; 32]>,
}
impl Manager {
    fn new(settings: Settings) -> Self {
        Self {
            settings,
            runs: vec![],
            seen: BTreeSet::new(),
        }
    }
    fn reap(&mut self) {
        self.runs.retain_mut(|run| {
            run.deadline > Instant::now() && matches!(run.child.try_wait(), Ok(None))
        });
    }
    fn revoke(&mut self) {
        self.runs.clear();
    }
    fn notify(&mut self, topic: &str, retain: bool, body: &[u8]) -> anyhow::Result<()> {
        self.reap();
        anyhow::ensure!(
            topic == self.settings.topic() && !retain,
            "refused notification topic/retention"
        );
        anyhow::ensure!(body.len() <= MAX_NOTIFICATION, "notification too large");
        let n: Notification = serde_json::from_slice(body)
            .map_err(|_| anyhow::anyhow!("invalid tunnel notification"))?;
        anyhow::ensure!(
            n.client_mode == "destination" && n.region == self.settings.region,
            "refused tunnel mode/region"
        );
        anyhow::ensure!(
            !n.services.is_empty()
                && n.services.len() <= 16
                && n.services.iter().collect::<BTreeSet<_>>().len() == n.services.len()
                && n.services
                    .iter()
                    .all(|s| self.settings.destinations.contains_key(s)),
            "refused tunnel service"
        );
        anyhow::ensure!(
            !n.client_access_token.is_empty()
                && n.client_access_token.len() <= 4096
                && n.client_access_token.bytes().all(|b| b.is_ascii_graphic()),
            "invalid tunnel token"
        );
        let digest: [u8; 32] = Sha256::digest(n.client_access_token.as_bytes()).into();
        anyhow::ensure!(
            !self.seen.contains(&digest),
            "duplicate tunnel notification"
        );
        anyhow::ensure!(
            self.seen.len() < MAX_SEEN,
            "notification budget exhausted; administrator restart required"
        );
        // Consume even refused-at-capacity tokens: never queue or retry them.
        self.seen.insert(digest);
        anyhow::ensure!(
            self.runs.len() < self.settings.max_concurrent_tunnels,
            "tunnel concurrency limit reached"
        );
        let mapping = n
            .services
            .iter()
            .map(|id| format!("{id}={}", self.settings.destinations[id]))
            .collect::<Vec<_>>()
            .join(",");
        let mut command = Command::new(&self.settings.proxy);
        command
            .args([
                "-r",
                &self.settings.region,
                "-d",
                &mapping,
                "-v",
                "0",
                "--settings-json",
            ])
            .arg(&self.settings.proxy_settings)
            .current_dir(&self.settings.working_directory)
            .env_clear()
            .env("AWSIOT_TUNNEL_ACCESS_TOKEN", &n.client_access_token)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let parent = std::process::id();
        unsafe {
            command.pre_exec(move || {
                if libc::setsid() < 0 || libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::getppid() as u32 != parent {
                    return Err(std::io::Error::other("adapter exited"));
                }
                let zero = libc::rlimit {
                    rlim_cur: 0,
                    rlim_max: 0,
                };
                if libc::setrlimit(libc::RLIMIT_CORE, &zero) < 0
                    || libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0
                {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let spawned = command.spawn();
        drop(command); // Drop the parent's copy of the token environment promptly.
        let child = spawned.map_err(|_| anyhow::anyhow!("could not start localproxy"))?;
        self.runs.push(Running {
            child,
            deadline: Instant::now() + Duration::from_secs(self.settings.tunnel_timeout_seconds),
        });
        tracing::info!(active = self.runs.len(), "Tunnel destination proxy started");
        Ok(())
    }
}

// A blocked MQTT writer must not suspend timeout/revocation indefinitely.
async fn send(session: &mut mqtt::Session, bytes: &[u8]) -> anyhow::Result<()> {
    tokio::time::timeout(Duration::from_secs(1), session.send(bytes))
        .await
        .map_err(|_| anyhow::anyhow!("tunnel MQTT write timed out"))?
}

/// One authenticated IoT connection. Connection loss revokes all local proxies;
/// there is no persisted notification/token queue and no proxy restart with a CAT.
async fn connection(
    manager: &mut Manager,
    credentials: &iotcore::Credentials,
) -> anyhow::Result<()> {
    let iot = manager.settings.aws_iot.clone();
    let (stream, _) = iotcore::connect(&iot, credentials).await?;
    let (mut session, _) =
        mqtt::Session::start_ephemeral(stream, iot.client_id(), iot.keep_alive_seconds).await?;
    send(
        &mut session,
        &mqtt::subscribe(1, &[(manager.settings.topic(), 1)]),
    )
    .await?;
    let mut subscribed = false;
    let subscribed_at = Instant::now();
    let mut tick = tokio::time::interval(Duration::from_millis(100));
    let mut sent = Instant::now();
    let mut heard = Instant::now();
    let mut checked = Instant::now();
    loop {
        tokio::select! {
            packet = session.next() => {
                heard = Instant::now();
                match packet? {
                    mqtt::Packet::SubAck { packet_id: 1, codes } => {
                        anyhow::ensure!(codes == [1] || codes == [0], "tunnel notification subscription refused");
                        subscribed = true;
                    }
                    mqtt::Packet::Publish { topic, mut payload, retain, qos, packet_id, .. } => {
                        // Never emit errors from payload parsing or the MQTT peer.
                        let result = if subscribed { manager.notify(&topic, retain, &payload) }
                            else { Err(anyhow::anyhow!("notification before subscription approval")) };
                        zeroize::Zeroize::zeroize(&mut payload);
                        if result.is_err() { tracing::warn!("Tunnel notification refused"); }
                        if qos == 1 { send(&mut session, &mqtt::puback(packet_id.unwrap_or(0))).await?; sent = Instant::now(); }
                    }
                    mqtt::Packet::PingResp => {},
                    _ => anyhow::bail!("unexpected tunnel MQTT packet"),
                }
            }
            _ = tick.tick() => {
                manager.reap();
                anyhow::ensure!(heard.elapsed() < Duration::from_secs(iot.keep_alive_seconds as u64 * 3 / 2), "tunnel MQTT keepalive expired");
                anyhow::ensure!(subscribed || subscribed_at.elapsed() < Duration::from_secs(10), "tunnel subscription timed out");
                if sent.elapsed() >= Duration::from_secs(iot.keep_alive_seconds as u64 / 2) {
                    send(&mut session, &mqtt::pingreq()).await?;
                    sent = Instant::now();
                }
                if checked.elapsed() >= Duration::from_secs(iot.credential_check_seconds) {
                    let _ = iotcore::Credentials::load(&iot)?;
                    anyhow::ensure!(!credentials.rotated(&iot), "tunnel IoT credentials changed");
                    checked = Instant::now();
                }
            }
        }
    }
}

pub async fn run(path: &Path) -> anyhow::Result<()> {
    let settings: Settings = serde_json::from_slice(&std::fs::read(path)?)?;
    settings.preflight(path)?;
    // Adapter never receives a device key; its AWS key is independently provisioned.
    let mut manager = Manager::new(settings);
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut backoff = 1;
    loop {
        let started = Instant::now();
        let attempt = async {
            let credentials = iotcore::Credentials::load(&manager.settings.aws_iot)?;
            connection(&mut manager, &credentials).await
        };
        tokio::select! {
            _ = term.recv() => break,
            _ = tokio::signal::ctrl_c() => break,
            _ = attempt => {},
        }
        manager.revoke();
        tracing::warn!("Tunnel IoT connection ended; local sessions revoked");
        if started.elapsed() >= Duration::from_secs(60) {
            backoff = 1;
        }
        tokio::select! {
            _ = term.recv() => break,
            _ = tokio::signal::ctrl_c() => break,
            _ = tokio::time::sleep(Duration::from_secs(backoff)) => {},
        }
        backoff = (backoff * 2).min(60);
    }
    manager.revoke();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    fn settings() -> Settings {
        serde_json::from_value(json!({
            "enabled": true, "region": "us-east-1", "proxy": "/bin/true", "proxy_sha256": "00".repeat(32), "working_directory": "/", "proxy_settings": "/settings.json",
            "destinations": {"SSH": "127.0.0.1:22", "HTTP": "127.0.0.1:8080"}, "max_concurrent_tunnels": 1, "tunnel_timeout_seconds": 1,
            "aws_iot": {"endpoint": "localhost", "thing_name": "thing1", "ca_file": "/ca", "certificate_file": "/cert", "private_key_file": "/key"}
        })).unwrap()
    }
    #[test]
    fn reference_notifications_and_configuration() {
        let cases: Value =
            serde_json::from_str(include_str!("../../interfaces/compat/tunnel-cases.json"))
                .unwrap();
        for case in cases["notifications"].as_array().unwrap() {
            let mut m = Manager::new(settings());
            let body = match case.get("raw") {
                Some(v) => v.as_str().unwrap().as_bytes().to_vec(),
                None => serde_json::to_vec(&case["payload"]).unwrap(),
            };
            let result = m.notify(
                case["topic"].as_str().unwrap(),
                case["retain"].as_bool().unwrap(),
                &body,
            );
            assert_eq!(
                result.is_ok(),
                case["accepted"].as_bool().unwrap(),
                "{}",
                case["id"]
            );
        }
        for case in cases["configuration"].as_array().unwrap() {
            let mut value = serde_json::to_value(json!({})).unwrap();
            // Settings intentionally has no Serialize; use the fixture baseline.
            value
                .as_object_mut()
                .unwrap()
                .extend(cases["settings"].as_object().unwrap().clone());
            value
                .as_object_mut()
                .unwrap()
                .extend(case["patch"].as_object().unwrap().clone());
            let result = serde_json::from_value::<Settings>(value)
                .and_then(|s| s.validate().map_err(serde::de::Error::custom));
            assert_eq!(
                result.is_ok(),
                case["accepted"].as_bool().unwrap(),
                "{}",
                case["id"]
            );
        }
    }
    #[tokio::test]
    async fn process_timeout_failure_replay_and_revocation() {
        let dir = std::env::temp_dir().join(format!("sf063-unit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let script = dir.join("proxy");
        std::fs::write(&script, "#!/bin/sh\nexec /bin/sleep 30\n").unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut cfg = settings();
        cfg.proxy = script;
        let mut m = Manager::new(cfg);
        let body = |token| {
            serde_json::to_vec(&json!({"clientAccessToken":token,"clientMode":"destination","region":"us-east-1","services":["SSH"]})).unwrap()
        };
        let topic = m.settings.topic();
        m.notify(&topic, false, &body("one")).unwrap();
        let pid = m.runs[0].child.id();
        assert!(m.notify(&topic, false, &body("one")).is_err());
        assert!(m.notify(&topic, false, &body("two")).is_err());
        tokio::time::sleep(Duration::from_millis(1100)).await;
        m.reap();
        assert!(m.runs.is_empty());
        assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
        assert!(m.notify(&topic, false, &body("two")).is_err()); // capacity refusals consumed
        m.notify(&topic, false, &body("three")).unwrap();
        let pid = m.runs[0].child.id();
        m.revoke();
        assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
        m.settings.proxy = "/bin/false".into();
        m.notify(&topic, false, &body("four")).unwrap();
        tokio::time::sleep(Duration::from_millis(30)).await;
        m.reap();
        assert!(m.runs.is_empty());
        assert!(m.notify(&topic, false, &body("four")).is_err());
        m.settings.proxy = "/does-not-exist".into();
        assert!(m.notify(&topic, false, &body("five")).is_err());
        assert!(m.notify(&topic, false, &body("five")).is_err());
        m.seen = (0..MAX_SEEN as u32)
            .map(|n| Sha256::digest(n.to_be_bytes()).into())
            .collect();
        assert!(m.notify(&topic, false, &body("six")).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn tunnel_topics_never_enter_bridge_spools() {
        for (topic, prefix) in [
            ("$aws/things/thing1/tunnels/notify", ""),
            ("$aws/#", ""),
            ("notify", "$aws/things/thing1/tunnels/"),
        ] {
            for (source, target) in [
                (
                    crate::bridge::Endpoint::IotCore,
                    crate::bridge::Endpoint::Pubsub,
                ),
                (
                    crate::bridge::Endpoint::Pubsub,
                    crate::bridge::Endpoint::IotCore,
                ),
            ] {
                let config: crate::bridge::BridgeConfig = serde_json::from_value(json!({"mappings":[{"topic":topic, "source":source, "target":target, "target_topic_prefix":prefix}]})).unwrap();
                assert!(config.validate(true).is_err());
            }
        }
    }
}
