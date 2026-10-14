use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Deserialize, Debug, Clone)]
pub struct Config {
    pub sui: SuiConfig,
    pub device: DeviceConfig,
    pub walrus: WalrusConfig,
    pub apply: ApplyConfig,
    #[serde(default)]
    pub runtime: RuntimeConfig,
}

#[derive(Deserialize, Debug, Clone)]
pub struct SuiConfig {
    pub rpc_url: String,
    #[serde(default)]
    pub original_package_id: String,
    pub package_id: String,
}

#[derive(Deserialize, Debug, Clone)]
pub struct DeviceConfig {
    pub keypair_path: PathBuf,
    pub device_object_id: String,
    pub device_cap_id: String,
}

#[derive(Deserialize, Debug, Clone)]
pub struct WalrusConfig {
    pub aggregator_url: String,
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
        let cfg: Self = toml::from_str(&raw)?;
        anyhow::ensure!(
            cfg.runtime.poll_seconds > 0,
            "runtime.poll_seconds must be positive"
        );
        anyhow::ensure!(
            cfg.apply.timeout_seconds > 0,
            "apply.timeout_seconds must be positive"
        );
        Ok(cfg)
    }
}

fn default_timeout() -> u64 {
    600
}

#[derive(Deserialize, Debug, Clone)]
#[serde(default)]
pub struct RuntimeConfig {
    pub state_dir: PathBuf,
    pub poll_seconds: u64,
    pub replay_history: bool,
}
impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            state_dir: "/var/lib/suifleet".into(),
            poll_seconds: 10,
            replay_history: false,
        }
    }
}
