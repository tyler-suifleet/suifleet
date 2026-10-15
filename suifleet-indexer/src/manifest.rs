//! The network deployment manifest (docs/DEPLOYMENT_MANIFEST.md) that the
//! dApp and the daemon also use, verified against the chain before indexing.
use crate::chain::{self, Chain, Owner};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Debug, Clone, Deserialize)]
pub struct Manifest {
    pub schema_version: u64,
    pub protocol_version: u64,
    pub network: String,
    pub chain_identifier: String,
    pub rpc_url: String,
    pub call_package_id: String,
    pub upgrade_cap_id: String,
    pub package_version: String,
    pub type_origins: BTreeMap<String, String>,
    pub shared_objects: SharedObjects,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SharedObjects {
    pub registry: SharedObject,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SharedObject {
    pub id: String,
    pub initial_shared_version: String,
}

impl Manifest {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let manifest: Self = serde_json::from_str(&std::fs::read_to_string(path)?)?;
        anyhow::ensure!(
            manifest.schema_version == 1 && manifest.protocol_version == 1,
            "unsupported manifest schema/protocol"
        );
        anyhow::ensure!(
            matches!(
                manifest.network.as_str(),
                "localnet" | "devnet" | "testnet" | "mainnet"
            ),
            "unsupported network"
        );
        anyhow::ensure!(
            manifest.chain_identifier.len() == 8
                && manifest
                    .chain_identifier
                    .bytes()
                    .all(|b| b.is_ascii_hexdigit()),
            "invalid chain identifier"
        );
        for id in [
            &manifest.call_package_id,
            &manifest.upgrade_cap_id,
            &manifest.shared_objects.registry.id,
        ] {
            anyhow::ensure!(chain::normalize(id).is_some(), "invalid object ID {id}");
        }
        Ok(manifest)
    }

    /// Chain identity, the latest package (its version and every recorded
    /// type origin), the UpgradeCap and the registry must all match. Returns
    /// every type of the package ("module::Type" → full Move type), from the
    /// package's own origin table.
    pub async fn verify(&self, chain: &Chain) -> anyhow::Result<BTreeMap<String, String>> {
        let info = chain.service_info().await?;
        anyhow::ensure!(
            info.chain_identifier == self.chain_identifier,
            "chain identifier {} does not match the manifest's {}; the network may have reset",
            info.chain_identifier,
            self.chain_identifier
        );
        let (version, origins) = chain.package(&self.call_package_id).await?;
        anyhow::ensure!(
            version.to_string() == self.package_version,
            "package version mismatch"
        );
        for (name, origin) in &self.type_origins {
            anyhow::ensure!(
                origins
                    .get(name)
                    .is_some_and(|actual| chain::same_id(actual, origin)),
                "type origin mismatch: {name}"
            );
        }
        let cap = chain
            .object(&self.upgrade_cap_id)
            .await?
            .ok_or_else(|| anyhow::anyhow!("UpgradeCap {} not found", self.upgrade_cap_id))?;
        anyhow::ensure!(
            chain::same_type(&cap.object_type, "0x2::package::UpgradeCap")
                && chain::same_id(
                    cap.fields["package"].as_str().unwrap_or(""),
                    &self.call_package_id
                )
                && chain::number(&cap.fields["version"])
                    .map(|v| v.to_string())
                    .as_deref()
                    == Some(self.package_version.as_str()),
            "UpgradeCap does not identify the latest call package/version"
        );
        let registry = chain
            .object(&self.shared_objects.registry.id)
            .await?
            .ok_or_else(|| anyhow::anyhow!("registry not found"))?;
        let registry_type = origins
            .get("device_registry::DeviceRegistry")
            .map(|origin| format!("{origin}::device_registry::DeviceRegistry"))
            .unwrap_or_default();
        anyhow::ensure!(
            chain::same_type(&registry.object_type, &registry_type)
                && matches!(registry.owner, Owner::Shared(v) if v.to_string() == self.shared_objects.registry.initial_shared_version),
            "registry type/shared version mismatch"
        );
        Ok(origins
            .into_iter()
            .map(|(name, origin)| (name.clone(), format!("{origin}::{name}")))
            .collect())
    }
}
