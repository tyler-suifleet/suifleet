//! Device telemetry (SF-019): heartbeat, installed inventory and health.
//!
//! Every `interval_seconds` the daemon builds a snapshot of what is installed
//! and how it runs, signs it with the device key as a Sui personal message
//! (never valid as a transaction), and appends it to a bounded spool under
//! `<state_dir>/telemetry`. The spool is then uploaded oldest first to the
//! fleet indexer (`POST {url}/v1/telemetry`, docs/TELEMETRY.md). While the
//! indexer is unreachable snapshots stay spooled, across restarts; beyond
//! `buffer_max` the oldest are dropped and counted in later snapshots.
//!
//! Telemetry is off-chain and high-volume. It never replaces the compact,
//! authoritative deployment receipts the daemon reports on chain.
//!
//! The snapshot states facts (versions, supervised phases, sync results) and
//! the device's own judgement: `healthy` only when nothing below is wrong.
use crate::identity::TxSigner;
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tracing::{info, warn};

pub const SCHEMA: u64 = 1;
pub const MAX_MESSAGE_BYTES: usize = 64 * 1024;
pub const MAX_COMPONENTS: usize = 64;
pub const MAX_REASONS: usize = 16;
pub const MAX_TEXT: usize = 200;
pub const MAX_NAME: usize = 128;
/// Envelopes per upload request.
pub const MAX_BATCH: usize = 16;
pub const MIN_INTERVAL_SECONDS: u64 = 10;
pub const MAX_INTERVAL_SECONDS: u64 = 3600;
pub const MAX_BUFFER: usize = 10_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Health {
    Healthy,
    Degraded,
}

/// The poll loop's chain synchronization, as of the latest attempt.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SyncHealth {
    pub last_success_ms: Option<u64>,
    pub consecutive_failures: u32,
    pub last_error: Option<String>,
}

impl SyncHealth {
    pub fn record(&mut self, result: &anyhow::Result<()>) {
        match result {
            Ok(()) => {
                self.last_success_ms = Some(now_ms());
                self.consecutive_failures = 0;
                self.last_error = None;
            }
            Err(error) => {
                self.consecutive_failures = self.consecutive_failures.saturating_add(1);
                self.last_error = Some(bounded(&format!("{error:#}"), MAX_TEXT));
            }
        }
    }
}

pub type SharedSync = Arc<Mutex<SyncHealth>>;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ComponentHealth {
    pub name: String,
    pub version: String,
    pub deployment_id: String,
    /// Its install is known to have succeeded (for a supervised component: its
    /// version is committed, i.e. it became ready once).
    pub installed: bool,
    pub supervised: bool,
    /// Supervised runtime phase (supervisor.rs); none for one-shot components.
    pub phase: Option<String>,
    pub restarts: Option<u32>,
    pub last_exit: Option<String>,
    pub since_ms: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub schema: u64,
    pub chain_identifier: String,
    pub registry: String,
    pub device_id: String,
    /// Address of the key that signs this snapshot.
    pub signer: String,
    /// Strictly increasing per device (see `Spool::next_seq`).
    pub seq: u64,
    /// Device clock, Unix milliseconds.
    pub observed_ms: u64,
    pub interval_seconds: u64,
    pub daemon_version: String,
    pub boot_id: Option<String>,
    pub started_ms: u64,
    /// Snapshots dropped from a full spool so far.
    pub dropped: u64,
    pub sync: SyncHealth,
    /// Deployment outcomes not yet reported on chain.
    pub pending_receipts: u32,
    pub components: Vec<ComponentHealth>,
    pub health: Health,
    pub reasons: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Envelope {
    /// Base64 of the exact snapshot JSON bytes that were signed.
    pub message: String,
    /// Base64 Sui `UserSignature` over those bytes as a personal message.
    pub signature: String,
}

pub fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
        .unwrap_or(0)
}

/// At most `limit` bytes, cut at a character boundary.
pub fn bounded(text: &str, limit: usize) -> String {
    if text.len() <= limit {
        return text.to_owned();
    }
    let mut end = limit.saturating_sub(3);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}...", &text[..end])
}

/// What the daemon's own durable state says is installed and running.
#[derive(Debug, Default)]
pub struct Inventory {
    pub components: Vec<ComponentHealth>,
    pub reasons: Vec<String>,
    pub pending_receipts: u32,
}

/// Read the journal and supervisor files (each replaced atomically by its
/// writer), never the live structures of the poll loop.
pub fn inventory(state_dir: &Path) -> Inventory {
    let mut inventory = Inventory::default();
    let journal: Option<crate::state::State> = std::fs::read(state_dir.join("state.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok());
    let Some(journal) = journal else {
        inventory.reasons.push("daemon journal unreadable".into());
        return inventory;
    };
    // Absent files are a device that never supervised anything; any other
    // failure leaves the supervisor's view unknown, which is never healthy.
    let supervised = state_dir.join("supervised");
    let active: Option<BTreeMap<String, crate::supervisor::Spec>> =
        read_state(&supervised.join("active.json"), "supervisor state", &mut inventory.reasons);
    let status: Option<BTreeMap<String, crate::supervisor::Status>> =
        read_state(&supervised.join("status.json"), "supervisor status", &mut inventory.reasons);
    let no_status = BTreeMap::new();
    let status_known = status.is_some();
    let status = status.as_ref().unwrap_or(&no_status);
    for (id, outcome) in &journal.deployments {
        if outcome.status.is_some() && !outcome.reported {
            inventory.pending_receipts = inventory.pending_receipts.saturating_add(1);
        }
        if outcome.indeterminate {
            inventory.reasons.push(format!("deployment {id}: outcome indeterminate; inspect the device"));
        }
    }
    // Without the committed specs, the supervisor's own status still names
    // what it runs; nothing else is asserted to be unsupervised by guess.
    let committed: BTreeMap<&String, (&String, &String)> = match &active {
        Some(active) => active.iter().map(|(name, spec)| (name, (&spec.version, &spec.deployment))).collect(),
        None => status.iter().map(|(name, s)| (name, (&s.version, &s.deployment))).collect(),
    };
    let mut names: Vec<&String> = journal.components.keys().chain(committed.keys().copied()).collect();
    names.sort();
    names.dedup();
    for name in names {
        let health = match committed.get(name) {
            // Supervised: what runs is the committed version, whatever a
            // failed replacement left in the journal.
            Some(&(version, deployment)) => {
                let current = status.get(name).filter(|s| &s.version == version && &s.deployment == deployment);
                let phase = current.map(|s| phase_name(s.phase));
                match phase.as_deref() {
                    Some("running") => {}
                    Some(phase) => inventory.reasons.push(match current.and_then(|s| s.last_exit.as_deref()) {
                        Some(exit) => format!("component {name}: {phase} ({exit})"),
                        None => format!("component {name}: {phase}"),
                    }),
                    None if status_known => inventory.reasons.push(format!("component {name}: not started")),
                    None => {}
                }
                ComponentHealth {
                    name: name.clone(),
                    version: version.clone(),
                    deployment_id: deployment.clone(),
                    installed: true,
                    supervised: true,
                    phase,
                    restarts: current.map(|s| s.restarts),
                    last_exit: current.and_then(|s| s.last_exit.clone()),
                    since_ms: current.map(|s| s.since.saturating_mul(1000)),
                }
            }
            None => {
                let component = &journal.components[name];
                if !component.confirmed {
                    inventory.reasons.push(format!(
                        "component {name} {}: install not confirmed (in progress, failed or interrupted)",
                        component.version
                    ));
                }
                ComponentHealth {
                    name: name.clone(),
                    version: component.version.clone(),
                    deployment_id: component.deployment_id.clone(),
                    installed: component.confirmed,
                    supervised: false,
                    phase: None,
                    restarts: None,
                    last_exit: None,
                    since_ms: None,
                }
            }
        };
        inventory.components.push(health);
    }
    inventory
}

/// A supervisor file: empty when it does not exist yet, `None` (and a
/// degraded reason) when it exists but cannot be read or parsed.
fn read_state<T: serde::de::DeserializeOwned + Default>(path: &Path, what: &str, reasons: &mut Vec<String>) -> Option<T> {
    let parsed = match std::fs::read(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Some(T::default()),
        Err(error) => Err(error.to_string()),
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|error| error.to_string()),
    };
    parsed
        .map_err(|error| reasons.push(format!("{what} unreadable ({error}); component supervision unknown")))
        .ok()
}

fn phase_name(phase: crate::supervisor::Phase) -> String {
    serde_json::to_value(phase)
        .ok()
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_else(|| "unknown".into())
}

/// Reasons the chain synchronization makes the device degraded.
pub fn sync_reasons(sync: &SyncHealth, poll_seconds: u64, now: u64) -> Vec<String> {
    let mut reasons = Vec::new();
    match sync.last_success_ms {
        None => reasons.push("chain sync not completed yet".into()),
        // A cycle may legitimately run long (an apply); allow three polls
        // beyond the longest heartbeat interval before calling it stale.
        Some(at) if now.saturating_sub(at) > 1000 * (3 * poll_seconds + MAX_INTERVAL_SECONDS) => {
            reasons.push("no successful chain sync recently".into())
        }
        Some(_) => {}
    }
    if sync.consecutive_failures > 0 {
        reasons.push(format!(
            "chain sync failing ({} consecutive): {}",
            sync.consecutive_failures,
            sync.last_error.as_deref().unwrap_or("unknown error")
        ));
    }
    reasons
}

/// Identity and settings a snapshot is bound to.
#[derive(Debug, Clone)]
pub struct Binding {
    pub chain_identifier: String,
    pub registry: String,
    pub device_id: String,
    pub interval_seconds: u64,
    pub poll_seconds: u64,
    pub started_ms: u64,
    pub boot_id: Option<String>,
}

/// Build a bounded snapshot. Everything a device can make arbitrarily long
/// is truncated, and components beyond the limits are left out (and said so).
pub fn snapshot(binding: &Binding, signer: &str, seq: u64, dropped: u64, sync: SyncHealth, inventory: Inventory, now: u64) -> Snapshot {
    let mut reasons = sync_reasons(&sync, binding.poll_seconds, now);
    reasons.extend(inventory.reasons);
    let mut components: Vec<ComponentHealth> = inventory
        .components
        .into_iter()
        .map(|c| ComponentHealth {
            name: bounded(&c.name, MAX_NAME),
            version: bounded(&c.version, MAX_NAME),
            deployment_id: bounded(&c.deployment_id, MAX_NAME),
            last_exit: c.last_exit.map(|e| bounded(&e, MAX_TEXT)),
            ..c
        })
        .collect();
    let mut omitted = components.len().saturating_sub(MAX_COMPONENTS);
    components.truncate(MAX_COMPONENTS);
    let mut sync = sync;
    sync.last_error = sync.last_error.map(|e| bounded(&e, MAX_TEXT));
    loop {
        let mut listed = reasons.iter().map(|r| bounded(r, MAX_TEXT)).collect::<Vec<_>>();
        if omitted > 0 {
            listed.insert(0, format!("{omitted} components not listed (snapshot size limit)"));
        }
        if listed.len() > MAX_REASONS {
            let more = listed.len() - (MAX_REASONS - 1);
            listed.truncate(MAX_REASONS - 1);
            listed.push(format!("{more} more problems"));
        }
        let health = if listed.is_empty() { Health::Healthy } else { Health::Degraded };
        let snapshot = Snapshot {
            schema: SCHEMA,
            chain_identifier: binding.chain_identifier.clone(),
            registry: binding.registry.clone(),
            device_id: binding.device_id.clone(),
            signer: signer.to_owned(),
            seq,
            observed_ms: now,
            interval_seconds: binding.interval_seconds,
            daemon_version: env!("CARGO_PKG_VERSION").into(),
            boot_id: binding.boot_id.clone(),
            started_ms: binding.started_ms,
            dropped,
            sync: sync.clone(),
            pending_receipts: inventory.pending_receipts,
            components: components.clone(),
            health,
            reasons: listed,
        };
        // JSON escaping can still outgrow the byte limit: list fewer.
        if components.is_empty() || serde_json::to_vec(&snapshot).map(|b| b.len()).unwrap_or(usize::MAX) <= MAX_MESSAGE_BYTES {
            return snapshot;
        }
        components.pop();
        omitted += 1;
    }
}

/// Sign a snapshot's exact bytes.
pub fn seal(signer: &dyn TxSigner, snapshot: &Snapshot) -> anyhow::Result<Envelope> {
    let bytes = serde_json::to_vec(snapshot)?;
    anyhow::ensure!(bytes.len() <= MAX_MESSAGE_BYTES, "telemetry snapshot exceeds {MAX_MESSAGE_BYTES} bytes");
    Ok(Envelope {
        message: STANDARD.encode(&bytes),
        signature: STANDARD.encode(signer.sign_personal_message(&bytes)?),
    })
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct SpoolState {
    /// Last sequence number used; persisted before it is signed.
    seq: u64,
    dropped: u64,
}

/// Bounded, crash-safe snapshot buffer: one file per envelope, named by its
/// sequence number, each written to a temporary name and renamed.
pub struct Spool {
    dir: PathBuf,
    max: usize,
    state: SpoolState,
}

impl Spool {
    pub fn open(dir: &Path, max: usize) -> anyhow::Result<Self> {
        std::fs::create_dir_all(dir)?;
        let state = match std::fs::read(dir.join("state.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => SpoolState::default(),
            Err(error) => return Err(error.into()),
        };
        for entry in std::fs::read_dir(dir)? {
            let path = entry?.path();
            if path.extension().is_some_and(|e| e == "tmp") {
                let _ = std::fs::remove_file(path);
            }
        }
        Ok(Self { dir: dir.to_owned(), max: max.max(1), state })
    }

    pub fn dropped(&self) -> u64 {
        self.state.dropped
    }

    /// Reserve the next sequence number: above every earlier one, and at least
    /// the clock in milliseconds, so a device whose state was wiped (with a
    /// correct clock) still continues above what it sent before.
    pub fn next_seq(&mut self, now: u64) -> anyhow::Result<u64> {
        let seq = self.state.seq.saturating_add(1).max(now);
        anyhow::ensure!(seq <= i64::MAX as u64, "telemetry sequence exhausted");
        self.state.seq = seq;
        self.save()?;
        Ok(seq)
    }

    /// Spool an envelope; beyond the bound the oldest are dropped and counted.
    pub fn push(&mut self, seq: u64, envelope: &Envelope) -> anyhow::Result<()> {
        write_atomic(&self.dir.join(format!("{seq:020}.json")), &serde_json::to_vec(envelope)?)?;
        let pending = self.pending()?;
        let excess = pending.len().saturating_sub(self.max);
        if excess > 0 {
            for (_, path) in &pending[..excess] {
                std::fs::remove_file(path)?;
            }
            self.state.dropped = self.state.dropped.saturating_add(excess as u64);
            self.save()?;
            warn!(dropped = excess, total = self.state.dropped, "Telemetry spool full; dropped the oldest snapshots");
        }
        Ok(())
    }

    /// Spooled envelopes, oldest first.
    pub fn pending(&self) -> anyhow::Result<Vec<(u64, PathBuf)>> {
        let mut found = Vec::new();
        for entry in std::fs::read_dir(&self.dir)? {
            let path = entry?.path();
            let seq = path
                .file_name()
                .and_then(|n| n.to_str())
                .and_then(|n| n.strip_suffix(".json"))
                .filter(|n| n.len() == 20)
                .and_then(|n| n.parse::<u64>().ok());
            if let Some(seq) = seq {
                found.push((seq, path));
            }
        }
        found.sort();
        Ok(found)
    }

    fn save(&self) -> anyhow::Result<()> {
        write_atomic(&self.dir.join("state.json"), &serde_json::to_vec(&self.state)?)
    }
}

fn write_atomic(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    use std::io::Write;
    let temporary = path.with_extension("tmp");
    let mut file = std::fs::File::create(&temporary)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    std::fs::rename(&temporary, path)?;
    std::fs::File::open(path.parent().unwrap_or(Path::new(".")))?.sync_all()?;
    Ok(())
}

/// The indexer's verdict on one envelope.
#[derive(Debug, Clone, Deserialize)]
pub struct Verdict {
    /// `accepted`, `duplicate`, `rejected` (never accepted; dropped) or
    /// `retry` (kept, with everything after it, for a later upload).
    pub status: String,
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct Response {
    results: Vec<Verdict>,
}

/// Upload the spool oldest first, in batches, until it is empty or the
/// indexer cannot take more now. Returns how many envelopes left the spool.
pub async fn flush(http: &reqwest::Client, url: &reqwest::Url, spool: &Spool) -> anyhow::Result<usize> {
    flush_as(http, url, spool, "Telemetry", "snapshot").await
}

/// [`flush`] for any signed envelope stream (`what` and `item` name it in logs).
pub async fn flush_as(http: &reqwest::Client, url: &reqwest::Url, spool: &Spool, what: &str, item: &str) -> anyhow::Result<usize> {
    let mut removed = 0;
    loop {
        let pending = spool.pending()?;
        if pending.is_empty() {
            return Ok(removed);
        }
        let batch = &pending[..pending.len().min(MAX_BATCH)];
        let mut envelopes = Vec::with_capacity(batch.len());
        for (_, path) in batch {
            envelopes.push(serde_json::from_slice::<Envelope>(&std::fs::read(path)?)?);
        }
        let response = http
            .post(url.clone())
            .json(&serde_json::json!({ "messages": envelopes }))
            .send()
            .await?;
        let status = response.status();
        anyhow::ensure!(status.is_success(), "{what} ingress answered {status}");
        let verdicts: Response = response.json().await?;
        anyhow::ensure!(verdicts.results.len() == batch.len(), "{what} ingress answered for another batch");
        for ((seq, path), verdict) in batch.iter().zip(&verdicts.results) {
            match verdict.status.as_str() {
                "accepted" | "duplicate" => {}
                "rejected" => warn!(seq, error = verdict.error.as_deref().unwrap_or(""), "{what} {item} rejected; dropped"),
                _ => {
                    info!(seq, error = verdict.error.as_deref().unwrap_or(""), "{what} ingress deferred a {item}; kept for retry");
                    return Ok(removed);
                }
            }
            std::fs::remove_file(path)?;
            removed += 1;
        }
    }
}

pub struct Task {
    pub binding: Binding,
    pub url: reqwest::Url,
    pub state_dir: PathBuf,
    pub buffer_max: usize,
    pub signer: Arc<dyn TxSigner>,
    pub sync: SharedSync,
}

impl Task {
    /// One heartbeat: spool a fresh snapshot, then upload what is spooled.
    pub async fn beat(&self, spool: &mut Spool, http: &reqwest::Client) -> anyhow::Result<()> {
        let now = now_ms();
        let seq = spool.next_seq(now)?;
        let sync = self.sync.lock().unwrap().clone();
        let snapshot = snapshot(&self.binding, &self.signer.address(), seq, spool.dropped(), sync, inventory(&self.state_dir), now);
        spool.push(seq, &seal(self.signer.as_ref(), &snapshot)?)?;
        if let Err(error) = flush(http, &self.url, spool).await {
            info!(%error, spooled = spool.pending()?.len(), "Telemetry ingress unreachable; snapshots stay spooled");
        }
        Ok(())
    }

    pub async fn run(self) {
        let http = match reqwest::Client::builder().timeout(Duration::from_secs(15)).build() {
            Ok(http) => http,
            Err(error) => return warn!(%error, "Telemetry disabled: no HTTP client"),
        };
        let mut spool = match Spool::open(&self.state_dir.join("telemetry"), self.buffer_max) {
            Ok(spool) => spool,
            Err(error) => return warn!(%error, "Telemetry disabled: spool unavailable"),
        };
        info!(url = %self.url, interval = self.binding.interval_seconds, "Reporting telemetry");
        loop {
            if let Err(error) = self.beat(&mut spool, &http).await {
                warn!(%error, "Telemetry snapshot failed");
            }
            tokio::time::sleep(Duration::from_secs(self.binding.interval_seconds)).await;
        }
    }
}

/// `{base}/v1/telemetry`, keeping any path prefix of a proxied base URL.
pub fn ingress_url(base: &str) -> anyhow::Result<reqwest::Url> {
    let mut url = reqwest::Url::parse(base)?;
    let path = format!("{}/v1/telemetry", url.path().trim_end_matches('/'));
    url.set_path(&path);
    Ok(url)
}

pub fn boot_id() -> Option<String> {
    crate::platform::boot_id().map(|id| bounded(&id, 64))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::Ed25519File;
    use ed25519_dalek::SigningKey;

    fn temp() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("suifleet-telemetry-{}", rand::random::<u128>()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn binding() -> Binding {
        Binding {
            chain_identifier: "abcd1234".into(),
            registry: "0x5".into(),
            device_id: "0xd1".into(),
            interval_seconds: 60,
            poll_seconds: 10,
            started_ms: 1_000,
            boot_id: Some("boot".into()),
        }
    }

    fn synced(now: u64) -> SyncHealth {
        SyncHealth { last_success_ms: Some(now), ..SyncHealth::default() }
    }

    #[test]
    fn inventory_reports_installed_versions_and_supervised_phases() {
        let dir = temp();
        std::fs::create_dir_all(dir.join("supervised")).unwrap();
        let component = |version: &str, deployment: &str, confirmed| serde_json::json!({
            "version": version, "deployment_id": deployment, "recipe_sha256": "r", "artifact_sha256": "a",
            "dependencies": {}, "confirmed": confirmed });
        std::fs::write(dir.join("state.json"), serde_json::to_vec(&serde_json::json!({
            "binding": "b", "cursor": null, "deployments": {
                "0xa": {"status": 1, "reported": false, "error": null},
                "0xb": {"status": null, "reported": false, "error": null, "indeterminate": true}},
            // A failed replacement of `svc` left 2.0.0 unconfirmed; 1.0.0 runs.
            "components": {"fw": component("3.1.0", "0xa", true), "svc": component("2.0.0", "0xc", false),
                           "tool": component("0.9.0", "0xd", false)},
        })).unwrap()).unwrap();
        let spec = |version: &str, deployment: &str| serde_json::json!({
            "name": "x", "version": version, "deployment": deployment, "run": "r", "shutdown": null,
            "readiness": null, "restart": "on-failure" });
        std::fs::write(dir.join("supervised/active.json"), serde_json::to_vec(&serde_json::json!({
            "svc": spec("1.0.0", "0xe"), "web": spec("1.2.0", "0xf"), "db": spec("4.0.0", "0x10") })).unwrap()).unwrap();
        std::fs::write(dir.join("supervised/status.json"), serde_json::to_vec(&serde_json::json!({
            "svc": {"version": "1.0.0", "deployment": "0xe", "phase": "running", "pid": 7, "restarts": 1, "last_exit": null, "since": 5},
            "web": {"version": "1.2.0", "deployment": "0xf", "phase": "broken", "pid": null, "restarts": 3,
                    "last_exit": "exit status: 1", "since": 6} })).unwrap()).unwrap();
        let inventory = inventory(&dir);
        let summary: Vec<_> = inventory.components.iter()
            .map(|c| (c.name.as_str(), c.version.as_str(), c.installed, c.phase.as_deref())).collect();
        assert_eq!(summary, [("db", "4.0.0", true, None), ("fw", "3.1.0", true, None), ("svc", "1.0.0", true, Some("running")),
                             ("tool", "0.9.0", false, None), ("web", "1.2.0", true, Some("broken"))]);
        assert_eq!(inventory.components[2].since_ms, Some(5000));
        assert_eq!(inventory.pending_receipts, 1);
        assert_eq!(inventory.reasons, [
            "deployment 0xb: outcome indeterminate; inspect the device",
            "component db: not started",
            "component tool 0.9.0: install not confirmed (in progress, failed or interrupted)",
            "component web: broken (exit status: 1)"]);
        std::fs::remove_dir_all(dir).unwrap();
        // No journal yet is never healthy.
        let empty = temp();
        assert_eq!(super::inventory(&empty).reasons, ["daemon journal unreadable"]);
        std::fs::remove_dir_all(empty).unwrap();
    }

    #[test]
    fn unreadable_supervisor_state_is_never_healthy() {
        let now = 10_000_000;
        let journal = serde_json::to_vec(&serde_json::json!({
            "binding": "b", "cursor": null, "deployments": {},
            "components": {"svc": {"version": "1.0.0", "deployment_id": "0xe", "recipe_sha256": "r", "artifact_sha256": "a",
                                   "dependencies": {}, "confirmed": true}},
        })).unwrap();
        let broken = serde_json::to_vec(&serde_json::json!({
            "svc": {"version": "1.0.0", "deployment": "0xe", "phase": "broken", "pid": null, "restarts": 3,
                    "last_exit": "exit status: 1", "since": 6} })).unwrap();
        let observe = |dir: &Path| {
            let inventory = inventory(dir);
            let components: Vec<_> = inventory.components.iter()
                .map(|c| (c.name.clone(), c.installed, c.supervised, c.phase.clone())).collect();
            let snapshot = snapshot(&binding(), "0xs", 1, 0, synced(now), inventory, now);
            (components, snapshot.health, snapshot.reasons)
        };

        // A confirmed component is broken under supervision; its committed
        // spec file is corrupt. The supervisor's status still names it.
        let dir = temp();
        std::fs::create_dir_all(dir.join("supervised")).unwrap();
        std::fs::write(dir.join("state.json"), &journal).unwrap();
        std::fs::write(dir.join("supervised/active.json"), b"{not json").unwrap();
        std::fs::write(dir.join("supervised/status.json"), &broken).unwrap();
        let (components, health, reasons) = observe(&dir);
        assert_eq!(components, [("svc".to_owned(), true, true, Some("broken".to_owned()))]);
        assert_eq!(health, Health::Degraded);
        assert!(reasons[0].starts_with("supervisor state unreadable ("), "{reasons:?}");
        assert_eq!(reasons[1], "component svc: broken (exit status: 1)");

        // Neither file readable (a directory in place of each): nothing is
        // known about supervision, so the device is not reported healthy.
        std::fs::remove_file(dir.join("supervised/active.json")).unwrap();
        std::fs::remove_file(dir.join("supervised/status.json")).unwrap();
        std::fs::create_dir(dir.join("supervised/active.json")).unwrap();
        std::fs::create_dir(dir.join("supervised/status.json")).unwrap();
        let (_, health, reasons) = observe(&dir);
        assert_eq!(health, Health::Degraded);
        assert_eq!(reasons.len(), 2, "{reasons:?}");
        assert!(reasons[0].starts_with("supervisor state unreadable ("), "{reasons:?}");
        assert!(reasons[1].starts_with("supervisor status unreadable ("), "{reasons:?}");

        // Committed specs readable, status corrupt: unknown, not "not started".
        std::fs::remove_dir(dir.join("supervised/active.json")).unwrap();
        std::fs::remove_dir(dir.join("supervised/status.json")).unwrap();
        std::fs::write(dir.join("supervised/active.json"), serde_json::to_vec(&serde_json::json!({
            "svc": {"name": "svc", "version": "1.0.0", "deployment": "0xe", "run": "r", "shutdown": null,
                    "readiness": null, "restart": "on-failure"} })).unwrap()).unwrap();
        std::fs::write(dir.join("supervised/status.json"), b"[").unwrap();
        let (components, health, reasons) = observe(&dir);
        assert_eq!(components, [("svc".to_owned(), true, true, None)]);
        assert_eq!(health, Health::Degraded);
        assert_eq!(reasons.len(), 1, "{reasons:?}");
        assert!(reasons[0].starts_with("supervisor status unreadable ("), "{reasons:?}");
        std::fs::remove_dir_all(dir).unwrap();

        // A one-shot-only device never created supervisor state: healthy.
        let oneshot = temp();
        std::fs::write(oneshot.join("state.json"), &journal).unwrap();
        let (components, health, reasons) = observe(&oneshot);
        assert_eq!(components, [("svc".to_owned(), true, false, None)]);
        assert_eq!((health, reasons.len()), (Health::Healthy, 0));
        std::fs::remove_dir_all(oneshot).unwrap();
    }

    #[test]
    fn healthy_only_without_any_reason() {
        let now = 10_000_000;
        let healthy = snapshot(&binding(), "0xs", 1, 0, synced(now), Inventory::default(), now);
        assert_eq!((healthy.health, healthy.reasons.len()), (Health::Healthy, 0));
        for (sync, reason) in [
            (SyncHealth::default(), "chain sync not completed yet"),
            (SyncHealth { last_success_ms: Some(now), consecutive_failures: 2, last_error: Some("unreachable".into()) },
             "chain sync failing (2 consecutive): unreachable"),
            (synced(now - 1000 * (30 + MAX_INTERVAL_SECONDS) - 1), "no successful chain sync recently"),
        ] {
            let degraded = snapshot(&binding(), "0xs", 1, 0, sync, Inventory::default(), now);
            assert_eq!(degraded.health, Health::Degraded);
            assert_eq!(degraded.reasons, [reason]);
        }
        let inventory = Inventory { reasons: vec!["component web: backoff".into()], ..Inventory::default() };
        assert_eq!(snapshot(&binding(), "0xs", 1, 0, synced(now), inventory, now).health, Health::Degraded);
    }

    #[test]
    fn snapshots_stay_within_bounds() {
        let now = 10_000_000;
        let long = "é".repeat(5000);
        let component = ComponentHealth {
            name: long.clone(), version: long.clone(), deployment_id: long.clone(), installed: true, supervised: true,
            phase: Some("backoff".into()), restarts: Some(1), last_exit: Some("\u{1}".repeat(5000)), since_ms: Some(1),
        };
        let inventory = Inventory {
            components: vec![component; 500],
            reasons: (0..100).map(|_| long.clone()).collect(),
            pending_receipts: 0,
        };
        let sync = SyncHealth { last_success_ms: Some(now), consecutive_failures: 1, last_error: Some(long.clone()) };
        let s = snapshot(&binding(), "0xs", 1, 0, sync, inventory, now);
        let bytes = serde_json::to_vec(&s).unwrap();
        assert!(bytes.len() <= MAX_MESSAGE_BYTES, "{}", bytes.len());
        assert!(s.components.len() <= MAX_COMPONENTS && !s.components.is_empty());
        assert_eq!(s.reasons.len(), MAX_REASONS);
        assert!(s.reasons[0].ends_with("components not listed (snapshot size limit)"), "{}", s.reasons[0]);
        assert!(s.reasons.last().unwrap().ends_with("more problems"));
        assert!(s.reasons.iter().all(|r| r.len() <= MAX_TEXT));
        assert!(s.components.iter().all(|c| c.name.len() <= MAX_NAME && c.last_exit.as_ref().unwrap().len() <= MAX_TEXT));
        assert_eq!(s.health, Health::Degraded);
        assert_eq!(bounded("abc", 3), "abc");
        assert_eq!(bounded("abcdef", 5), "ab...");
    }

    #[test]
    fn spool_is_bounded_ordered_and_never_reuses_a_sequence() {
        let dir = temp();
        let signer = Ed25519File(SigningKey::from_bytes(&[7; 32]));
        let mut spool = Spool::open(&dir, 3).unwrap();
        let mut seqs = Vec::new();
        for now in [100, 100, 50, 200, 201] {
            let seq = spool.next_seq(now).unwrap();
            let s = snapshot(&binding(), &signer.address(), seq, spool.dropped(), synced(now), Inventory::default(), now);
            spool.push(seq, &seal(&signer, &s).unwrap()).unwrap();
            seqs.push(seq);
        }
        // Clock-floored and strictly increasing, even when the clock steps back.
        assert_eq!(seqs, [100, 101, 102, 200, 201]);
        assert_eq!(spool.pending().unwrap().iter().map(|p| p.0).collect::<Vec<_>>(), [102, 200, 201]);
        assert_eq!(spool.dropped(), 2);
        // A reserved but unwritten sequence (crash before push) is not reused;
        // an interrupted write leaves no envelope.
        assert_eq!(spool.next_seq(0).unwrap(), 202);
        std::fs::write(dir.join("00000000000000000203.tmp"), b"partial").unwrap();
        drop(spool);
        let mut spool = Spool::open(&dir, 3).unwrap();
        assert_eq!(spool.dropped(), 2);
        assert_eq!(spool.next_seq(0).unwrap(), 203);
        assert!(!dir.join("00000000000000000203.tmp").exists());
        assert_eq!(spool.pending().unwrap().len(), 3);
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn sealed_snapshots_verify_as_personal_messages() {
        use sui_crypto::SuiVerifier;
        let signer = Ed25519File(SigningKey::from_bytes(&[7; 32]));
        let s = snapshot(&binding(), &signer.address(), 9, 0, synced(5), Inventory::default(), 5);
        let envelope = seal(&signer, &s).unwrap();
        let bytes = STANDARD.decode(&envelope.message).unwrap();
        assert_eq!(serde_json::from_slice::<Snapshot>(&bytes).unwrap(), s);
        let signature = sui_sdk_types::UserSignature::from_bytes(&STANDARD.decode(&envelope.signature).unwrap()).unwrap();
        sui_crypto::UserSignatureVerifier::new()
            .verify_personal_message(&sui_sdk_types::PersonalMessage(bytes.into()), &signature)
            .unwrap();
    }

    #[test]
    fn signs_the_shared_vector_exactly() {
        // interfaces/telemetry-cases.json: signed independently with Mysten's
        // TypeScript SDK; the indexer and the dApp verify the same envelope.
        let cases: serde_json::Value = serde_json::from_str(include_str!("../../interfaces/telemetry-cases.json")).unwrap();
        let snapshot: Snapshot = serde_json::from_value(cases["signed"]["snapshot"].clone()).unwrap();
        let expected: Envelope = serde_json::from_value(cases["signed"]["envelope"].clone()).unwrap();
        let signer = Ed25519File(SigningKey::from_bytes(&[7; 32]));
        assert_eq!(signer.address(), cases["signed"]["signer"].as_str().unwrap());
        assert_eq!(seal(&signer, &snapshot).unwrap(), expected);
    }

    #[test]
    fn ingress_url_keeps_a_proxy_prefix() {
        assert_eq!(ingress_url("https://fleet.example").unwrap().as_str(), "https://fleet.example/v1/telemetry");
        assert_eq!(ingress_url("https://fleet.example/api/").unwrap().as_str(), "https://fleet.example/api/v1/telemetry");
    }
}
