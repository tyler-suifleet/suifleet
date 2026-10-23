use crate::sui_client::DeploymentCreatedEvent;
pub use crate::protocol::Recipe;
impl Recipe {
    /// Validate a deployment's recipe; returns its declared dependencies.
    pub fn validate(
        &self,
        event: &DeploymentCreatedEvent,
        allow_commands: bool,
    ) -> anyhow::Result<crate::deps::Dependencies> {
        self.validate_component(
            &event.component_name,
            &event.version,
            &event.artifact_sha256,
            allow_commands,
        )
    }

    /// Validate a recipe for an authenticated component identity and artifact
    /// digest; returns its declared dependencies.
    pub fn validate_component(
        &self,
        name: &str,
        version: &str,
        artifact_sha256: &str,
        allow_commands: bool,
    ) -> anyhow::Result<crate::deps::Dependencies> {
        anyhow::ensure!(
            crate::protocol::RECIPE_VERSIONS.contains(&self.schema_version.as_str()),
            "unsupported recipe schema"
        );
        anyhow::ensure!(
            self.component_name == name && self.component_version == version,
            "recipe component identity differs from deployment"
        );
        anyhow::ensure!(
            self.artifact_sha256 == artifact_sha256,
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
        if let Some(services) = &self.services {
            anyhow::ensure!(self.schema_version == "1.3", "services requires recipe schema 1.3");
            services.validate()?;
        }
        let lifecycle = &self.lifecycle;
        anyhow::ensure!(
            lifecycle.run.is_some()
                || (lifecycle.shutdown.is_none() && lifecycle.readiness.is_none() && lifecycle.restart.is_none()),
            "shutdown, readiness and restart need a supervised run command"
        );
        crate::supervisor::Restart::parse(lifecycle.restart.as_deref())?;
        anyhow::ensure!(lifecycle.run.is_none() || allow_commands, "recipe run commands are disabled; set apply.allow_recipe_commands only for a trusted operator");
        let configuration = &self.configuration;
        let configured = configuration.defaults.is_some()
            || configuration.merge.is_some()
            || configuration.reset.is_some()
            || configuration.confidential.is_some()
            || lifecycle.validate.is_some();
        // Older daemons reject these fields: they need a 1.2 consumer.
        anyhow::ensure!(!configured || matches!(self.schema_version.as_str(), "1.2" | "1.3"), "recipe configuration requires schemaVersion 1.2");
        let command = |c: &Option<String>| c.as_deref().is_some_and(|c| !c.is_empty());
        anyhow::ensure!(
            !configured || command(&lifecycle.install) || command(&lifecycle.run),
            "recipe configuration needs an install or run command to receive it"
        );
        anyhow::ensure!(lifecycle.validate.is_none() || allow_commands, "recipe validate commands are disabled; set apply.allow_recipe_commands only for a trusted operator");
        crate::configuration::validate_recipe(configuration)?;
        anyhow::ensure!(self.lifecycle.install.is_none() || allow_commands, "recipe install commands are disabled; set apply.allow_recipe_commands only for a trusted operator");
        anyhow::ensure!(
            self.component_dependencies.is_none() || self.schema_version != "1.0",
            "recipe componentDependencies requires schemaVersion 1.1"
        );
        crate::deps::declared(&self.component_name, self.component_dependencies.as_ref())
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
        let mut recipe: Recipe = serde_json::from_value(value.clone()).unwrap();
        assert!(recipe.validate(&event, false).unwrap().is_empty());
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
        // Dependencies are returned parsed; a self dependency is rejected.
        let mut value = value;
        value["schemaVersion"] = json!("1.1");
        value["componentDependencies"] = json!({"lib": {"versionRequirement": "^1.0.0"}, "log": {"versionRequirement": "*", "dependencyType": "SOFT"}});
        let recipe: Recipe = serde_json::from_value(value.clone()).unwrap();
        let dependencies = recipe.validate(&event, false).unwrap();
        assert_eq!(dependencies["lib"].kind, crate::deps::Kind::Hard);
        assert_eq!(dependencies["log"].kind, crate::deps::Kind::Soft);
        value["componentDependencies"] = json!({"app": {"versionRequirement": "*"}});
        let recipe: Recipe = serde_json::from_value(value).unwrap();
        assert!(recipe.validate(&event, false).unwrap_err().to_string().contains("depends on itself"));
    }
}
