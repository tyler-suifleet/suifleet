//! Materialized fleet state in SQLite (WAL): one writer (the indexer), any
//! number of readers (the API). Every stream page's rows, change-feed
//! entries and cursor are committed in one transaction.
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursors (
  stream TEXT PRIMARY KEY, cursor TEXT NOT NULL, checkpoint INTEGER, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, owner TEXT, name TEXT, device_address TEXT, arch TEXT,
  registered_at INTEGER, deleted INTEGER NOT NULL DEFAULT 0, activated INTEGER NOT NULL DEFAULT 0,
  revoked INTEGER NOT NULL DEFAULT 0, signer TEXT, owner_cap TEXT);
CREATE INDEX IF NOT EXISTS devices_owner ON devices(owner, id);
CREATE TABLE IF NOT EXISTS groups (
  id TEXT PRIMARY KEY, name TEXT, description TEXT, admin TEXT, created_at INTEGER,
  deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS groups_admin ON groups(admin, id);
CREATE TABLE IF NOT EXISTS group_members (
  group_id TEXT NOT NULL, device_id TEXT NOT NULL, PRIMARY KEY (group_id, device_id));
CREATE INDEX IF NOT EXISTS members_device ON group_members(device_id);
CREATE TABLE IF NOT EXISTS deployments (
  id TEXT PRIMARY KEY, creator TEXT, component_name TEXT, version TEXT, recipe_blob_id TEXT,
  artifact_sha256 TEXT, recipe_sha256 TEXT, created_at INTEGER, schema_version INTEGER,
  deleted INTEGER NOT NULL DEFAULT 0, catalog_version TEXT);
CREATE INDEX IF NOT EXISTS deployments_creator ON deployments(creator, id);
CREATE INDEX IF NOT EXISTS deployments_component ON deployments(component_name, id);
CREATE TABLE IF NOT EXISTS deployment_targets (
  deployment_id TEXT NOT NULL, target_id TEXT NOT NULL, kind TEXT NOT NULL,
  PRIMARY KEY (deployment_id, kind, target_id));
CREATE INDEX IF NOT EXISTS targets_target ON deployment_targets(target_id);
CREATE TABLE IF NOT EXISTS receipts (
  deployment_id TEXT NOT NULL, device_id TEXT NOT NULL, status INTEGER NOT NULL,
  PRIMARY KEY (deployment_id, device_id));
CREATE TABLE IF NOT EXISTS changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, object_id TEXT NOT NULL,
  checkpoint INTEGER, payload TEXT NOT NULL);
"#;

pub const MAX_LIMIT: usize = 500;

/// `owner` is the operator: the DeviceOwnerCap's owner for an activated
/// device, otherwise the address that owns the (untrusted) Device object.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Device {
    pub id: String,
    pub owner: Option<String>,
    pub name: String,
    pub device_address: String,
    pub arch: String,
    pub registered_at: u64,
    pub deleted: bool,
    /// In registry custody with an identity (SF-008).
    pub activated: bool,
    /// The identity is revoked (or the device deregistered).
    pub revoked: bool,
    /// The only address whose reports the registry accepts.
    pub signer: Option<String>,
    pub owner_cap: Option<String>,
    pub groups: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Group {
    pub id: String,
    pub name: String,
    pub description: String,
    pub admin: String,
    pub created_at: u64,
    pub deleted: bool,
    pub devices: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Deployment {
    pub id: String,
    pub creator: String,
    pub component_name: String,
    pub version: String,
    pub recipe_blob_id: String,
    pub artifact_sha256: String,
    pub recipe_sha256: Option<String>,
    pub created_at: u64,
    pub schema_version: u64,
    /// The ComponentVersion a catalog deployment (schema 2, SF-011) binds;
    /// its recipe and artifact digests are per platform on that object.
    pub catalog_version: Option<String>,
    pub deleted: bool,
    pub target_groups: Vec<String>,
    pub target_devices: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Change {
    pub seq: i64,
    pub kind: String,
    pub object_id: String,
    pub checkpoint: Option<u64>,
    pub payload: Value,
}

#[derive(Debug, Default, Clone)]
pub struct Filter {
    pub owner: Option<String>,
    pub group: Option<String>,
    pub device: Option<String>,
    pub component: Option<String>,
    pub include_deleted: bool,
}

/// A page of results; `next` resumes after the last item (keyset).
#[derive(Debug, Serialize)]
pub struct Page<T> {
    pub items: Vec<T>,
    pub next: Option<String>,
}

pub fn open(path: &Path) -> anyhow::Result<Connection> {
    let connection = Connection::open(path)?;
    connection.pragma_update(None, "journal_mode", "WAL")?;
    connection.pragma_update(None, "synchronous", "FULL")?;
    connection.pragma_update(None, "foreign_keys", "ON")?;
    connection.busy_timeout(std::time::Duration::from_secs(10))?;
    connection.execute_batch(SCHEMA)?;
    // Databases from before identities (SF-008) gain the columns; the startup
    // sweep re-reads every device and fills them in.
    let has_identity: bool = connection.query_row(
        "SELECT COUNT(*) > 0 FROM pragma_table_info('devices') WHERE name = 'activated'",
        [],
        |row| row.get(0),
    )?;
    if !has_identity {
        connection.execute_batch(
            "ALTER TABLE devices ADD COLUMN activated INTEGER NOT NULL DEFAULT 0;
             ALTER TABLE devices ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0;
             ALTER TABLE devices ADD COLUMN signer TEXT;
             ALTER TABLE devices ADD COLUMN owner_cap TEXT;",
        )?;
    }
    // Databases from before the component catalog (SF-011) gain its column.
    let has_catalog: bool = connection.query_row(
        "SELECT COUNT(*) > 0 FROM pragma_table_info('deployments') WHERE name = 'catalog_version'",
        [],
        |row| row.get(0),
    )?;
    if !has_catalog {
        connection.execute_batch("ALTER TABLE deployments ADD COLUMN catalog_version TEXT;")?;
    }
    Ok(connection)
}

/// Opens read connections for API requests (WAL readers never block the writer).
#[derive(Clone)]
pub struct Readers {
    path: PathBuf,
}

impl Readers {
    pub fn new(path: &Path) -> Self {
        Self {
            path: path.to_owned(),
        }
    }
    pub fn open(&self) -> anyhow::Result<Connection> {
        let connection =
            Connection::open_with_flags(&self.path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        connection.busy_timeout(std::time::Duration::from_secs(10))?;
        Ok(connection)
    }
}

pub fn meta(connection: &Connection, key: &str) -> anyhow::Result<Option<String>> {
    Ok(connection
        .query_row("SELECT value FROM meta WHERE key = ?1", [key], |row| {
            row.get(0)
        })
        .optional()?)
}

pub fn set_meta(connection: &Connection, key: &str, value: &str) -> anyhow::Result<()> {
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?1, ?2) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        params![key, value],
    )?;
    Ok(())
}

pub fn cursor(connection: &Connection, stream: &str) -> anyhow::Result<Option<Value>> {
    let text: Option<String> = connection
        .query_row(
            "SELECT cursor FROM cursors WHERE stream = ?1",
            [stream],
            |row| row.get(0),
        )
        .optional()?;
    Ok(text.map(|text| serde_json::from_str(&text)).transpose()?)
}

pub fn cursors(connection: &Connection) -> anyhow::Result<Vec<(String, Option<u64>, u64)>> {
    let mut statement =
        connection.prepare("SELECT stream, checkpoint, updated_at FROM cursors ORDER BY stream")?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, Option<i64>>(1)?.map(|c| c as u64),
            row.get::<_, i64>(2)? as u64,
        ))
    })?;
    Ok(rows.collect::<Result<_, _>>()?)
}

pub fn set_cursor(tx: &Transaction, stream: &str, cursor: &Value, now: u64) -> anyhow::Result<()> {
    let checkpoint = cursor
        .get("checkpoint")
        .and_then(Value::as_u64)
        .map(|c| c as i64);
    tx.execute(
        "INSERT INTO cursors (stream, cursor, checkpoint, updated_at) VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(stream) DO UPDATE SET cursor = excluded.cursor, checkpoint = excluded.checkpoint,
           updated_at = excluded.updated_at",
        params![stream, cursor.to_string(), checkpoint, now as i64],
    )?;
    Ok(())
}

fn record_change(
    tx: &Transaction,
    kind: &str,
    id: &str,
    checkpoint: Option<u64>,
    payload: &Value,
) -> anyhow::Result<()> {
    tx.execute(
        "INSERT INTO changes (kind, object_id, checkpoint, payload) VALUES (?1, ?2, ?3, ?4)",
        params![kind, id, checkpoint.map(|c| c as i64), payload.to_string()],
    )?;
    Ok(())
}

// ── Writes: each returns whether anything changed (and records the change) ──

/// Keep a device ID an event named but whose object was absent on its first
/// read (wrapped, possibly in the registering transaction). The placeholder
/// tombstone has no fields and records no change; it exists so the sweep
/// re-reads the ID and picks the device up when it is unwrapped.
pub fn put_device_placeholder(tx: &Transaction, id: &str) -> anyhow::Result<bool> {
    let inserted = tx.execute(
        "INSERT INTO devices (id, owner, name, device_address, arch, registered_at, deleted)
         VALUES (?1, NULL, '', '', '', 0, 1) ON CONFLICT(id) DO NOTHING",
        params![id],
    )?;
    Ok(inserted > 0)
}

pub fn put_device(
    tx: &Transaction,
    device: &Device,
    checkpoint: Option<u64>,
) -> anyhow::Result<bool> {
    let current = read_device(tx, &device.id)?;
    let mut wanted = device.clone();
    wanted.groups = current
        .as_ref()
        .map(|d| d.groups.clone())
        .unwrap_or_default();
    if current.as_ref() == Some(&wanted) {
        return Ok(false);
    }
    tx.execute(
        "INSERT INTO devices (id, owner, name, device_address, arch, registered_at, deleted, activated,
           revoked, signer, owner_cap)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
         ON CONFLICT(id) DO UPDATE SET owner = excluded.owner, name = excluded.name,
           device_address = excluded.device_address, arch = excluded.arch,
           registered_at = excluded.registered_at, deleted = excluded.deleted,
           activated = excluded.activated, revoked = excluded.revoked, signer = excluded.signer,
           owner_cap = excluded.owner_cap",
        params![
            device.id,
            device.owner,
            device.name,
            device.device_address,
            device.arch,
            device.registered_at as i64,
            device.deleted,
            device.activated,
            device.revoked,
            device.signer,
            device.owner_cap
        ],
    )?;
    let kind = if device.deleted {
        "device.deleted"
    } else {
        "device.updated"
    };
    record_change(
        tx,
        kind,
        &device.id,
        checkpoint,
        &serde_json::to_value(&wanted)?,
    )?;
    Ok(true)
}

pub fn put_group(tx: &Transaction, group: &Group, checkpoint: Option<u64>) -> anyhow::Result<bool> {
    let current = read_group(tx, &group.id)?;
    let mut wanted = group.clone();
    wanted.devices.sort();
    wanted.devices.dedup();
    if current.as_ref() == Some(&wanted) {
        return Ok(false);
    }
    tx.execute(
        "INSERT INTO groups (id, name, description, admin, created_at, deleted) VALUES (?1, ?2, ?3, ?4, ?5, ?6)
         ON CONFLICT(id) DO UPDATE SET name = excluded.name, description = excluded.description,
           admin = excluded.admin, created_at = excluded.created_at, deleted = excluded.deleted",
        params![group.id, group.name, group.description, group.admin, group.created_at as i64, group.deleted],
    )?;
    tx.execute("DELETE FROM group_members WHERE group_id = ?1", [&group.id])?;
    for device in &wanted.devices {
        tx.execute(
            "INSERT INTO group_members (group_id, device_id) VALUES (?1, ?2)",
            params![group.id, device],
        )?;
    }
    let kind = if group.deleted {
        "group.deleted"
    } else {
        "group.updated"
    };
    record_change(
        tx,
        kind,
        &group.id,
        checkpoint,
        &serde_json::to_value(&wanted)?,
    )?;
    Ok(true)
}

pub fn put_deployment(
    tx: &Transaction,
    deployment: &Deployment,
    checkpoint: Option<u64>,
) -> anyhow::Result<bool> {
    let current = read_deployment(tx, &deployment.id)?;
    let mut wanted = deployment.clone();
    for list in [&mut wanted.target_groups, &mut wanted.target_devices] {
        list.sort();
        list.dedup();
    }
    if current.as_ref() == Some(&wanted) {
        return Ok(false);
    }
    tx.execute(
        "INSERT INTO deployments (id, creator, component_name, version, recipe_blob_id, artifact_sha256,
           recipe_sha256, created_at, schema_version, deleted, catalog_version)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
         ON CONFLICT(id) DO UPDATE SET creator = excluded.creator, component_name = excluded.component_name,
           version = excluded.version, recipe_blob_id = excluded.recipe_blob_id,
           artifact_sha256 = excluded.artifact_sha256, recipe_sha256 = excluded.recipe_sha256,
           created_at = excluded.created_at, schema_version = excluded.schema_version, deleted = excluded.deleted,
           catalog_version = excluded.catalog_version",
        params![wanted.id, wanted.creator, wanted.component_name, wanted.version, wanted.recipe_blob_id,
                wanted.artifact_sha256, wanted.recipe_sha256, wanted.created_at as i64,
                wanted.schema_version as i64, wanted.deleted, wanted.catalog_version],
    )?;
    tx.execute(
        "DELETE FROM deployment_targets WHERE deployment_id = ?1",
        [&wanted.id],
    )?;
    for (kind, list) in [
        ("group", &wanted.target_groups),
        ("device", &wanted.target_devices),
    ] {
        for target in list {
            tx.execute("INSERT INTO deployment_targets (deployment_id, target_id, kind) VALUES (?1, ?2, ?3)",
                params![wanted.id, target, kind])?;
        }
    }
    let kind = if wanted.deleted {
        "deployment.deleted"
    } else {
        "deployment.updated"
    };
    record_change(
        tx,
        kind,
        &wanted.id,
        checkpoint,
        &serde_json::to_value(&wanted)?,
    )?;
    Ok(true)
}

/// Replace a deployment's authoritative receipts (device → status).
pub fn put_receipts(
    tx: &Transaction,
    deployment: &str,
    receipts: &[(String, u8)],
    checkpoint: Option<u64>,
) -> anyhow::Result<bool> {
    let mut wanted = receipts.to_vec();
    wanted.sort();
    if read_receipts(tx, deployment)? == wanted {
        return Ok(false);
    }
    tx.execute(
        "DELETE FROM receipts WHERE deployment_id = ?1",
        [deployment],
    )?;
    for (device, status) in &wanted {
        tx.execute(
            "INSERT INTO receipts (deployment_id, device_id, status) VALUES (?1, ?2, ?3)",
            params![deployment, device, *status as i64],
        )?;
    }
    let payload = serde_json::json!({ "deployment_id": deployment, "receipts": wanted.iter()
        .map(|(device, status)| serde_json::json!({"device_id": device, "status": status})).collect::<Vec<_>>() });
    record_change(tx, "receipts.updated", deployment, checkpoint, &payload)?;
    Ok(true)
}

// ── Reads ─────────────────────────────────────────────────────────────────────

fn ids(connection: &Connection, sql: &str, id: &str) -> anyhow::Result<Vec<String>> {
    let mut statement = connection.prepare_cached(sql)?;
    let rows = statement.query_map([id], |row| row.get(0))?;
    Ok(rows.collect::<Result<_, _>>()?)
}

pub fn read_device(connection: &Connection, id: &str) -> anyhow::Result<Option<Device>> {
    let row = connection
        .query_row("SELECT id, owner, name, device_address, arch, registered_at, deleted, activated, revoked,
                signer, owner_cap FROM devices WHERE id = ?1",
            [id], |row| Ok(Device {
                id: row.get(0)?, owner: row.get(1)?, name: row.get(2)?, device_address: row.get(3)?,
                arch: row.get(4)?, registered_at: row.get::<_, i64>(5)? as u64, deleted: row.get(6)?,
                activated: row.get(7)?, revoked: row.get(8)?, signer: row.get(9)?, owner_cap: row.get(10)?,
                groups: vec![],
            }))
        .optional()?;
    row.map(|mut device| {
        device.groups = ids(
            connection,
            "SELECT m.group_id FROM group_members m JOIN groups g ON g.id = m.group_id
             WHERE m.device_id = ?1 AND g.deleted = 0 ORDER BY m.group_id",
            id,
        )?;
        Ok(device)
    })
    .transpose()
}

pub fn read_group(connection: &Connection, id: &str) -> anyhow::Result<Option<Group>> {
    let row = connection
        .query_row(
            "SELECT id, name, description, admin, created_at, deleted FROM groups WHERE id = ?1",
            [id],
            |row| {
                Ok(Group {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    description: row.get(2)?,
                    admin: row.get(3)?,
                    created_at: row.get::<_, i64>(4)? as u64,
                    deleted: row.get(5)?,
                    devices: vec![],
                })
            },
        )
        .optional()?;
    row.map(|mut group| {
        group.devices = ids(
            connection,
            "SELECT device_id FROM group_members WHERE group_id = ?1 ORDER BY device_id",
            id,
        )?;
        Ok(group)
    })
    .transpose()
}

pub fn read_deployment(connection: &Connection, id: &str) -> anyhow::Result<Option<Deployment>> {
    let row = connection
        .query_row("SELECT id, creator, component_name, version, recipe_blob_id, artifact_sha256, recipe_sha256,
                created_at, schema_version, deleted, catalog_version FROM deployments WHERE id = ?1",
            [id], |row| Ok(Deployment {
                id: row.get(0)?, creator: row.get(1)?, component_name: row.get(2)?, version: row.get(3)?,
                recipe_blob_id: row.get(4)?, artifact_sha256: row.get(5)?, recipe_sha256: row.get(6)?,
                created_at: row.get::<_, i64>(7)? as u64, schema_version: row.get::<_, i64>(8)? as u64,
                deleted: row.get(9)?, catalog_version: row.get(10)?, target_groups: vec![], target_devices: vec![],
            }))
        .optional()?;
    row.map(|mut deployment| {
        deployment.target_groups = ids(connection,
            "SELECT target_id FROM deployment_targets WHERE deployment_id = ?1 AND kind = 'group' ORDER BY target_id", id)?;
        deployment.target_devices = ids(connection,
            "SELECT target_id FROM deployment_targets WHERE deployment_id = ?1 AND kind = 'device' ORDER BY target_id", id)?;
        Ok(deployment)
    }).transpose()
}

pub fn read_receipts(
    connection: &Connection,
    deployment: &str,
) -> anyhow::Result<Vec<(String, u8)>> {
    let mut statement = connection.prepare_cached(
        "SELECT device_id, status FROM receipts WHERE deployment_id = ?1 ORDER BY device_id",
    )?;
    let rows = statement.query_map([deployment], |row| {
        Ok((row.get(0)?, row.get::<_, i64>(1)? as u8))
    })?;
    Ok(rows.collect::<Result<_, _>>()?)
}

/// Devices a deployment reaches: its direct targets plus current members of
/// target groups whose admin is the deployment's creator (as devices check).
pub fn resolved_devices(
    connection: &Connection,
    deployment: &Deployment,
) -> anyhow::Result<Vec<String>> {
    let mut devices = deployment.target_devices.clone();
    for group in &deployment.target_groups {
        if let Some(group) = read_group(connection, group)? {
            if !group.deleted && group.admin == deployment.creator {
                devices.extend(group.devices);
            }
        }
    }
    devices.sort();
    devices.dedup();
    Ok(devices)
}

fn limit(limit: Option<usize>) -> usize {
    limit.unwrap_or(100).clamp(1, MAX_LIMIT)
}

/// Keyset page of IDs from `sql` (which must select `id` ordered ascending and
/// take ?1 = after-cursor, ?2 = limit + 1, then its own filter params).
fn page_ids(
    connection: &Connection,
    sql: &str,
    after: &str,
    size: usize,
    filters: &[&dyn rusqlite::ToSql],
) -> anyhow::Result<(Vec<String>, Option<String>)> {
    let mut parameters: Vec<&dyn rusqlite::ToSql> = vec![&after];
    let fetch = (size + 1) as i64;
    parameters.push(&fetch);
    parameters.extend_from_slice(filters);
    let mut statement = connection.prepare_cached(sql)?;
    let mut items: Vec<String> = statement
        .query_map(parameters.as_slice(), |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    let next = (items.len() > size).then(|| {
        items.truncate(size);
        items.last().cloned().unwrap_or_default()
    });
    Ok((items, next))
}

pub fn list_devices(
    connection: &Connection,
    filter: &Filter,
    after: Option<&str>,
    size: Option<usize>,
) -> anyhow::Result<Page<Device>> {
    let (ids, next) = page_ids(connection,
        "SELECT id FROM devices d WHERE id > ?1
           AND (?3 IS NULL OR owner = ?3)
           AND (?4 IS NULL OR EXISTS (SELECT 1 FROM group_members m WHERE m.device_id = d.id AND m.group_id = ?4))
           AND (?5 OR deleted = 0)
         ORDER BY id LIMIT ?2",
        after.unwrap_or(""), limit(size), &[&filter.owner, &filter.group, &filter.include_deleted])?;
    let items = ids
        .iter()
        .filter_map(|id| read_device(connection, id).transpose())
        .collect::<Result<_, _>>()?;
    Ok(Page { items, next })
}

pub fn list_groups(
    connection: &Connection,
    filter: &Filter,
    after: Option<&str>,
    size: Option<usize>,
) -> anyhow::Result<Page<Group>> {
    let (ids, next) = page_ids(connection,
        "SELECT id FROM groups g WHERE id > ?1
           AND (?3 IS NULL OR admin = ?3)
           AND (?4 IS NULL OR EXISTS (SELECT 1 FROM group_members m WHERE m.group_id = g.id AND m.device_id = ?4))
           AND (?5 OR deleted = 0)
         ORDER BY id LIMIT ?2",
        after.unwrap_or(""), limit(size), &[&filter.owner, &filter.device, &filter.include_deleted])?;
    let items = ids
        .iter()
        .filter_map(|id| read_group(connection, id).transpose())
        .collect::<Result<_, _>>()?;
    Ok(Page { items, next })
}

pub fn list_deployments(
    connection: &Connection,
    filter: &Filter,
    after: Option<&str>,
    size: Option<usize>,
) -> anyhow::Result<Page<Deployment>> {
    let (ids, next) = page_ids(connection,
        "SELECT id FROM deployments d WHERE id > ?1
           AND (?3 IS NULL OR creator = ?3)
           AND (?4 IS NULL OR component_name = ?4)
           AND (?5 IS NULL OR EXISTS (SELECT 1 FROM deployment_targets t
                 WHERE t.deployment_id = d.id AND t.kind = 'group' AND t.target_id = ?5))
           AND (?6 IS NULL OR (EXISTS (SELECT 1 FROM devices device WHERE device.id = ?6 AND device.owner = d.creator)
               AND (EXISTS (SELECT 1 FROM deployment_targets t
                 WHERE t.deployment_id = d.id AND t.kind = 'device' AND t.target_id = ?6)
               OR EXISTS (SELECT 1 FROM deployment_targets t JOIN group_members m ON m.group_id = t.target_id
                 JOIN groups g ON g.id = t.target_id
                 WHERE t.deployment_id = d.id AND t.kind = 'group' AND m.device_id = ?6 AND g.admin = d.creator))))
           AND (?7 OR deleted = 0)
         ORDER BY id LIMIT ?2",
        after.unwrap_or(""), limit(size),
        &[&filter.owner, &filter.component, &filter.group, &filter.device, &filter.include_deleted])?;
    let items = ids
        .iter()
        .filter_map(|id| read_deployment(connection, id).transpose())
        .collect::<Result<_, _>>()?;
    Ok(Page { items, next })
}

pub fn changes_after(
    connection: &Connection,
    after: i64,
    size: Option<usize>,
) -> anyhow::Result<Vec<Change>> {
    let mut statement = connection.prepare_cached(
        "SELECT seq, kind, object_id, checkpoint, payload FROM changes WHERE seq > ?1 ORDER BY seq LIMIT ?2")?;
    let rows = statement.query_map(params![after, limit(size) as i64], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<i64>>(3)?,
            row.get::<_, String>(4)?,
        ))
    })?;
    rows.map(|row| {
        let (seq, kind, object_id, checkpoint, payload) = row?;
        Ok(Change {
            seq,
            kind,
            object_id,
            checkpoint: checkpoint.map(|c| c as u64),
            payload: serde_json::from_str(&payload)?,
        })
    })
    .collect()
}

pub fn last_change(connection: &Connection) -> anyhow::Result<i64> {
    Ok(
        connection.query_row("SELECT COALESCE(MAX(seq), 0) FROM changes", [], |row| {
            row.get(0)
        })?,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(n: u32) -> String {
        format!("0x{n:064x}")
    }

    fn device(n: u32, owner: u32) -> Device {
        Device {
            id: id(n),
            owner: Some(id(owner)),
            name: format!("d{n}"),
            device_address: id(9000 + n),
            arch: "aarch64".into(),
            registered_at: n as u64,
            deleted: false,
            activated: true,
            revoked: false,
            signer: Some(id(9000 + n)),
            owner_cap: Some(id(8000 + n)),
            groups: vec![],
        }
    }

    #[test]
    fn writes_are_idempotent_and_feed_changes() {
        let dir = tempfile::tempdir().unwrap();
        let mut connection = open(&dir.path().join("fleet.db")).unwrap();
        let tx = connection.transaction().unwrap();
        assert!(put_device(&tx, &device(1, 100), Some(5)).unwrap());
        assert!(
            !put_device(&tx, &device(1, 100), Some(6)).unwrap(),
            "unchanged rewrite records nothing"
        );
        let group = Group {
            id: id(50),
            name: "g".into(),
            description: String::new(),
            admin: id(100),
            created_at: 1,
            deleted: false,
            devices: vec![id(1), id(1)],
        };
        assert!(put_group(&tx, &group, None).unwrap());
        assert!(!put_group(&tx, &group, None).unwrap());
        tx.commit().unwrap();
        assert_eq!(
            read_device(&connection, &id(1)).unwrap().unwrap().groups,
            vec![id(50)]
        );
        let changes = changes_after(&connection, 0, None).unwrap();
        assert_eq!(
            changes.iter().map(|c| c.kind.as_str()).collect::<Vec<_>>(),
            ["device.updated", "group.updated"]
        );
        assert_eq!(
            changes_after(&connection, changes[0].seq, None)
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn databases_from_before_identities_gain_the_columns() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("fleet.db");
        let old = Connection::open(&path).unwrap();
        old.execute_batch(
            "CREATE TABLE devices (id TEXT PRIMARY KEY, owner TEXT, name TEXT, device_address TEXT, arch TEXT,
               registered_at INTEGER, deleted INTEGER NOT NULL DEFAULT 0);
             INSERT INTO devices VALUES ('0x1', '0x2', 'old', '0x3', 'aarch64', 0, 0);",
        )
        .unwrap();
        drop(old);
        let mut connection = open(&path).unwrap();
        let migrated = read_device(&connection, "0x1").unwrap().unwrap();
        assert!(!migrated.activated && migrated.signer.is_none() && migrated.name == "old");
        let tx = connection.transaction().unwrap();
        assert!(put_device(&tx, &device(1, 100), None).unwrap());
        tx.commit().unwrap();
        assert_eq!(read_device(&connection, &id(1)).unwrap().unwrap(), device(1, 100));
        drop(connection);
        open(&path).unwrap(); // Reopening an upgraded database is a no-op.
    }

    #[test]
    fn databases_from_before_the_catalog_gain_its_column() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("fleet.db");
        let old = Connection::open(&path).unwrap();
        old.execute_batch(
            "CREATE TABLE deployments (id TEXT PRIMARY KEY, creator TEXT, component_name TEXT, version TEXT,
               recipe_blob_id TEXT, artifact_sha256 TEXT, recipe_sha256 TEXT, created_at INTEGER,
               schema_version INTEGER, deleted INTEGER NOT NULL DEFAULT 0);
             INSERT INTO deployments VALUES ('0x7', '0x1', 'fw', '1', 'r', 'a', 'b', 0, 1, 0);",
        )
        .unwrap();
        drop(old);
        let mut connection = open(&path).unwrap();
        let migrated = read_deployment(&connection, "0x7").unwrap().unwrap();
        assert!(migrated.catalog_version.is_none() && migrated.component_name == "fw");
        let catalogued = Deployment {
            id: id(71),
            catalog_version: Some(id(72)),
            schema_version: 2,
            recipe_sha256: None,
            ..migrated
        };
        let tx = connection.transaction().unwrap();
        assert!(put_deployment(&tx, &catalogued, None).unwrap());
        tx.commit().unwrap();
        assert_eq!(read_deployment(&connection, &id(71)).unwrap().unwrap(), catalogued);
        drop(connection);
        open(&path).unwrap(); // Reopening an upgraded database is a no-op.
    }

    #[test]
    fn rolled_back_page_leaves_neither_rows_nor_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let mut connection = open(&dir.path().join("fleet.db")).unwrap();
        {
            let tx = connection.transaction().unwrap();
            put_device(&tx, &device(1, 100), None).unwrap();
            set_cursor(&tx, "devices", &serde_json::json!({"checkpoint": 3}), 0).unwrap();
            // Dropped without commit: a crash mid-page.
        }
        assert!(read_device(&connection, &id(1)).unwrap().is_none());
        assert!(cursor(&connection, "devices").unwrap().is_none());
        assert_eq!(last_change(&connection).unwrap(), 0);
    }

    #[test]
    fn keyset_pagination_and_filters() {
        let dir = tempfile::tempdir().unwrap();
        let mut connection = open(&dir.path().join("fleet.db")).unwrap();
        let tx = connection.transaction().unwrap();
        for n in 1..=25 {
            put_device(&tx, &device(n, if n % 2 == 0 { 100 } else { 200 }), None).unwrap();
        }
        let mut gone = device(26, 100);
        gone.deleted = true;
        put_device(&tx, &gone, None).unwrap();
        tx.commit().unwrap();
        let mut seen = Vec::new();
        let mut after: Option<String> = None;
        loop {
            let page =
                list_devices(&connection, &Filter::default(), after.as_deref(), Some(7)).unwrap();
            seen.extend(page.items.iter().map(|d| d.id.clone()));
            match page.next {
                Some(next) => after = Some(next),
                None => break,
            }
        }
        assert_eq!(
            seen,
            (1..=25).map(id).collect::<Vec<_>>(),
            "every live device exactly once, in order"
        );
        let owned = list_devices(
            &connection,
            &Filter {
                owner: Some(id(100)),
                ..Default::default()
            },
            None,
            None,
        )
        .unwrap();
        assert_eq!(owned.items.len(), 12);
        let all = list_devices(
            &connection,
            &Filter {
                include_deleted: true,
                ..Default::default()
            },
            None,
            Some(500),
        )
        .unwrap();
        assert_eq!(all.items.len(), 26);
        assert_eq!(limit(Some(10_000)), MAX_LIMIT);
    }

    #[test]
    fn deployment_resolution_follows_the_creators_groups_only() {
        let dir = tempfile::tempdir().unwrap();
        let mut connection = open(&dir.path().join("fleet.db")).unwrap();
        let tx = connection.transaction().unwrap();
        let mine = Group {
            id: id(50),
            name: "mine".into(),
            description: String::new(),
            admin: id(100),
            created_at: 1,
            deleted: false,
            devices: vec![id(1)],
        };
        let foreign = Group {
            id: id(51),
            admin: id(999),
            devices: vec![id(2)],
            ..mine.clone()
        };
        for n in 1..=3 {
            put_device(&tx, &device(n, 100), None).unwrap();
        }
        put_group(&tx, &mine, None).unwrap();
        put_group(&tx, &foreign, None).unwrap();
        let deployment = Deployment {
            id: id(70),
            creator: id(100),
            component_name: "fw".into(),
            version: "1".into(),
            recipe_blob_id: "r".into(),
            artifact_sha256: "a".into(),
            recipe_sha256: Some("b".into()),
            created_at: 1,
            schema_version: 1,
            catalog_version: None,
            deleted: false,
            target_groups: vec![id(50), id(51)],
            target_devices: vec![id(3)],
        };
        put_deployment(&tx, &deployment, None).unwrap();
        put_receipts(&tx, &id(70), &[(id(1), 1)], None).unwrap();
        tx.commit().unwrap();
        let stored = read_deployment(&connection, &id(70)).unwrap().unwrap();
        assert_eq!(
            resolved_devices(&connection, &stored).unwrap(),
            vec![id(1), id(3)]
        );
        let for_device = |n| {
            list_deployments(
                &connection,
                &Filter {
                    device: Some(id(n)),
                    ..Default::default()
                },
                None,
                None,
            )
            .unwrap()
            .items
            .len()
        };
        assert_eq!((for_device(1), for_device(2), for_device(3)), (1, 0, 1));
        assert_eq!(
            read_receipts(&connection, &id(70)).unwrap(),
            vec![(id(1), 1)]
        );
    }
}
