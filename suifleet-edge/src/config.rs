use serde::Deserialize;
use std::path::{Path, PathBuf};

/// `[pubsub]` (docs/MESSAGING.md).
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct PubsubConfig {
    #[serde(default = "default_pubsub_messages")]
    pub max_messages_per_subscription: usize,
    #[serde(default = "default_pubsub_bytes")]
    pub max_bytes_per_subscription: u64,
    #[serde(default = "default_pubsub_qos0")]
    pub qos0_messages_per_subscription: usize,
    #[serde(default)]
    pub bridge: Option<crate::bridge::BridgeConfig>,
}
fn default_pubsub_messages() -> usize {
    1000
}
fn default_pubsub_bytes() -> u64 {
    4 * 1024 * 1024
}
fn default_pubsub_qos0() -> usize {
    100
}
impl PubsubConfig {
    pub fn limits(&self) -> crate::pubsub::Limits {
        crate::pubsub::Limits {
            max_messages: self.max_messages_per_subscription,
            max_bytes: self.max_bytes_per_subscription,
            qos0_messages: self.qos0_messages_per_subscription,
        }
    }
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!((1..=100_000).contains(&self.max_messages_per_subscription), "pubsub.max_messages_per_subscription must be 1..=100000");
        anyhow::ensure!((4096..=1 << 30).contains(&self.max_bytes_per_subscription), "pubsub.max_bytes_per_subscription must be 4 KiB..=1 GiB");
        anyhow::ensure!((1..=10_000).contains(&self.qos0_messages_per_subscription), "pubsub.qos0_messages_per_subscription must be 1..=10000");
        if let Some(bridge) = &self.bridge {
            bridge.validate()?;
            // Inbound copies of one MQTT message are stored together, so a
            // queue must hold them all or the message could never be accepted.
            let copies = bridge.max_inbound_copies();
            anyhow::ensure!(copies <= self.max_messages_per_subscription,
                "pubsub.max_messages_per_subscription must be at least {copies}, the most overlapping LocalMqtt→Pubsub mappings");
        }
        Ok(())
    }
}

#[derive(Deserialize, Debug, Clone)]
pub struct Config {
    pub sui: SuiConfig,
    #[serde(default)]
    pub ipc: Option<crate::ipc::Settings>,
    /// Durable pub/sub and the local MQTT bridge (SF-032); none when omitted.
    #[serde(default)]
    pub pubsub: Option<PubsubConfig>,
    pub device: DeviceConfig,
    #[serde(default)]
    pub walrus: WalrusConfig,
    pub apply: ApplyConfig,
    #[serde(default)]
    pub runtime: RuntimeConfig,
    #[serde(default)]
    pub components: ComponentsConfig,
    /// Heartbeat and health telemetry (SF-019); none when omitted.
    #[serde(default)]
    pub telemetry: Option<TelemetryConfig>,
    /// Redacted remote logs (SF-020); needs `[telemetry]` for the indexer URL.
    #[serde(default)]
    pub logs: Option<LogsConfig>,
    #[serde(default)]
    pub daemon_update: DaemonUpdateConfig,
    /// The file this configuration was loaded from (for the update preflight).
    #[serde(skip)]
    pub path: PathBuf,
}

/// `[telemetry]`: signed snapshots to the fleet indexer (docs/TELEMETRY.md).
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct TelemetryConfig {
    /// Fleet indexer base URL; snapshots go to `{url}/v1/telemetry`.
    pub url: String,
    #[serde(default = "default_telemetry_interval")]
    pub interval_seconds: u64,
    /// Snapshots kept while the indexer is unreachable; the oldest beyond
    /// this are dropped (and counted in later snapshots).
    #[serde(default = "default_telemetry_buffer")]
    pub buffer_max: usize,
}

/// `[logs]`: redacted, signed log batches to the fleet indexer (docs/LOGS.md).
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct LogsConfig {
    #[serde(default = "default_telemetry_interval")]
    pub interval_seconds: u64,
    /// Lines read per interval; the rest waits for later intervals.
    #[serde(default = "default_logs_lines")]
    pub max_lines_per_interval: usize,
    /// Batches kept while the indexer is unreachable.
    #[serde(default = "default_logs_buffer")]
    pub buffer_max: usize,
}
fn default_logs_lines() -> usize {
    512
}
fn default_logs_buffer() -> usize {
    256
}

impl LogsConfig {
    pub fn validate(&self) -> anyhow::Result<()> {
        use crate::telemetry::{MAX_BUFFER, MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS};
        anyhow::ensure!(
            (MIN_INTERVAL_SECONDS..=MAX_INTERVAL_SECONDS).contains(&self.interval_seconds),
            "logs.interval_seconds must be {MIN_INTERVAL_SECONDS}..={MAX_INTERVAL_SECONDS}"
        );
        anyhow::ensure!(
            (1..=crate::logs::MAX_LINES_PER_INTERVAL).contains(&self.max_lines_per_interval),
            "logs.max_lines_per_interval must be 1..={}", crate::logs::MAX_LINES_PER_INTERVAL
        );
        anyhow::ensure!((1..=MAX_BUFFER).contains(&self.buffer_max), "logs.buffer_max must be 1..={MAX_BUFFER}");
        Ok(())
    }
}

fn default_telemetry_interval() -> u64 {
    60
}
fn default_telemetry_buffer() -> usize {
    1024
}

/// Daemon self-update (SF-026; docs/DAEMON_UPDATE.md). Off unless enabled; the
/// service must then start `suifleet --launch` so a failed new daemon falls
/// back without its own help.
#[derive(Deserialize, Debug, Clone)]
#[serde(default, deny_unknown_fields)]
pub struct DaemonUpdateConfig {
    pub enabled: bool,
    /// Deployments of this component name carry a new daemon binary.
    pub component: String,
    /// Default: `<state_dir>/daemon`.
    pub install_dir: Option<PathBuf>,
    /// Starts a new daemon gets to complete startup before rollback.
    pub max_start_attempts: u32,
    pub preflight_timeout_seconds: u64,
}
impl Default for DaemonUpdateConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            component: "suifleet-daemon".into(),
            install_dir: None,
            max_start_attempts: 3,
            preflight_timeout_seconds: 60,
        }
    }
}

/// Where component dependency candidates may come from.
#[derive(Deserialize, Debug, Clone, Copy, Default, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum OfflinePolicy {
    /// Only the live catalog and Walrus; nothing is cached.
    #[default]
    Never,
    /// Keep authenticated versions, recipes and artifacts under
    /// `state_dir/components`, reuse them by digest, and resolve from them
    /// when the catalog or Walrus is unreachable.
    Cached,
}

#[derive(Deserialize, Debug, Clone)]
#[serde(default, deny_unknown_fields)]
pub struct ComponentsConfig {
    pub offline_candidates: OfflinePolicy,
    /// Cached artifacts beyond this are evicted oldest first, except those
    /// of installed versions.
    pub cache_max_bytes: u64,
    /// Device-local configuration values (`<name>.json`, JSON pointer →
    /// value), the only source of confidential ones. Not readable by others.
    pub configuration_overlay_dir: PathBuf,
    /// How long a component's validate command may keep deferring one
    /// deployment's configuration update before the deployment fails.
    pub configuration_defer_max_seconds: u64,
    /// Operator-owned application recovery scripts, keyed by component name.
    /// Run in the previous committed version workspace, never for platform installers.
    #[serde(default)]
    pub recovery_commands: std::collections::BTreeMap<String, String>,
}
impl Default for ComponentsConfig {
    fn default() -> Self {
        Self {
            offline_candidates: OfflinePolicy::Never,
            cache_max_bytes: 512 * 1024 * 1024,
            configuration_overlay_dir: "/etc/suifleet/component-config".into(),
            configuration_defer_max_seconds: 86_400,
            recovery_commands: Default::default(),
        }
    }
}

#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct SuiConfig {
    pub manifest_path: PathBuf,
    #[serde(skip)]
    pub rpc_url: String,
    #[serde(skip)]
    pub package_id: String,
    #[serde(skip)]
    pub manifest: Option<DeploymentManifest>,
}

pub use crate::protocol::REQUIRED_TYPES;

#[derive(Deserialize, Debug, Clone)]
pub struct DeploymentManifest {
    pub schema_version: u64,
    pub protocol_version: u64,
    pub network: String,
    pub chain_identifier: String,
    pub rpc_url: String,
    pub call_package_id: String,
    pub upgrade_cap_id: String,
    pub package_version: String,
    pub type_origins: std::collections::BTreeMap<String, String>,
    pub shared_objects: SharedObjects,
    pub supported_clients: std::collections::BTreeMap<String, ClientRange>,
    pub walrus: ManifestWalrus,
}
#[derive(Deserialize, Debug, Clone)]
pub struct SharedObjects {
    pub registry: SharedObject,
}
#[derive(Deserialize, Debug, Clone)]
pub struct SharedObject {
    pub id: String,
    pub initial_shared_version: String,
}
#[derive(Deserialize, Debug, Clone)]
pub struct ClientRange {
    pub min: String,
    pub max_exclusive: String,
}
#[derive(Deserialize, Debug, Clone)]
pub struct ManifestWalrus {
    pub publisher_url: String,
    pub aggregator_url: String,
}

fn version(value: &str) -> anyhow::Result<(u64, u64, u64)> {
    let parts: Vec<_> = value.split('.').collect();
    anyhow::ensure!(
        parts.len() == 3
            && parts
                .iter()
                .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit())),
        "invalid client version"
    );
    Ok((parts[0].parse()?, parts[1].parse()?, parts[2].parse()?))
}
fn endpoint(value: &str) -> anyhow::Result<()> {
    let url = reqwest::Url::parse(value)?;
    anyhow::ensure!(
        matches!(url.scheme(), "http" | "https")
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.fragment().is_none(),
        "invalid public endpoint"
    );
    Ok(())
}
fn positive(value: &str) -> bool {
    !value.starts_with('0') && !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit())
}
impl DeploymentManifest {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(
            self.schema_version == crate::protocol::MANIFEST_SCHEMA_VERSION && self.protocol_version == crate::protocol::PROTOCOL_VERSION,
            "unsupported manifest schema/protocol"
        );
        anyhow::ensure!(
            matches!(
                self.network.as_str(),
                "localnet" | "devnet" | "testnet" | "mainnet"
            ),
            "unsupported network"
        );
        anyhow::ensure!(
            self.chain_identifier.len() == 8
                && self
                    .chain_identifier
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
            "invalid chain identifier"
        );
        anyhow::ensure!(
            (self.network != "mainnet" || self.chain_identifier == "35834a8a")
                && (self.network != "testnet" || self.chain_identifier == "4c78adac"),
            "network label/chain mismatch"
        );
        anyhow::ensure!(positive(&self.package_version), "invalid package version");
        endpoint(&self.rpc_url)?;
        endpoint(&self.walrus.publisher_url)?;
        endpoint(&self.walrus.aggregator_url)?;
        anyhow::ensure!(
            crate::sui_client::same_id(&self.call_package_id, &self.call_package_id),
            "invalid call package ID"
        );
        let catalog_count = crate::catalog::TYPES.iter().filter(|name| self.type_origins.contains_key(**name)).count();
        anyhow::ensure!(catalog_count == 0 || catalog_count == crate::catalog::TYPES.len(), "partial catalog type origins");
        for name in REQUIRED_TYPES.iter().chain(crate::catalog::TYPES.iter().filter(|name| self.type_origins.contains_key(**name))) {
            let origin = self
                .type_origins
                .get(*name)
                .ok_or_else(|| anyhow::anyhow!("missing type origin: {name}"))?;
            anyhow::ensure!(
                crate::sui_client::same_id(origin, origin),
                "invalid type origin: {name}"
            );
        }
        anyhow::ensure!(
            crate::sui_client::same_id(&self.upgrade_cap_id, &self.upgrade_cap_id),
            "invalid UpgradeCap ID"
        );
        let registry = &self.shared_objects.registry;
        anyhow::ensure!(
            crate::sui_client::same_id(&registry.id, &registry.id)
                && positive(&registry.initial_shared_version),
            "invalid shared registry"
        );
        let range = self
            .supported_clients
            .get("edge")
            .ok_or_else(|| anyhow::anyhow!("missing edge compatibility range"))?;
        let current = version(env!("CARGO_PKG_VERSION"))?;
        anyhow::ensure!(
            version(&range.min)? <= current && current < version(&range.max_exclusive)?,
            "unsupported edge client version"
        );
        Ok(())
    }
    pub fn move_type(&self, name: &str) -> String {
        // Called only after validating every required type; no first-package fallback.
        format!("{}::{name}", self.type_origins[name])
    }
}

#[derive(Deserialize, Debug, Clone)]
pub struct DeviceConfig {
    /// ed25519 key file; unused when `signer` is set.
    #[serde(default)]
    pub keypair_path: PathBuf,
    pub device_object_id: String,
    pub device_cap_id: String,
    /// A key held in a PKCS#11 token instead of `keypair_path` (SF-021).
    #[serde(default)]
    pub signer: Option<SignerConfig>,
}

/// `suifleet --enroll FILE` (SF-021): enroll this device's key under an
/// operator's enrollment policy, sponsored by a claim key.
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct EnrollConfig {
    /// The network deployment manifest (relative paths: to this file).
    pub manifest_path: PathBuf,
    pub policy_id: String,
    /// The claim key (32-byte hex ed25519 seed) from provisioning media.
    pub claim_key_path: PathBuf,
    pub name: String,
    /// Defaults to this machine's architecture.
    #[serde(default)]
    pub arch: Option<String>,
    #[serde(default)]
    pub keypair_path: PathBuf,
    #[serde(default)]
    pub signer: Option<SignerConfig>,
}

impl EnrollConfig {
    pub fn load(path: &Path) -> anyhow::Result<(Self, SuiConfig, DeviceConfig)> {
        let cfg: Self = toml::from_str(&std::fs::read_to_string(path)?)?;
        let base = path.parent().unwrap_or(Path::new("."));
        let resolve = |p: &Path| if p.is_absolute() { p.to_owned() } else { base.join(p) };
        let manifest: DeploymentManifest = serde_json::from_str(&std::fs::read_to_string(resolve(&cfg.manifest_path))?)?;
        manifest.validate()?;
        let sui = SuiConfig {
            manifest_path: cfg.manifest_path.clone(),
            rpc_url: manifest.rpc_url.clone(),
            package_id: manifest.call_package_id.clone(),
            manifest: Some(manifest),
        };
        anyhow::ensure!(cfg.signer.is_some() || !cfg.keypair_path.as_os_str().is_empty(), "enrollment needs keypair_path or a [signer]");
        let device = DeviceConfig {
            keypair_path: if cfg.keypair_path.as_os_str().is_empty() { PathBuf::new() } else { resolve(&cfg.keypair_path) },
            device_object_id: String::new(),
            device_cap_id: String::new(),
            signer: cfg.signer.clone(),
        };
        let claim = resolve(&cfg.claim_key_path);
        Ok((Self { claim_key_path: claim, ..cfg }, sui, device))
    }
}

/// `[device.signer]`. `kind = "service"`: the device signing service
/// (SF-053; the daemon user cannot read the key). `kind = "pkcs11"`: a P-256
/// key in a PKCS#11 token (HSM, SoftHSM2, or a TPM's PKCS#11 provider), which
/// requires a build with `--features pkcs11`.
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
#[cfg_attr(not(feature = "pkcs11"), allow(dead_code))]
pub struct SignerConfig {
    /// "service" or "pkcs11".
    pub kind: String,
    /// service: the signing service's socket.
    #[serde(default)]
    pub socket: PathBuf,
    /// pkcs11: the token's module, e.g. /usr/lib/softhsm/libsofthsm2.so.
    #[serde(default)]
    pub module: PathBuf,
    #[serde(default)]
    pub token_label: String,
    /// pkcs11: CKA_LABEL of both the private and the public key object.
    #[serde(default)]
    pub key_label: String,
    /// pkcs11: file holding the user PIN (readable only by the key's user).
    #[serde(default)]
    pub pin_file: PathBuf,
}

#[derive(Deserialize, Debug, Clone)]
pub struct WalrusConfig {
    /// Taken from the deployment manifest; a configured value must match it.
    #[serde(default)]
    pub aggregator_url: String,
    /// Largest artifact the device will download.
    #[serde(default = "default_max_artifact_bytes")]
    pub max_artifact_bytes: u64,
    /// Free space that must remain on the state filesystem after a download.
    #[serde(default = "default_min_free_bytes")]
    pub min_free_bytes: u64,
    /// Attempts per blob for interrupted or transiently failing transfers;
    /// partial downloads resume with HTTP Range when the aggregator supports it.
    #[serde(default = "default_download_attempts")]
    pub download_attempts: u32,
    /// Deadline for fetching one blob, across all attempts. Bounds how long a
    /// slow or stalled aggregator can hold the serial deployment loop.
    #[serde(default = "default_download_timeout_seconds")]
    pub download_timeout_seconds: u64,
}

// Applies when [walrus] is omitted, as in the manifest-based configuration.
impl Default for WalrusConfig {
    fn default() -> Self {
        Self {
            aggregator_url: String::new(),
            max_artifact_bytes: default_max_artifact_bytes(),
            min_free_bytes: default_min_free_bytes(),
            download_attempts: default_download_attempts(),
            download_timeout_seconds: default_download_timeout_seconds(),
        }
    }
}

fn default_max_artifact_bytes() -> u64 {
    1024 * 1024 * 1024
}
fn default_min_free_bytes() -> u64 {
    64 * 1024 * 1024
}
fn default_download_attempts() -> u32 {
    4
}
fn default_download_timeout_seconds() -> u64 {
    3600
}

#[derive(Deserialize, Debug, Clone)]
pub struct ApplyConfig {
    /// Command to run with the artifact path appended as the final argument.
    pub command: String,
    #[serde(default = "default_timeout")]
    pub timeout_seconds: u64,
    #[serde(default)]
    pub allow_recipe_commands: bool,
    /// `direct` runs the updater as the daemon user. `privileged-helper` hands
    /// verified firmware to the root-owned helper (suifleet-firmware.service);
    /// recipe install scripts always stay unprivileged.
    #[serde(default)]
    pub mode: ApplyMode,
    /// Spool shared with the firmware helper (daemon-writable).
    #[serde(default = "default_firmware_spool")]
    pub firmware_spool: PathBuf,
    /// Optional address-space limit (RLIMIT_AS) for updater/recipe children.
    #[serde(default)]
    pub max_memory_bytes: Option<u64>,
}

#[derive(Deserialize, Debug, Clone, Copy, Default, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum ApplyMode {
    #[default]
    Direct,
    PrivilegedHelper,
}

fn default_firmware_spool() -> PathBuf {
    "/var/lib/suifleet/firmware".into()
}

/// Root-owned configuration for `suifleet --firmware-helper`. It is separate
/// from the daemon config so a compromised daemon cannot choose the command.
#[derive(Deserialize, Debug, Clone)]
pub struct FirmwareHelperConfig {
    /// Firmware updater; the verified artifact path is appended.
    pub command: String,
    #[serde(default = "default_timeout")]
    pub timeout_seconds: u64,
    #[serde(default = "default_firmware_spool")]
    pub spool_dir: PathBuf,
    /// Root-only directory holding the helper's private artifact copy.
    #[serde(default = "default_helper_work_dir")]
    pub work_dir: PathBuf,
    #[serde(default)]
    pub max_memory_bytes: Option<u64>,
}

fn default_helper_work_dir() -> PathBuf {
    "/var/lib/suifleet-firmware".into()
}

impl FirmwareHelperConfig {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let cfg: Self = toml::from_str(&std::fs::read_to_string(path)?)?;
        anyhow::ensure!(cfg.timeout_seconds > 0, "timeout_seconds must be positive");
        Ok(cfg)
    }
}

impl Config {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let raw = std::fs::read_to_string(path)?;
        let mut cfg: Self = toml::from_str(&raw)?;
        if let Some(ipc) = &cfg.ipc { ipc.validate()?; }
        if let Some(pubsub) = &cfg.pubsub { pubsub.validate()?; }
        cfg.path = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
        anyhow::ensure!(
            !cfg.daemon_update.enabled
                || (crate::deps::valid_name(&cfg.daemon_update.component) && cfg.daemon_update.max_start_attempts > 0
                    && cfg.daemon_update.preflight_timeout_seconds > 0
                    && cfg.daemon_update.install_dir.as_ref().is_none_or(|d| d.is_absolute())),
            "daemon_update needs a valid component name, positive attempts/timeout and an absolute install_dir"
        );
        // The launcher execs whatever its install directory names, and that
        // directory is writable by the daemon user and so by every component:
        // a component could make itself the unit's main process, which the
        // signing service trusts for status reports (SF-053, KEY_ISOLATION.md).
        anyhow::ensure!(
            !(cfg.daemon_update.enabled && cfg.device.signer.as_ref().is_some_and(|signer| signer.kind == "service")),
            "daemon_update cannot be enabled with the service signer ([device.signer] kind = \"service\"): \
             components could replace the daemon the signing service trusts"
        );
        anyhow::ensure!(
            cfg.components.recovery_commands.iter().all(|(name, command)| crate::deps::valid_name(name) && !command.trim().is_empty()),
            "application recovery needs valid component names and nonempty commands"
        );
        anyhow::ensure!(cfg.components.recovery_commands.is_empty() || cfg.apply.allow_recipe_commands,
            "application recovery commands require apply.allow_recipe_commands");
        let manifest_path = if cfg.sui.manifest_path.is_absolute() {
            cfg.sui.manifest_path.clone()
        } else {
            path.parent()
                .unwrap_or(Path::new("."))
                .join(&cfg.sui.manifest_path)
        };
        let manifest: DeploymentManifest =
            serde_json::from_str(&std::fs::read_to_string(manifest_path)?)?;
        manifest.validate()?;
        anyhow::ensure!(
            cfg.walrus.aggregator_url.is_empty()
                || cfg.walrus.aggregator_url == manifest.walrus.aggregator_url,
            "stale Walrus configuration"
        );
        cfg.walrus.aggregator_url = manifest.walrus.aggregator_url.clone();
        cfg.sui.rpc_url = manifest.rpc_url.clone();
        cfg.sui.package_id = manifest.call_package_id.clone();
        cfg.sui.manifest = Some(manifest);
        anyhow::ensure!(
            cfg.device.signer.is_some() || !cfg.device.keypair_path.as_os_str().is_empty(),
            "[device] needs keypair_path or a [device.signer]"
        );
        anyhow::ensure!(
            cfg.runtime.poll_seconds > 0,
            "runtime.poll_seconds must be positive"
        );
        anyhow::ensure!(
            cfg.apply.timeout_seconds > 0,
            "apply.timeout_seconds must be positive"
        );
        anyhow::ensure!(
            cfg.walrus.download_attempts > 0,
            "walrus.download_attempts must be positive"
        );
        anyhow::ensure!(
            cfg.walrus.download_timeout_seconds > 0,
            "walrus.download_timeout_seconds must be positive"
        );
        if let Some(telemetry) = &cfg.telemetry {
            telemetry.validate()?;
        }
        if let Some(logs) = &cfg.logs {
            anyhow::ensure!(cfg.telemetry.is_some(), "[logs] needs [telemetry] (the fleet indexer URL)");
            logs.validate()?;
        }
        Ok(cfg)
    }
}

fn default_timeout() -> u64 {
    600
}

impl TelemetryConfig {
    pub fn validate(&self) -> anyhow::Result<()> {
        use crate::telemetry::{MAX_BUFFER, MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS};
        endpoint(&self.url).map_err(|e| e.context("invalid telemetry.url"))?;
        anyhow::ensure!(
            (MIN_INTERVAL_SECONDS..=MAX_INTERVAL_SECONDS).contains(&self.interval_seconds),
            "telemetry.interval_seconds must be {MIN_INTERVAL_SECONDS}..={MAX_INTERVAL_SECONDS}"
        );
        anyhow::ensure!(
            (1..=MAX_BUFFER).contains(&self.buffer_max),
            "telemetry.buffer_max must be 1..={MAX_BUFFER}"
        );
        Ok(())
    }
}

#[derive(Deserialize, Debug, Clone)]
#[serde(default)]
pub struct RuntimeConfig {
    pub state_dir: PathBuf,
    pub poll_seconds: u64,
    pub replay_history: bool,
    /// Supervised components without a readiness command are ready once their
    /// run survives this long.
    pub component_startup_seconds: u64,
    /// How long a readiness command may keep failing before the start fails.
    pub component_ready_timeout_seconds: u64,
    /// Grace for the shutdown command (or SIGTERM) before SIGKILL.
    pub component_stop_timeout_seconds: u64,
    /// Restarts allowed within ten minutes before a component is Broken.
    pub component_max_restarts: u32,
}
impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            state_dir: "/var/lib/suifleet".into(),
            poll_seconds: 10,
            replay_history: false,
            component_startup_seconds: 3,
            component_ready_timeout_seconds: 60,
            component_stop_timeout_seconds: 10,
            component_max_restarts: 5,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> DeploymentManifest {
        serde_json::from_str(include_str!("../../manifests/fixture.localnet.json")).unwrap()
    }
    #[test]
    fn mixed_origins_and_compatibility() {
        let manifest = fixture();
        manifest.validate().unwrap();
        assert_ne!(
            manifest
                .move_type("deployment_manager::RecipeDigestKey")
                .split("::")
                .next(),
            manifest
                .move_type("device_cap::DeviceAddressKey")
                .split("::")
                .next()
        );
        for field in ["schema", "protocol", "chain", "origin", "client"] {
            let mut bad = fixture();
            match field {
                "schema" => bad.schema_version = 2,
                "protocol" => bad.protocol_version = 2,
                "chain" => bad.chain_identifier = "zzzzzzzz".into(),
                "origin" => {
                    bad.type_origins.remove("device_cap::DeviceAddressKey");
                }
                "client" => {
                    bad.supported_clients.get_mut("edge").unwrap().max_exclusive = "0.1.0".into()
                }
                _ => unreachable!(),
            }
            assert!(bad.validate().is_err(), "{field}");
        }
    }
    #[test]
    fn pubsub_queues_must_hold_every_inbound_copy_of_a_message() {
        let bridge = "[bridge]\nbroker='127.0.0.1:1883'\n\
            [[bridge.mappings]]\ntopic='lan/#'\nsource='LocalMqtt'\ntarget='Pubsub'\ntarget_topic_prefix='a/'\n\
            [[bridge.mappings]]\ntopic='lan/+'\nsource='LocalMqtt'\ntarget='Pubsub'\ntarget_topic_prefix='b/'\n";
        for (messages, valid) in [(1, false), (2, true)] {
            let pubsub: PubsubConfig = toml::from_str(&format!("max_messages_per_subscription={messages}\n{bridge}")).unwrap();
            assert_eq!(pubsub.validate().is_ok(), valid, "{messages}");
        }
    }
    #[test]
    fn walrus_limits_default_when_omitted_or_partial() {
        let base = "[sui]\nmanifest_path='deployment.json'\n[device]\nkeypair_path='k'\n\
            device_object_id='0x1'\ndevice_cap_id='0x2'\n[apply]\ncommand='true'\n";
        let omitted: Config = toml::from_str(base).unwrap();
        let partial: Config = toml::from_str(&format!("{base}[walrus]\ndownload_attempts=2\n")).unwrap();
        for walrus in [&omitted.walrus, &partial.walrus] {
            assert_eq!(walrus.aggregator_url, "");
            assert_eq!(walrus.max_artifact_bytes, 1024 * 1024 * 1024);
            assert_eq!(walrus.min_free_bytes, 64 * 1024 * 1024);
            assert_eq!(walrus.download_timeout_seconds, 3600);
        }
        assert_eq!(omitted.walrus.download_attempts, 4);
        assert_eq!(omitted.components.offline_candidates, OfflinePolicy::Never);
        let cached: Config = toml::from_str(&format!("{base}[components]\noffline_candidates='cached'\ncache_max_bytes=10\n")).unwrap();
        assert_eq!(cached.components.offline_candidates, OfflinePolicy::Cached);
        assert_eq!(cached.components.cache_max_bytes, 10);
        assert_eq!(omitted.components.configuration_overlay_dir, PathBuf::from("/etc/suifleet/component-config"));
        let overlay: Config = toml::from_str(&format!("{base}[components]\nconfiguration_overlay_dir='/run/x'\nconfiguration_defer_max_seconds=5\n")).unwrap();
        assert_eq!(overlay.components.configuration_overlay_dir, PathBuf::from("/run/x"));
        assert_eq!(overlay.components.configuration_defer_max_seconds, 5);
        assert!(toml::from_str::<Config>(&format!("{base}[components]\noffline_candidates='always'\n")).is_err());
        assert!(toml::from_str::<Config>(&format!("{base}[components]\noffline='cached'\n")).is_err());
        assert_eq!(partial.walrus.download_attempts, 2);
    }
    #[test]
    fn optional_catalog_origins_are_complete_and_valid() {
        let mut m = fixture();
        m.type_origins.insert(crate::catalog::TYPES[0].into(), "0x4".into());
        assert!(m.validate().is_err());
        for name in crate::catalog::TYPES { m.type_origins.insert((*name).into(), "0x4".into()); }
        m.validate().unwrap();
        m.type_origins.insert(crate::catalog::TYPES[0].into(), "bad".into());
        assert!(m.validate().is_err());
    }
    #[test]
    fn telemetry_is_optional_and_bounded() {
        let base = "[sui]\nmanifest_path='deployment.json'\n[device]\nkeypair_path='k'\n\
            device_object_id='0x1'\ndevice_cap_id='0x2'\n[apply]\ncommand='true'\n";
        assert!(toml::from_str::<Config>(base).unwrap().telemetry.is_none());
        let parsed: Config = toml::from_str(&format!("{base}[telemetry]\nurl='https://fleet.example'\n")).unwrap();
        let telemetry = parsed.telemetry.unwrap();
        assert_eq!((telemetry.interval_seconds, telemetry.buffer_max), (60, 1024));
        telemetry.validate().unwrap();
        for bad in ["url='ftp://fleet.example'", "url='https://u:p@fleet.example'",
                    "url='https://fleet.example'\ninterval_seconds=5", "url='https://fleet.example'\ninterval_seconds=3601",
                    "url='https://fleet.example'\nbuffer_max=0", "url='https://fleet.example'\nbuffer_max=10001"] {
            let parsed: Config = toml::from_str(&format!("{base}[telemetry]\n{bad}\n")).unwrap();
            assert!(parsed.telemetry.unwrap().validate().is_err(), "{bad}");
        }
        assert!(toml::from_str::<Config>(&format!("{base}[telemetry]\nurl='https://x'\ntoken='t'\n")).is_err());
    }
    #[test]
    fn logs_are_optional_bounded_and_need_telemetry() {
        let base = "[sui]\nmanifest_path='deployment.json'\n[device]\nkeypair_path='k'\n\
            device_object_id='0x1'\ndevice_cap_id='0x2'\n[apply]\ncommand='true'\n";
        assert!(toml::from_str::<Config>(base).unwrap().logs.is_none());
        let parsed: Config = toml::from_str(&format!("{base}[logs]\n")).unwrap();
        let logs = parsed.logs.unwrap();
        assert_eq!((logs.interval_seconds, logs.max_lines_per_interval, logs.buffer_max), (60, 512, 256));
        logs.validate().unwrap();
        for bad in ["interval_seconds=5", "max_lines_per_interval=0", "max_lines_per_interval=10001", "buffer_max=0"] {
            let parsed: Config = toml::from_str(&format!("{base}[logs]\n{bad}\n")).unwrap();
            assert!(parsed.logs.unwrap().validate().is_err(), "{bad}");
        }
        assert!(toml::from_str::<Config>(&format!("{base}[logs]\nurl='https://x'\n")).is_err(), "logs take their URL from [telemetry]");
        // Config::load refuses [logs] without [telemetry].
        let dir = std::env::temp_dir().join(format!("suifleet-config-logs-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("deployment.json"), include_str!("../../manifests/fixture.localnet.json")).unwrap();
        std::fs::write(dir.join("config.toml"), format!("{base}[logs]\n")).unwrap();
        let error = Config::load(&dir.join("config.toml")).unwrap_err();
        assert!(format!("{error:#}").contains("[logs] needs [telemetry]"), "{error:#}");
    }
    #[test]
    fn daemon_update_is_refused_with_the_service_signer() {
        let dir = std::env::temp_dir().join(format!("suifleet-config-update-signer-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("config.toml");
        let base = "[sui]\nmanifest_path='missing.json'\n[device]\ndevice_object_id='0x1'\ndevice_cap_id='0x2'\n\
            [device.signer]\nkind='service'\nsocket='/run/s.sock'\n[apply]\ncommand='true'\n";
        std::fs::write(&path, format!("{base}[daemon_update]\nenabled=true\ncomponent='suifleet'\n")).unwrap();
        let error = Config::load(&path).unwrap_err().to_string();
        assert!(error.contains("daemon_update cannot be enabled with the service signer"), "{error}");
        // Without daemon_update the check passes (and loading reaches the manifest).
        std::fs::write(&path, base).unwrap();
        let error = format!("{:#}", Config::load(&path).unwrap_err());
        assert!(!error.contains("daemon_update"), "{error}");
        let _ = std::fs::remove_dir_all(&dir);
    }
    #[test]
    fn legacy_network_config_rejected() {
        assert!(toml::from_str::<SuiConfig>(
            "rpc_url='http://localhost:9000'\npackage_id='0x1'\nmanifest_path='deployment.json'"
        )
        .is_err());
    }
}
