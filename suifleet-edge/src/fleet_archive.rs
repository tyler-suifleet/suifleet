//! Durable encrypted archive scheduler. The local codec handles public
//! ciphertext and storage-node traffic only; all signatures remain native.
use std::{path::{Path, PathBuf}, str::FromStr, time::Duration};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sui_sdk_types::Address;
use crate::{fleet_data::{Archive, Record, Task}, telemetry};

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub storage_package: String,
    pub storage_type_origin: String,
    pub system_id: String,
    pub walrus_package: String,
    pub key_server_package: String,
    pub key_servers: Vec<crate::config::KeyServerConfig>,
    pub threshold: u8,
    /// Explicitly configured local Node executable and reviewed codec file.
    pub node: PathBuf,
    pub codec: PathBuf,
    pub codec_sha256: String,
}
impl Config {
    pub fn validate(&self) -> anyhow::Result<()> {
        for id in [&self.storage_package, &self.storage_type_origin, &self.system_id, &self.walrus_package, &self.key_server_package] { Address::from_str(id)?; }
        anyhow::ensure!(!self.key_servers.is_empty() && self.key_servers.len() < 255 && self.threshold > 0
            && self.threshold as usize <= self.key_servers.len() && self.key_servers.iter().all(|s| s.weight == 1), "invalid archive encryption providers/threshold (equal weights required)");
        anyhow::ensure!(self.node.is_absolute() && self.codec.is_absolute() && self.codec_sha256.len() == 64
            && hex::decode(&self.codec_sha256)?.len() == 32, "configure absolute codec paths and SHA256");
        let mut ids = std::collections::HashSet::new();
        for server in &self.key_servers {
            anyhow::ensure!(ids.insert(Address::from_str(&server.object_id)?), "duplicate archive key server");
            anyhow::ensure!(server.api_key_name.is_none() && server.api_key_file.is_none(), "public-key archive encryption needs no provider credentials");
        }
        Ok(())
    }
}
#[derive(Serialize, Deserialize)]
struct Pending {
    seq: u64, kind: u8, request_seq: u64, fingerprint: String, anchor_index: u64, logs: Vec<u64>,
    blob_id: String, root_hash: String, ciphertext_sha256: Vec<u8>,
    #[serde(default)] registration: Option<String>,
    #[serde(default)] blob_object_id: Option<String>,
    #[serde(default)] certificate: Option<Certificate>,
    #[serde(default)] certification_digest: Option<String>,
    #[serde(default)] anchor_digest: Option<String>,
    /// Fingerprint of the archived shadow snapshot (schema 2), if any.
    #[serde(default)] shadows: String,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Certificate { pub signature: String, pub bitmap: String, pub message: String }
#[derive(Default, Serialize, Deserialize)]
struct Completed { crash: String, #[serde(default)] request_seq: u64,
    #[serde(default)] shadows: String,
    #[serde(default)] seq: u64, #[serde(default)] registration: Option<String>,
    #[serde(default)] certification: Option<String>, #[serde(default)] anchor: Option<String> }
fn atomic(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    use std::{io::Write, os::unix::fs::OpenOptionsExt};
    let temporary = path.with_extension("tmp");
    let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&temporary)?;
    f.write_all(bytes)?; f.sync_all()?; std::fs::rename(&temporary, path)?;
    std::fs::File::open(path.parent().unwrap())?.sync_all()?; Ok(())
}
fn load<T: for<'a> Deserialize<'a>>(path: &Path) -> anyhow::Result<Option<T>> {
    match std::fs::read(path) {
        Ok(b) => Ok(Some(serde_json::from_slice(&b).map_err(|_| anyhow::anyhow!("invalid archive journal"))?)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None), Err(e) => Err(e.into()),
    }
}
async fn codec(task: &Task, config: &Config, dir: &Path, operation: &str, extra: Value) -> anyhow::Result<Value> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    anyhow::ensure!(hex::encode(Sha256::digest(std::fs::read(&config.codec)?)) == config.codec_sha256.to_ascii_lowercase(), "archive codec checksum mismatch");
    let mut request = json!({ "operation": operation, "chain": task.sui.manifest().chain_identifier,
        "rpc_url": task.sui.manifest().rpc_url, "ciphertext_path": dir.join("ciphertext.bin"),
        "system_id": config.system_id, "walrus_package": config.walrus_package,
        "storage_package": config.storage_package, "storage_type_origin": config.storage_type_origin,
        "storage_id": task.config.storage_id, "device_id": task.device_id });
    for (k,v) in extra.as_object().ok_or_else(|| anyhow::anyhow!("invalid codec arguments"))? { request[k] = v.clone(); }
    let mut command = tokio::process::Command::new(&config.node);
    command.arg(&config.codec);
    crate::apply::harden(&mut command, dir, None);
    command.stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::null());
    let mut input = serde_json::to_vec(&request)?; input.push(b'\n');
    // The supervisor kills unregistered children. Keep the helper registered
    // and reap its entire process group on timeout/error, even if our caller
    // is cancelled while storage-node traffic is in flight.
    let result = tokio::spawn(async move {
        let mut child = crate::supervisor::spawn_tracked(&mut command)?;
        let mut reaped = false;
        let result = tokio::time::timeout(Duration::from_secs(300), async {
            child.take_stdin().ok_or_else(|| anyhow::anyhow!("missing codec input"))?.write_all(&input).await?;
            let mut stdout = child.take_stdout().ok_or_else(|| anyhow::anyhow!("missing codec output"))?.take(16_385);
            let mut output = Vec::new(); stdout.read_to_end(&mut output).await?;
            anyhow::ensure!(output.len() <= 16_384, "archive codec response too large");
            let status = child.wait().await?; reaped = true;
            anyhow::ensure!(status.success(), "archive codec unavailable");
            Ok::<_, anyhow::Error>(serde_json::from_slice::<Value>(&output).map_err(|_| anyhow::anyhow!("invalid codec response"))?)
        }).await.unwrap_or_else(|_| Err(anyhow::anyhow!("archive codec timeout")));
        if !reaped { crate::apply::kill_group(child.pid() as i32); let _ = child.wait().await; }
        result
    }).await.map_err(|_| anyhow::anyhow!("archive codec task failed"))??;
    Ok(result)
}
fn string(v: &Value, field: &str) -> anyhow::Result<String> {
    Ok(v[field].as_str().ok_or_else(|| anyhow::anyhow!("invalid codec field {field}"))?.to_owned())
}

pub async fn tick(task: &Task, fleet_settings: Option<&crate::fleet_settings::Settings>) -> anyhow::Result<()> {
    let Some(config) = &task.config.archives else { return Ok(()); };
    let settings = task.log_settings.as_ref().ok_or_else(|| anyhow::anyhow!("archives need [logs] collection limits"))?;
    let dir = task.state_dir.join("fleet-data"); std::fs::create_dir_all(&dir)?;
    let mut spool = telemetry::Spool::open(&dir.join("logs"), settings.buffer_max)?;
    let collector = crate::logs::Task { binding: crate::logs::Binding { chain_identifier: task.sui.manifest().chain_identifier.clone(),
        registry: task.sui.manifest().shared_objects.registry.id.clone(), device_id: task.device_id.clone() },
        // Collection has no network effect. This URL is never used.
        url: "http://127.0.0.1/".parse()?, state_dir: task.state_dir.clone(), overlay_dir: task.overlay_dir.clone(),
        interval_seconds: settings.interval_seconds, max_lines: settings.max_lines_per_interval, buffer_max: settings.buffer_max, signer: task.signer.clone() };
    collector.collect(&mut spool)?;
    task.sui.validate_cap(&task.cap, &task.device_id).await?;
    let (fleet, writer) = task.config.read(&task.sui, &task.device_id).await?;
    let completed: Completed = load(&dir.join("completed.json"))?.unwrap_or_default();
    let pending_path = dir.join("pending.json");
    let mut pending: Pending = if let Some(pending) = load(&pending_path)? { pending } else {
        let clock = task.sui.object("0x6").await?;
        let now = clock.fields["timestamp_ms"].as_u64().or_else(|| clock.fields["timestamp_ms"].as_str().and_then(|s| s.parse().ok())).ok_or_else(|| anyhow::anyhow!("invalid chain clock"))?;
        let inventory = telemetry::inventory(&task.state_dir);
        let crashed: Vec<_> = inventory.components.iter().filter(|c| matches!(c.phase.as_deref(), Some("broken" | "backoff"))).collect();
        let fingerprint = if crashed.is_empty() { String::new() } else { hex::encode(Sha256::digest(serde_json::to_vec(&crashed)?)) };
        let request_seq = task.sui.requested_diagnostics(&task.config).await?;
        let diagnostics = request_seq > completed.request_seq;
        let crash = !fingerprint.is_empty() && fingerprint != completed.crash;
        let kind = if diagnostics { 1 } else if crash { 2 } else { 0 };
        // SF-084: with shadows enabled every archive carries their current
        // documents (schema 2), and a change archives on the routine cadence.
        let shadows = task.services.lock().unwrap().health_sources().shadows.map(|store| store.lock().unwrap().snapshot());
        let shadow_fingerprint = match &shadows { Some(s) => hex::encode(Sha256::digest(serde_json::to_vec(s)?)), None => String::new() };
        let shadows_changed = shadows.is_some() && shadow_fingerprint != completed.shadows;
        let shadow_bytes = match &shadows { Some(s) => serde_json::to_vec(s)?.len(), None => 0 };
        let last = if kind != 0 { writer.emergency_at_ms } else { writer.batch_at_ms };
        let interval = if kind != 0 { fleet.emergency_interval_ms } else { crate::fleet_settings::archive_interval_ms(fleet.batch_interval_ms, fleet_settings) };
        if last.is_some_and(|at| now < at.saturating_add(interval)) { return Ok(()); }
        let mut records = Vec::new(); let mut logs = Vec::new(); let mut bytes = 0;
        let mut dropped_lines = spool.dropped().saturating_mul(crate::logs::MAX_RECORDS as u64);
        for (seq,path) in spool.pending()? {
            let envelope: telemetry::Envelope = serde_json::from_slice(&std::fs::read(path)?)?;
            let message = zeroize::Zeroizing::new(STANDARD.decode(envelope.message)?);
            let batch: crate::logs::Batch = serde_json::from_slice(&message).map_err(|_| anyhow::anyhow!("invalid archived log batch"))?;
            crate::logs::validate(&batch)?;
            anyhow::ensure!(batch.chain_identifier == fleet.chain_identifier && crate::sui_client::same_id(&batch.registry, &fleet.registry_id.to_string())
                && crate::sui_client::same_id(&batch.device_id,&task.device_id) && crate::sui_client::same_id(&batch.signer,&task.signer.address()), "foreign log spool binding");
            let size = message.len();
            // The shadow snapshot shares the plaintext budget with the logs.
            if bytes + size + shadow_bytes > 2*1024*1024 || records.len() + batch.records.len() > 4096 { break; }
            bytes += size; logs.push(seq); dropped_lines = dropped_lines.max(batch.dropped_lines);
            records.extend(batch.records.into_iter().map(|r| Record { source: r.source, line: r.line }));
        }
        if records.is_empty() && kind == 0 && !shadows_changed { return Ok(()); }
        if kind != 0 && records.len() < 4096 { records.push(Record { source: "daemon".into(), line: if diagnostics { "Owner/support diagnostic request observed" } else { "Supervised component crash observed" }.into() }); }
        if records.is_empty() { records.push(Record { source: "daemon".into(), line: "Shadow state snapshot".into() }); }
        let seq = writer.batch_seq.checked_add(1).filter(|n| *n <= 9_007_199_254_740_991).ok_or_else(|| anyhow::anyhow!("archive sequence exhausted"))?;
        let archive = Archive { kind: "fleet-data".into(), chain_identifier: fleet.chain_identifier.clone(),
            registry: fleet.registry_id.to_string(), fleet_id: fleet.id.to_string(), device_id: task.device_id.clone(), signer: task.signer.address(),
            seq, observed_ms: telemetry::now_ms(), reason: ["logs", "diagnostics", "crash"][kind as usize].into(), dropped_lines, records,
            schema: if shadows.is_some() { 2 } else { 1 }, shadows };
        archive.validate()?;
        let message = zeroize::Zeroizing::new(serde_json::to_vec(&archive)?);
        let signature = task.signer.sign_personal_message(&message)?;
        let plaintext = zeroize::Zeroizing::new(serde_json::to_vec(&telemetry::Envelope { message: STANDARD.encode(&message), signature: STANDARD.encode(signature) })?);
        let mut servers = Vec::new();
        for s in &config.key_servers { let server = crate::seal_client::load_key_server(&task.sui, &config.key_server_package, s).await?; servers.push((server.object_id, server.public_key)); }
        let mut id = fleet.id.inner().to_vec(); id.extend(fleet.key_epoch.to_le_bytes());
        use rand::RngCore; let mut nonce = [0;16]; rand::rngs::OsRng.fill_bytes(&mut nonce); id.extend(nonce);
        let aad = format!("suifleet/fleet-data/v1/{}/{}/{}/{}",fleet.chain_identifier,fleet.id,seq,kind).into_bytes();
        let ciphertext = crate::seal::encrypt(*Address::from_str(&task.config.type_origin)?.inner(), id, &servers, config.threshold, aad, &plaintext)?;
        anyhow::ensure!(ciphertext.len() <= crate::fleet_data::MAX_ARCHIVE, "encrypted archive too large");
        atomic(&dir.join("ciphertext.bin"), &ciphertext)?;
        let encoded = codec(task, config, &dir, "prepare", json!({})).await?;
        let pending = Pending { seq, kind, request_seq: if diagnostics { request_seq } else { completed.request_seq }, fingerprint, anchor_index: writer.anchor_count, logs,
            blob_id: string(&encoded,"blob_id")?, root_hash: string(&encoded,"root_hash")?, ciphertext_sha256: Sha256::digest(&ciphertext).to_vec(),
            registration: None, blob_object_id: None, certificate: None, certification_digest: None, anchor_digest: None, shadows: shadow_fingerprint };
        atomic(&pending_path, &serde_json::to_vec(&pending)?)?; pending
    };
    // Never regenerate ciphertext or purchase again after an uncertain submit.
    anyhow::ensure!(Sha256::digest(std::fs::read(dir.join("ciphertext.bin"))?).as_slice() == pending.ciphertext_sha256, "pending ciphertext changed");
    if writer.batch_seq >= pending.seq {
        // A chain anchor, not an execution response, acknowledges completion.
        task.sui.verify_archive_anchor(&task.config, pending.anchor_index, pending.seq, &pending.ciphertext_sha256).await?;
        for (seq,path) in spool.pending()? { if pending.logs.contains(&seq) { std::fs::remove_file(path)?; } }
        std::fs::File::open(dir.join("logs"))?.sync_all()?;
        atomic(&dir.join("completed.json"), &serde_json::to_vec(&Completed { crash: pending.fingerprint, request_seq: pending.request_seq, shadows: pending.shadows, seq: pending.seq, registration: pending.registration, certification: pending.certification_digest, anchor: pending.anchor_digest })?)?;
        for name in ["register.bcs", "certify.bcs", "anchor.bcs", "register.checkpoint", "certify.checkpoint", "anchor.checkpoint"] {
            match std::fs::remove_file(dir.join(name)) { Ok(()) => (), Err(e) if e.kind() == std::io::ErrorKind::NotFound => (), Err(e) => return Err(e.into()) }
        }
        std::fs::File::open(&dir)?.sync_all()?;
        std::fs::remove_file(&pending_path)?; std::fs::File::open(&dir)?.sync_all()?;
        let _ = std::fs::remove_file(dir.join("ciphertext.bin"));
        return Ok(());
    }
    anyhow::ensure!(crate::seal::parse(&std::fs::read(dir.join("ciphertext.bin"))?)?.key_epoch() == Some(fleet.key_epoch), "pending archive key epoch was revoked; owner recovery required");
    if pending.registration.is_none() {
        let digest = task.sui.register_archive(&task.config, config, &task.cap, &pending.blob_id, &pending.root_hash,
            std::fs::metadata(dir.join("ciphertext.bin"))?.len(), &dir.join("register.bcs")).await?;
        pending.registration = Some(digest); atomic(&pending_path, &serde_json::to_vec(&pending)?)?;
    }
    if pending.blob_object_id.is_none() {
        let registered = codec(task,config,&dir,"registered",json!({ "digest":pending.registration })).await?;
        pending.blob_object_id = Some(string(&registered,"blob_object_id")?); atomic(&pending_path,&serde_json::to_vec(&pending)?)?;
    }
    let blob = pending.blob_object_id.as_ref().unwrap();
    if pending.certificate.is_none() {
        let result = codec(task,config,&dir,"write",json!({"blob_id":pending.blob_id,"root_hash":pending.root_hash,"blob_object_id":blob})).await?;
        pending.certificate = Some(serde_json::from_value(result)?); atomic(&pending_path,&serde_json::to_vec(&pending)?)?;
    }
    if pending.certification_digest.is_none() {
        match task.sui.certify_archive(&task.config,config,blob,pending.certificate.as_ref().unwrap(),&dir.join("certify.bcs")).await {
            Ok(digest) => pending.certification_digest=Some(digest),
            Err(error) => {
                // Missing signed intent means simulation failed before signing,
                // or effects proved a failed execution. Refresh confirmations
                // for this same blob, never registration or ciphertext. Unknown
                // signed transactions keep their exact certificate/bytes.
                if !dir.join("certify.bcs").try_exists()? {
                    pending.certificate=None;
                    atomic(&pending_path,&serde_json::to_vec(&pending)?)?;
                }
                return Err(error);
            }
        }
        atomic(&pending_path,&serde_json::to_vec(&pending)?)?;
    }
    if pending.anchor_digest.is_none() {
        pending.anchor_digest=Some(task.sui.anchor_archive(&task.config,&task.cap,blob,pending.seq,pending.kind,pending.ciphertext_sha256.clone(),&dir.join("anchor.bcs")).await?);
        atomic(&pending_path,&serde_json::to_vec(&pending)?)?;
    }
    Ok(()) // Next tick verifies the anchored record before deleting any spool data.
}

/// Persistent categorical status only: errors can quote private upstream data.
pub fn status(state_dir: &Path, blocked: bool) -> anyhow::Result<()> {
    let dir = state_dir.join("fleet-data"); std::fs::create_dir_all(&dir)?;
    atomic(&dir.join("status.json"), &serde_json::to_vec(&json!({"schema":1,"blocked":blocked,"at_ms":telemetry::now_ms()}))?)
}

pub fn health_receipt(state_dir: &Path, seq: u64, digest: &str) -> anyhow::Result<()> {
    let dir=state_dir.join("fleet-data");std::fs::create_dir_all(&dir)?;
    atomic(&dir.join("health.json"), &serde_json::to_vec(&json!({"schema":1,"seq":seq,"digest":digest,"at_ms":telemetry::now_ms()}))?)
}
