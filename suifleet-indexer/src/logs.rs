//! Device log ingress, search and diagnostic bundles (SF-020).
//!
//! `POST /v1/logs` takes the device's signed log batches (no token) and
//! verifies them exactly like telemetry: the signature over the exact bytes,
//! the device's current registry identity, the chain and registry binding, the
//! clock and a new `seq`. Lines are kept per device up to `retain` lines.
//! Every line served (search, bundles) is redacted again with the daemon's own
//! rules ([`crate::redact`]), in case an older daemon did not.
//!
//! Search and bundles need a `support` token, and each access is audited.
use crate::chain;
use crate::store::{self, Device, Filter};
use crate::telemetry::{verify_bytes, Envelope, Verdict, MAX_CLOCK_AHEAD_MS};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

pub const SCHEMA: u64 = 1;
pub const KIND: &str = "logs";
pub const MAX_MESSAGE_BYTES: usize = 64 * 1024;
pub const MAX_RECORDS: usize = 256;
pub const MAX_LINE: usize = 1024;
pub const MAX_SOURCE: usize = 160;
/// Lines kept per device by default (`--logs-retain`).
pub const DEFAULT_RETAIN: usize = 20_000;
/// Lines in a diagnostic bundle.
pub const BUNDLE_LINES: usize = 500;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Record {
    pub source: String,
    pub line: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Batch {
    pub schema: u64,
    pub kind: String,
    pub chain_identifier: String,
    pub registry: String,
    pub device_id: String,
    pub signer: String,
    pub seq: u64,
    pub observed_ms: u64,
    pub dropped_lines: u64,
    pub redactions: u64,
    pub records: Vec<Record>,
}

pub fn validate(batch: &Batch) -> Result<(), String> {
    if batch.schema != SCHEMA || batch.kind != KIND {
        return Err("not a schema-1 log batch".into());
    }
    if batch.records.is_empty() || batch.records.len() > MAX_RECORDS {
        return Err(format!("a log batch holds 1 to {MAX_RECORDS} records"));
    }
    let ok = batch.records.iter().all(|r| {
        r.line.len() <= MAX_LINE && !r.line.contains('\n') && r.source.len() <= MAX_SOURCE
            && (r.source == "daemon" || r.source.strip_prefix("component:").is_some_and(|n| !n.is_empty()))
    });
    if !ok {
        return Err("a log record exceeds its limits".into());
    }
    if batch.seq == 0 || batch.seq > i64::MAX as u64 || batch.observed_ms > i64::MAX as u64 {
        return Err("seq or observed_ms out of range".into());
    }
    Ok(())
}

/// Check one log envelope against the indexed device and store its lines.
pub fn ingest(
    db: &Connection,
    binding: &crate::telemetry::Binding,
    retain: usize,
    device_of: impl Fn(&str) -> anyhow::Result<Option<Device>>,
    envelope: &Envelope,
    now: u64,
) -> anyhow::Result<Verdict> {
    let (bytes, signer) = match verify_bytes(envelope) {
        Ok(found) => found,
        Err(error) => return Ok(Verdict::rejected(error, None)),
    };
    if bytes.len() > MAX_MESSAGE_BYTES {
        return Ok(Verdict::rejected("log batch too large", None));
    }
    let batch: Batch = match serde_json::from_slice(&bytes) {
        Ok(batch) => batch,
        Err(error) => return Ok(Verdict::rejected(format!("invalid log batch: {error}"), None)),
    };
    let seq = Some(batch.seq);
    if let Err(error) = validate(&batch) {
        return Ok(Verdict::rejected(error, seq));
    }
    if batch.chain_identifier != binding.chain_identifier || chain::normalize(&batch.registry).as_deref() != Some(binding.registry.as_str()) {
        return Ok(Verdict::rejected("log batch is bound to another chain or registry", seq));
    }
    if chain::normalize(&batch.signer).as_deref() != Some(signer.as_str()) {
        return Ok(Verdict::rejected("log batch names another signer than the one that signed it", seq));
    }
    let Some(device_id) = chain::normalize(&batch.device_id) else {
        return Ok(Verdict::rejected("invalid device ID", seq));
    };
    let Some(device) = device_of(&device_id)? else {
        return Ok(Verdict::retry("device not indexed yet", seq));
    };
    if device.deleted || device.revoked {
        return Ok(Verdict::rejected("device identity is revoked or deregistered", seq));
    }
    if !device.activated || device.signer.as_deref().and_then(chain::normalize).as_deref() != Some(signer.as_str()) {
        return Ok(Verdict::rejected("signer is not the device's registry identity", seq));
    }
    if batch.observed_ms > now.saturating_add(MAX_CLOCK_AHEAD_MS) {
        return Ok(Verdict::rejected("device clock is ahead of the indexer", seq));
    }
    let digest = hex::encode(sui_sdk_types::hash::Hasher::digest(&bytes).inner());
    let existing: Option<String> = db
        .query_row("SELECT digest FROM log_batches WHERE device_id = ?1 AND seq = ?2", params![device_id, batch.seq as i64], |row| row.get(0))
        .optional()?;
    match existing {
        Some(existing) if existing == digest => return Ok(Verdict::ok("duplicate", batch.seq)),
        Some(_) => return Ok(Verdict::rejected("sequence number already used for another batch", seq)),
        None => {}
    }
    let transaction = db.unchecked_transaction()?;
    transaction.execute(
        "INSERT INTO log_batches (device_id, seq, signer, observed_ms, received_ms, digest, dropped_lines, redactions, message, signature)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        params![device_id, batch.seq as i64, signer, batch.observed_ms as i64, now as i64, digest,
                batch.dropped_lines.min(i64::MAX as u64) as i64, batch.redactions.min(i64::MAX as u64) as i64,
                envelope.message, envelope.signature],
    )?;
    for (index, record) in batch.records.iter().enumerate() {
        transaction.execute(
            "INSERT INTO log_lines (device_id, seq, idx, source, line, observed_ms) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![device_id, batch.seq as i64, index as i64, record.source, record.line, batch.observed_ms as i64],
        )?;
    }
    // Keep the newest `retain` lines of this device.
    transaction.execute(
        "DELETE FROM log_lines WHERE device_id = ?1 AND (seq, idx) < (
           SELECT seq, idx FROM log_lines WHERE device_id = ?1 ORDER BY seq DESC, idx DESC LIMIT 1 OFFSET ?2)",
        params![device_id, retain.saturating_sub(1) as i64],
    )?;
    transaction.execute(
        "DELETE FROM log_batches WHERE device_id = ?1 AND NOT EXISTS (
           SELECT 1 FROM log_lines l WHERE l.device_id = log_batches.device_id AND l.seq = log_batches.seq)",
        params![device_id],
    )?;
    transaction.commit()?;
    Ok(Verdict::ok("accepted", batch.seq))
}

/// One served (redacted) line.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Line {
    pub seq: u64,
    pub idx: u64,
    pub source: String,
    pub line: String,
    pub observed_ms: u64,
}

#[derive(Debug, Clone, Default)]
pub struct Search {
    /// Case-insensitive substring.
    pub q: Option<String>,
    pub source: Option<String>,
    pub since_ms: Option<u64>,
    /// Resume below this `(seq, idx)` (newest first).
    pub before: Option<(u64, u64)>,
    pub limit: usize,
}

/// Lines of a device, newest first, redacted again.
pub fn search(db: &Connection, device_id: &str, query: &Search) -> anyhow::Result<Vec<Line>> {
    let (before_seq, before_idx) = query.before.map(|(s, i)| (s as i64, i as i64)).unwrap_or((i64::MAX, i64::MAX));
    let mut statement = db.prepare_cached(
        "SELECT seq, idx, source, line, observed_ms FROM log_lines
          WHERE device_id = ?1 AND (seq, idx) < (?2, ?3)
            AND (?4 IS NULL OR instr(lower(line), lower(?4)) > 0)
            AND (?5 IS NULL OR source = ?5)
            AND (?6 IS NULL OR observed_ms >= ?6)
          ORDER BY seq DESC, idx DESC LIMIT ?7",
    )?;
    let rows = statement.query_map(
        params![device_id, before_seq, before_idx, query.q, query.source, query.since_ms.map(|m| m as i64), query.limit as i64],
        |row| {
            Ok(Line {
                seq: row.get::<_, i64>(0)? as u64,
                idx: row.get::<_, i64>(1)? as u64,
                source: row.get(2)?,
                line: crate::redact::redact(&row.get::<_, String>(3)?, &[]).0,
                observed_ms: row.get::<_, i64>(4)? as u64,
            })
        },
    )?;
    Ok(rows.collect::<Result<_, _>>()?)
}

/// Totals for a device's stored logs.
pub fn stats(db: &Connection, device_id: &str) -> anyhow::Result<Value> {
    let (batches, last_received, dropped, redactions): (i64, Option<i64>, Option<i64>, Option<i64>) = db.query_row(
        "SELECT count(*), max(received_ms), max(dropped_lines), sum(redactions) FROM log_batches WHERE device_id = ?1",
        [device_id],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
    )?;
    let lines: i64 = db.query_row("SELECT count(*) FROM log_lines WHERE device_id = ?1", [device_id], |row| row.get(0))?;
    Ok(json!({ "batches": batches, "lines": lines, "last_received_ms": last_received,
               "dropped_lines": dropped.unwrap_or(0), "redactions": redactions.unwrap_or(0) }))
}

/// A diagnostic bundle: everything the indexer knows about one device that
/// support needs, assembled from stored data (no command reaches the device).
pub fn bundle(index: &Connection, telemetry: &Connection, device: &Device, now: u64, stuck_after_ms: u64) -> anyhow::Result<Value> {
    let health = crate::telemetry::device_health(telemetry, device, now)?;
    let reports: Vec<Value> = crate::telemetry::history(telemetry, &device.id, None, 5)?
        .into_iter()
        .map(|r| json!({ "seq": r.seq, "observed_ms": r.observed_ms, "received_ms": r.received_ms, "snapshot": r.snapshot }))
        .collect();
    let mut deployments = Vec::new();
    let filter = Filter { device: Some(device.id.clone()), ..Filter::default() };
    let mut cursor = None;
    loop {
        let page = store::list_deployments(index, &filter, cursor.as_deref(), Some(store::MAX_LIMIT))?;
        for deployment in &page.items {
            let status = store::read_receipts(index, &deployment.id)?.into_iter().find(|(id, _)| id == &device.id).map(|(_, s)| s);
            deployments.push(json!({
                "deployment_id": deployment.id, "component": deployment.component_name, "version": deployment.version,
                "created_at": deployment.created_at, "receipt": match status { Some(1) => "applied", Some(2) => "failed", Some(_) => "other", None => "none" },
            }));
        }
        match page.next {
            Some(next) => cursor = Some(next),
            None => break,
        }
    }
    let alerts = crate::alerts::for_device(index, telemetry, device, now, stuck_after_ms)?;
    let logs = search(telemetry, &device.id, &Search { limit: BUNDLE_LINES, ..Search::default() })?;
    Ok(json!({
        "generated_ms": now,
        "device": device,
        "health": health,
        "reports": reports,
        "deployments": deployments,
        "alerts": alerts,
        "log_stats": stats(telemetry, &device.id)?,
        "logs": logs,
    }))
}

/// Record a support access (who by token fingerprint, what, which device).
pub fn audit(db: &Connection, token: &str, route: &str, device_id: Option<&str>, detail: &Value, now: u64) -> anyhow::Result<()> {
    db.execute(
        "INSERT INTO support_audit (at_ms, token, route, device_id, detail) VALUES (?1, ?2, ?3, ?4, ?5)",
        params![now as i64, token, route, device_id, detail.to_string()],
    )?;
    Ok(())
}

/// The newest audit entries.
pub fn audit_log(db: &Connection, limit: usize) -> anyhow::Result<Vec<Value>> {
    let mut statement = db.prepare_cached("SELECT at_ms, token, route, device_id, detail FROM support_audit ORDER BY id DESC LIMIT ?1")?;
    let rows = statement.query_map([limit as i64], |row| {
        Ok(json!({
            "at_ms": row.get::<_, i64>(0)?, "token": row.get::<_, String>(1)?, "route": row.get::<_, String>(2)?,
            "device_id": row.get::<_, Option<String>>(3)?,
            "detail": serde_json::from_str::<Value>(&row.get::<_, String>(4)?).unwrap_or(Value::Null),
        }))
    })?;
    Ok(rows.collect::<Result<_, _>>()?)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::telemetry::tests::{address, binding, device, KEY};
    use base64::{engine::general_purpose::STANDARD, Engine};

    pub(crate) fn batch(seq: u64, lines: &[(&str, &str)]) -> Batch {
        Batch {
            schema: 1, kind: "logs".into(), chain_identifier: "abcd1234".into(), registry: "0x5".into(), device_id: "0xd1".into(),
            signer: address(&KEY), seq, observed_ms: 1_000 + seq, dropped_lines: 0, redactions: 0,
            records: lines.iter().map(|(s, l)| Record { source: (*s).into(), line: (*l).into() }).collect(),
        }
    }

    pub(crate) fn sign(batch: &Batch, key: &[u8; 32]) -> Envelope {
        use sui_crypto::SuiSigner;
        let bytes = serde_json::to_vec(batch).unwrap();
        let signer = sui_crypto::ed25519::Ed25519PrivateKey::new(*key);
        let signature = signer.sign_personal_message(&sui_sdk_types::PersonalMessage(bytes.as_slice().into())).unwrap();
        Envelope { message: STANDARD.encode(&bytes), signature: STANDARD.encode(signature.to_bytes()) }
    }

    fn db() -> (tempfile::TempDir, Connection) {
        let dir = tempfile::tempdir().unwrap();
        let connection = crate::telemetry::open(&dir.path().join("telemetry.db"), &binding(), false).unwrap();
        (dir, connection)
    }

    #[test]
    fn ingest_verifies_like_telemetry() {
        let (_dir, db) = db();
        let me = device(&address(&KEY));
        let found = |_: &str| Ok(Some(me.clone()));
        let ok = sign(&batch(1, &[("daemon", "started"), ("component:app", "ready")]), &KEY);
        assert_eq!(ingest(&db, &binding(), 100, found, &ok, 2_000).unwrap().status, "accepted");
        assert_eq!(ingest(&db, &binding(), 100, found, &ok, 2_000).unwrap().status, "duplicate");
        let reused = sign(&batch(1, &[("daemon", "other")]), &KEY);
        assert_eq!(ingest(&db, &binding(), 100, found, &reused, 2_000).unwrap().status, "rejected");
        let cases: Vec<(&str, Envelope, &str)> = vec![
            ("other chain", sign(&Batch { chain_identifier: "ffff0000".into(), ..batch(2, &[("daemon", "x")]) }, &KEY), "another chain"),
            ("other key", sign(&batch(3, &[("daemon", "x")]), &[9; 32]), "another signer"),
            ("long line", sign(&batch(4, &[("daemon", &"x".repeat(MAX_LINE + 1))]), &KEY), "exceeds its limits"),
            ("bad source", sign(&batch(5, &[("kernel", "x")]), &KEY), "exceeds its limits"),
            ("future clock", sign(&Batch { observed_ms: 2_000 + MAX_CLOCK_AHEAD_MS + 1, ..batch(6, &[("daemon", "x")]) }, &KEY), "ahead"),
        ];
        for (label, envelope, expected) in cases {
            let verdict = ingest(&db, &binding(), 100, found, &envelope, 2_000).unwrap();
            assert_eq!(verdict.status, "rejected", "{label}");
            assert!(verdict.error.as_deref().unwrap_or("").contains(expected), "{label}: {verdict:?}");
        }
        let unknown = ingest(&db, &binding(), 100, |_: &str| Ok(None), &sign(&batch(7, &[("daemon", "x")]), &KEY), 2_000).unwrap();
        assert_eq!(unknown.status, "retry");
        let mut revoked = me.clone();
        revoked.revoked = true;
        let refused = ingest(&db, &binding(), 100, |_: &str| Ok(Some(revoked.clone())), &sign(&batch(8, &[("daemon", "x")]), &KEY), 2_000).unwrap();
        assert_eq!(refused.status, "rejected");
        let mut tampered = sign(&batch(9, &[("daemon", "x")]), &KEY);
        tampered.signature = sign(&batch(10, &[("daemon", "x")]), &KEY).signature;
        assert_eq!(ingest(&db, &binding(), 100, found, &tampered, 2_000).unwrap().status, "rejected");
    }

    #[test]
    fn retention_search_and_redaction_on_output() {
        let (_dir, db) = db();
        let me = device(&address(&KEY));
        let found = |_: &str| Ok(Some(me.clone()));
        for seq in 1..=3u64 {
            let lines = [("daemon", format!("batch {seq} a")), ("component:app", format!("batch {seq} B")), ("daemon", format!("password=hunter{seq}"))];
            let lines: Vec<(&str, &str)> = lines.iter().map(|(s, l)| (*s, l.as_str())).collect();
            assert_eq!(ingest(&db, &binding(), 5, found, &sign(&batch(seq, &lines), &KEY), 5_000).unwrap().status, "accepted");
        }
        let id = me.id.clone();
        let all = search(&db, &id, &Search { limit: 100, ..Search::default() }).unwrap();
        assert_eq!(all.len(), 5, "kept the newest 5 lines");
        assert_eq!((all[0].seq, all[0].idx), (3, 2));
        let batches: i64 = db.query_row("SELECT count(*) FROM log_batches", [], |r| r.get(0)).unwrap();
        assert_eq!(batches, 2, "a batch without kept lines is dropped");
        // Lines an old daemon did not redact are redacted when served.
        assert!(all.iter().all(|l| !l.line.contains("hunter")), "{all:?}");
        assert_eq!(all[0].line, "password=[REDACTED]");
        let found = search(&db, &id, &Search { q: Some("BATCH 3".into()), limit: 100, ..Search::default() }).unwrap();
        assert_eq!(found.iter().map(|l| l.line.as_str()).collect::<Vec<_>>(), ["batch 3 B", "batch 3 a"]);
        let app = search(&db, &id, &Search { source: Some("component:app".into()), limit: 100, ..Search::default() }).unwrap();
        assert!(app.iter().all(|l| l.source == "component:app") && app.len() == 2);
        let page = search(&db, &id, &Search { before: Some((3, 1)), limit: 2, ..Search::default() }).unwrap();
        assert_eq!(page.iter().map(|l| (l.seq, l.idx)).collect::<Vec<_>>(), [(3, 0), (2, 2)]);
        let stats = stats(&db, &id).unwrap();
        assert_eq!((stats["lines"].as_i64(), stats["batches"].as_i64()), (Some(5), Some(2)));
    }

    #[test]
    fn audit_entries_are_kept_newest_first() {
        let (_dir, db) = db();
        audit(&db, "aaaa", "logs", Some("0xd1"), &json!({ "q": "error" }), 1).unwrap();
        audit(&db, "bbbb", "diagnostics", Some("0xd1"), &json!({}), 2).unwrap();
        let log = audit_log(&db, 10).unwrap();
        assert_eq!(log.iter().map(|e| e["route"].as_str().unwrap()).collect::<Vec<_>>(), ["diagnostics", "logs"]);
        assert_eq!(log[1]["detail"]["q"], "error");
    }
}
