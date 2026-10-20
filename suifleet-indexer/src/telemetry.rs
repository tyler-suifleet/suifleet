//! Device telemetry ingress and fleet health (SF-019, docs/TELEMETRY.md).
//!
//! Devices POST signed snapshots (the daemon's `telemetry.rs`). A snapshot is
//! accepted only when its Sui personal-message signature verifies, its signer
//! is the device's current registry identity signer (activated, not revoked,
//! as indexed from the chain), it is bound to this chain and registry, within
//! bounds, not from the future, and its sequence number is new for the device.
//!
//! Telemetry lives in its own SQLite file: it is high-volume, off-chain and
//! cannot be rebuilt from the chain, unlike the index. Per device only the
//! newest `retain` snapshots are kept. Nothing here is ever written on chain;
//! deployment receipts stay the compact authoritative record.
use crate::chain;
use crate::store::Device;
use base64::{engine::general_purpose::STANDARD, Engine};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use sui_crypto::SuiVerifier;

pub const SCHEMA: u64 = 1;
/// A snapshot with a services section (SF-085, `crate::services`).
pub const SCHEMA_SERVICES: u64 = 2;
pub const MAX_MESSAGE_BYTES: usize = 64 * 1024;
pub const MAX_COMPONENTS: usize = 64;
pub const MAX_REASONS: usize = 16;
pub const MAX_TEXT: usize = 200;
pub const MAX_NAME: usize = 128;
pub const MAX_BATCH: usize = 16;
pub const MIN_INTERVAL_SECONDS: u64 = 10;
pub const MAX_INTERVAL_SECONDS: u64 = 3600;
/// Device clocks may run ahead of the indexer's by at most this much.
pub const MAX_CLOCK_AHEAD_MS: u64 = 5 * 60 * 1000;
/// A device is offline after missing this many heartbeat intervals.
pub const OFFLINE_INTERVALS: u64 = 3;
/// A snapshot received this many intervals after it was taken is delayed.
pub const DELAYED_INTERVALS: u64 = 2;

const SCHEMA_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
  device_id TEXT NOT NULL, seq INTEGER NOT NULL, signer TEXT NOT NULL,
  observed_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL, digest TEXT NOT NULL,
  message TEXT NOT NULL, signature TEXT NOT NULL, PRIMARY KEY (device_id, seq));
CREATE INDEX IF NOT EXISTS reports_signer ON reports(device_id, signer, seq);
CREATE TABLE IF NOT EXISTS log_batches (
  device_id TEXT NOT NULL, seq INTEGER NOT NULL, signer TEXT NOT NULL,
  observed_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL, digest TEXT NOT NULL,
  dropped_lines INTEGER NOT NULL, redactions INTEGER NOT NULL,
  message TEXT NOT NULL, signature TEXT NOT NULL, PRIMARY KEY (device_id, seq));
CREATE TABLE IF NOT EXISTS log_lines (
  device_id TEXT NOT NULL, seq INTEGER NOT NULL, idx INTEGER NOT NULL,
  source TEXT NOT NULL, line TEXT NOT NULL, observed_ms INTEGER NOT NULL,
  PRIMARY KEY (device_id, seq, idx));
CREATE TABLE IF NOT EXISTS support_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL, token TEXT NOT NULL,
  route TEXT NOT NULL, device_id TEXT, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alert_state (
  key TEXT PRIMARY KEY, first_ms INTEGER NOT NULL, notified_ms INTEGER, body TEXT NOT NULL);
"#;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SyncHealth {
    pub last_success_ms: Option<u64>,
    pub consecutive_failures: u32,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ComponentHealth {
    pub name: String,
    pub version: String,
    pub deployment_id: String,
    pub installed: bool,
    pub supervised: bool,
    pub phase: Option<String>,
    pub restarts: Option<u32>,
    pub last_exit: Option<String>,
    pub since_ms: Option<u64>,
}

/// The daemon's snapshot (suifleet-edge/src/telemetry.rs): schema 1, or
/// schema 2 with a services section.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub schema: u64,
    pub chain_identifier: String,
    pub registry: String,
    pub device_id: String,
    pub signer: String,
    pub seq: u64,
    pub observed_ms: u64,
    pub interval_seconds: u64,
    pub daemon_version: String,
    pub boot_id: Option<String>,
    pub started_ms: u64,
    pub dropped: u64,
    pub sync: SyncHealth,
    pub pending_receipts: u32,
    pub components: Vec<ComponentHealth>,
    pub health: String,
    pub reasons: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub services: Option<crate::services::Services>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Envelope {
    pub message: String,
    pub signature: String,
}

/// Outcome for one envelope, in request order. `retry` asks the device to
/// keep it (and everything after it) for later; `rejected` is final.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Verdict {
    pub status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub seq: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl Verdict {
    pub(crate) fn ok(status: &'static str, seq: u64) -> Self {
        Self { status, seq: Some(seq), error: None }
    }
    pub(crate) fn rejected(error: impl Into<String>, seq: Option<u64>) -> Self {
        Self { status: "rejected", seq, error: Some(error.into()) }
    }
    pub(crate) fn retry(error: impl Into<String>, seq: Option<u64>) -> Self {
        Self { status: "retry", seq, error: Some(error.into()) }
    }
}

/// A stored, verified snapshot.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Report {
    pub device_id: String,
    pub seq: u64,
    pub signer: String,
    pub observed_ms: u64,
    pub received_ms: u64,
    pub message: String,
    pub signature: String,
    #[serde(skip)]
    pub snapshot: Snapshot,
}

/// The chain and registry the telemetry store is bound to.
#[derive(Debug, Clone)]
pub struct Binding {
    pub chain_identifier: String,
    pub registry: String,
}

pub fn open(path: &Path, binding: &Binding, reset: bool) -> anyhow::Result<Connection> {
    let connection = Connection::open(path)?;
    connection.pragma_update(None, "journal_mode", "WAL")?;
    connection.pragma_update(None, "synchronous", "FULL")?;
    connection.busy_timeout(std::time::Duration::from_secs(10))?;
    connection.execute_batch(SCHEMA_SQL)?;
    let bound: Option<(String, String)> = connection
        .query_row(
            "SELECT (SELECT value FROM meta WHERE key = 'chain_identifier'), (SELECT value FROM meta WHERE key = 'registry')",
            [],
            |row| Ok(row.get::<_, Option<String>>(0)?.zip(row.get::<_, Option<String>>(1)?)),
        )?;
    let wanted = (binding.chain_identifier.clone(), binding.registry.clone());
    match bound {
        Some(bound) if bound == wanted => {}
        Some(bound) if !reset => anyhow::bail!(
            "telemetry database is bound to chain {} registry {}, not this deployment (chain {} registry {}); \
             pass --reset to discard its telemetry",
            bound.0, bound.1, wanted.0, wanted.1
        ),
        _ => {
            let transaction = connection.unchecked_transaction()?;
            for table in ["reports", "log_batches", "log_lines", "alert_state"] {
                transaction.execute(&format!("DELETE FROM {table}"), [])?;
            }
            for (key, value) in [("chain_identifier", &wanted.0), ("registry", &wanted.1)] {
                transaction.execute(
                    "INSERT INTO meta (key, value) VALUES (?1, ?2) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    params![key, value],
                )?;
            }
            transaction.commit()?;
        }
    }
    Ok(connection)
}

/// Shared by the API: one writer connection, read connections per request.
pub struct Service {
    pub path: PathBuf,
    pub binding: Binding,
    pub retain: usize,
    pub writer: std::sync::Mutex<Connection>,
}

impl Service {
    pub fn reader(&self) -> anyhow::Result<Connection> {
        let connection = Connection::open_with_flags(&self.path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        connection.busy_timeout(std::time::Duration::from_secs(10))?;
        Ok(connection)
    }
}

pub fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
        .unwrap_or(0)
}

/// Verify the signature over the exact message bytes and return the snapshot
/// with the signer's address.
pub fn verify(envelope: &Envelope) -> Result<(Snapshot, String), String> {
    let (bytes, address) = verify_bytes(envelope)?;
    let snapshot: Snapshot = serde_json::from_slice(&bytes).map_err(|e| format!("invalid snapshot: {e}"))?;
    Ok((snapshot, address))
}

/// Verify a device personal-message envelope (telemetry or logs): the exact
/// bytes and the normalized address of the ed25519/P-256 key that signed them.
pub fn verify_bytes(envelope: &Envelope) -> Result<(Vec<u8>, String), String> {
    let bytes = STANDARD.decode(&envelope.message).map_err(|_| "message is not base64")?;
    if bytes.len() > MAX_MESSAGE_BYTES {
        return Err(format!("message exceeds {MAX_MESSAGE_BYTES} bytes"));
    }
    let signature = STANDARD.decode(&envelope.signature).map_err(|_| "signature is not base64")?;
    let signature = sui_sdk_types::UserSignature::from_bytes(&signature).map_err(|_| "undecodable signature")?;
    // Device keys are plain ed25519 or P-256 keys (SF-021), never multisig,
    // zkLogin or passkeys.
    let address = match &signature {
        sui_sdk_types::UserSignature::Simple(sui_sdk_types::SimpleSignature::Ed25519 { public_key, .. }) => public_key.derive_address(),
        sui_sdk_types::UserSignature::Simple(sui_sdk_types::SimpleSignature::Secp256r1 { public_key, .. }) => public_key.derive_address(),
        _ => return Err("unsupported signature scheme (ed25519 or secp256r1 only)".into()),
    };
    sui_crypto::UserSignatureVerifier::new()
        .verify_personal_message(&sui_sdk_types::PersonalMessage(bytes.as_slice().into()), &signature)
        .map_err(|_| "signature does not verify")?;
    Ok((bytes, chain::normalize(&address.to_string()).unwrap_or_default()))
}

/// Bounds a snapshot must keep.
pub fn validate(snapshot: &Snapshot) -> Result<(), String> {
    match (snapshot.schema, &snapshot.services) {
        (SCHEMA, None) => {}
        (SCHEMA_SERVICES, Some(services)) => crate::services::validate(services)?,
        (SCHEMA | SCHEMA_SERVICES, _) => return Err("schema 2, and only schema 2, has a services section".into()),
        (schema, _) => return Err(format!("unsupported telemetry schema {schema}")),
    }
    if !(MIN_INTERVAL_SECONDS..=MAX_INTERVAL_SECONDS).contains(&snapshot.interval_seconds) {
        return Err("interval_seconds out of range".into());
    }
    if snapshot.seq == 0 || snapshot.seq > i64::MAX as u64 || snapshot.observed_ms > i64::MAX as u64 {
        return Err("seq or observed_ms out of range".into());
    }
    if !matches!(snapshot.health.as_str(), "healthy" | "degraded") {
        return Err("health must be healthy or degraded".into());
    }
    if snapshot.health == "healthy" && !snapshot.reasons.is_empty() {
        return Err("a healthy snapshot lists no reasons".into());
    }
    if snapshot.components.len() > MAX_COMPONENTS || snapshot.reasons.len() > MAX_REASONS {
        return Err("too many components or reasons".into());
    }
    let short = |text: &str, limit| text.len() <= limit;
    let texts = snapshot.reasons.iter().all(|r| short(r, MAX_TEXT))
        && snapshot.sync.last_error.as_deref().is_none_or(|e| short(e, MAX_TEXT))
        && short(&snapshot.daemon_version, MAX_NAME)
        && snapshot.boot_id.as_deref().is_none_or(|b| short(b, MAX_NAME))
        && snapshot.components.iter().all(|c| {
            short(&c.name, MAX_NAME) && short(&c.version, MAX_NAME) && short(&c.deployment_id, MAX_NAME)
                && c.phase.as_deref().is_none_or(|p| short(p, MAX_NAME))
                && c.last_exit.as_deref().is_none_or(|e| short(e, MAX_TEXT))
        });
    if !texts {
        return Err("a text field exceeds its limit".into());
    }
    Ok(())
}

/// Check one envelope against the indexed device and store it.
pub fn ingest(
    telemetry: &Connection,
    binding: &Binding,
    retain: usize,
    device_of: impl Fn(&str) -> anyhow::Result<Option<Device>>,
    envelope: &Envelope,
    now: u64,
) -> anyhow::Result<Verdict> {
    let (snapshot, signer) = match verify(envelope) {
        Ok(found) => found,
        Err(error) => return Ok(Verdict::rejected(error, None)),
    };
    let seq = Some(snapshot.seq);
    if let Err(error) = validate(&snapshot) {
        return Ok(Verdict::rejected(error, seq));
    }
    if snapshot.chain_identifier != binding.chain_identifier
        || chain::normalize(&snapshot.registry).as_deref() != Some(binding.registry.as_str())
    {
        return Ok(Verdict::rejected("snapshot is bound to another chain or registry", seq));
    }
    if chain::normalize(&snapshot.signer).as_deref() != Some(signer.as_str()) {
        return Ok(Verdict::rejected("snapshot names another signer than the one that signed it", seq));
    }
    let Some(device_id) = chain::normalize(&snapshot.device_id) else {
        return Ok(Verdict::rejected("invalid device ID", seq));
    };
    // An enrollment can be newer than the index: let the device retry.
    let Some(device) = device_of(&device_id)? else {
        return Ok(Verdict::retry("device not indexed yet", seq));
    };
    if device.deleted || device.revoked {
        return Ok(Verdict::rejected("device identity is revoked or deregistered", seq));
    }
    if !device.activated || device.signer.as_deref().and_then(chain::normalize).as_deref() != Some(signer.as_str()) {
        return Ok(Verdict::rejected("signer is not the device's registry identity", seq));
    }
    if snapshot.observed_ms > now.saturating_add(MAX_CLOCK_AHEAD_MS) {
        return Ok(Verdict::rejected("device clock is ahead of the indexer", seq));
    }
    let digest = hex::encode(sui_sdk_types::hash::Hasher::digest(STANDARD.decode(&envelope.message)?).inner());
    let existing: Option<String> = telemetry
        .query_row(
            "SELECT digest FROM reports WHERE device_id = ?1 AND seq = ?2",
            params![device_id, snapshot.seq as i64],
            |row| row.get(0),
        )
        .optional()?;
    match existing {
        Some(existing) if existing == digest => return Ok(Verdict::ok("duplicate", snapshot.seq)),
        Some(_) => return Ok(Verdict::rejected("sequence number already used for another snapshot", seq)),
        None => {}
    }
    let transaction = telemetry.unchecked_transaction()?;
    transaction.execute(
        "INSERT INTO reports (device_id, seq, signer, observed_ms, received_ms, digest, message, signature)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        params![device_id, snapshot.seq as i64, signer, snapshot.observed_ms as i64, now as i64, digest,
                envelope.message, envelope.signature],
    )?;
    transaction.execute(
        "DELETE FROM reports WHERE device_id = ?1 AND seq < (
           SELECT seq FROM reports WHERE device_id = ?1 ORDER BY seq DESC LIMIT 1 OFFSET ?2)",
        params![device_id, retain.saturating_sub(1) as i64],
    )?;
    transaction.commit()?;
    Ok(Verdict::ok("accepted", snapshot.seq))
}

fn report(row: &rusqlite::Row) -> rusqlite::Result<Report> {
    let message: String = row.get(5)?;
    let snapshot = STANDARD
        .decode(&message)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .ok_or_else(|| rusqlite::Error::InvalidColumnType(5, "message".into(), rusqlite::types::Type::Text))?;
    Ok(Report {
        device_id: row.get(0)?,
        seq: row.get::<_, i64>(1)? as u64,
        signer: row.get(2)?,
        observed_ms: row.get::<_, i64>(3)? as u64,
        received_ms: row.get::<_, i64>(4)? as u64,
        message,
        signature: row.get(6)?,
        snapshot,
    })
}

const COLUMNS: &str = "device_id, seq, signer, observed_ms, received_ms, message, signature";

/// The newest snapshot signed by `signer` (the device's current identity).
pub fn latest(connection: &Connection, device_id: &str, signer: &str) -> anyhow::Result<Option<Report>> {
    Ok(connection
        .query_row(
            &format!("SELECT {COLUMNS} FROM reports WHERE device_id = ?1 AND signer = ?2 ORDER BY seq DESC LIMIT 1"),
            params![device_id, signer],
            report,
        )
        .optional()?)
}

/// A device's stored snapshots, newest first, before `before` (a seq).
pub fn history(connection: &Connection, device_id: &str, before: Option<u64>, limit: usize) -> anyhow::Result<Vec<Report>> {
    let mut statement = connection.prepare(&format!(
        "SELECT {COLUMNS} FROM reports WHERE device_id = ?1 AND seq < ?2 ORDER BY seq DESC LIMIT ?3"
    ))?;
    let rows = statement.query_map(
        params![device_id, before.map(|b| b.min(i64::MAX as u64) as i64).unwrap_or(i64::MAX), limit as i64],
        report,
    )?;
    Ok(rows.collect::<Result<_, _>>()?)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Status {
    Unreported,
    Offline,
    Degraded,
    Healthy,
}

/// A device's fleet status. `codes` are stable (shared with the dApp through
/// interfaces/telemetry-cases.json); `reasons` explain them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Assessment {
    pub status: Status,
    pub codes: Vec<&'static str>,
    pub reasons: Vec<String>,
}

/// Fleet status from the device's identity and its newest snapshot from that
/// identity's signer. `healthy` requires a fresh, timely snapshot whose own
/// judgement is healthy and whose facts agree with it.
pub fn assess(revoked: bool, latest: Option<(&Snapshot, u64)>, now: u64) -> Assessment {
    let mut codes = Vec::new();
    let mut reasons = Vec::new();
    if revoked {
        return Assessment {
            status: Status::Unreported,
            codes: vec!["revoked"],
            reasons: vec!["device identity is revoked or deregistered; its telemetry is not trusted".into()],
        };
    }
    let Some((snapshot, received_ms)) = latest else {
        return Assessment { status: Status::Unreported, codes: vec!["no-telemetry"], reasons: vec!["no telemetry received".into()] };
    };
    let interval = snapshot.interval_seconds.clamp(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS) * 1000;
    let offline = now.saturating_sub(received_ms) > OFFLINE_INTERVALS * interval;
    if offline {
        codes.push("offline");
        reasons.push(format!("no report for {} s", now.saturating_sub(received_ms) / 1000));
    }
    if received_ms.saturating_sub(snapshot.observed_ms) > DELAYED_INTERVALS * interval {
        codes.push("delayed");
        reasons.push("newest report was taken long before it arrived (buffered offline, or the device clock is behind)".into());
    }
    if snapshot.health != "healthy" || !snapshot.reasons.is_empty() {
        codes.push("device-degraded");
        reasons.extend(snapshot.reasons.iter().cloned());
        if snapshot.reasons.is_empty() {
            reasons.push("device reports degraded".into());
        }
    } else if snapshot.components.iter().any(|c| !c.installed || (c.supervised && c.phase.as_deref() != Some("running"))) {
        codes.push("inconsistent");
        reasons.push("device reports healthy, but a component is not installed or not running".into());
    }
    let status = if offline {
        Status::Offline
    } else if codes.is_empty() {
        Status::Healthy
    } else {
        Status::Degraded
    };
    Assessment { status, codes, reasons }
}

/// One device's health, as the API serves it.
#[derive(Debug, Clone, Serialize)]
pub struct DeviceHealth {
    pub device_id: String,
    pub name: String,
    pub owner: Option<String>,
    pub signer: Option<String>,
    pub revoked: bool,
    #[serde(flatten)]
    pub assessment: Assessment,
    pub last_seen_ms: Option<u64>,
    pub observed_ms: Option<u64>,
    pub seq: Option<u64>,
    pub daemon_version: Option<String>,
    pub components: Vec<ComponentHealth>,
    /// The signed snapshot itself, for clients that verify it independently.
    pub report: Option<Report>,
}

pub fn device_health(connection: &Connection, device: &Device, now: u64) -> anyhow::Result<DeviceHealth> {
    let signer = device.signer.as_deref().and_then(chain::normalize);
    let latest = match (&signer, device.activated) {
        (Some(signer), true) => latest(connection, &device.id, signer)?,
        _ => None,
    };
    let revoked = device.revoked || device.deleted;
    let assessment = assess(revoked, latest.as_ref().map(|r| (&r.snapshot, r.received_ms)), now);
    let trusted = latest.filter(|_| !revoked);
    Ok(DeviceHealth {
        device_id: device.id.clone(),
        name: device.name.clone(),
        owner: device.owner.clone(),
        signer,
        revoked,
        assessment,
        last_seen_ms: trusted.as_ref().map(|r| r.received_ms),
        observed_ms: trusted.as_ref().map(|r| r.observed_ms),
        seq: trusted.as_ref().map(|r| r.seq),
        daemon_version: trusted.as_ref().map(|r| r.snapshot.daemon_version.clone()),
        components: trusted.as_ref().map(|r| r.snapshot.components.clone()).unwrap_or_default(),
        report: trusted,
    })
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) const KEY: [u8; 32] = [7; 32];

    fn cases() -> serde_json::Value {
        serde_json::from_str(include_str!("../../interfaces/telemetry-cases.json")).unwrap()
    }

    pub(crate) fn sign(snapshot: &Snapshot, key: &[u8; 32]) -> Envelope {
        use sui_crypto::SuiSigner;
        let bytes = serde_json::to_vec(snapshot).unwrap();
        let signer = sui_crypto::ed25519::Ed25519PrivateKey::new(*key);
        let signature = signer.sign_personal_message(&sui_sdk_types::PersonalMessage(bytes.as_slice().into())).unwrap();
        Envelope { message: STANDARD.encode(&bytes), signature: STANDARD.encode(signature.to_bytes()) }
    }

    pub(crate) fn address(key: &[u8; 32]) -> String {
        let public = sui_crypto::ed25519::Ed25519PrivateKey::new(*key).public_key();
        chain::normalize(&public.derive_address().to_string()).unwrap()
    }

    pub(crate) fn binding() -> Binding {
        Binding { chain_identifier: "abcd1234".into(), registry: chain::normalize("0x5").unwrap() }
    }

    pub(crate) fn snapshot(seq: u64, observed_ms: u64) -> Snapshot {
        Snapshot {
            schema: 1, chain_identifier: "abcd1234".into(), registry: "0x5".into(), device_id: "0xd1".into(),
            signer: address(&KEY), seq, observed_ms, interval_seconds: 60, daemon_version: "0.2.1".into(),
            boot_id: None, started_ms: 0, dropped: 0,
            sync: SyncHealth { last_success_ms: Some(observed_ms), consecutive_failures: 0, last_error: None },
            pending_receipts: 0,
            components: vec![ComponentHealth {
                name: "fw".into(), version: "1.0.0".into(), deployment_id: "0xa".into(), installed: true,
                supervised: true, phase: Some("running".into()), restarts: Some(0), last_exit: None, since_ms: Some(1),
            }],
            health: "healthy".into(), reasons: vec![], services: None,
        }
    }

    pub(crate) fn device(signer: &str) -> Device {
        Device {
            id: chain::normalize("0xd1").unwrap(), owner: Some(chain::normalize("0x0a").unwrap()), name: "pi".into(),
            device_address: signer.into(), arch: "aarch64".into(), registered_at: 0, deleted: false,
            activated: true, revoked: false, signer: Some(signer.into()), owner_cap: None, groups: vec![],
        }
    }

    fn db() -> (tempfile::TempDir, Connection) {
        let dir = tempfile::tempdir().unwrap();
        let connection = open(&dir.path().join("telemetry.db"), &binding(), false).unwrap();
        (dir, connection)
    }

    #[test]
    fn shared_vector_verifies_and_is_ingested() {
        let cases = cases();
        for case in ["signed", "signed_services"] {
            let vector: Envelope = serde_json::from_value(cases[case]["envelope"].clone()).unwrap();
            let (snapshot, signer) = verify(&vector).unwrap();
            assert_eq!(signer, chain::normalize(cases[case]["signer"].as_str().unwrap()).unwrap());
            assert_eq!(snapshot, serde_json::from_value::<Snapshot>(cases[case]["snapshot"].clone()).unwrap());
            // The edge signs the same bytes to the same signature (ed25519 is deterministic).
            assert_eq!(sign(&snapshot, &KEY).signature, vector.signature, "{case}");
            let (_dir, telemetry) = db();
            let bound = Binding { chain_identifier: snapshot.chain_identifier.clone(), registry: chain::normalize(&snapshot.registry).unwrap() };
            let mut found = device(&signer);
            found.id = chain::normalize(&snapshot.device_id).unwrap();
            let verdict = ingest(&telemetry, &bound, 10, |_| Ok(Some(found.clone())), &vector, snapshot.observed_ms).unwrap();
            assert_eq!(verdict.status, "accepted", "{case}: {verdict:?}");
            // Any changed byte breaks the signature.
            let mut tampered = vector.clone();
            let mut bytes = STANDARD.decode(&tampered.message).unwrap();
            *bytes.last_mut().unwrap() ^= 1;
            tampered.message = STANDARD.encode(bytes);
            assert_eq!(verify(&tampered).unwrap_err(), "signature does not verify");
        }
    }

    #[test]
    fn ingress_authenticates_binds_and_deduplicates() {
        let (_dir, telemetry) = db();
        let me = address(&KEY);
        let now = 1_000_000;
        let ok = |d: Device| move |_: &str| Ok(Some(d.clone()));
        let run = |envelope: &Envelope, d: Option<Device>| {
            ingest(&telemetry, &binding(), 3, move |_| Ok(d.clone()), envelope, now).unwrap()
        };
        let first = sign(&snapshot(10, now), &KEY);
        assert_eq!(run(&first, Some(device(&me))).status, "accepted");
        assert_eq!(run(&first, Some(device(&me))), Verdict::ok("duplicate", 10));
        let mut other = snapshot(10, now);
        other.pending_receipts = 1;
        assert_eq!(run(&sign(&other, &KEY), Some(device(&me))).error.unwrap(), "sequence number already used for another snapshot");
        // Not yet indexed: the device keeps it.
        assert_eq!(run(&sign(&snapshot(11, now), &KEY), None).status, "retry");
        // Another key, even claiming this device's signer.
        let stranger = sign(&snapshot(12, now), &[8; 32]);
        assert_eq!(run(&stranger, Some(device(&me))).error.unwrap(), "snapshot names another signer than the one that signed it");
        let mut theirs = snapshot(12, now);
        theirs.signer = address(&[8; 32]);
        assert_eq!(run(&sign(&theirs, &[8; 32]), Some(device(&me))).error.unwrap(), "signer is not the device's registry identity");
        // A rotated-out key, a revoked or unactivated device.
        assert_eq!(run(&sign(&snapshot(13, now), &KEY), Some(device(&address(&[9; 32])))).status, "rejected");
        for change in [|d: &mut Device| d.revoked = true, |d: &mut Device| d.deleted = true, |d: &mut Device| d.activated = false] {
            let mut d = device(&me);
            change(&mut d);
            assert_eq!(run(&sign(&snapshot(14, now), &KEY), Some(d)).status, "rejected");
        }
        // Other chain or registry, future clock, bounds, schema.
        let mut s = snapshot(15, now);
        s.chain_identifier = "4c78adac".into();
        assert_eq!(run(&sign(&s, &KEY), Some(device(&me))).error.unwrap(), "snapshot is bound to another chain or registry");
        let mut s = snapshot(15, now);
        s.registry = "0x6".into();
        assert_eq!(run(&sign(&s, &KEY), Some(device(&me))).status, "rejected");
        let ahead = sign(&snapshot(16, now + MAX_CLOCK_AHEAD_MS + 1), &KEY);
        assert_eq!(run(&ahead, Some(device(&me))).error.unwrap(), "device clock is ahead of the indexer");
        for change in [
            |s: &mut Snapshot| s.schema = 2,
            |s: &mut Snapshot| s.interval_seconds = 1,
            |s: &mut Snapshot| s.health = "great".into(),
            |s: &mut Snapshot| s.reasons = vec!["x".into()],
            |s: &mut Snapshot| s.reasons = vec!["x".repeat(MAX_TEXT + 1)],
            |s: &mut Snapshot| { s.health = "degraded".into(); s.reasons = vec!["x".into(); MAX_REASONS + 1] },
            |s: &mut Snapshot| s.components = vec![s.components[0].clone(); MAX_COMPONENTS + 1],
            |s: &mut Snapshot| s.components[0].name = "n".repeat(MAX_NAME + 1),
            |s: &mut Snapshot| s.seq = i64::MAX as u64 + 1,
        ] {
            let mut s = snapshot(17, now);
            change(&mut s);
            assert_eq!(run(&sign(&s, &KEY), Some(device(&me))).status, "rejected", "{s:?}");
        }
        // Schema 2 carries a valid services section (SF-085), and only it does.
        let mut with = snapshot(17, now);
        with.services = Some(crate::services::tests::services());
        assert_eq!(run(&sign(&with, &KEY), Some(device(&me))).error.unwrap(), "schema 2, and only schema 2, has a services section");
        with.services.as_mut().unwrap().version = 2;
        with.schema = SCHEMA_SERVICES;
        assert_eq!(run(&sign(&with, &KEY), Some(device(&me))).error.unwrap(), "unsupported services section version 2");
        with.services.as_mut().unwrap().version = 1;
        assert_eq!(run(&sign(&with, &KEY), Some(device(&me))).status, "accepted");
        let mut unknown = serde_json::to_value(snapshot(17, now)).unwrap();
        (unknown["schema"], unknown["services"]) = (2.into(), serde_json::to_value(crate::services::tests::services()).unwrap());
        unknown["services"]["messaging"]["queues"][0]["topic"] = "plant/valves".into();
        let bytes = serde_json::to_vec(&unknown).unwrap();
        let key = sui_crypto::ed25519::Ed25519PrivateKey::new(KEY);
        let signature = sui_crypto::SuiSigner::sign_personal_message(&key, &sui_sdk_types::PersonalMessage(bytes.as_slice().into())).unwrap();
        let envelope = Envelope { message: STANDARD.encode(&bytes), signature: STANDARD.encode(signature.to_bytes()) };
        assert!(run(&envelope, Some(device(&me))).error.unwrap().starts_with("invalid snapshot"), "unknown service fields are refused");
        // Unknown fields and oversize messages are refused.
        let mut extended = serde_json::to_value(snapshot(18, now)).unwrap();
        extended["extra"] = true.into();
        let bytes = serde_json::to_vec(&extended).unwrap();
        let signer = sui_crypto::ed25519::Ed25519PrivateKey::new(KEY);
        let signature = sui_crypto::SuiSigner::sign_personal_message(&signer, &sui_sdk_types::PersonalMessage(bytes.as_slice().into())).unwrap();
        let envelope = Envelope { message: STANDARD.encode(&bytes), signature: STANDARD.encode(signature.to_bytes()) };
        assert!(run(&envelope, Some(device(&me))).error.unwrap().starts_with("invalid snapshot"));
        let huge = Envelope { message: STANDARD.encode(vec![b' '; MAX_MESSAGE_BYTES + 1]), signature: first.signature.clone() };
        assert_eq!(run(&huge, Some(device(&me))).status, "rejected");
        assert_eq!(run(&Envelope { message: "!".into(), signature: "!".into() }, None).status, "rejected");
        // Retention keeps the newest three; the latest is by sequence.
        for seq in [20, 19, 21, 22] {
            assert_eq!(ingest(&telemetry, &binding(), 3, ok(device(&me)), &sign(&snapshot(seq, now), &KEY), now).unwrap().status, "accepted");
        }
        let id = chain::normalize("0xd1").unwrap();
        assert_eq!(history(&telemetry, &id, None, 10).unwrap().iter().map(|r| r.seq).collect::<Vec<_>>(), [22, 21, 20]);
        assert_eq!(history(&telemetry, &id, Some(22), 1).unwrap()[0].seq, 21);
        assert_eq!(latest(&telemetry, &id, &me).unwrap().unwrap().seq, 22);
        assert!(latest(&telemetry, &id, &address(&[9; 32])).unwrap().is_none());
    }

    #[test]
    fn shared_status_cases() {
        for case in cases()["status"].as_array().unwrap() {
            let latest: Option<Snapshot> = serde_json::from_value(case["snapshot"].clone()).unwrap();
            let received = case["received_ms"].as_u64();
            let assessment = assess(
                case["revoked"].as_bool().unwrap_or(false),
                latest.as_ref().zip(received),
                case["now_ms"].as_u64().unwrap(),
            );
            let status = serde_json::to_value(assessment.status).unwrap();
            assert_eq!(status, case["status"], "{}", case["name"]);
            assert_eq!(serde_json::to_value(&assessment.codes).unwrap(), case["codes"], "{}", case["name"]);
        }
    }

    #[test]
    fn store_is_bound_to_one_deployment() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("telemetry.db");
        let connection = open(&path, &binding(), false).unwrap();
        let me = address(&KEY);
        assert_eq!(ingest(&connection, &binding(), 3, |_| Ok(Some(device(&me))), &sign(&snapshot(1, 5), &KEY), 5).unwrap().status, "accepted");
        drop(connection);
        let other = Binding { chain_identifier: "4c78adac".into(), registry: binding().registry };
        assert!(open(&path, &other, false).unwrap_err().to_string().contains("--reset"));
        assert!(open(&path, &binding(), false).is_ok(), "refusal changed nothing");
        let reset = open(&path, &other, true).unwrap();
        let count: i64 = reset.query_row("SELECT COUNT(*) FROM reports", [], |row| row.get(0)).unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn device_health_ignores_untrusted_reports() {
        let (_dir, telemetry) = db();
        let me = address(&KEY);
        let now = 1_000_000;
        ingest(&telemetry, &binding(), 3, |_| Ok(Some(device(&me))), &sign(&snapshot(1, now), &KEY), now).unwrap();
        let health = device_health(&telemetry, &device(&me), now + 1000).unwrap();
        assert_eq!(health.assessment.status, Status::Healthy);
        assert_eq!((health.last_seen_ms, health.seq, health.components.len()), (Some(now), Some(1), 1));
        // After rotation, the old key's reports no longer count.
        let rotated = device_health(&telemetry, &device(&address(&[9; 32])), now).unwrap();
        assert_eq!((rotated.assessment.status, rotated.report.is_none()), (Status::Unreported, true));
        let mut revoked = device(&me);
        revoked.revoked = true;
        let revoked = device_health(&telemetry, &revoked, now).unwrap();
        assert_eq!((revoked.assessment.codes.clone(), revoked.last_seen_ms), (vec!["revoked"], None));
        let offline = device_health(&telemetry, &device(&me), now + OFFLINE_INTERVALS * 60_000 + 1).unwrap();
        assert_eq!(offline.assessment.status, Status::Offline);
    }
}
