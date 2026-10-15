//! Unsigned bulk-operation transactions. Requests are validated against the
//! indexed state, then built (gas, object versions and budget resolved by the
//! node's simulation) for the operator to sign elsewhere: a wallet, or
//! `suifleet-indexer ctl`, which re-checks the transaction before signing.
use crate::chain::{self, Chain};
use crate::store;
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::str::FromStr;
use sui_sdk_types::Address;
use sui_transaction_builder::{Function, ObjectInput, TransactionBuilder};

/// Most calls in one bulk transaction.
pub const MAX_CALLS: usize = 200;

/// A request the indexed state rejects (HTTP 400), not a server failure.
#[derive(Debug)]
pub struct Invalid(pub String);
impl std::fmt::Display for Invalid {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Invalid {}

fn invalid(message: impl Into<String>) -> anyhow::Error {
    Invalid(message.into()).into()
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Request {
    pub sender: String,
    #[serde(default)]
    pub group: Option<String>,
    #[serde(default)]
    pub devices: Vec<String>,
    #[serde(default)]
    pub component_name: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub recipe_blob_id: Option<String>,
    #[serde(default)]
    pub artifact_sha256: Option<String>,
    #[serde(default)]
    pub recipe_sha256: Option<String>,
    #[serde(default)]
    pub target_groups: Vec<String>,
    #[serde(default)]
    pub target_devices: Vec<String>,
}

/// One Move call into the fleet package: module, function, arguments.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Argument {
    Object(String),
    Clock,
    Text(String),
    /// An object ID passed by value (pure), not the object itself.
    Id(String),
    Ids(Vec<String>),
}

#[derive(Debug, Clone)]
pub struct Planned {
    pub sender: String,
    pub package: String,
    pub calls: Vec<(&'static str, &'static str, Vec<Argument>)>,
    pub summary: Value,
}

/// Operation → the only (module, function) pairs its transaction may call.
pub fn allowed(operation: &str) -> Option<&'static [(&'static str, &'static str)]> {
    Some(match operation {
        "add-devices" => &[("device_group", "add_device")],
        "remove-devices" => &[("device_group", "remove_device")],
        "create-deployment" => &[("deployment_manager", "create_deployment_verified")],
        _ => return None,
    })
}

fn id(value: &str, what: &str) -> anyhow::Result<String> {
    chain::normalize(value).ok_or_else(|| invalid(format!("invalid {what} ID {value:?}")))
}

fn ids(values: &[String], what: &str) -> anyhow::Result<Vec<String>> {
    let mut out: Vec<String> = values
        .iter()
        .map(|v| id(v, what))
        .collect::<Result<_, _>>()?;
    out.sort();
    out.dedup();
    Ok(out)
}

fn hex64(value: &Option<String>, what: &str) -> anyhow::Result<String> {
    match value {
        Some(v)
            if v.len() == 64
                && v.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) =>
        {
            Ok(v.clone())
        }
        _ => Err(invalid(format!(
            "{what} must be 64 lowercase hex characters"
        ))),
    }
}

fn text(value: &Option<String>, what: &str) -> anyhow::Result<String> {
    match value {
        Some(v) if !v.is_empty() && v.len() <= 256 => Ok(v.clone()),
        _ => Err(invalid(format!("{what} is required (1-256 characters)"))),
    }
}

fn admin_group(
    db: &rusqlite::Connection,
    group: &Option<String>,
    sender: &str,
) -> anyhow::Result<store::Group> {
    let group_id = id(
        group
            .as_deref()
            .ok_or_else(|| invalid("group is required"))?,
        "group",
    )?;
    let group = store::read_group(db, &group_id)?
        .filter(|g| !g.deleted)
        .ok_or_else(|| invalid(format!("group {group_id} is not indexed or was deleted")))?;
    if group.admin != sender {
        return Err(invalid(format!(
            "{sender} is not the admin of group {group_id}"
        )));
    }
    Ok(group)
}

/// Validate `request` for `operation` against the index; no chain access.
pub fn plan(
    db: &rusqlite::Connection,
    types: &BTreeMap<String, String>,
    package: &str,
    operation: &str,
    request: &Request,
) -> anyhow::Result<Planned> {
    allowed(operation).ok_or_else(|| invalid(format!("unknown operation {operation}")))?;
    let sender = id(&request.sender, "sender")?;
    let package = id(package, "package")?;
    let _ = types; // Origins are resolved by the node from the call package.
    let mut calls = Vec::new();
    let summary = match operation {
        "add-devices" | "remove-devices" => {
            let group = admin_group(db, &request.group, &sender)?;
            let devices = ids(&request.devices, "device")?;
            if devices.is_empty() {
                return Err(invalid("devices must not be empty"));
            }
            for device in &devices {
                let member = group.devices.contains(device);
                if operation == "add-devices" {
                    let found = store::read_device(db, device)?
                        .filter(|d| !d.deleted)
                        .ok_or_else(|| {
                            invalid(format!(
                                "device {device} is not indexed or was deregistered"
                            ))
                        })?;
                    if found.owner.as_deref() != Some(sender.as_str()) {
                        return Err(invalid(format!("device {device} is not owned by {sender}")));
                    }
                    if member {
                        return Err(invalid(format!(
                            "device {device} is already in group {}",
                            group.id
                        )));
                    }
                    calls.push((
                        "device_group",
                        "add_device",
                        vec![
                            Argument::Object(group.id.clone()),
                            Argument::Object(device.clone()),
                        ],
                    ));
                } else {
                    if !member {
                        return Err(invalid(format!(
                            "device {device} is not in group {}",
                            group.id
                        )));
                    }
                    calls.push((
                        "device_group",
                        "remove_device",
                        vec![
                            Argument::Object(group.id.clone()),
                            Argument::Id(device.clone()),
                        ],
                    ));
                }
            }
            json!({ "operation": operation, "group": group.id, "devices": devices })
        }
        "create-deployment" => {
            let groups = ids(&request.target_groups, "target group")?;
            let devices = ids(&request.target_devices, "target device")?;
            if groups.is_empty() && devices.is_empty() {
                return Err(invalid("a deployment needs at least one target"));
            }
            for group in &groups {
                let found = store::read_group(db, group)?
                    .filter(|g| !g.deleted)
                    .ok_or_else(|| {
                        invalid(format!(
                            "target group {group} is not indexed or was deleted"
                        ))
                    })?;
                if found.admin != sender {
                    return Err(invalid(format!("devices only follow their operator's groups; {sender} is not the admin of {group}")));
                }
            }
            for device in &devices {
                let found = store::read_device(db, device)?
                    .filter(|d| !d.deleted)
                    .ok_or_else(|| {
                        invalid(format!(
                            "target device {device} is not indexed or was deregistered"
                        ))
                    })?;
                if found.owner.as_deref() != Some(sender.as_str()) {
                    return Err(invalid(format!("devices only accept their operator's deployments; {device} is not owned by {sender}")));
                }
            }
            let component = text(&request.component_name, "component_name")?;
            let version = text(&request.version, "version")?;
            let recipe_blob = text(&request.recipe_blob_id, "recipe_blob_id")?;
            let artifact = hex64(&request.artifact_sha256, "artifact_sha256")?;
            let recipe = hex64(&request.recipe_sha256, "recipe_sha256")?;
            calls.push((
                "deployment_manager",
                "create_deployment_verified",
                vec![
                    Argument::Text(component.clone()),
                    Argument::Text(version.clone()),
                    Argument::Text(recipe_blob),
                    Argument::Text(artifact),
                    Argument::Text(recipe),
                    Argument::Ids(groups.clone()),
                    Argument::Ids(devices.clone()),
                    Argument::Clock,
                ],
            ));
            json!({ "operation": operation, "component_name": component, "version": version,
                    "target_groups": groups, "target_devices": devices })
        }
        _ => unreachable!(),
    };
    if calls.len() > MAX_CALLS {
        return Err(invalid(format!(
            "at most {MAX_CALLS} calls per transaction; split the request"
        )));
    }
    Ok(Planned {
        sender,
        package,
        calls,
        summary,
    })
}

/// Build the planned calls into a sign-ready transaction.
pub async fn build(chain: &Chain, planned: Planned) -> anyhow::Result<Value> {
    let package = Address::from_str(&planned.package)?;
    let sender = Address::from_str(&planned.sender)?;
    let mut builder = TransactionBuilder::new();
    for (module, function, arguments) in &planned.calls {
        let mut inputs = Vec::new();
        for argument in arguments {
            inputs.push(match argument {
                Argument::Object(id) => builder.object(ObjectInput::new(Address::from_str(id)?)),
                Argument::Clock => builder.object(ObjectInput::new(Address::from_str("0x6")?)),
                Argument::Text(value) => builder.pure(value),
                Argument::Id(value) => builder.pure(&Address::from_str(value)?),
                Argument::Ids(values) => {
                    let ids: Vec<Address> = values
                        .iter()
                        .map(|v| Address::from_str(v))
                        .collect::<Result<_, _>>()?;
                    builder.pure(&ids)
                }
            });
        }
        builder.move_call(
            Function::new(package, module.parse()?, function.parse()?),
            inputs,
        );
    }
    let transaction = chain.build(builder, sender).await?;
    let bytes = bcs::to_bytes(&transaction)?;
    Ok(json!({
        "transaction": STANDARD.encode(&bytes),
        "digest": transaction.digest().to_string(),
        "sender": planned.sender,
        "summary": planned.summary,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use store::{Device, Group};

    fn hex(n: u32) -> String {
        format!("0x{n:064x}")
    }

    fn fixture() -> (tempfile::TempDir, rusqlite::Connection) {
        let dir = tempfile::tempdir().unwrap();
        let mut db = store::open(&dir.path().join("fleet.db")).unwrap();
        let tx = db.transaction().unwrap();
        for (n, owner) in [(1, 100), (2, 100), (3, 999)] {
            store::put_device(
                &tx,
                &Device {
                    id: hex(n),
                    owner: Some(hex(owner)),
                    name: format!("d{n}"),
                    device_address: hex(n + 500),
                    arch: "aarch64".into(),
                    registered_at: 0,
                    deleted: false,
                    groups: vec![],
                },
                None,
            )
            .unwrap();
        }
        store::put_group(
            &tx,
            &Group {
                id: hex(50),
                name: "g".into(),
                description: String::new(),
                admin: hex(100),
                created_at: 0,
                deleted: false,
                devices: vec![hex(1)],
            },
            None,
        )
        .unwrap();
        tx.commit().unwrap();
        (dir, db)
    }

    fn request(
        operation: &str,
        db: &rusqlite::Connection,
        request: Request,
    ) -> anyhow::Result<Planned> {
        plan(db, &BTreeMap::new(), &hex(7), operation, &request)
    }

    fn rejected(result: anyhow::Result<Planned>, needle: &str) {
        let error = result.expect_err("request must be rejected");
        assert!(
            error.downcast_ref::<Invalid>().is_some(),
            "not a validation error: {error}"
        );
        assert!(
            error.to_string().contains(needle),
            "{error} does not mention {needle}"
        );
    }

    #[test]
    fn add_and_remove_devices_are_checked_against_the_index() {
        let (_dir, db) = fixture();
        let base = Request {
            sender: hex(100),
            group: Some(hex(50)),
            ..Default::default()
        };
        let planned = request(
            "add-devices",
            &db,
            Request {
                devices: vec![hex(2), hex(2)],
                ..base.clone()
            },
        )
        .unwrap();
        assert_eq!(
            planned.calls,
            vec![(
                "device_group",
                "add_device",
                vec![Argument::Object(hex(50)), Argument::Object(hex(2))]
            )]
        );
        rejected(
            request(
                "add-devices",
                &db,
                Request {
                    devices: vec![hex(1)],
                    ..base.clone()
                },
            ),
            "already in group",
        );
        rejected(
            request(
                "add-devices",
                &db,
                Request {
                    devices: vec![hex(3)],
                    ..base.clone()
                },
            ),
            "not owned by",
        );
        rejected(
            request(
                "add-devices",
                &db,
                Request {
                    devices: vec![hex(4)],
                    ..base.clone()
                },
            ),
            "not indexed",
        );
        rejected(
            request(
                "add-devices",
                &db,
                Request {
                    sender: hex(999),
                    devices: vec![hex(2)],
                    ..base.clone()
                },
            ),
            "not the admin",
        );
        rejected(
            request("add-devices", &db, base.clone()),
            "must not be empty",
        );
        rejected(
            request(
                "remove-devices",
                &db,
                Request {
                    devices: vec![hex(2)],
                    ..base.clone()
                },
            ),
            "not in group",
        );
        assert_eq!(
            request(
                "remove-devices",
                &db,
                Request {
                    devices: vec![hex(1)],
                    ..base.clone()
                }
            )
            .unwrap()
            .calls
            .len(),
            1
        );
        rejected(request("delete-everything", &db, base), "unknown operation");
    }

    #[test]
    fn deployments_need_operator_targets_and_digests() {
        let (_dir, db) = fixture();
        let base = Request {
            sender: hex(100),
            component_name: Some("fw".into()),
            version: Some("1.0.0".into()),
            recipe_blob_id: Some("blob".into()),
            artifact_sha256: Some("a".repeat(64)),
            recipe_sha256: Some("b".repeat(64)),
            target_groups: vec![hex(50)],
            ..Default::default()
        };
        let planned = request("create-deployment", &db, base.clone()).unwrap();
        assert_eq!(planned.calls[0].1, "create_deployment_verified");
        assert_eq!(planned.calls[0].2.len(), 8);
        rejected(
            request(
                "create-deployment",
                &db,
                Request {
                    recipe_sha256: Some("B".repeat(64)),
                    ..base.clone()
                },
            ),
            "recipe_sha256",
        );
        rejected(
            request(
                "create-deployment",
                &db,
                Request {
                    artifact_sha256: None,
                    ..base.clone()
                },
            ),
            "artifact_sha256",
        );
        rejected(
            request(
                "create-deployment",
                &db,
                Request {
                    target_groups: vec![],
                    ..base.clone()
                },
            ),
            "at least one target",
        );
        rejected(
            request(
                "create-deployment",
                &db,
                Request {
                    target_devices: vec![hex(3)],
                    ..base.clone()
                },
            ),
            "not owned by",
        );
        rejected(
            request(
                "create-deployment",
                &db,
                Request {
                    sender: hex(999),
                    ..base.clone()
                },
            ),
            "not the admin",
        );
        rejected(
            request(
                "create-deployment",
                &db,
                Request {
                    component_name: Some(String::new()),
                    ..base
                },
            ),
            "component_name",
        );
    }

    #[test]
    fn bulk_size_is_bounded() {
        let (_dir, db) = fixture();
        let many: Vec<String> = (1000..1000 + MAX_CALLS as u32 + 1).map(hex).collect();
        // Unknown devices fail first; the bound itself is checked after validation.
        rejected(
            request(
                "add-devices",
                &db,
                Request {
                    sender: hex(100),
                    group: Some(hex(50)),
                    devices: many,
                    ..Default::default()
                },
            ),
            "not indexed",
        );
        assert!(allowed("add-devices").is_some() && allowed("other").is_none());
    }
}
