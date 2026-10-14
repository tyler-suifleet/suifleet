use crate::sui_client::DeploymentCreatedEvent;
use serde::Deserialize;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Recipe {
    pub schema_version: String,
    pub component_name: String,
    pub component_version: String,
    #[serde(default)]
    #[serde(rename = "description")]
    pub _description: Option<String>,
    pub artifact_blob_id: String,
    pub artifact_sha256: String,
    pub lifecycle: Lifecycle,
    pub configuration: serde_json::Map<String, serde_json::Value>,
}
#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Lifecycle {
    pub install: Option<String>,
    pub run: Option<String>,
    pub shutdown: Option<String>,
}
impl Recipe {
    pub fn validate(
        &self,
        event: &DeploymentCreatedEvent,
        allow_commands: bool,
    ) -> anyhow::Result<()> {
        anyhow::ensure!(self.schema_version == "1.0", "unsupported recipe schema");
        anyhow::ensure!(
            self.component_name == event.component_name && self.component_version == event.version,
            "recipe component identity differs from deployment"
        );
        anyhow::ensure!(
            self.artifact_sha256 == event.artifact_sha256,
            "recipe hash differs from on-chain hash"
        );
        anyhow::ensure!(
            self.artifact_sha256.len() == 64 && hex::decode(&self.artifact_sha256).is_ok(),
            "invalid SHA-256"
        );
        anyhow::ensure!(
            !self.artifact_blob_id.is_empty()
                && self
                    .artifact_blob_id
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'),
            "invalid artifact blob ID"
        );
        anyhow::ensure!(self.lifecycle.run.is_none() && self.lifecycle.shutdown.is_none(), "managed run/shutdown lifecycle requires a component supervisor, which is not implemented");
        anyhow::ensure!(
            self.configuration.is_empty(),
            "component configuration management is not implemented"
        );
        anyhow::ensure!(self.lifecycle.install.is_none() || allow_commands, "recipe install commands are disabled; set apply.allow_recipe_commands only for a trusted operator");
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn rejects_unbound_or_unsupported_recipes() {
        let event: DeploymentCreatedEvent = serde_json::from_value(json!({
            "deployment_id":"0x1","component_name":"app","version":"1","recipe_blob_id":"recipe",
            "artifact_sha256":"a".repeat(64),"target_groups":[],"target_devices":["0x2"],"creator":"0x3"
        })).unwrap();
        let value = json!({"schemaVersion":"1.0","componentName":"app","componentVersion":"1",
            "artifactBlobId":"blob","artifactSha256":"a".repeat(64),"lifecycle":{},"configuration":{}});
        let mut recipe: Recipe = serde_json::from_value(value).unwrap();
        assert!(recipe.validate(&event, false).is_ok());
        recipe.artifact_sha256 = "b".repeat(64);
        assert!(recipe.validate(&event, true).is_err());
        recipe.artifact_sha256 = event.artifact_sha256.clone();
        recipe.lifecycle.install = Some("true".into());
        assert!(recipe.validate(&event, false).is_err());
        assert!(recipe.validate(&event, true).is_ok());
        recipe.lifecycle.run = Some("sleep 60".into());
        assert!(recipe.validate(&event, true).is_err());
    }
}
