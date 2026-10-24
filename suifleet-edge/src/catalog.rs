//! Catalog validation is performed on every manifest, before platform selection or downloads.
use crate::sui_client::{same_id, same_type, DeploymentCreatedEvent, Owner, SuiClient};
use serde::{Deserialize, Serialize};

pub use crate::protocol::CATALOG_TYPES as TYPES;
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Version {
    pub catalog_id: String,
    pub publisher: String,
    pub component_name: String,
    pub version: String,
    pub schema_version: serde_json::Value,
    pub platform_os: Vec<String>,
    pub platform_arch: Vec<String>,
    pub recipe_blob_ids: Vec<String>,
    pub recipe_sha256s: Vec<String>,
    pub artifact_sha256s: Vec<String>,
}
fn token(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}
fn hash(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
impl Version {
    pub fn select(&self, os: &str, arch: &str) -> anyhow::Result<usize> {
        self.validate()?;
        self.platform(os, arch)
            .ok_or_else(|| anyhow::anyhow!("unsupported device platform {os}/{arch}"))
    }
    /// The first manifest matching the platform; call after `validate`.
    pub fn platform(&self, os: &str, arch: &str) -> Option<usize> {
        (0..self.platform_os.len()).find(|&i| {
            (self.platform_os[i] == "*" || self.platform_os[i] == os)
                && (self.platform_arch[i] == "*" || self.platform_arch[i] == arch)
        })
    }
    /// Every manifest is validated, including ones that are never selected.
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(
            self.schema_version == "1" || self.schema_version == 1,
            "unsupported catalog schema"
        );
        anyhow::ensure!(
            same_id(&self.catalog_id, &self.catalog_id)
                && same_id(&self.publisher, &self.publisher),
            "invalid catalog identity"
        );
        anyhow::ensure!(
            !self.component_name.is_empty()
                && self.component_name.len() <= 128
                && self
                    .component_name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b)),
            "invalid component name"
        );
        anyhow::ensure!(self.version.len() <= 128, "invalid component version");
        semver::Version::parse(&self.version)?;
        let n = self.platform_os.len();
        anyhow::ensure!(
            n > 0
                && n <= 16
                && [
                    self.platform_arch.len(),
                    self.recipe_blob_ids.len(),
                    self.recipe_sha256s.len(),
                    self.artifact_sha256s.len()
                ]
                .iter()
                .all(|len| *len == n),
            "invalid manifest vectors"
        );
        let mut pairs = std::collections::BTreeSet::new();
        for i in 0..n {
            anyhow::ensure!(
                matches!(self.platform_os[i].as_str(), "linux" | "macos" | "*")
                    && matches!(
                        self.platform_arch[i].as_str(),
                        // Rust `std::env::consts::ARCH` names; `x86` is 32-bit i686.
                        "x86_64" | "aarch64" | "arm" | "x86" | "*"
                    ),
                "unsupported manifest platform"
            );
            anyhow::ensure!(
                pairs.insert((&self.platform_os[i], &self.platform_arch[i])),
                "duplicate manifest platform"
            );
            anyhow::ensure!(
                token(&self.recipe_blob_ids[i])
                    && hash(&self.recipe_sha256s[i])
                    && hash(&self.artifact_sha256s[i]),
                "invalid manifest blob/digest"
            );
        }
        Ok(())
    }
}

/// The catalog an authenticated schema-2 deployment was bound to.
#[derive(Debug, Clone)]
pub struct CatalogRef {
    pub catalog_id: String,
    pub publisher: String,
    pub version_id: String,
}
/// A deployment with its recipe/artifact selection resolved.
pub struct Resolved {
    pub event: DeploymentCreatedEvent,
    pub recipe_sha256: String,
    /// Absent for schema-1 deployments, which name no catalog.
    pub catalog: Option<CatalogRef>,
    /// The authenticated catalog version, for schema-2 deployments.
    pub version: Option<Version>,
}

/// Every `name@version -> ComponentVersion ID` entry of the shared catalog,
/// decoded from its BCS after checking its type, ownership and publisher.
pub async fn index(sui: &SuiClient, catalog: &CatalogRef) -> anyhow::Result<Vec<(String, String, String)>> {
    let (object_type, owner, contents) = sui.move_object(&catalog.catalog_id).await?;
    anyhow::ensure!(
        same_type(&object_type, &sui.move_type("component_catalog::ComponentCatalog"))
            && matches!(owner, Owner::Shared { .. }),
        "component catalog must have authenticated type and shared ownership"
    );
    let value: crate::protocol::component_catalog::ComponentCatalog = bcs::from_bytes(&contents)
        .map_err(|e| anyhow::anyhow!("undecodable component catalog: {e}"))?;
    let hex_id = |bytes: &[u8; 32]| format!("0x{}", hex::encode(bytes));
    anyhow::ensure!(
        same_id(&hex_id(&value.id), &catalog.catalog_id)
            && same_id(&hex_id(&value.publisher), &catalog.publisher),
        "component catalog identity mismatch"
    );
    value
        .versions
        .contents
        .iter()
        .map(|entry| {
            let (name, version) = entry
                .key
                .split_once('@')
                .ok_or_else(|| anyhow::anyhow!("invalid catalog index key {:?}", entry.key))?;
            Ok((name.to_owned(), version.to_owned(), hex_id(&entry.value)))
        })
        .collect()
}

/// A frozen catalog version, authenticated and checked against its index entry.
pub async fn version(
    sui: &SuiClient,
    catalog: &CatalogRef,
    id: &str,
    name: &str,
    version: &str,
) -> anyhow::Result<Version> {
    let object = sui.object(id).await?;
    anyhow::ensure!(
        same_type(
            &object.object_type,
            &sui.move_type("component_catalog::ComponentVersion")
        ) && object.owner == Owner::Immutable,
        "catalog version must have authenticated type and immutable ownership"
    );
    let value: Version = serde_json::from_value(object.fields)?;
    anyhow::ensure!(
        same_id(&value.catalog_id, &catalog.catalog_id)
            && same_id(&value.publisher, &catalog.publisher)
            && value.component_name == name
            && value.version == version,
        "catalog version identity mismatch"
    );
    value.validate()?;
    Ok(value)
}

pub async fn resolve(
    sui: &SuiClient,
    event: &DeploymentCreatedEvent,
    fields: &serde_json::Value,
) -> anyhow::Result<Resolved> {
    let mut resolved = event.clone();
    let schema = sui.deployment_schema(&event.deployment_id, fields).await?;
    anyhow::ensure!((1..=3).contains(&schema), "unsupported DeploymentRecord schema version");
    anyhow::ensure!(
        fields["component_name"] == event.component_name && fields["version"] == event.version,
        "deployment identity mismatch"
    );
    if schema == 1 || schema == 3 {
        let digest = sui
            .dynamic_field(&event.deployment_id, "deployment_manager::RecipeDigestKey")
            .await?;
        let digest = digest
            .and_then(|v| v.as_str().map(str::to_owned))
            .ok_or_else(|| anyhow::anyhow!("deployment has no authenticated recipe digest"))?;
        return Ok(Resolved {
            event: resolved,
            recipe_sha256: digest,
            catalog: None,
            version: None,
        });
    }
    for name in TYPES {
        anyhow::ensure!(
            sui.manifest().type_origins.contains_key(*name),
            "missing catalog type origin: {name}"
        );
    }
    let binding = sui
        .dynamic_field(&event.deployment_id, if fields["schema_version"] == "4" || fields["schema_version"] == 4 { "component_catalog::ShardedCatalogBindingKey" } else { "component_catalog::CatalogBindingKey" })
        .await?
        .ok_or_else(|| anyhow::anyhow!("missing catalog binding"))?;
    let id = binding["version_id"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("invalid catalog binding"))?;
    let object = sui.object(id).await?;
    anyhow::ensure!(
        same_type(
            &object.object_type,
            &sui.move_type("component_catalog::ComponentVersion")
        ) && object.owner == Owner::Immutable,
        "catalog version must have authenticated type and immutable ownership"
    );
    let version: Version = serde_json::from_value(object.fields)?;
    anyhow::ensure!(
        version.component_name == event.component_name && version.version == event.version,
        "catalog identity mismatch"
    );
    let i = version.select(std::env::consts::OS, std::env::consts::ARCH)?;
    resolved.recipe_blob_id = version.recipe_blob_ids[i].clone();
    resolved.artifact_sha256 = version.artifact_sha256s[i].clone();
    Ok(Resolved {
        event: resolved,
        recipe_sha256: version.recipe_sha256s[i].clone(),
        catalog: Some(CatalogRef {
            catalog_id: version.catalog_id.clone(),
            publisher: version.publisher.clone(),
            version_id: id.to_owned(),
        }),
        version: Some(version),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shared_independent_selection_vectors() {
        let cases: serde_json::Value =
            serde_json::from_str(include_str!("../../interfaces/component-cases.json")).unwrap();
        for c in cases.as_array().unwrap() {
            let actual = serde_json::from_value::<Version>(c["value"].clone())
                .ok()
                .and_then(|v| {
                    v.select(c["os"].as_str().unwrap(), c["arch"].as_str().unwrap())
                        .ok()
                });
            assert_eq!(
                actual,
                c["selected"].as_u64().map(|i| i as usize),
                "{}",
                c["name"]
            );
        }
    }
}
