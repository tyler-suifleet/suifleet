use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Deserialize, Debug, Clone)]
pub struct Config {
    pub sui: SuiConfig,
    pub device: DeviceConfig,
    #[serde(default)]
    pub walrus: WalrusConfig,
    pub apply: ApplyConfig,
    #[serde(default)]
    pub runtime: RuntimeConfig,
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

pub const REQUIRED_TYPES: &[&str] = &[
    "device_registry::DeviceRegistry",
    "device_registry::Device",
    "device_group::DeviceGroup",
    "device_group::GroupCreated",
    "device_cap::DeviceCap",
    "device_cap::DeviceAddressKey",
    "deployment_manager::DeploymentRecord",
    "deployment_manager::DeploymentCreated",
    "deployment_manager::DeviceStatusUpdated",
    "deployment_manager::TargetGroupsKey",
    "deployment_manager::TargetDevicesKey",
    "deployment_manager::StatusesKey",
    "deployment_manager::RecipeDigestKey",
];

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
            self.schema_version == 1 && self.protocol_version == 1,
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
        for name in REQUIRED_TYPES {
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
    pub keypair_path: PathBuf,
    pub device_object_id: String,
    pub device_cap_id: String,
}

#[derive(Deserialize, Debug, Clone, Default)]
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
        let mut cfg: Self = toml::from_str(&raw)?;
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
    fn legacy_network_config_rejected() {
        assert!(toml::from_str::<SuiConfig>(
            "rpc_url='http://localhost:9000'\npackage_id='0x1'\nmanifest_path='deployment.json'"
        )
        .is_err());
    }
}
