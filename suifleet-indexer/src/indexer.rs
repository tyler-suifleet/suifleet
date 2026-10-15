//! Materializes fleet state from typed event streams and authoritative
//! object reads.
//!
//! Each event type is its own `ListEvents` stream with a durable cursor.
//! Events are only hints naming objects to re-read: the stored state comes
//! from the objects themselves (groups, devices, deployment records, target
//! and receipt fields), so a duplicated, reordered or missed event cannot
//! corrupt it, and deleted objects become tombstones. A page's rows, its
//! change-feed entries and its cursor commit in one SQLite transaction.
use crate::chain::{self, Chain, Owner};
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
}

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
        Ok(Self { chain, db, types })
    }

    fn move_type(&self, name: &str) -> anyhow::Result<&str> {
        self.types
            .get(name)
            .map(String::as_str)
            .ok_or_else(|| anyhow::anyhow!("package has no type {name}"))
    }

    /// Catch every stream up to the chain tip once. Returns rows changed.
    pub async fn sync(&mut self) -> anyhow::Result<usize> {
        let mut changed = 0;
        for (event, field, kind) in STREAMS {
            changed += self.sync_stream(event, field, *kind).await?;
        }
        Ok(changed)
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
        let Some(object) = self.chain.object(id).await? else {
            return Ok(None);
        };
        anyhow::ensure!(
            chain::same_type(
                &object.object_type,
                self.move_type("device_registry::Device")?
            ),
            "{id} is not a Device"
        );
        let f = &object.fields;
        Ok(Some(Device {
            id: id.to_owned(),
            owner: match &object.owner {
                Owner::Address(owner) => chain::normalize(owner),
                _ => None,
            },
            name: text(&f["name"]),
            device_address: chain::normalize(&text(&f["device_address"])).unwrap_or_default(),
            arch: text(&f["arch"]),
            registered_at: chain::number(&f["registered_at"]).unwrap_or_default(),
            deleted: false,
            groups: vec![],
        }))
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
                _ => false, // Never seen alive, or already a tombstone.
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
