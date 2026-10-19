//! SF-028/SF-037: daemon checks for the AWS recipe compatibility fixtures.
//! The daemon never reads AWS recipes: these tests pin that AWS-format
//! documents are rejected, and that every translation the SF-037 translator
//! (dapp/src/lib/greengrass-recipe.ts) must produce satisfies the daemon's own
//! recipe and configuration validation. scripts/test_greengrass_runtime.py
//! runs translated recipes on the real daemon.
use crate::recipe::Recipe;
use serde_json::Value;

fn cases() -> Vec<Value> {
    serde_json::from_str(include_str!("../../interfaces/compat/aws-recipe-cases.json")).unwrap()
}

#[test]
fn native_validator_rejects_aws_recipes() {
    for case in cases() {
        assert!(
            serde_json::from_value::<Recipe>(case["recipe"].clone()).is_err(),
            "{} was accepted as a native recipe",
            case["id"]
        );
    }
}

#[test]
fn proposed_translations_satisfy_native_recipe_contract() {
    let mut translated = 0;
    for case in cases() {
        let Some(manifests) = case["expected"]["manifests"].as_array() else {
            assert_ne!(case["expected"]["outcome"], "translate", "{}", case["id"]);
            continue;
        };
        for manifest in manifests {
            let recipe: Recipe = serde_json::from_value(manifest["recipe"].clone())
                .unwrap_or_else(|e| panic!("{}: {e}", case["id"]));
            recipe
                .validate_component(
                    &recipe.component_name,
                    &recipe.component_version,
                    &recipe.artifact_sha256,
                    true,
                )
                .unwrap_or_else(|e| panic!("{}: {e}", case["id"]));
            crate::configuration::validate_recipe(&recipe.configuration)
                .unwrap_or_else(|e| panic!("{}: {e}", case["id"]));
            assert!(["linux", "*"].contains(&manifest["os"].as_str().unwrap()));
            assert!(["x86_64", "aarch64", "arm", "*"].contains(&manifest["arch"].as_str().unwrap()));
            translated += 1;
        }
    }
    assert!(translated >= 10);
}

#[test]
fn rejected_configuration_limits_match_the_daemon() {
    let case = cases()
        .into_iter()
        .find(|c| c["id"] == "configuration-limits-rejected")
        .unwrap();
    let defaults = case["recipe"]["ComponentConfiguration"]["DefaultConfiguration"].clone();
    let configuration = serde_json::from_value(serde_json::json!({ "defaults": defaults })).unwrap();
    assert!(crate::configuration::validate_recipe(&configuration).is_err());
}

#[test]
fn empty_lifecycle_commands_are_rejected_by_the_daemon() {
    // What a translator copying the empty run would emit; empty-step rejects it first.
    let case = cases()
        .into_iter()
        .find(|c| c["id"] == "empty-run-with-configuration-rejected")
        .unwrap();
    let binding = &case["bindings"]["s3://example-bucket/hello/1.0.0/hello.tar"];
    let recipe: Recipe = serde_json::from_value(serde_json::json!({
        "schemaVersion": "1.2",
        "componentName": case["recipe"]["ComponentName"],
        "componentVersion": case["recipe"]["ComponentVersion"],
        "artifactBlobId": binding["artifactBlobId"],
        "artifactSha256": binding["artifactSha256"],
        "lifecycle": case["recipe"]["Manifests"][0]["Lifecycle"],
        "configuration": { "defaults": case["recipe"]["ComponentConfiguration"]["DefaultConfiguration"] },
    }))
    .unwrap();
    let error = recipe
        .validate_component(&recipe.component_name, &recipe.component_version, &recipe.artifact_sha256, true)
        .unwrap_err();
    assert!(error.to_string().contains("install or run command"), "{error}");
}

#[test]
fn translated_soft_dependencies_are_required_by_the_resolver() {
    // AWS SOFT still needs the dependency at start; a native SOFT one could be
    // omitted, so the translation is native HARD and an unavailable dependency
    // fails the plan instead of starting the component without it.
    use crate::deps::{resolve, Candidate, Detail, Kind, Pool, Root, Step};
    use std::collections::BTreeMap;
    for id in ["json-soft-dependency-required", "yaml-soft-dependency-required"] {
        let case = cases().into_iter().find(|c| c["id"] == id).unwrap();
        assert_eq!(case["recipe"]["ComponentDependencies"]["example.RequiredAtStartup"]["DependencyType"], "SOFT");
        let recipe: Recipe = serde_json::from_value(case["expected"]["manifests"][0]["recipe"].clone()).unwrap();
        let dependencies = recipe
            .validate_component(&recipe.component_name, &recipe.component_version, &recipe.artifact_sha256, true)
            .unwrap();
        assert_eq!(dependencies["example.RequiredAtStartup"].kind, Kind::Hard, "{id}");
        let root = Root { name: recipe.component_name.clone(), version: recipe.component_version.clone(), dependencies };
        let mut pool = Pool::default();
        pool.listings.insert(
            "example.RequiredAtStartup".into(),
            vec![Candidate { version: "2.0.0".into(), detail: Some(Detail::Ready(Default::default())) }],
        );
        let error = resolve(&root, &BTreeMap::new(), &pool).unwrap_err().to_string();
        assert!(error.contains("no available version of example.RequiredAtStartup"), "{id}: {error}");
        pool.listings.get_mut("example.RequiredAtStartup").unwrap()[0].version = "1.4.0".into();
        let Step::Plan(plan) = resolve(&root, &BTreeMap::new(), &pool).unwrap() else { panic!("{id}: needs more data") };
        assert!(plan.omitted.is_empty(), "{id}");
        let order: Vec<_> = plan.order.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(order, ["example.RequiredAtStartup", "example.App"], "{id}");
    }
}
