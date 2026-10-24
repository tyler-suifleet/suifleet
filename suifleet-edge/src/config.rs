use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

/// `[pubsub]` (docs/MESSAGING.md).
#[derive(Deserialize, Serialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct PubsubConfig {
    #[serde(default = "default_pubsub_messages")]
    pub max_messages_per_subscription: usize,
    #[serde(default = "default_pubsub_bytes")]
    pub max_bytes_per_subscription: u64,
    #[serde(default = "default_pubsub_qos0")]
    pub qos0_messages_per_subscription: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
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
    /// `aws_iot`: whether the `[aws_iot]` connection is configured.
    pub fn validate(&self, aws_iot: bool) -> anyhow::Result<()> {
        anyhow::ensure!((1..=100_000).contains(&self.max_messages_per_subscription), "pubsub.max_messages_per_subscription must be 1..=100000");
        anyhow::ensure!((4096..=1 << 30).contains(&self.max_bytes_per_subscription), "pubsub.max_bytes_per_subscription must be 4 KiB..=1 GiB");
        anyhow::ensure!((1..=10_000).contains(&self.qos0_messages_per_subscription), "pubsub.qos0_messages_per_subscription must be 1..=10000");
        if let Some(bridge) = &self.bridge {
            bridge.validate(aws_iot)?;
            // Inbound copies of one MQTT message are stored together, so a
            // queue must hold them all or the message could never be accepted.
            let copies = bridge.max_inbound_copies();
            anyhow::ensure!(copies <= self.max_messages_per_subscription,
                "pubsub.max_messages_per_subscription must be at least {copies}, the most overlapping inbound mappings from one broker");
        }
        Ok(())
    }
}

#[derive(Deserialize, Debug, Clone)]
pub struct Config {
    #[serde(skip)]
    pub services: Option<std::sync::Arc<std::sync::Mutex<crate::services::Runtime>>>,
    /// Operator-only authorization of fleet credential paths and destinations.
    #[serde(default)]
    pub service_credentials: Vec<crate::services::CredentialAuthorization>,
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
    /// Confidential delivery (SF-022); without it, Seal-encrypted recipes and
    /// artifacts are refused and public ones applied as before.
    #[serde(default)]
    pub seal: Option<SealConfig>,
    /// Durable local streams and exports (SF-034); none when omitted.
    #[serde(default)]
    pub streams: Option<StreamsConfig>,
    /// Local device shadows and cloud sync (SF-033); none when omitted.
    #[serde(default)]
    pub shadows: Option<ShadowsConfig>,
    /// Local support console (SF-079, docs/LOCAL_CONSOLE.md); none when omitted.
    #[serde(default)]
    pub console: Option<crate::console::Settings>,
    /// The AWS IoT Core connection (SF-061); none when omitted.
    #[serde(default)]
    pub aws_iot: Option<crate::iotcore::IotConfig>,
    /// Operator-only Device Defender device metrics (SF-062).
    #[serde(default)]
    pub device_defender: Option<crate::defender::Settings>,
    /// LAN client-device authentication and discovery (SF-035); none when omitted.
    #[serde(default)]
    pub client_devices: Option<crate::clientdevices::Settings>,
    /// Gas top-ups from the owner's prepaid fleet budget (SF-083); none when
    /// omitted, and the device pays its own gas as before.
    #[serde(default)]
    pub budget: Option<crate::funding::BudgetConfig>,
    /// Direct Sui health and prepaid encrypted archive delivery (SF-082).
    #[serde(default)]
    pub fleet_data: Option<crate::fleet_data::Config>,
    /// The file this configuration was loaded from (for the update preflight).
    #[serde(skip)]
    pub path: PathBuf,
}

/// `[streams]`: durable local streams (docs/STREAMS.md). Streams with an
/// export are declared here only: components cannot direct device-signed
/// data to a destination.
#[derive(Deserialize, Serialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct StreamsConfig {
    #[serde(default = "default_max_streams")]
    pub max_streams: usize,
    /// Sum of all streams' max_size (their disk/memory reservation).
    #[serde(default = "default_streams_bytes")]
    pub max_total_bytes: u64,
    #[serde(default)]
    pub definitions: Vec<crate::streams::Definition>,
}
fn default_max_streams() -> usize {
    32
}
fn default_streams_bytes() -> u64 {
    64 << 20
}
impl StreamsConfig {
    pub fn quota(&self) -> crate::streams::Quota {
        crate::streams::Quota { max_streams: self.max_streams, max_total_bytes: self.max_total_bytes }
    }
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!((1..=1024).contains(&self.max_streams), "streams.max_streams must be 1..=1024");
        anyhow::ensure!((1024..=64 << 30).contains(&self.max_total_bytes), "streams.max_total_bytes must be 1 KiB..=64 GiB");
        anyhow::ensure!(self.definitions.len() <= self.max_streams, "more stream definitions than streams.max_streams");
        let mut names = std::collections::BTreeSet::new();
        for def in &self.definitions {
            def.validate().map_err(|e| anyhow::anyhow!("stream {:?}: {e}", def.name))?;
            anyhow::ensure!(names.insert(&def.name), "stream {:?} is defined twice", def.name);
        }
        anyhow::ensure!(self.definitions.iter().map(|d| d.max_size).sum::<u64>() <= self.max_total_bytes,
            "stream definitions exceed streams.max_total_bytes");
        Ok(())
    }
}

/// `[shadows]`: local device shadows (docs/SHADOWS.md).
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct ShadowsConfig {
    /// The AWS-style thing name in shadow resources and notification topics.
    pub thing_name: String,
    #[serde(default = "default_max_shadows")]
    pub max_shadows: usize,
    /// The cloud sync adapter; none when omitted.
    #[serde(default)]
    pub sync: Option<crate::shadows::SyncConfig>,
    /// Synchronization with AWS IoT Device Shadow over `[aws_iot]`.
    #[serde(default)]
    pub aws_iot: Option<crate::shadows::AwsSyncConfig>,
}
fn default_max_shadows() -> usize {
    64
}
impl ShadowsConfig {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(crate::shadows::valid_thing(&self.thing_name), "shadows.thing_name must be 1-128 of [A-Za-z0-9:_-]");
        anyhow::ensure!((1..=crate::shadows::MAX_SHADOWS).contains(&self.max_shadows), "shadows.max_shadows must be 1..=256");
        if let Some(sync) = &self.sync {
            sync.validate(self.max_shadows)?;
        }
        if let Some(sync) = &self.aws_iot {
            anyhow::ensure!(self.sync.is_none(), "a shadow synchronizes with one cloud: configure [shadows.sync] or [shadows.aws_iot], not both");
            sync.validate(self.max_shadows)?;
        }
        Ok(())
    }
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
    #[serde(default)]
    pub rpc_urls: Vec<String>,
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
    #[serde(default)]
    pub rpc_urls: Vec<String>,
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
    #[serde(default)]
    pub aggregator_urls: Vec<String>,
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
        anyhow::ensure!(self.rpc_urls.len() <= 8, "at most 8 RPC endpoints");
        for url in &self.rpc_urls {
            endpoint(url)?;
        }
        endpoint(&self.walrus.publisher_url)?;
        endpoint(&self.walrus.aggregator_url)?;
        for url in &self.walrus.aggregator_urls {
            endpoint(url)?;
        }
        anyhow::ensure!(
            crate::sui_client::same_id(&self.call_package_id, &self.call_package_id),
            "invalid call package ID"
        );
        for (label, group) in [
            ("catalog", crate::catalog::TYPES),
            ("seal policy", crate::protocol::SEAL_POLICY_TYPES),
            ("fleet budget", crate::protocol::BUDGET_TYPES),
            ("fleet inbox", crate::protocol::INBOX_TYPES),
        ] {
            let count = group.iter().filter(|name| self.type_origins.contains_key(**name)).count();
            anyhow::ensure!(count == 0 || count == group.len(), "partial {label} type origins");
        }
        for name in REQUIRED_TYPES.iter().chain(self.optional_types()) {
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
    /// Optional type-origin groups present in this manifest (all or none of each).
    pub fn optional_types(&self) -> impl Iterator<Item = &&'static str> {
        crate::catalog::TYPES
            .iter()
            .chain(crate::protocol::SEAL_POLICY_TYPES)
            .chain(crate::protocol::BUDGET_TYPES)
            .chain(crate::protocol::INBOX_TYPES)
            .filter(|name| self.type_origins.contains_key(**name))
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
            rpc_urls: manifest.rpc_urls.clone(),
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

/// Whether unencrypted recipes and artifacts may still be applied.
#[derive(Deserialize, Debug, Clone, Copy, Default, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum SealMode {
    /// Decrypt confidential blobs; also apply explicitly public ones.
    #[default]
    Optional,
    /// Refuse any recipe or artifact that is not Seal-encrypted.
    Required,
}

/// `[seal]`: Seal key servers and limits for confidential delivery (SF-022,
/// docs/CONFIDENTIAL_DELIVERY.md).
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct SealConfig {
    #[serde(default)]
    pub mode: SealMode,
    /// The Seal package whose `key_server::KeyServer` objects are trusted.
    pub key_server_package: String,
    pub key_servers: Vec<KeyServerConfig>,
    /// Ciphertexts needing fewer key servers are refused.
    #[serde(default = "default_min_threshold")]
    pub min_threshold: u8,
    /// Require each key server's proof of possession (`/v1/service`).
    #[serde(default = "default_true")]
    pub verify_key_servers: bool,
    /// Session certificate lifetime (Seal allows 1 to 30 minutes).
    #[serde(default = "default_session_ttl_minutes")]
    pub session_ttl_minutes: u16,
    /// Certificates are dated this far back for key servers' clocks.
    #[serde(default = "default_clock_skew_seconds")]
    pub clock_skew_seconds: u64,
    /// Fetched decryption keys stay in memory (never on disk) this long; 0
    /// fetches for every blob.
    #[serde(default = "default_key_cache_seconds")]
    pub key_cache_seconds: u64,
    #[serde(default = "default_seal_timeout_seconds")]
    pub request_timeout_seconds: u64,
    /// Confidential blobs are decrypted in memory, so this bounds memory use.
    #[serde(default = "default_max_plaintext_bytes")]
    pub max_plaintext_bytes: u64,
}

#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct KeyServerConfig {
    pub object_id: String,
    #[serde(default = "default_weight")]
    pub weight: u8,
    /// Provider API credential header (e.g. "X-API-Key") and the file holding
    /// it. A provider credential is not device identity or decryption authority.
    #[serde(default)]
    pub api_key_name: Option<String>,
    #[serde(default)]
    pub api_key_file: Option<PathBuf>,
}

fn default_true() -> bool {
    true
}
fn default_min_threshold() -> u8 {
    1
}
fn default_weight() -> u8 {
    1
}
fn default_session_ttl_minutes() -> u16 {
    10
}
fn default_clock_skew_seconds() -> u64 {
    60
}
fn default_key_cache_seconds() -> u64 {
    120
}
fn default_seal_timeout_seconds() -> u64 {
    10
}
fn default_max_plaintext_bytes() -> u64 {
    128 * 1024 * 1024
}

impl SealConfig {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(crate::sui_client::same_id(&self.key_server_package, &self.key_server_package), "invalid key_server_package");
        anyhow::ensure!(!self.key_servers.is_empty(), "[seal] needs at least one key server");
        let mut seen = std::collections::BTreeSet::new();
        for server in &self.key_servers {
            anyhow::ensure!(crate::sui_client::same_id(&server.object_id, &server.object_id), "invalid key server object ID");
            anyhow::ensure!(seen.insert(server.object_id.to_ascii_lowercase()), "duplicate key server {}", server.object_id);
            anyhow::ensure!(server.weight > 0, "key server weight must be positive");
        }
        let total: u32 = self.key_servers.iter().map(|s| u32::from(s.weight)).sum();
        anyhow::ensure!(self.min_threshold >= 1 && u32::from(self.min_threshold) <= total, "min_threshold must be between 1 and the total key server weight");
        anyhow::ensure!((1..=crate::seal_client::MAX_SESSION_MINUTES).contains(&self.session_ttl_minutes), "session_ttl_minutes must be 1 to 30");
        anyhow::ensure!(self.clock_skew_seconds * 2 < u64::from(self.session_ttl_minutes) * 60, "clock_skew_seconds must be under half the session lifetime");
        anyhow::ensure!(self.request_timeout_seconds > 0 && self.max_plaintext_bytes > 0, "request_timeout_seconds and max_plaintext_bytes must be positive");
        Ok(())
    }
}

/// A secret from a file that other users cannot read.
pub fn read_secret(path: &Path) -> anyhow::Result<String> {
    use anyhow::Context;
    use std::os::unix::fs::PermissionsExt;
    let mode = std::fs::metadata(path).with_context(|| format!("reading {}", path.display()))?.permissions().mode();
    anyhow::ensure!(mode & 0o007 == 0, "{} must not be accessible to other users", path.display());
    let value = std::fs::read_to_string(path)?.trim().to_owned();
    anyhow::ensure!(!value.is_empty(), "{} is empty", path.display());
    Ok(value)
}

#[derive(Deserialize, Debug, Clone)]
pub struct WalrusConfig {
    /// Ordered mirrors; empty uses the legacy aggregator_url. Manifest-pinned.
    #[serde(default)]
    pub aggregator_urls: Vec<String>,
    /// Maximum bytes requested per HTTP Range (1..=10 MiB).
    #[serde(default = "default_range_chunk_bytes")]
    pub range_chunk_bytes: u64,
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
            aggregator_urls: Vec::new(),
            range_chunk_bytes: default_range_chunk_bytes(),
            max_artifact_bytes: default_max_artifact_bytes(),
            min_free_bytes: default_min_free_bytes(),
            download_attempts: default_download_attempts(),
            download_timeout_seconds: default_download_timeout_seconds(),
        }
    }
}

fn default_range_chunk_bytes() -> u64 {
    4 * 1024 * 1024
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
        if let Some(pubsub) = &cfg.pubsub { pubsub.validate(cfg.aws_iot.is_some())?; }
        if let Some(console) = &cfg.console { console.validate()?; }
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
        if let Some(reason) = unsupported_on(std::env::consts::OS, &cfg) {
            anyhow::bail!("{reason} (docs/MACOS_RUNTIME.md)");
        }
        anyhow::ensure!(
            cfg.components.recovery_commands.iter().all(|(name, command)| crate::deps::valid_name(name) && !command.trim().is_empty()),
            "application recovery needs valid component names and nonempty commands"
        );
        anyhow::ensure!(cfg.components.recovery_commands.is_empty() || cfg.apply.allow_recipe_commands,
            "application recovery commands require apply.allow_recipe_commands");
        if let Some(seal) = &cfg.seal {
            seal.validate()?;
            anyhow::ensure!(
                seal.max_plaintext_bytes <= cfg.walrus.max_artifact_bytes,
                "seal.max_plaintext_bytes cannot exceed walrus.max_artifact_bytes"
            );
        }
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
        anyhow::ensure!(
            cfg.walrus.aggregator_urls.is_empty()
                || cfg.walrus.aggregator_urls == manifest.walrus.aggregator_urls,
            "stale Walrus mirror configuration"
        );
        cfg.walrus.aggregator_urls = manifest.walrus.aggregator_urls.clone();
        anyhow::ensure!(
            (1..=10 * 1024 * 1024).contains(&cfg.walrus.range_chunk_bytes),
            "walrus.range_chunk_bytes must be between 1 and 10 MiB"
        );
        cfg.walrus.aggregator_url = manifest.walrus.aggregator_url.clone();
        anyhow::ensure!(cfg.sui.rpc_urls.len() <= 8, "at most 8 RPC endpoints");
        for url in &cfg.sui.rpc_urls {
            endpoint(url)?;
        }
        if cfg.sui.rpc_urls.is_empty() {
            cfg.sui.rpc_urls = manifest.rpc_urls.clone();
        }
        cfg.sui.rpc_url = manifest.rpc_url.clone();
        cfg.sui.package_id = manifest.call_package_id.clone();
        cfg.sui.manifest = Some(manifest);
        anyhow::ensure!(
            cfg.device.signer.is_some() || !cfg.device.keypair_path.as_os_str().is_empty(),
            "[device] needs keypair_path or a [device.signer]"
        );
        anyhow::ensure!(
            (1..=86_400).contains(&cfg.runtime.poll_seconds)
                && (1..=86_400).contains(&cfg.runtime.rollout_poll_seconds),
            "poll_seconds must be 1..=86400; rollout_poll_seconds must be 1..=86400"
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
            anyhow::ensure!(cfg.telemetry.is_some() || cfg.fleet_data.as_ref().is_some_and(|d| d.archives.is_some()), "[logs] needs [telemetry] or fleet_data.archives");
            logs.validate()?;
        }
        if let Some(streams) = &cfg.streams {
            streams.validate()?;
        }
        if let Some(shadows) = &cfg.shadows {
            shadows.validate()?;
        }
        validate_aws_iot(&cfg)?;
        if let Some(data) = &cfg.fleet_data {
            data.validate()?;
            anyhow::ensure!(data.archives.is_none() || cfg.logs.is_some(), "fleet_data.archives needs [logs]");
            anyhow::ensure!(cfg.telemetry.is_none(), "configure fleet_data or HTTP telemetry, not both");
        }
        if let Some(budget) = &cfg.budget {
            budget.validate(cfg.sui.manifest.as_ref().expect("validated manifest"))?;
        }
        if let Some(client_devices) = &mut cfg.client_devices {
            if client_devices.device_groups_file.is_relative() {
                client_devices.device_groups_file = path.parent().unwrap_or(Path::new(".")).join(&client_devices.device_groups_file);
            }
            client_devices.validate()?;
            anyhow::ensure!(cfg.ipc.as_ref().is_some_and(|ipc| !ipc.administrators.is_empty()),
                "[client_devices] needs [ipc] administrators: enrollment and revocation are operator IPC requests");
        }
        Ok(cfg)
    }
}

fn default_timeout() -> u64 {
    600
}

/// `[aws_iot]` and what uses it: the connection runs in the pub/sub bridge,
/// with its own credentials, within AWS IoT Core subscription quotas.
pub fn validate_aws_iot(cfg: &Config) -> anyhow::Result<()> {
    if let Some(settings) = &cfg.device_defender {
        settings.validate()?;
        anyhow::ensure!(cfg.aws_iot.is_some(), "[device_defender] needs [aws_iot]");
    }
    let aws_sync = cfg.shadows.as_ref().and_then(|s| s.aws_iot.as_ref().map(|sync| (s, sync)));
    let Some(iot) = &cfg.aws_iot else {
        anyhow::ensure!(aws_sync.is_none(), "[shadows.aws_iot] needs the [aws_iot] connection");
        return Ok(());
    };
    iot.validate()?;
    iot.check_separate(&cfg.device.keypair_path)?;
    anyhow::ensure!(cfg.pubsub.is_some(), "[aws_iot] runs in the pub/sub bridge: add [pubsub]");
    let mut subscriptions = if cfg.device_defender.is_some() { 2 } else { 0 };
    subscriptions += cfg.pubsub.as_ref().and_then(|p| p.bridge.as_ref())
        .map_or(0, |b| b.mappings.iter().filter(|m| m.source == crate::bridge::Endpoint::IotCore).count());
    if let Some((shadows, sync)) = aws_sync {
        anyhow::ensure!(shadows.thing_name == iot.thing_name, "shadows.thing_name must equal aws_iot.thing_name to synchronize with AWS IoT Device Shadow");
        subscriptions += crate::iotcore::shadow_filters(&iot.thing_name, &sync.shadows).len();
    }
    anyhow::ensure!(subscriptions <= crate::iotcore::MAX_SUBSCRIPTIONS,
        "AWS IoT Core allows {} subscriptions per connection; IotCore mappings plus 3 per synchronized shadow need {subscriptions}",
        crate::iotcore::MAX_SUBSCRIPTIONS);
    Ok(())
}

/// A configured feature this platform cannot provide as specified. On macOS
/// (SF-075) these rest on Linux/systemd mechanisms with no validated
/// equivalent, so they are refused rather than silently weakened.
pub fn unsupported_on(os: &str, cfg: &Config) -> Option<&'static str> {
    if os != "macos" {
        return None;
    }
    if cfg.device.signer.as_ref().is_some_and(|signer| signer.kind == "service") {
        return Some("the device signing service needs systemd unit/cgroup caller identity; it is not supported on macOS");
    }
    if matches!(cfg.apply.mode, ApplyMode::PrivilegedHelper) {
        return Some("the privileged firmware helper is a systemd path-activated unit; it is not supported on macOS");
    }
    if cfg.apply.max_memory_bytes.is_some() {
        return Some("apply.max_memory_bytes is not enforced by macOS (RLIMIT_AS); it is not supported on macOS");
    }
    if cfg.daemon_update.enabled {
        return Some("daemon self-update has not been validated under launchd; it is not supported on macOS");
    }
    if cfg.aws_iot.is_some() {
        return Some("the AWS IoT Core connection is validated on Linux first (SF-061); it is not yet supported on macOS");
    }
    None
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
    pub rollout_poll_seconds: u64,
    pub replay_history: bool,
    /// With device inboxes (SF-125), how often the DeploymentCreated and
    /// DeviceAddedToGroup event scan runs as a safety net for deployments
    /// older packages and clients create without announcing them. 0 scans
    /// every poll. Without inboxes the scan runs every poll regardless.
    pub event_scan_seconds: u64,
    /// Supervised components without a readiness command are ready once their
    /// run survives this long.
    pub component_startup_seconds: u64,
    /// How long a readiness command may keep failing before the start fails.
    pub component_ready_timeout_seconds: u64,
    /// Grace for the shutdown command (or SIGTERM) before SIGKILL.
    pub component_stop_timeout_seconds: u64,
    /// Restarts allowed within ten minutes before a component is Broken.
    pub component_max_restarts: u32,
    /// Let the daemon's group read its own and its components' logs and the
    /// retained confidential values, for a separate log reader such as the
    /// CloudWatch Logs adapter (SF-060). Also writes the daemon's own log.
    pub log_group_read: bool,
}
impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            state_dir: "/var/lib/suifleet".into(),
            poll_seconds: 1800,
            rollout_poll_seconds: 60,
            replay_history: false,
            event_scan_seconds: 60,
            component_startup_seconds: 3,
            component_ready_timeout_seconds: 60,
            component_stop_timeout_seconds: 10,
            component_max_restarts: 5,
            log_group_read: false,
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
    fn shadows_need_a_thing_name_and_bounded_limits() {
        for (text, valid) in [
            ("thing_name = \"dev-1:a_b\"", true),
            ("thing_name = \"\"", false),
            ("thing_name = \"a b\"", false),
            ("thing_name = \"d\"\nmax_shadows = 0", false),
            ("thing_name = \"d\"\nmax_shadows = 129", false),
            ("thing_name = \"d\"\nmax_shadows = 2\n[sync]\nurl = \"https://x\"\nshadows = [\"\", \"b\"]", true),
            ("thing_name = \"d\"\nmax_shadows = 2\n[sync]\nurl = \"https://x\"\nshadows = [\"a\", \"b\", \"c\"]", false),
        ] {
            let cfg: ShadowsConfig = toml::from_str(text).unwrap();
            assert_eq!(cfg.validate().is_ok(), valid, "{text}");
        }
        assert!(toml::from_str::<ShadowsConfig>("thing_name = \"d\"\nunknown = 1").is_err());
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
            assert_eq!(pubsub.validate(false).is_ok(), valid, "{messages}");
        }
    }
    #[test]
    fn aws_iot_needs_pubsub_its_own_key_and_fits_aws_quotas() {
        let base = "[sui]\nmanifest_path='deployment.json'\n[device]\nkeypair_path='/etc/suifleet/device.key'\n\
            device_object_id='0x1'\ndevice_cap_id='0x2'\n[apply]\ncommand='true'\n";
        let iot = "[aws_iot]\nendpoint='abc-ats.iot.us-east-1.amazonaws.com'\nthing_name='dev1'\nca_file='/etc/aws/ca.pem'\n\
            certificate_file='/etc/aws/cert.pem'\nprivate_key_file='/etc/aws/key.pem'\n";
        let names = |n: usize| (0..n).map(|i| format!("'s{i}'")).collect::<Vec<_>>().join(",");
        let check = |text: String| validate_aws_iot(&toml::from_str::<Config>(&text).unwrap()).map_err(|e| e.to_string());
        check(format!("{base}[pubsub]\n{iot}")).unwrap();
        check(format!("{base}[device_defender]\n[pubsub]\n{iot}")).unwrap();
        assert!(check(format!("{base}[device_defender]\n[pubsub]\n")).unwrap_err().contains("needs [aws_iot]"));
        assert!(check(format!("{base}[device_defender]\n[pubsub]\n[shadows]\nthing_name='dev1'\nmax_shadows=32\n[shadows.aws_iot]\nshadows=[{}]\n{iot}", names(16))).is_ok());
        assert!(check(format!("{base}[device_defender]\n[pubsub]\n[shadows]\nthing_name='dev1'\nmax_shadows=32\n[shadows.aws_iot]\nshadows=[{}]\n{iot}", names(17))).is_err());
        check(format!("{base}[pubsub]\n[shadows]\nthing_name='dev1'\nmax_shadows=32\n[shadows.aws_iot]\nshadows=[{}]\n{iot}", names(16))).unwrap();
        for (text, error) in [
            (format!("{base}{iot}"), "add [pubsub]"),
            (format!("{base}[pubsub]\n[shadows]\nthing_name='dev1'\n[shadows.aws_iot]\nshadows=['']\n"), "needs the [aws_iot] connection"),
            (format!("{base}[pubsub]\n[shadows]\nthing_name='other'\n[shadows.aws_iot]\nshadows=['']\n{iot}"), "must equal aws_iot.thing_name"),
            (format!("{base}[pubsub]\n[shadows]\nthing_name='dev1'\nmax_shadows=32\n[shadows.aws_iot]\nshadows=[{}]\n{iot}", names(17)), "subscriptions per connection"),
            (format!("{base}[pubsub]\n{}", iot.replace("/etc/aws/key.pem", "/etc/suifleet/device.key")), "Sui device key"),
        ] {
            let message = check(text.clone()).err().unwrap_or_else(|| panic!("accepted: {text}"));
            assert!(message.contains(error), "{message}");
        }
        let both: ShadowsConfig = toml::from_str("thing_name='dev1'\n[sync]\nurl='https://x'\nshadows=['']\n[aws_iot]\nshadows=['']\n").unwrap();
        assert!(both.validate().unwrap_err().to_string().contains("not both"));
        let cfg: Config = toml::from_str(&format!("{base}[pubsub]\n{iot}")).unwrap();
        assert!(unsupported_on("macos", &cfg).is_some_and(|r| r.contains("AWS IoT Core")));
        assert!(unsupported_on("linux", &cfg).is_none());
    }
    #[test]
    fn walrus_mirrors_are_manifest_pinned_and_chunk_size_bounded() {
        let dir =
            std::env::temp_dir().join(format!("suifleet-config-mirrors-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut manifest: serde_json::Value =
            serde_json::from_str(include_str!("../../manifests/fixture.localnet.json")).unwrap();
        manifest["walrus"]["aggregator_urls"] =
            serde_json::json!(["https://one.example", "https://two.example"]);
        std::fs::write(
            dir.join("deployment.json"),
            serde_json::to_vec(&manifest).unwrap(),
        )
        .unwrap();
        let base = "[sui]\nmanifest_path='deployment.json'\n[device]\nkeypair_path='k'\ndevice_object_id='0x1'\ndevice_cap_id='0x2'\n[apply]\ncommand='true'\n[walrus]\n";
        let path = dir.join("config.toml");
        std::fs::write(&path, base).unwrap();
        let config = Config::load(&path).unwrap();
        assert_eq!(
            config.walrus.aggregator_urls,
            ["https://one.example", "https://two.example"]
        );
        assert_eq!(config.walrus.range_chunk_bytes, 4 * 1024 * 1024);
        for extra in [
            "range_chunk_bytes=0",
            "range_chunk_bytes=10485761",
            "aggregator_urls=['https://stale.example']",
        ] {
            std::fs::write(&path, format!("{base}{extra}\n")).unwrap();
            assert!(Config::load(&path).is_err(), "{extra}");
        }
        let mut bad = fixture();
        bad.walrus.aggregator_urls = vec!["file:///tmp/blob".into()];
        assert!(bad.validate().is_err());
        std::fs::remove_dir_all(dir).unwrap();
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
    fn linux_only_features_are_refused_on_macos() {
        let base = "[sui]\nmanifest_path='deployment.json'\n[device]\nkeypair_path='/k'\ndevice_object_id='0x1'\ndevice_cap_id='0x2'\n";
        let parse = |extra: &str| -> Config { toml::from_str(&format!("{base}{extra}")).unwrap() };
        let plain = parse("[apply]\ncommand='/usr/bin/true'\n");
        assert_eq!(unsupported_on("macos", &plain), None);
        for (extra, needle) in [
            ("[device.signer]\nkind='service'\nsocket='/s'\n[apply]\ncommand='/usr/bin/true'\n", "signing service"),
            ("[apply]\ncommand='/usr/bin/true'\nmode='privileged-helper'\n", "firmware helper"),
            ("[apply]\ncommand='/usr/bin/true'\nmax_memory_bytes=1000000\n", "max_memory_bytes"),
            ("[apply]\ncommand='/usr/bin/true'\n[daemon_update]\nenabled=true\ncomponent='suifleet'\n", "self-update"),
        ] {
            let cfg = parse(extra);
            assert!(unsupported_on("macos", &cfg).is_some_and(|reason| reason.contains(needle)), "{extra}");
            // Linux keeps every one of them.
            assert_eq!(unsupported_on("linux", &cfg), None, "{extra}");
        }
    }
    #[test]
    fn legacy_network_config_rejected() {
        assert!(toml::from_str::<SuiConfig>(
            "rpc_url='http://localhost:9000'\npackage_id='0x1'\nmanifest_path='deployment.json'"
        )
        .is_err());
    }
}
