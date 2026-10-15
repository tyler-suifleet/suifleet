//! Devices as the daemon trusts them (SF-008). `activate_device` moves a
//! Device into registry custody (a dynamic object field) and records its
//! identity on the registry: the trusted signer, a revocation flag and the
//! DeviceOwnerCap. Whoever owns that cap is the device's operator, the only
//! address whose deployments it applies. A Device that was never activated is
//! address-owned and untrusted: it can join groups but takes no deployments.
//!
//! The cap has `store`, so it can be transferred (or wrapped) without any
//! fleet event, like an unactivated Device; the indexer's sweep re-reads both.
use crate::chain::{self, Chain, Object, Owner};
use crate::store::Device;
use serde_json::Value;
use std::collections::BTreeMap;

fn text(value: &Value) -> String {
    value.as_str().unwrap_or_default().to_owned()
}

fn move_type<'a>(types: &'a BTreeMap<String, String>, name: &str) -> anyhow::Result<&'a str> {
    types
        .get(name)
        .map(String::as_str)
        .ok_or_else(|| anyhow::anyhow!("package has no type {name}"))
}

/// A registry identity: (signer, revoked, owner cap).
type Identity = (String, bool, String);

fn identity(id: &str, field: Option<Object>) -> anyhow::Result<Option<Identity>> {
    let Some(field) = field else {
        return Ok(None);
    };
    let value = &field.fields["value"];
    let signer = chain::normalize(&text(&value["signer"]));
    let cap = chain::normalize(&text(&value["owner_cap"]));
    match (signer, value["revoked"].as_bool(), cap) {
        (Some(signer), Some(revoked), Some(cap)) => Ok(Some((signer, revoked, cap))),
        _ => anyhow::bail!("undecodable identity of {id}: {value}"),
    }
}

/// Read `ids` from chain (batched): None where the Device is absent (deleted,
/// wrapped), otherwise its fields, custody, identity and operator.
pub async fn read(
    chain: &Chain,
    types: &BTreeMap<String, String>,
    registry: &str,
    ids: &[String],
) -> anyhow::Result<Vec<Option<Device>>> {
    let device_type = move_type(types, "device_registry::Device")?;
    let identity_key = move_type(types, "device_registry::IdentityKey")?;
    let custody_key = move_type(types, "device_registry::CustodyKey")?;
    let cap_type = move_type(types, "device_registry::DeviceOwnerCap")?;
    let objects = chain.objects(ids).await?;
    for (id, object) in ids.iter().zip(&objects) {
        if let Some(object) = object {
            anyhow::ensure!(
                chain::same_type(&object.object_type, device_type),
                "{id} is not a Device"
            );
        }
    }
    let live: Vec<&String> = ids
        .iter()
        .zip(&objects)
        .filter(|(_, o)| o.is_some())
        .map(|(id, _)| id)
        .collect();
    let field_ids = live
        .iter()
        .map(|id| chain::field_id(registry, identity_key, &chain::id_bytes(id)?))
        .collect::<anyhow::Result<Vec<_>>>()?;
    let mut identities = BTreeMap::new();
    for (id, field) in live.iter().zip(chain.objects(&field_ids).await?) {
        if let Some(found) = identity(id, field)? {
            identities.insert((*id).clone(), found);
        }
    }
    let cap_ids: Vec<String> = identities.values().map(|(_, _, cap)| cap.clone()).collect();
    let mut operators = BTreeMap::new();
    for ((device, _), cap) in identities.iter().zip(chain.objects(&cap_ids).await?) {
        // A cap that is deleted, wrapped or held by an object has no operator.
        let operator = match cap {
            Some(cap) => {
                anyhow::ensure!(
                    chain::same_type(&cap.object_type, cap_type)
                        && chain::same_id(&text(&cap.fields["device_id"]), device),
                    "the identity of {device} names a wrong DeviceOwnerCap"
                );
                match &cap.owner {
                    Owner::Address(owner) => chain::normalize(owner),
                    _ => None,
                }
            }
            None => None,
        };
        operators.insert(device.clone(), operator);
    }
    ids.iter()
        .zip(objects)
        .map(|(id, object)| {
            let Some(object) = object else {
                return Ok(None);
            };
            let custody = chain::object_field_id(registry, custody_key, id)?;
            let custodied = matches!(&object.owner, Owner::Object(parent) if chain::same_id(parent, &custody));
            let identity = identities.get(id);
            let activated = custodied && identity.is_some();
            let owner = if activated {
                operators.get(id).cloned().flatten()
            } else {
                match &object.owner {
                    Owner::Address(owner) => chain::normalize(owner),
                    _ => None,
                }
            };
            let f = &object.fields;
            Ok(Some(Device {
                id: id.clone(),
                owner,
                name: text(&f["name"]),
                device_address: chain::normalize(&text(&f["device_address"])).unwrap_or_default(),
                arch: text(&f["arch"]),
                registered_at: chain::number(&f["registered_at"]).unwrap_or_default(),
                deleted: false,
                activated,
                revoked: identity.is_some_and(|(_, revoked, _)| *revoked),
                signer: identity.map(|(signer, _, _)| signer.clone()),
                owner_cap: identity.map(|(_, _, cap)| cap.clone()),
                groups: vec![],
            }))
        })
        .collect()
}

/// Owner caps of the activated devices among `devices` (device → cap).
pub fn owner_caps(devices: &BTreeMap<String, Option<Device>>) -> BTreeMap<String, String> {
    devices
        .iter()
        .filter_map(|(id, device)| {
            let device = device.as_ref().filter(|d| d.activated)?;
            Some((id.clone(), device.owner_cap.clone()?))
        })
        .collect()
}
