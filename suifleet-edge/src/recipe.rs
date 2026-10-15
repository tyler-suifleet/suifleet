use crate::sui_client::DeploymentCreatedEvent;
pub use crate::protocol::Recipe;
impl Recipe {
    pub fn validate(
        &self,
        event: &DeploymentCreatedEvent,
        allow_commands: bool,
    ) -> anyhow::Result<()> {
        anyhow::ensure!(self.schema_version == crate::protocol::RECIPE_VERSION, "unsupported recipe schema");
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
        let lifecycle = &self.lifecycle;
        anyhow::ensure!(
            lifecycle.run.is_some()
                || (lifecycle.shutdown.is_none() && lifecycle.readiness.is_none() && lifecycle.restart.is_none()),
            "shutdown, readiness and restart need a supervised run command"
        );
        crate::supervisor::Restart::parse(lifecycle.restart.as_deref())?;
        anyhow::ensure!(lifecycle.run.is_none() || allow_commands, "recipe run commands are disabled; set apply.allow_recipe_commands only for a trusted operator");
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
    fn shares_frontend_recipe_compatibility_cases() {
        let event: DeploymentCreatedEvent = serde_json::from_str(
            r#"{"deployment_id":"0x1","component_name":"app","version":"v1","recipe_blob_id":"recipe","artifact_sha256":"abababababababababababababababababababababababababababababababab","target_groups":[],"target_devices":["0x2"],"creator":"0x3"}"#,
        ).unwrap();
        let cases: serde_json::Value = serde_json::from_str(include_str!("../../interfaces/recipe-cases.json")).unwrap();
        for case in cases.as_array().unwrap() {
            let accepted = serde_json::from_value::<Recipe>(case["value"].clone())
                .is_ok_and(|recipe| recipe.validate(&event, true).is_ok());
            assert_eq!(accepted, case["valid"].as_bool().unwrap(), "{}", case["name"]);
        }
    }
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
        recipe.lifecycle.shutdown = Some("true".into());
        assert!(recipe.validate(&event, true).is_err(), "shutdown without run");
        recipe.lifecycle.run = Some("sleep 60".into());
        assert!(recipe.validate(&event, false).is_err(), "run needs trusted recipe commands");
        assert!(recipe.validate(&event, true).is_ok());
        recipe.lifecycle.restart = Some("sometimes".into());
        assert!(recipe.validate(&event, true).is_err());
    }
}
