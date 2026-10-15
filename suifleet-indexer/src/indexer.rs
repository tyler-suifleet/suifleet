//! Materializes fleet state from typed event streams and authoritative
//! object reads.
//!
//! Each event type is its own `ListEvents` stream with a durable cursor.
//! Events are only hints naming objects to re-read: the stored state comes
//! from the objects themselves (groups, devices, deployment records, target
//! and receipt fields), so a duplicated, reordered or missed event cannot
//! corrupt it, and deleted objects become tombstones. A page's rows, its
//! change-feed entries and its cursor commit in one SQLite transaction.
//!
//! Devices have `store`, so a plain transfer (or wrapping) changes their owner
//! without any fleet event, and `set_meta` edits them silently. A durable
//! reconciliation sweep therefore re-reads indexed devices in ID order, a
//! batch per pass, wrapping around; `index` and `serve` start with a full one.
//! A device that is absent on its first read (wrapped, even in the
//! registering transaction) is kept as a placeholder tombstone for the sweep.
//! Identity changes (SF-008) are their own stream; the operator of an
//! activated device is its DeviceOwnerCap's owner (see `devices`), and the
//! sweep re-reads that cap too, since it also transfers without events.
use crate::chain::{self, Chain};
use crate::devices;
use crate::manifest::Manifest;
use crate::store::{self, Deployment, Device, Group};
use rusqlite::Connection;
use serde_json::Value;
use std::collections::BTreeMap;
use tracing::{info, warn};

/// Which object an event names, and how to refresh it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Kind {
    Device,
    Group,
    Deployment,
}

/// (event type, the field naming the object, the object kind)
const STREAMS: &[(&str, &str, Kind)] = &[
    (
        "device_registry::DeviceRegistered",
        "device_id",
        Kind::Device,
    ),
    (
        "device_registry::DeviceDeregistered",
        "device_id",
        Kind::Device,
    ),
    (
        "device_registry::DeviceIdentityChanged",
        "device_id",
        Kind::Device,
    ),
    ("device_group::GroupCreated", "group_id", Kind::Group),
    ("device_group::DeviceAddedToGroup", "group_id", Kind::Group),
    (
        "device_group::DeviceRemovedFromGroup",
        "group_id",
        Kind::Group,
    ),
    ("device_group::GroupDeleted", "group_id", Kind::Group),
    (
        "deployment_manager::DeploymentCreated",
        "deployment_id",
        Kind::Deployment,
    ),
    (
        "deployment_manager::DeploymentDeleted",
        "deployment_id",
        Kind::Deployment,
    ),
    (
        "deployment_manager::DeviceStatusReported",
        "deployment_id",
        Kind::Deployment,
    ),
];

pub struct Indexer {
    chain: Chain,
    db: Connection,
    /// "module::Type" → full Move type (introducing package::module::Type).
    types: BTreeMap<String, String>,
    registry: String,
    /// Devices the reconciliation sweep re-reads per pass.
    pub reconcile_batch: usize,
}

/// Receipts authority version of the contracts this indexer targets (as the
/// dApp's RECEIPTS_AUTHORITY_VERSION). Lower versions were written by older
/// packages that accept reports from revoked or rotated-out keys.
pub const RECEIPTS_AUTHORITY_VERSION: u64 = 2;

/// Default devices re-read per pass (at a 2 s interval, ~15k devices/minute).
pub const RECONCILE_BATCH: usize = 500;
/// Meta keys: the sweep's position (last device ID read; "" = start) and when
/// it last completed a full pass over every indexed device.
const SWEEP_AFTER: &str = "reconcile_devices_after";
pub const SWEEP_COMPLETED: &str = "devices_reconciled_at";

fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or_default()
}

fn text(value: &Value) -> String {
    value.as_str().unwrap_or_default().to_owned()
}

impl Indexer {
    /// Verify the manifest against the chain and bind the database to its
    /// chain identity. A database bound to another chain (e.g. a reset
    /// network) is refused unless `reset` explicitly wipes it.
    pub async fn open(manifest: &Manifest, db: Connection, reset: bool) -> anyhow::Result<Self> {
        let chain = Chain::new(&manifest.rpc_url)?;
        let types = manifest.verify(&chain).await?;
        let bound = store::meta(&db, "chain_identifier")?;
        if bound
            .as_deref()
            .is_some_and(|bound| bound != manifest.chain_identifier)
        {
            anyhow::ensure!(
                reset,
                "database is bound to chain {} but the manifest names {}; the network may have reset. \
                 Re-run with --reset to discard the old index",
                bound.unwrap_or_default(),
                manifest.chain_identifier
            );
            warn!("Discarding the index of another chain");
            db.execute_batch(
                "DELETE FROM cursors; DELETE FROM devices; DELETE FROM groups; DELETE FROM group_members;
                 DELETE FROM deployments; DELETE FROM deployment_targets; DELETE FROM receipts;
                 DELETE FROM changes; DELETE FROM meta;",
            )?;
        }
        store::set_meta(&db, "chain_identifier", &manifest.chain_identifier)?;
        store::set_meta(&db, "call_package_id", &manifest.call_package_id)?;
        store::set_meta(&db, "package_version", &manifest.package_version)?;
        Ok(Self {
            chain,
            db,
            types,
            registry: chain::normalize(&manifest.shared_objects.registry.id).unwrap_or_default(),
            reconcile_batch: RECONCILE_BATCH,
        })
    }

    fn move_type(&self, name: &str) -> anyhow::Result<&str> {
        self.types
            .get(name)
            .map(String::as_str)
            .ok_or_else(|| anyhow::anyhow!("package has no type {name}"))
    }

    /// Catch every stream up to the chain tip once, then advance the device
    /// sweep by one batch. Returns rows changed.
    pub async fn sync(&mut self) -> anyhow::Result<usize> {
        let mut changed = 0;
        for (event, field, kind) in STREAMS {
            changed += self.sync_stream(event, field, *kind).await?;
        }
        Ok(changed + self.reconcile_devices(self.reconcile_batch).await?.0)
    }

    /// Catch up, then re-read every indexed device from the start: after a
    /// restart, transfers made while stopped are reflected before serving.
    pub async fn sync_full(&mut self) -> anyhow::Result<usize> {
        let mut changed = 0;
        for (event, field, kind) in STREAMS {
            changed += self.sync_stream(event, field, *kind).await?;
        }
        store::set_meta(&self.db, SWEEP_AFTER, "")?;
        loop {
            let (more, wrapped) = self.reconcile_devices(self.reconcile_batch).await?;
            changed += more;
            if wrapped {
                return Ok(changed);
            }
        }
    }

    /// Re-read up to `limit` indexed devices (tombstones included, as an
    /// object can be unwrapped) after the stored sweep position, and commit
    /// their rows, change-feed entries and the new position together.
    /// Returns rows changed and whether this batch completed a full pass.
    pub async fn reconcile_devices(&mut self, limit: usize) -> anyhow::Result<(usize, bool)> {
        let after = store::meta(&self.db, SWEEP_AFTER)?.unwrap_or_default();
        let ids = sweep_ids(&self.db, &after, limit)?;
        let read = devices::read(&self.chain, &self.types, &self.registry, &ids).await?;
        let updates: Vec<Update> = ids
            .iter()
            .zip(read)
            .map(|(id, device)| Update::Device(id.clone(), device))
            .collect();
        let wrapped = ids.len() < limit.max(1);
        let tx = self.db.transaction()?;
        let mut changed = 0;
        for update in &updates {
            changed += update.apply(&tx, None)? as usize;
        }
        if wrapped {
            store::set_meta(&tx, SWEEP_AFTER, "")?;
            store::set_meta(&tx, SWEEP_COMPLETED, &now().to_string())?;
        } else {
            store::set_meta(&tx, SWEEP_AFTER, ids.last().map(String::as_str).unwrap_or(""))?;
        }
        tx.commit()?;
        if changed > 0 {
            info!(changed, "Device sweep found changes without fleet events");
        }
        Ok((changed, wrapped))
    }

    async fn sync_stream(&mut self, event: &str, field: &str, kind: Kind) -> anyhow::Result<usize> {
        let event_type = self.move_type(event)?.to_owned();
        let mut changed = 0;
        loop {
            let cursor = store::cursor(&self.db, event)?;
            let page = self.chain.events(&event_type, &cursor).await?;
            let mut targets: BTreeMap<(Kind, String), Option<u64>> = BTreeMap::new();
            for item in &page.events {
                let id = item.fields[field]
                    .as_str()
                    .and_then(chain::normalize)
                    .ok_or_else(|| {
                        anyhow::anyhow!("{event} event without a valid {field}: {}", item.fields)
                    })?;
                targets.insert((kind, id), item.checkpoint);
            }
            // Read everything first; only then open the write transaction.
            let mut updates = Vec::new();
            for ((kind, id), checkpoint) in &targets {
                updates.push((self.read(*kind, id).await?, *checkpoint));
            }
            let tx = self.db.transaction()?;
            for (update, checkpoint) in &updates {
                changed += update.apply(&tx, *checkpoint)? as usize;
            }
            let progressed = cursor.as_ref() != Some(&page.end);
            store::set_cursor(&tx, event, &page.end, now())?;
            tx.commit()?;
            if !page.has_more {
                return Ok(changed);
            }
            anyhow::ensure!(
                progressed,
                "{event} stream claims more data without advancing"
            );
        }
    }

    /// Re-read every known object, then restart all streams at the node's
    /// lowest retained checkpoint. For a node whose history was pruned past
    /// the stored cursors; objects that were never indexed cannot be found
    /// this way and must be re-registered or read from an archival node.
    pub async fn rebuild_from_objects(&mut self) -> anyhow::Result<usize> {
        let lowest = self
            .chain
            .service_info()
            .await?
            .lowest_available_checkpoint
            .unwrap_or(0);
        let mut known = Vec::new();
        for (kind, table) in [
            (Kind::Device, "devices"),
            (Kind::Group, "groups"),
            (Kind::Deployment, "deployments"),
        ] {
            let mut statement = self.db.prepare(&format!("SELECT id FROM {table}"))?;
            let ids: Vec<String> = statement
                .query_map([], |row| row.get(0))?
                .collect::<Result<_, _>>()?;
            known.extend(ids.into_iter().map(|id| (kind, id)));
        }
        let mut updates = Vec::new();
        for (kind, id) in &known {
            updates.push(self.read(*kind, id).await?);
        }
        let tx = self.db.transaction()?;
        let mut changed = 0;
        for update in &updates {
            changed += update.apply(&tx, None)? as usize;
        }
        for (event, _, _) in STREAMS {
            store::set_cursor(
                &tx,
                event,
                &serde_json::json!({ "checkpoint": lowest }),
                now(),
            )?;
        }
        tx.commit()?;
        warn!(
            objects = known.len(),
            changed,
            lowest,
            "Rebuilt from objects; streams restart at the lowest retained checkpoint"
        );
        Ok(changed)
    }

    async fn read(&self, kind: Kind, id: &str) -> anyhow::Result<Update> {
        Ok(match kind {
            Kind::Device => Update::Device(id.to_owned(), self.read_device(id).await?),
            Kind::Group => Update::Group(id.to_owned(), self.read_group(id).await?),
            Kind::Deployment => {
                let deployment = self.read_deployment(id).await?;
                let receipts = match &deployment {
                    Some(d) if !d.deleted => self.read_receipts(id).await?,
                    _ => Vec::new(),
                };
                Update::Deployment(id.to_owned(), deployment, receipts)
            }
        })
    }

    async fn read_device(&self, id: &str) -> anyhow::Result<Option<Device>> {
        let ids = [id.to_owned()];
        let mut read = devices::read(&self.chain, &self.types, &self.registry, &ids).await?;
        Ok(read.pop().flatten())
    }

    async fn read_group(&self, id: &str) -> anyhow::Result<Option<Group>> {
        let Some(object) = self.chain.object(id).await? else {
            return Ok(None);
        };
        anyhow::ensure!(
            chain::same_type(
                &object.object_type,
                self.move_type("device_group::DeviceGroup")?
            ),
            "{id} is not a DeviceGroup"
        );
        let f = &object.fields;
        Ok(Some(Group {
            id: id.to_owned(),
            name: text(&f["name"]),
            description: text(&f["description"]),
            admin: chain::normalize(&text(&f["admin"])).unwrap_or_default(),
            created_at: chain::number(&f["created_at"]).unwrap_or_default(),
            deleted: false,
            devices: chain::id_list(&f["device_ids"]),
        }))
    }

    async fn field(&self, parent: &str, key: &str) -> anyhow::Result<Option<Value>> {
        match self.types.get(key) {
            Some(key_type) => self.chain.dynamic_field(parent, key_type).await,
            None => Ok(None), // Not introduced by this package version.
        }
    }

    async fn read_deployment(&self, id: &str) -> anyhow::Result<Option<Deployment>> {
        let Some(object) = self.chain.object(id).await? else {
            return Ok(None);
        };
        anyhow::ensure!(
            chain::same_type(
                &object.object_type,
                self.move_type("deployment_manager::DeploymentRecord")?
            ),
            "{id} is not a DeploymentRecord"
        );
        let f = &object.fields;
        let groups = self
            .field(id, "deployment_manager::TargetGroupsKey")
            .await?;
        let devices = self
            .field(id, "deployment_manager::TargetDevicesKey")
            .await?;
        let digest = self
            .field(id, "deployment_manager::RecipeDigestKey")
            .await?;
        Ok(Some(Deployment {
            id: id.to_owned(),
            creator: chain::normalize(&text(&f["creator"])).unwrap_or_default(),
            component_name: text(&f["component_name"]),
            version: text(&f["version"]),
            recipe_blob_id: text(&f["recipe_blob_id"]),
            artifact_sha256: text(&f["artifact_sha256"]),
            recipe_sha256: digest.as_ref().and_then(Value::as_str).map(str::to_owned),
            created_at: chain::number(&f["created_at"]).unwrap_or_default(),
            schema_version: chain::number(&f["schema_version"]).unwrap_or_default(),
            deleted: false,
            target_groups: groups.as_ref().map(chain::id_list).unwrap_or_default(),
            target_devices: devices.as_ref().map(chain::id_list).unwrap_or_default(),
        }))
    }

    /// Authoritative receipts only; legacy StatusesKey entries are forgeable.
    async fn read_receipts(&self, id: &str) -> anyhow::Result<Vec<(String, u8)>> {
        let Some(receipts) = self.field(id, "deployment_manager::ReceiptsKey").await? else {
            return Ok(Vec::new());
        };
        if !current_receipts(id, &receipts)? {
            return Ok(Vec::new());
        }
        let entries = receipts["statuses"]["contents"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("undecodable receipts of {id}: {receipts}"))?;
        entries
            .iter()
            .map(|entry| {
                let device = entry["key"].as_str().and_then(chain::normalize);
                let status = chain::number(&entry["value"]).filter(|s| *s == 1 || *s == 2);
                match (device, status) {
                    (Some(device), Some(status)) => Ok((device, status as u8)),
                    _ => anyhow::bail!("invalid receipt entry of {id}: {entry}"),
                }
            })
            .collect()
    }
}

/// Whether receipts were written under the current report authority. Older
/// versions stay writable through older packages until migrated, which
/// discards them; they are never evidence (as the dApp's `readReceipts`).
fn current_receipts(id: &str, receipts: &Value) -> anyhow::Result<bool> {
    let version = chain::number(&receipts["version"])
        .filter(|v| *v > 0)
        .ok_or_else(|| anyhow::anyhow!("invalid receipts authority version of {id}: {receipts}"))?;
    Ok(version >= RECEIPTS_AUTHORITY_VERSION)
}

/// The next `limit` indexed device IDs after `after`, tombstones and
/// placeholders included.
fn sweep_ids(db: &Connection, after: &str, limit: usize) -> anyhow::Result<Vec<String>> {
    Ok(db
        .prepare("SELECT id FROM devices WHERE id > ?1 ORDER BY id LIMIT ?2")?
        .query_map(rusqlite::params![after, limit.max(1) as i64], |row| {
            row.get(0)
        })?
        .collect::<Result<_, _>>()?)
}

/// An object re-read from chain; None = absent (deleted, wrapped).
enum Update {
    Device(String, Option<Device>),
    Group(String, Option<Group>),
    Deployment(String, Option<Deployment>, Vec<(String, u8)>),
}

impl Update {
    fn apply(&self, tx: &rusqlite::Transaction, checkpoint: Option<u64>) -> anyhow::Result<bool> {
        Ok(match self {
            Update::Device(_, Some(device)) => store::put_device(tx, device, checkpoint)?,
            Update::Device(id, None) => match store::read_device(tx, id)? {
                Some(mut device) if !device.deleted => {
                    device.deleted = true;
                    store::put_device(tx, &device, checkpoint)?
                }
                Some(_) => false, // Already a tombstone.
                None => store::put_device_placeholder(tx, id)?,
            },
            Update::Group(_, Some(group)) => store::put_group(tx, group, checkpoint)?,
            Update::Group(id, None) => match store::read_group(tx, id)? {
                Some(mut group) if !group.deleted => {
                    group.deleted = true;
                    group.devices.clear();
                    store::put_group(tx, &group, checkpoint)?
                }
                _ => false,
            },
            Update::Deployment(_, Some(deployment), receipts) => {
                let a = store::put_deployment(tx, deployment, checkpoint)?;
                let b = store::put_receipts(tx, &deployment.id, receipts, checkpoint)?;
                a || b
            }
            Update::Deployment(id, None, _) => match store::read_deployment(tx, id)? {
                Some(mut deployment) if !deployment.deleted => {
                    deployment.deleted = true;
                    store::put_deployment(tx, &deployment, checkpoint)?
                }
                _ => false,
            },
        })
    }
}

/// Index forever: catch up, then poll every `interval`.
pub async fn run(mut indexer: Indexer, interval: std::time::Duration) -> anyhow::Result<()> {
    loop {
        match indexer.sync().await {
            Ok(0) => {}
            Ok(changed) => info!(changed, "Indexed fleet changes"),
            Err(error) => {
                warn!(%error, "Index pass failed; retrying without advancing the failed stream")
            }
        }
        tokio::time::sleep(interval).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(n: u32) -> String {
        format!("0x{n:064x}")
    }

    fn apply(db: &mut Connection, update: Update) -> bool {
        let tx = db.transaction().unwrap();
        let changed = update.apply(&tx, None).unwrap();
        tx.commit().unwrap();
        changed
    }

    #[test]
    fn only_current_authority_receipts_are_evidence() {
        let receipts = |version: Value| serde_json::json!({ "version": version, "statuses": { "contents": [] } });
        assert!(!current_receipts("d", &receipts(serde_json::json!("1"))).unwrap());
        assert!(current_receipts("d", &receipts(serde_json::json!("2"))).unwrap());
        assert!(current_receipts("d", &receipts(serde_json::json!("3"))).unwrap());
        for invalid in [serde_json::json!("0"), Value::Null, serde_json::json!("x")] {
            assert!(current_receipts("d", &receipts(invalid)).is_err());
        }
    }

    #[test]
    fn device_first_seen_wrapped_is_kept_for_the_sweep() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("fleet.db");
        let mut db = store::open(&path).unwrap();
        // DeviceRegistered names a device that is already wrapped.
        assert!(apply(&mut db, Update::Device(id(1), None)));
        assert!(!apply(&mut db, Update::Device(id(1), None)), "idempotent");
        let placeholder = store::read_device(&db, &id(1)).unwrap().unwrap();
        assert!(placeholder.deleted && placeholder.owner.is_none());
        assert_eq!(store::last_change(&db).unwrap(), 0, "no feed entry");
        assert!(store::list_devices(&db, &store::Filter::default(), None, None)
            .unwrap()
            .items
            .is_empty());
        // Durable: a restarted indexer's sweep still selects the ID.
        drop(db);
        let mut db = store::open(&path).unwrap();
        assert_eq!(sweep_ids(&db, "", 10).unwrap(), [id(1)]);
        // Unwrapped later without any fleet event: the sweep's read revives it.
        let device = Device {
            id: id(1),
            owner: Some(id(100)),
            name: "wrapped".into(),
            device_address: id(200),
            arch: "aarch64".into(),
            registered_at: 7,
            deleted: false,
            activated: true,
            revoked: false,
            signer: Some(id(200)),
            owner_cap: Some(id(300)),
            groups: vec![],
        };
        assert!(apply(&mut db, Update::Device(id(1), Some(device.clone()))));
        assert_eq!(store::read_device(&db, &id(1)).unwrap().unwrap(), device);
        let changes = store::changes_after(&db, 0, None).unwrap();
        assert_eq!(
            changes.iter().map(|c| c.kind.as_str()).collect::<Vec<_>>(),
            ["device.updated"]
        );
        // Wrapped again: a regular tombstone, not overwritten by a placeholder.
        assert!(apply(&mut db, Update::Device(id(1), None)));
        let tombstone = store::read_device(&db, &id(1)).unwrap().unwrap();
        assert!(tombstone.deleted && tombstone.name == "wrapped");
    }
}
