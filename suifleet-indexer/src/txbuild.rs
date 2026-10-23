//! Unsigned bulk-operation transactions. Requests are validated against the
//! indexed state, then built (gas, object versions and budget resolved by the
//! node's simulation) for the operator to sign elsewhere: a wallet, or
//! `suifleet-indexer ctl`, which re-checks the transaction before signing.
//!
//! An activated device (SF-008) is in registry custody: `add_device` gets it
//! through `borrow_device` with the operator's DeviceOwnerCap, and the same
//! transaction returns it with `return_device`.
use crate::chain::{self, Chain};
use crate::store::{self, Device};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::str::FromStr;
use sui_sdk_types::Address;
use sui_transaction_builder::{Function, ObjectInput, TransactionBuilder};

/// Most devices in one bulk transaction (up to five calls each).
pub const MAX_CALLS: usize = 200;

/// A device's inbox (SF-125): its derived ID and whether it exists yet.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Inbox {
    pub id: String,
    pub exists: bool,
}

/// Device → inbox, for the devices a request names; None when the package
/// has no inboxes, and every operation keeps its previous calls.
pub type Inboxes = Option<BTreeMap<String, Inbox>>;

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

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Argument {
    Object(String),
    Clock,
    Text(String),
    /// An object ID passed by value (pure), not the object itself.
    Id(String),
    Ids(Vec<String>),
    /// Result `.1` of the transaction's command `.0`.
    Result(u16, u16),
}

/// One Move call into the fleet package: module, function, arguments.
pub type Call = (&'static str, &'static str, Vec<Argument>);

#[derive(Debug, Clone)]
pub struct Planned {
    pub sender: String,
    pub package: String,
    pub calls: Vec<Call>,
    pub summary: Value,
}

/// The bulk operations.
pub const OPERATIONS: &[&str] = &["add-devices", "remove-devices", "create-deployment"];

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

/// The exact calls `request` asks for, from the request, the manifest's
/// `registry` and `owner_caps` (device → DeviceOwnerCap of each activated
/// device, read from chain) alone: the API builds these after validating them
/// against the index, and `ctl` rebuilds them from the operator's own request
/// and its own chain reads to check what the API returned before signing it.
///
/// With inboxes, deployments are announced to every target group (plan
/// checks the sender administers them) and every activated target device
/// (by its owner cap), and membership changes list the group in the
/// device's inbox; an inbox that does not exist yet is created and shared
/// in the same transaction.
pub fn calls(
    operation: &str,
    request: &Request,
    registry: &str,
    owner_caps: &BTreeMap<String, String>,
    inboxes: &Inboxes,
) -> anyhow::Result<Vec<Call>> {
    let inbox_of = |device: &str| inboxes.as_ref().and_then(|all| all.get(device));
    if !OPERATIONS.contains(&operation) {
        return Err(invalid(format!("unknown operation {operation}")));
    }
    let calls: Vec<Call> = match operation {
        "add-devices" | "remove-devices" => {
            let group = id(
                request
                    .group
                    .as_deref()
                    .ok_or_else(|| invalid("group is required"))?,
                "group",
            )?;
            let devices = ids(&request.devices, "device")?;
            if devices.is_empty() {
                return Err(invalid("devices must not be empty"));
            }
            if devices.len() > MAX_CALLS {
                return Err(invalid(format!(
                    "at most {MAX_CALLS} devices per transaction; split the request"
                )));
            }
            let mut calls: Vec<Call> = Vec::new();
            for device in devices {
                if operation == "remove-devices" {
                    match inbox_of(&device) {
                        Some(inbox) if inbox.exists => calls.push((
                            "fleet_inbox",
                            "remove_member",
                            vec![Argument::Object(group.clone()), Argument::Object(inbox.id.clone())],
                        )),
                        _ => calls.push((
                            "device_group",
                            "remove_device",
                            vec![Argument::Object(group.clone()), Argument::Id(device)],
                        )),
                    }
                    continue;
                }
                let Some(cap) = owner_caps.get(&device) else {
                    calls.push((
                        "device_group",
                        "add_device",
                        vec![Argument::Object(group.clone()), Argument::Object(device)],
                    ));
                    continue;
                };
                let borrow = calls.len() as u16;
                let registry = id(registry, "registry")?;
                calls.push((
                    "device_registry",
                    "borrow_device",
                    vec![Argument::Object(registry.clone()), Argument::Object(cap.clone())],
                ));
                match inbox_of(&device) {
                    Some(inbox) => with_inbox(&mut calls, inbox, &registry, cap, |inbox| (
                        "fleet_inbox",
                        "add_member",
                        vec![Argument::Object(group.clone()), inbox, Argument::Result(borrow, 0)],
                    )),
                    None => calls.push((
                        "device_group",
                        "add_device",
                        vec![Argument::Object(group.clone()), Argument::Result(borrow, 0)],
                    )),
                }
                calls.push((
                    "device_registry",
                    "return_device",
                    vec![
                        Argument::Object(registry),
                        Argument::Result(borrow, 0),
                        Argument::Result(borrow, 1),
                    ],
                ));
            }
            calls
        }
        "create-deployment" => {
            let groups = ids(&request.target_groups, "target group")?;
            let devices = ids(&request.target_devices, "target device")?;
            if groups.is_empty() && devices.is_empty() {
                return Err(invalid("a deployment needs at least one target"));
            }
            let function = if inboxes.is_some() { "new_deployment_verified" } else { "create_deployment_verified" };
            let mut calls: Vec<Call> = vec![(
                "deployment_manager",
                function,
                vec![
                    Argument::Text(text(&request.component_name, "component_name")?),
                    Argument::Text(text(&request.version, "version")?),
                    Argument::Text(text(&request.recipe_blob_id, "recipe_blob_id")?),
                    Argument::Text(hex64(&request.artifact_sha256, "artifact_sha256")?),
                    Argument::Text(hex64(&request.recipe_sha256, "recipe_sha256")?),
                    Argument::Ids(groups.clone()),
                    Argument::Ids(devices.clone()),
                    Argument::Clock,
                ],
            )];
            if inboxes.is_some() {
                // Announce the unshared record (command 0), then share it.
                for group in groups {
                    calls.push(("fleet_inbox", "announce_group",
                        vec![Argument::Object(group), Argument::Result(0, 0), Argument::Clock]));
                }
                let registry = id(registry, "registry")?;
                for device in devices {
                    let (Some(inbox), Some(cap)) = (inbox_of(&device), owner_caps.get(&device)) else { continue };
                    with_inbox(&mut calls, inbox, &registry, cap, |inbox| ("fleet_inbox", "announce_device", vec![
                        inbox, Argument::Object(registry.clone()), Argument::Object(cap.clone()),
                        Argument::Result(0, 0), Argument::Clock]));
                }
                calls.push(("deployment_manager", "share_deployment", vec![Argument::Result(0, 0)]));
            }
            calls
        }
        _ => unreachable!(),
    };
    Ok(calls)
}

/// Push `call` with the device's inbox, first creating it (and sharing it
/// after) when it does not exist yet.
fn with_inbox(calls: &mut Vec<Call>, inbox: &Inbox, registry: &str, cap: &str,
    call: impl FnOnce(Argument) -> Call) {
    if inbox.exists {
        calls.push(call(Argument::Object(inbox.id.clone())));
        return;
    }
    let created = calls.len() as u16;
    calls.push(("fleet_inbox", "new_inbox",
        vec![Argument::Object(registry.to_owned()), Argument::Object(cap.to_owned())]));
    calls.push(call(Argument::Result(created, 0)));
    calls.push(("fleet_inbox", "share_inbox", vec![Argument::Result(created, 0)]));
}

/// The device IDs whose inboxes a request needs.
pub fn inbox_devices(operation: &str, request: &Request) -> Vec<String> {
    let named = match operation {
        "add-devices" | "remove-devices" => &request.devices,
        "create-deployment" => &request.target_devices,
        _ => return Vec::new(),
    };
    ids(named, "device").unwrap_or_default()
}

/// The device `id` as read from chain for this request (`live`), provided
/// the index knows it: the index's owner may be stale (a transfer of the
/// Device or its owner cap emits no fleet event until the sweep).
fn live_device(
    db: &rusqlite::Connection,
    live: &BTreeMap<String, Option<Device>>,
    id: &str,
    what: &str,
) -> anyhow::Result<Device> {
    let indexed = store::read_device(db, id)?.filter(|d| !d.deleted);
    match (indexed, live.get(id)) {
        (Some(_), Some(Some(device))) => Ok(device.clone()),
        _ => Err(invalid(format!(
            "{what} {id} is not indexed, was deregistered or is wrapped"
        ))),
    }
}

/// Validate `request` for `operation` against the index. `live` holds the
/// request's devices (`devices_to_check`), read from chain by the caller.
pub fn plan(
    db: &rusqlite::Connection,
    package: &str,
    registry: &str,
    operation: &str,
    request: &Request,
    live: &BTreeMap<String, Option<Device>>,
    inboxes: &Inboxes,
) -> anyhow::Result<Planned> {
    let calls = calls(operation, request, registry, &crate::devices::owner_caps(live), inboxes)?;
    let sender = id(&request.sender, "sender")?;
    let package = id(package, "package")?;
    let summary = match operation {
        "add-devices" | "remove-devices" => {
            let group = admin_group(db, &request.group, &sender)?;
            let devices = ids(&request.devices, "device")?;
            for device in &devices {
                let member = group.devices.contains(device);
                if operation == "add-devices" {
                    let owner = live_device(db, live, device, "device")?.owner;
                    if owner.as_deref() != Some(sender.as_str()) {
                        return Err(invalid(format!("device {device} is not owned by {sender}")));
                    }
                    if member {
                        return Err(invalid(format!(
                            "device {device} is already in group {}",
                            group.id
                        )));
                    }
                } else if !member {
                    return Err(invalid(format!(
                        "device {device} is not in group {}",
                        group.id
                    )));
                }
            }
            json!({ "operation": operation, "group": group.id, "devices": devices })
        }
        "create-deployment" => {
            let groups = ids(&request.target_groups, "target group")?;
            let devices = ids(&request.target_devices, "target device")?;
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
                let found = live_device(db, live, device, "target device")?;
                if found.owner.as_deref() != Some(sender.as_str()) {
                    return Err(invalid(format!("devices only accept their operator's deployments; {device} is not owned by {sender}")));
                }
                if !found.activated || found.revoked {
                    return Err(invalid(format!(
                        "target device {device} has no active registry identity (activate or reinstate it); \
                         devices only apply deployments while active"
                    )));
                }
            }
            json!({ "operation": operation, "component_name": request.component_name,
                    "version": request.version, "target_groups": groups, "target_devices": devices })
        }
        _ => unreachable!(),
    };
    Ok(Planned {
        sender,
        package,
        calls,
        summary,
    })
}

/// The device IDs `plan` needs read from chain for `request`.
pub fn devices_to_check(operation: &str, request: &Request) -> Vec<String> {
    let named = match operation {
        "add-devices" => &request.devices,
        "create-deployment" => &request.target_devices,
        _ => return Vec::new(),
    };
    ids(named, "device").unwrap_or_default()
}

/// Build the planned calls into a sign-ready transaction.
pub async fn build(chain: &Chain, planned: Planned) -> anyhow::Result<Value> {
    let package = Address::from_str(&planned.package)?;
    let sender = Address::from_str(&planned.sender)?;
    let mut builder = TransactionBuilder::new();
    let mut results = Vec::new();
    for (module, function, arguments) in &planned.calls {
        let mut inputs = Vec::new();
        for argument in arguments {
            inputs.push(match argument {
                Argument::Result(command, index) => {
                    let result: &sui_transaction_builder::Argument = results
                        .get(*command as usize)
                        .ok_or_else(|| anyhow::anyhow!("result of a later command"))?;
                    result.to_nested(*index as usize + 1)[*index as usize]
                }
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
        results.push(builder.move_call(
            Function::new(package, module.parse()?, function.parse()?),
            inputs,
        ));
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
        // 2 was never activated (address-owned); 5 is activated but revoked.
        for (n, owner) in [(1, 100), (2, 100), (3, 999), (4, 100), (5, 100)] {
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
                    activated: n != 2,
                    revoked: n == 5,
                    signer: (n != 2).then(|| hex(n + 500)),
                    owner_cap: (n != 2).then(|| hex(n + 800)),
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

    const REGISTRY: u32 = 60;

    /// The request's devices as chain reads would return them: as indexed.
    fn live(
        db: &rusqlite::Connection,
        operation: &str,
        request: &Request,
    ) -> BTreeMap<String, Option<Device>> {
        devices_to_check(operation, request)
            .into_iter()
            .map(|id| {
                let device = store::read_device(db, &id).unwrap().filter(|d| !d.deleted);
                (id, device)
            })
            .collect()
    }

    fn request(
        operation: &str,
        db: &rusqlite::Connection,
        request: Request,
    ) -> anyhow::Result<Planned> {
        let live = live(db, operation, &request);
        plan(db, &hex(7), &hex(REGISTRY), operation, &request, &live, &None)
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
    fn inboxes_announce_deployments_and_list_groups() {
        let (registry, group, cap) = (hex(REGISTRY), hex(50), hex(801));
        let caps = BTreeMap::from([(hex(1), cap.clone()), (hex(4), hex(804))]);
        let inboxes: Inboxes = Some(BTreeMap::from([
            (hex(1), Inbox { id: hex(901), exists: true }),
            (hex(4), Inbox { id: hex(904), exists: false }),
        ]));
        let deploy = Request {
            sender: hex(100), component_name: Some("app".into()), version: Some("1".into()),
            recipe_blob_id: Some("blob".into()), artifact_sha256: Some("a".repeat(64)),
            recipe_sha256: Some("b".repeat(64)), target_groups: vec![group.clone()],
            target_devices: vec![hex(1), hex(4), hex(9)], ..Default::default()
        };
        let planned = calls("create-deployment", &deploy, &registry, &caps, &inboxes).unwrap();
        let names: Vec<_> = planned.iter().map(|(m, f, _)| format!("{m}::{f}")).collect();
        assert_eq!(names, [
            "deployment_manager::new_deployment_verified", "fleet_inbox::announce_group",
            "fleet_inbox::announce_device", "fleet_inbox::new_inbox", "fleet_inbox::announce_device",
            "fleet_inbox::share_inbox", "deployment_manager::share_deployment",
        ]);
        assert_eq!(planned[1].2, vec![Argument::Object(group.clone()), Argument::Result(0, 0), Argument::Clock]);
        assert_eq!(planned[2].2, vec![Argument::Object(hex(901)), Argument::Object(registry.clone()),
            Argument::Object(cap.clone()), Argument::Result(0, 0), Argument::Clock]);
        assert_eq!(planned[3].2, vec![Argument::Object(registry.clone()), Argument::Object(hex(804))]);
        assert_eq!(planned[4].2[0], Argument::Result(3, 0));
        assert_eq!(planned[5].2, vec![Argument::Result(3, 0)]);
        // Without inboxes the deployment stays one call.
        let plain = calls("create-deployment", &deploy, &registry, &caps, &None).unwrap();
        assert_eq!(plain.len(), 1);
        assert_eq!(plain[0].1, "create_deployment_verified");

        let add = Request { sender: hex(100), group: Some(group.clone()), devices: vec![hex(1), hex(4)], ..Default::default() };
        let planned = calls("add-devices", &add, &registry, &caps, &inboxes).unwrap();
        let names: Vec<_> = planned.iter().map(|(m, f, _)| format!("{m}::{f}")).collect();
        assert_eq!(names, [
            "device_registry::borrow_device", "fleet_inbox::add_member", "device_registry::return_device",
            "device_registry::borrow_device", "fleet_inbox::new_inbox", "fleet_inbox::add_member",
            "fleet_inbox::share_inbox", "device_registry::return_device",
        ]);
        assert_eq!(planned[1].2, vec![Argument::Object(group.clone()), Argument::Object(hex(901)), Argument::Result(0, 0)]);
        assert_eq!(planned[5].2, vec![Argument::Object(group.clone()), Argument::Result(4, 0), Argument::Result(3, 0)]);
        let remove = Request { devices: vec![hex(1), hex(4)], ..add };
        let planned = calls("remove-devices", &remove, &registry, &caps, &inboxes).unwrap();
        assert_eq!(planned, vec![
            ("fleet_inbox", "remove_member", vec![Argument::Object(group.clone()), Argument::Object(hex(901))]),
            ("device_group", "remove_device", vec![Argument::Object(group), Argument::Id(hex(4))]),
        ]);
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
                    devices: vec![hex(9)],
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
        // Direct targets need an active identity: activated and not revoked.
        assert!(request(
            "create-deployment",
            &db,
            Request {
                target_devices: vec![hex(4)],
                ..base.clone()
            }
        )
        .is_ok());
        for inactive in [2, 5] {
            rejected(
                request(
                    "create-deployment",
                    &db,
                    Request {
                        target_devices: vec![hex(inactive)],
                        ..base.clone()
                    },
                ),
                "no active registry identity",
            );
        }
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
            "at most",
        );
        assert!(OPERATIONS.contains(&"add-devices") && !OPERATIONS.contains(&"other"));
    }

    #[test]
    fn custodied_devices_are_borrowed_with_their_owner_cap() {
        let (_dir, db) = fixture();
        let planned = request(
            "add-devices",
            &db,
            Request {
                sender: hex(100),
                group: Some(hex(50)),
                devices: vec![hex(4), hex(2)],
                ..Default::default()
            },
        )
        .unwrap();
        // Sorted by ID: 2 is address-owned and passed directly; 4 is in custody.
        assert_eq!(
            planned.calls,
            vec![
                (
                    "device_group",
                    "add_device",
                    vec![Argument::Object(hex(50)), Argument::Object(hex(2))]
                ),
                (
                    "device_registry",
                    "borrow_device",
                    vec![Argument::Object(hex(REGISTRY)), Argument::Object(hex(804))]
                ),
                (
                    "device_group",
                    "add_device",
                    vec![Argument::Object(hex(50)), Argument::Result(1, 0)]
                ),
                (
                    "device_registry",
                    "return_device",
                    vec![
                        Argument::Object(hex(REGISTRY)),
                        Argument::Result(1, 0),
                        Argument::Result(1, 1)
                    ]
                ),
            ]
        );
    }

    #[test]
    fn live_owners_override_a_stale_index() {
        let (_dir, db) = fixture();
        let add = |device: u32, owner: Option<u32>| {
            let request = Request {
                sender: hex(100),
                group: Some(hex(50)),
                devices: vec![hex(device)],
                ..Default::default()
            };
            let mut live = live(&db, "add-devices", &request);
            for found in live.values_mut() {
                match owner {
                    Some(owner) => found.as_mut().unwrap().owner = Some(hex(owner)),
                    None => *found = None,
                }
            }
            plan(&db, &hex(7), &hex(REGISTRY), "add-devices", &request, &live, &None)
        };
        // Indexed as 999's, but its owner cap moved to the sender without a fleet event.
        assert!(add(3, Some(100)).is_ok());
        // Indexed as the sender's, but transferred away.
        rejected(add(2, Some(999)), "not owned by");
        // Indexed, but now wrapped or deleted.
        rejected(add(2, None), "is wrapped");
        assert_eq!(
            devices_to_check(
                "create-deployment",
                &Request {
                    target_devices: vec!["0x3".into(), hex(3)],
                    ..Default::default()
                }
            ),
            vec![hex(3)]
        );
    }
}
