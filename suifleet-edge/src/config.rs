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
