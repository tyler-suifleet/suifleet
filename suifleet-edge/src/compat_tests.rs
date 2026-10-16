//! SF-028: current-API checks for the AWS recipe compatibility fixtures.
//! The daemon has no AWS recipe translator; these tests pin that AWS-format
//! documents are rejected and that every proposed translation targets the
//! existing native recipe contract. They do not exercise SF-037 itself.
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
            assert!(["linux", "*"].contains(&manifest["os"].as_str().unwrap()));
            assert!(["x86_64", "aarch64", "arm", "*"].contains(&manifest["arch"].as_str().unwrap()));
            translated += 1;
        }
    }
    assert!(translated >= 5);
}
