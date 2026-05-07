use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Deserialize, Debug, Clone)]
pub struct Config {
    pub sui: SuiConfig,
    pub device: DeviceConfig,
    pub walrus: WalrusConfig,
    pub swupdate: SwupdateConfig,
}

#[derive(Deserialize, Debug, Clone)]
pub struct SuiConfig {
    pub rpc_url: String,
    pub ws_url: String,
    pub package_id: String,
}

#[derive(Deserialize, Debug, Clone)]
pub struct DeviceConfig {
    pub keypair_path: PathBuf,
    pub device_object_id: String,
    pub device_cap_id: String,
    /// Device group object IDs this device belongs to.
    /// Any deployment targeting one of these groups will be applied.
    #[serde(default)]
    pub group_ids: Vec<String>,
}

#[derive(Deserialize, Debug, Clone)]
pub struct WalrusConfig {
    pub aggregator_url: String,
}

#[derive(Deserialize, Debug, Clone)]
pub struct SwupdateConfig {
    pub binary: PathBuf,
    #[serde(default)]
    pub dry_run: bool,
}

impl Config {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let raw = std::fs::read_to_string(path)?;
        Ok(toml::from_str(&raw)?)
    }
}
