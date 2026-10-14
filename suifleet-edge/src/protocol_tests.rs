use crate::protocol;

#[test]
fn generated_daemon_calls_keep_compiled_argument_order_and_scalar_types() {
    assert_eq!(protocol::deployment_manager::report_status_args("deployment", "cap", 1),
        vec![serde_json::json!("deployment"), serde_json::json!("cap"), serde_json::json!("1")]);
    assert_eq!(protocol::deployment_manager::report_status_for_group_args("deployment", "cap", "group", 2),
        vec![serde_json::json!("deployment"), serde_json::json!("cap"), serde_json::json!("group"), serde_json::json!("2")]);
}

#[test]
fn generated_wire_layout_matches_independent_cross_client_vector() {
    let fixture: serde_json::Value = serde_json::from_str(include_str!("../../interfaces/wire-vectors.json")).unwrap();
    let expected = hex::decode(fixture["deployment_created"]["bcs_hex"].as_str().unwrap()).unwrap();
    let event = protocol::deployment_manager::DeploymentCreated {
        deployment_id: [0x11; 32], component_name: "app".into(), version: "v1".into(),
        recipe_blob_id: "recipe".into(), artifact_sha256: "ab".repeat(32),
        target_groups: vec![[0x22; 32]], target_devices: vec![[0x33; 32]], creator: [0x44; 32],
    };
    assert_eq!(bcs::to_bytes(&event).unwrap(), expected);
    let decoded: protocol::deployment_manager::DeploymentCreated = bcs::from_bytes(&expected).unwrap();
    assert_eq!(decoded.component_name, "app");
    assert_eq!(decoded.creator, [0x44; 32]);
    assert_eq!(bcs::to_bytes(&protocol::deployment_manager::ReceiptsKey { dummy_field: false }).unwrap(), [0]);
    assert!(bcs::from_bytes::<protocol::deployment_manager::DeploymentCreated>(&expected[..expected.len() - 1]).is_err());
    let json: protocol::DeploymentCreatedEvent = serde_json::from_value(fixture["deployment_created"]["json"].clone()).unwrap();
    assert_eq!(json.component_name, decoded.component_name);
}
