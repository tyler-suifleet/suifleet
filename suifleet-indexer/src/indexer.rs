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
        "deployment_manager::ReceiptLayoutChanged",
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
    last_reconcile: std::time::Instant,
    pub reconcile_interval: std::time::Duration,
}

/// Receipts authority version of the contracts this indexer targets (as the
/// dApp's RECEIPTS_AUTHORITY_VERSION). Lower versions were written by older
/// packages that accept reports from revoked or rotated-out keys.
pub const RECEIPTS_AUTHORITY_VERSION: u64 = 2;

/// Bounded device batch, independently scheduled every ten minutes.
pub const RECONCILE_BATCH: usize = 500;
pub const RECONCILE_INTERVAL: std::time::Duration = std::time::Duration::from_secs(600);
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
    /// deployment (see `bind`). An index of another chain or deployment is
    /// refused unless `reset` explicitly wipes it.
    pub async fn open(manifest: &Manifest, mut db: Connection, reset: bool) -> anyhow::Result<Self> {
        let chain = Chain::new(&manifest.rpc_url)?;
        let types = manifest.verify(&chain).await?;
        let registry = chain::normalize(&manifest.shared_objects.registry.id).unwrap_or_default();
        bind(&mut db, &manifest.chain_identifier, &registry, &types, reset)?;
        store::set_meta(&db, "call_package_id", &manifest.call_package_id)?;
        store::set_meta(&db, "package_version", &manifest.package_version)?;
        Ok(Self {
            chain,
            db,
            types,
            registry,
            reconcile_batch: RECONCILE_BATCH,
            last_reconcile: std::time::Instant::now(),
            reconcile_interval: RECONCILE_INTERVAL,
        })
    }

    fn move_type(&self, name: &str) -> anyhow::Result<&str> {
        self.types
            .get(name)
            .map(String::as_str)
            .ok_or_else(|| anyhow::anyhow!("package has no type {name}"))
    }

    /// Catch every stream up to the chain tip once; reconcile devices only
    /// when their independent ten-minute timer is due. Returns rows changed.
    pub async fn sync(&mut self) -> anyhow::Result<usize> {
        let mut changed = 0;
        for (event, field, kind) in STREAMS {
            changed += self.sync_stream(event, field, *kind).await?;
        }
        if self.last_reconcile.elapsed() >= self.reconcile_interval {
            loop {
                let (more, wrapped) = self.reconcile_devices(self.reconcile_batch).await?;
                changed += more;
                if wrapped {
                    break;
                }
            }
            self.last_reconcile = std::time::Instant::now();
        }
        Ok(changed)
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
                self.last_reconcile = std::time::Instant::now();
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
        if event == "deployment_manager::ReceiptLayoutChanged" && !self.types.contains_key(event) {
            return Ok(0);
        }
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
        let mut devices = chain::id_list(&f["device_ids"]);
        // Indexed members (SF-125) are MemberKey fields, not vector entries.
        if let Some(member_key) = self.types.get("device_group::MemberKey") {
            for (key_type, key) in self.chain.field_names(id).await? {
                if chain::same_type(&key_type, member_key) {
                    let member: [u8; 32] = key.as_slice().try_into()
                        .map_err(|_| anyhow::anyhow!("undecodable member key of group {id}"))?;
                    devices.push(sui_sdk_types::Address::new(member).to_string());
                }
            }
            devices.sort();
            devices.dedup();
        }
        Ok(Some(Group {
            id: id.to_owned(),
            name: text(&f["name"]),
            description: text(&f["description"]),
            admin: chain::normalize(&text(&f["admin"])).unwrap_or_default(),
            created_at: chain::number(&f["created_at"]).unwrap_or_default(),
            deleted: false,
            devices,
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
        let sharded = self.field(id, "deployment_manager::ReceiptShardKey").await?.is_some();
        let groups = self
            .field(
                id,
                if sharded {
                    "deployment_manager::ShardedTargetGroupsKey"
                } else {
                    "deployment_manager::TargetGroupsKey"
                },
            )
            .await?;
        let devices = self
            .field(id, "deployment_manager::TargetDevicesKey")
            .await?;
        let digest = self
            .field(id, "deployment_manager::RecipeDigestKey")
            .await?;
        let binding = self
            .field(
                id,
                if sharded {
                    "component_catalog::ShardedCatalogBindingKey"
                } else {
                    "component_catalog::CatalogBindingKey"
                },
            )
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
            catalog_version: binding
                .as_ref()
                .and_then(|b| b["version_id"].as_str())
                .and_then(chain::normalize),
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
        if chain::number(&receipts["version"]) == Some(3) {
            return self.read_sharded_receipts(id).await;
        }
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
    async fn read_sharded_receipts(&self, id: &str) -> anyhow::Result<Vec<(String, u8)>> {
        let directory = self
            .field(id, "deployment_manager::ReceiptShardKey")
            .await?
            .ok_or_else(|| anyhow::anyhow!("missing v3 receipt directory"))?;
        if directory["ready"] != true || directory["retiring"] != false {
            return Ok(Vec::new());
        }
        let roots = chain::id_list(&directory["roots"]);
        anyhow::ensure!(
            roots.len() == 16
                && roots
                    .iter()
                    .collect::<std::collections::BTreeSet<_>>()
                    .len()
                    == 16
                && chain::number(&directory["migrated"]) == Some(16),
            "invalid receipt directory"
        );
        let mut receipts = Vec::new();
        for (index, root) in roots.iter().enumerate() {
            let shard = self
                .chain
                .object(root)
                .await?
                .ok_or_else(|| anyhow::anyhow!("missing receipt root"))?;
            anyhow::ensure!(
                chain::same_type(
                    &shard.object_type,
                    self.move_type("deployment_manager::ReceiptShard")?
                ) && matches!(shard.owner, chain::Owner::Shared(_))
                    && shard.fields["deployment"]
                        .as_str()
                        .is_some_and(|d| chain::same_id(d, id))
                    && chain::number(&shard.fields["index"]) == Some(index as u64)
                    && shard.fields["migrated"] == true,
                "invalid receipt root binding"
            );
            let count = chain::number(&shard.fields["count"])
                .ok_or_else(|| anyhow::anyhow!("invalid receipt count"))?;
            let mut start = 0;
            while start < count {
                let end = (start + 64).min(count);
                let ids = (start..end)
                    .map(|n| {
                        chain::field_id(
                            root,
                            self.move_type("deployment_manager::ReceiptIndexKey")?,
                            &n.to_le_bytes(),
                        )
                    })
                    .collect::<anyhow::Result<Vec<_>>>()?;
                let entries = self.chain.objects(&ids).await?;
                let devices = entries
                    .iter()
                    .map(|entry| {
                        let device = entry
                            .as_ref()
                            .and_then(|e| e.fields["value"].as_str())
                            .and_then(chain::normalize)
                            .ok_or_else(|| anyhow::anyhow!("missing receipt index"))?;
                        anyhow::ensure!(
                            chain::id_bytes(&device)?[31] as usize % 16 == index,
                            "wrong receipt shard"
                        );
                        Ok(device)
                    })
                    .collect::<anyhow::Result<Vec<String>>>()?;
                let fields = devices
                    .iter()
                    .map(|device| {
                        chain::field_id(
                            root,
                            self.move_type("deployment_manager::ReceiptKey")?,
                            &chain::id_bytes(device)?,
                        )
                    })
                    .collect::<anyhow::Result<Vec<_>>>()?;
                let values = self.chain.objects(&fields).await?;
                for (device, value) in devices.into_iter().zip(values) {
                    let value = value.ok_or_else(|| anyhow::anyhow!("missing receipt"))?;
                    let status = chain::number(&value.fields["value"]["status"])
                        .filter(|s| *s == 1 || *s == 2)
                        .ok_or_else(|| anyhow::anyhow!("invalid receipt status"))?;
                    receipts.push((device, status as u8));
                }
                start = end;
            }
        }
        let after = self
            .field(id, "deployment_manager::ReceiptShardKey")
            .await?
            .ok_or_else(|| anyhow::anyhow!("receipt directory disappeared"))?;
        anyhow::ensure!(
            after["ready"] == true && after["retiring"] == false,
            "receipt authority changed during read"
        );
        Ok(receipts)
    }
}

/// Meta key: the deployment the index materializes, as its registry and
/// every type of its package ("module::Type" → full type).
const BINDING: &str = "deployment_binding";

/// Bind the database to a chain and deployment. Cursors are keyed by
/// "module::Type" and objects by ID alone, so they are only valid for the
/// deployment that produced them: the same registry and, for every type
/// already bound, the same origin. A compatible upgrade keeps both and may
/// add types; another publication (even on the same chain) changes both.
/// Anything else is refused, or with `reset` discarded in the same
/// transaction that records the new binding.
fn bind(
    db: &mut Connection,
    chain_identifier: &str,
    registry: &str,
    types: &BTreeMap<String, String>,
    reset: bool,
) -> anyhow::Result<()> {
    let tx = db.transaction()?;
    if let Some(reason) = incompatible(&tx, chain_identifier, registry, types)? {
        anyhow::ensure!(reset, "{reason}. Re-run with --reset to discard the old index");
        warn!(%reason, "Discarding the index");
        tx.execute_batch(
            "DELETE FROM cursors; DELETE FROM devices; DELETE FROM groups; DELETE FROM group_members;
             DELETE FROM deployments; DELETE FROM deployment_targets; DELETE FROM receipts;
             DELETE FROM changes; DELETE FROM meta;",
        )?;
    }
    store::set_meta(&tx, "chain_identifier", chain_identifier)?;
    let binding = serde_json::json!({ "registry": registry, "types": types });
    store::set_meta(&tx, BINDING, &binding.to_string())?;
    tx.commit()?;
    Ok(())
}

/// Why an existing index cannot continue with this deployment, if it cannot.
fn incompatible(
    db: &Connection,
    chain_identifier: &str,
    registry: &str,
    types: &BTreeMap<String, String>,
) -> anyhow::Result<Option<String>> {
    let Some(bound_chain) = store::meta(db, "chain_identifier")? else {
        return Ok(None); // A new database.
    };
    if bound_chain != chain_identifier {
        return Ok(Some(format!(
            "database is bound to chain {bound_chain} but the manifest names {chain_identifier}; \
             the network may have reset"
        )));
    }
    let Some(binding) = store::meta(db, BINDING)? else {
        return Ok(Some("database does not record which deployment it indexes".into()));
    };
    let binding: Value = serde_json::from_str(&binding)?;
    let bound_registry = text(&binding["registry"]);
    if !chain::same_id(&bound_registry, registry) {
        return Ok(Some(format!(
            "database indexes the deployment with registry {bound_registry} but the manifest names \
             registry {registry}; another publication is not an upgrade of the indexed one"
        )));
    }
    let bound_types = binding["types"]
        .as_object()
        .ok_or_else(|| anyhow::anyhow!("undecodable deployment binding: {binding}"))?;
    for (name, bound_type) in bound_types {
        let bound_type = bound_type.as_str().unwrap_or_default();
        match types.get(name) {
            Some(full) if chain::same_type(full, bound_type) => {}
            other => {
                return Ok(Some(format!(
                    "indexed type {bound_type} is {} in the manifest's package; \
                     it is not a compatible upgrade of the indexed deployment",
                    other.map(String::as_str).unwrap_or("absent")
                )))
            }
        }
    }
    Ok(None)
}

/// Whether receipts were written under the current report authority. Older
/// versions stay writable through older packages until migrated, which
/// discards them; they are never evidence (as the dApp's `readReceipts`).
fn current_receipts(id: &str, receipts: &Value) -> anyhow::Result<bool> {
    let version = chain::number(&receipts["version"])
        .filter(|v| *v > 0)
        .ok_or_else(|| anyhow::anyhow!("invalid receipts authority version of {id}: {receipts}"))?;
    anyhow::ensure!(version <= 3, "unsupported receipts authority version");
    Ok(version == RECEIPTS_AUTHORITY_VERSION)
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
        assert!(!current_receipts("d", &receipts(serde_json::json!("3"))).unwrap());
        assert!(current_receipts("d", &receipts(serde_json::json!("4"))).is_err());
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

    #[test]
    fn index_is_bound_to_its_deployment() {
        let dir = tempfile::tempdir().unwrap();
        let mut db = store::open(&dir.path().join("fleet.db")).unwrap();
        let types = |origin: u32, extra: Option<(u32, &str)>| {
            let mut types: BTreeMap<String, String> = ["device_registry::Device", "device_group::DeviceGroup"]
                .into_iter()
                .map(|name| (name.to_owned(), format!("{}::{name}", id(origin))))
                .collect();
            if let Some((origin, name)) = extra {
                types.insert(name.to_owned(), format!("{}::{name}", id(origin)));
            }
            types
        };
        let populated = |db: &mut Connection| {
            let tx = db.transaction().unwrap();
            store::set_cursor(&tx, "device_group::GroupCreated", &serde_json::json!({ "checkpoint": 9 }), 1).unwrap();
            tx.commit().unwrap();
            apply(db, Update::Device(id(1), None));
        };
        let kept = |db: &Connection| {
            store::cursor(db, "device_group::GroupCreated").unwrap().is_some()
                && store::read_device(db, &id(1)).unwrap().is_some()
        };
        let (chain, registry, published) = ("4c78adac", id(50), types(10, None));
        bind(&mut db, chain, &registry, &published, false).unwrap();
        populated(&mut db);
        bind(&mut db, chain, &registry, &published, false).unwrap();
        assert!(kept(&db), "reopening the same deployment keeps the index");

        // A compatible upgrade: same registry and origins, plus a new type.
        let upgraded = types(10, Some((20, "device_registry::DeviceIdentityChanged")));
        bind(&mut db, chain, &registry, &upgraded, false).unwrap();
        assert!(kept(&db), "a compatible upgrade keeps the index");
        // The new type is now bound too: dropping it is incompatible.
        assert!(bind(&mut db, chain, &registry, &published, false).is_err());

        // Another publication on the same chain: new registry and origins.
        let other = (id(51), types(30, Some((30, "device_registry::DeviceIdentityChanged"))));
        let refused = bind(&mut db, chain, &other.0, &other.1, false).unwrap_err().to_string();
        assert!(refused.contains("another publication") && refused.contains("--reset"), "{refused}");
        assert!(kept(&db), "a refusal leaves the index untouched");
        // Same registry but a re-originated type is not an upgrade either.
        let refused = bind(&mut db, chain, &registry, &other.1, false).unwrap_err().to_string();
        assert!(refused.contains("not a compatible upgrade"), "{refused}");
        // Another chain.
        let refused = bind(&mut db, "deadbeef", &registry, &upgraded, false).unwrap_err().to_string();
        assert!(refused.contains("network may have reset"), "{refused}");
        assert!(kept(&db));

        // --reset discards rows, placeholders, cursors, feed and sweep state.
        store::set_meta(&db, SWEEP_AFTER, &id(1)).unwrap();
        bind(&mut db, chain, &other.0, &other.1, true).unwrap();
        assert!(!kept(&db));
        assert!(sweep_ids(&db, "", 10).unwrap().is_empty());
        assert!(store::cursors(&db).unwrap().is_empty());
        assert_eq!(store::meta(&db, SWEEP_AFTER).unwrap(), None);
        bind(&mut db, chain, &other.0, &other.1, false).unwrap();
        assert!(bind(&mut db, chain, &registry, &upgraded, false).is_err(), "rebound to the new deployment");

        // A database bound to a chain without a deployment binding (indexed
        // before deployments were bound) cannot be trusted either.
        db.execute("DELETE FROM meta WHERE key = ?1", [BINDING]).unwrap();
        let refused = bind(&mut db, chain, &other.0, &other.1, false).unwrap_err().to_string();
        assert!(refused.contains("does not record which deployment"), "{refused}");
    }
}
