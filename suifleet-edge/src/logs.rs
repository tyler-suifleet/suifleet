//! Remote logs (SF-020): bounded, redacted, signed log batches.
//!
//! Sources are the daemon's own log (written by [`RotatingFile`] to
//! `<state_dir>/logs/daemon.log`, 1 MiB, one rotated file kept) and each
//! supervised component's `component.log` (rotated by the supervisor). A
//! shipper tails them with per-source cursors (inode and offset) that follow
//! rotation into the `.1` file, so a line is read once. Every line is
//! redacted (by the [`redact`] module, including the device's confidential
//! configuration values) and truncated before it leaves the daemon.
//!
//! Lines go out as canonical schema-1 batches, signed with the device key as
//! a Sui personal message (like telemetry, never valid as a transaction), to
//! a bounded spool under `<state_dir>/logs/spool`, then to the fleet indexer
//! (`POST {url}/v1/logs`). Each interval reads at most
//! `max_lines_per_interval` lines; the rest stays for later intervals, and
//! lines lost to a rotation the shipper never saw are counted.
use crate::{identity::TxSigner, redact, telemetry};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    io::{Read, Seek, SeekFrom, Write},
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tracing::{info, warn};

pub const SCHEMA: u64 = 1;
pub const KIND: &str = "logs";
pub const MAX_MESSAGE_BYTES: usize = telemetry::MAX_MESSAGE_BYTES;
pub const MAX_RECORDS: usize = 256;
pub const MAX_LINE: usize = 1024;
pub const MAX_SOURCE: usize = 160;
pub const ROTATE_BYTES: u64 = 1024 * 1024;
pub const MAX_LINES_PER_INTERVAL: usize = 10_000;
pub const DAEMON_SOURCE: &str = "daemon";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Record {
    /// `daemon` or `component:<name>`.
    pub source: String,
    pub line: String,
}

/// One signed batch. Field order is the canonical form.
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
    /// Lines lost so far: to rotations the shipper did not see, or batches
    /// dropped from a full spool (counted by their lines).
    pub dropped_lines: u64,
    /// Secrets replaced in this batch.
    pub redactions: u64,
    pub records: Vec<Record>,
}

/// Bounds every batch keeps (the signing service and indexer check them).
pub fn validate(batch: &Batch) -> anyhow::Result<()> {
    anyhow::ensure!(batch.schema == SCHEMA && batch.kind == KIND, "not a schema-1 log batch");
    anyhow::ensure!(!batch.records.is_empty() && batch.records.len() <= MAX_RECORDS, "a log batch holds 1 to {MAX_RECORDS} records");
    anyhow::ensure!(
        batch.records.iter().all(|r| r.line.len() <= MAX_LINE && !r.line.contains('\n') && r.source.len() <= MAX_SOURCE
            && (r.source == DAEMON_SOURCE || r.source.strip_prefix("component:").is_some_and(|n| !n.is_empty()))),
        "a log record exceeds its limits"
    );
    anyhow::ensure!(batch.seq > 0 && batch.seq <= i64::MAX as u64 && batch.observed_ms <= i64::MAX as u64, "seq or observed_ms out of range");
    Ok(())
}

// --- The daemon's own log file ----------------------------------------------

/// A size-rotated log file for `tracing` (`daemon.log`, then `daemon.log.1`).
#[derive(Clone)]
pub struct RotatingFile {
    inner: Arc<Mutex<(PathBuf, Option<std::fs::File>)>>,
}

impl RotatingFile {
    pub fn new(path: PathBuf) -> std::io::Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        Ok(Self { inner: Arc::new(Mutex::new((path, None))) })
    }
}

impl Write for RotatingFile {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let mut guard = self.inner.lock().unwrap();
        let (path, file) = &mut *guard;
        if std::fs::metadata(&*path).is_ok_and(|m| m.len() + buf.len() as u64 > ROTATE_BYTES) {
            *file = None;
            let mut rotated = path.as_os_str().to_owned();
            rotated.push(".1");
            let _ = std::fs::rename(&*path, PathBuf::from(rotated));
        }
        if file.is_none() {
            use std::os::unix::fs::OpenOptionsExt;
            *file = Some(std::fs::OpenOptions::new().create(true).append(true).mode(0o640).open(&*path)?);
        }
        file.as_mut().unwrap().write(buf)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        match &mut self.inner.lock().unwrap().1 {
            Some(file) => file.flush(),
            None => Ok(()),
        }
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for RotatingFile {
    type Writer = RotatingFile;
    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

/// The daemon's own log file once `[logs]` is enabled; until then the file
/// layer writes nowhere (tracing starts before the configuration is read).
static DAEMON_LOG: std::sync::OnceLock<RotatingFile> = std::sync::OnceLock::new();

pub fn enable_daemon_log(state_dir: &Path) -> std::io::Result<()> {
    let file = RotatingFile::new(state_dir.join("logs/daemon.log"))?;
    let _ = DAEMON_LOG.set(file);
    Ok(())
}

/// `tracing` writer for [`DAEMON_LOG`].
#[derive(Clone, Copy)]
pub struct DaemonLog;

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for DaemonLog {
    type Writer = Box<dyn Write + 'a>;
    fn make_writer(&'a self) -> Self::Writer {
        match DAEMON_LOG.get() {
            Some(file) => Box::new(file.clone()),
            None => Box::new(std::io::sink()),
        }
    }
}

// --- Tailing ---------------------------------------------------------------

/// Where a source was read up to. Inode numbers are reused once a file is
/// deleted, so a file is the same one only if it also starts with `head`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Cursor {
    pub inode: u64,
    pub offset: u64,
    /// Hex of the file's first bytes (up to 32) when last read.
    #[serde(default)]
    pub head: String,
}

const HEAD_BYTES: u64 = 32;

fn head(path: &Path) -> String {
    let mut bytes = Vec::new();
    let _ = std::fs::File::open(path).map(|f| f.take(HEAD_BYTES).read_to_end(&mut bytes));
    hex::encode(bytes)
}

/// Whether `path` (with metadata `meta`) is the file `cursor` was reading.
fn same_file(path: &Path, meta: &std::fs::Metadata, cursor: &Cursor) -> bool {
    meta.ino() == cursor.inode && head(path).starts_with(&cursor.head)
}

fn rotated(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(".1");
    PathBuf::from(name)
}

/// Complete lines from `file` starting at `offset`, at most `budget`; returns
/// the lines and the offset after the last one consumed.
fn read_lines(path: &Path, offset: u64, budget: usize, lines: &mut Vec<String>) -> std::io::Result<u64> {
    let mut file = std::fs::File::open(path)?;
    let length = file.metadata()?.len();
    if offset >= length {
        return Ok(offset.min(length));
    }
    file.seek(SeekFrom::Start(offset))?;
    let mut bytes = Vec::new();
    // Bounded read: enough for the budget at the line limit.
    file.take(((budget as u64).saturating_mul(MAX_LINE as u64 + 1)).min(length - offset)).read_to_end(&mut bytes)?;
    let mut consumed = 0usize;
    let mut start = 0usize;
    while lines.len() < budget {
        match bytes[start..].iter().position(|b| *b == b'\n') {
            Some(newline) => {
                lines.push(String::from_utf8_lossy(&bytes[start..start + newline]).into_owned());
                start += newline + 1;
                consumed = start;
            }
            // An overlong partial line is cut rather than held forever.
            None if bytes.len() - start > MAX_LINE => {
                lines.push(String::from_utf8_lossy(&bytes[start..start + MAX_LINE]).into_owned());
                start += MAX_LINE;
                consumed = start;
            }
            None => break,
        }
    }
    Ok(offset + consumed as u64)
}

/// New lines of one source since `cursor`, following one rotation. Returns
/// the lines, the new cursor and whether unseen lines were lost.
pub fn tail(path: &Path, cursor: Cursor, budget: usize) -> std::io::Result<(Vec<String>, Cursor, bool)> {
    let mut lines = Vec::new();
    let current = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok((lines, cursor, false)),
        Err(e) => return Err(e),
    };
    let mut lost = false;
    let mut offset = 0;
    if same_file(path, &current, &cursor) && cursor.offset <= current.len() {
        offset = cursor.offset;
    } else if cursor.inode != 0 {
        // Rotated since the last read: finish the old file if it is `.1`.
        let old_path = rotated(path);
        match std::fs::metadata(&old_path) {
            Ok(old) if same_file(&old_path, &old, &cursor) => {
                let end = read_lines(&old_path, cursor.offset, budget, &mut lines)?;
                if end < old.len() {
                    // Budget spent inside the rotated file: continue there next time.
                    return Ok((lines, Cursor { inode: cursor.inode, offset: end, head: head(&old_path) }, false));
                }
            }
            _ => lost = true,
        }
    }
    let remaining = budget.saturating_sub(lines.len());
    let end = read_lines(path, offset, remaining, &mut lines)?;
    Ok((lines, Cursor { inode: current.ino(), offset: end, head: head(path) }, lost))
}

/// The sources present now: the daemon log and each supervised component's.
pub fn sources(state_dir: &Path) -> Vec<(String, PathBuf)> {
    let mut found = vec![(DAEMON_SOURCE.to_owned(), state_dir.join("logs/daemon.log"))];
    if let Ok(entries) = std::fs::read_dir(state_dir.join("supervised")) {
        let mut components: Vec<_> = entries.flatten().filter(|e| e.path().join("component.log").exists()).collect();
        components.sort_by_key(|e| e.file_name());
        for entry in components {
            let name = entry.file_name().to_string_lossy().into_owned();
            found.push((format!("component:{name}"), entry.path().join("component.log")));
        }
    }
    found
}

/// Confidential values to redact: every string leaf of every overlay.
pub fn confidential_values(overlay_dir: &Path) -> Vec<String> {
    let mut values = Vec::new();
    if let Ok(entries) = std::fs::read_dir(overlay_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().is_some_and(|e| e == "json") {
                if let Ok(value) = std::fs::read(&path).map(|b| serde_json::from_slice::<serde_json::Value>(&b)) {
                    if let Ok(value) = value {
                        redact::string_leaves(&value, &mut values);
                    }
                }
            }
        }
    }
    values.retain(|v| v.chars().count() >= 4);
    // Longest first, so a value containing another is replaced whole.
    values.sort_by_key(|v| std::cmp::Reverse(v.len()));
    values.dedup();
    values
}

fn bound(line: &str) -> String {
    let mut end = line.len().min(MAX_LINE);
    while !line.is_char_boundary(end) {
        end -= 1;
    }
    line[..end].to_owned()
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct ShipperState {
    cursors: BTreeMap<String, Cursor>,
    dropped_lines: u64,
}

/// Identity a batch is bound to.
#[derive(Debug, Clone)]
pub struct Binding {
    pub chain_identifier: String,
    pub registry: String,
    pub device_id: String,
}

pub struct Task {
    pub binding: Binding,
    pub url: reqwest::Url,
    pub state_dir: PathBuf,
    pub overlay_dir: PathBuf,
    pub interval_seconds: u64,
    pub max_lines: usize,
    pub buffer_max: usize,
    pub signer: Arc<dyn TxSigner>,
}

impl Task {
    /// One interval: tail, redact and batch new lines into the spool, then
    /// upload. Cursors advance only after the batches are spooled.
    pub fn collect(&self, spool: &mut telemetry::Spool) -> anyhow::Result<usize> {
        let path = self.state_dir.join("logs/shipper.json");
        let mut state: ShipperState = std::fs::read(&path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        let secrets = confidential_values(&self.overlay_dir);
        let mut records = Vec::new();
        let mut redactions = 0u64;
        let mut budget = self.max_lines;
        let mut cursors = state.cursors.clone();
        let mut lost_sources = Vec::new();
        for (source, file) in sources(&self.state_dir) {
            if budget == 0 {
                break;
            }
            let cursor = cursors.get(&source).cloned().unwrap_or_default();
            let (lines, next, lost) = tail(&file, cursor, budget)?;
            if lost {
                lost_sources.push(source.clone());
            }
            budget -= lines.len();
            for line in lines {
                let (clean, n) = redact::redact(&line, &secrets);
                redactions += n as u64;
                records.push(Record { source: bound(&source), line: bound(&clean) });
            }
            cursors.insert(source, next);
        }
        for source in lost_sources {
            records.push(Record { source: bound(&source), line: "[suifleet: earlier lines of this log were rotated away before they could be shipped]".into() });
        }
        let dropped_before = spool.dropped();
        let mut sent = 0;
        for chunk in batches(records) {
            let seq = spool.next_seq(telemetry::now_ms())?;
            let batch = Batch {
                schema: SCHEMA,
                kind: KIND.into(),
                chain_identifier: self.binding.chain_identifier.clone(),
                registry: self.binding.registry.clone(),
                device_id: self.binding.device_id.clone(),
                signer: self.signer.address(),
                seq,
                observed_ms: telemetry::now_ms(),
                dropped_lines: state.dropped_lines,
                redactions,
                records: chunk,
            };
            redactions = 0;
            sent += batch.records.len();
            spool.push(seq, &seal(self.signer.as_ref(), &batch)?)?;
        }
        // Batches the spool dropped count against their lines (at most a full batch each).
        state.dropped_lines += spool.dropped().saturating_sub(dropped_before) * MAX_RECORDS as u64;
        state.cursors = cursors;
        let bytes = serde_json::to_vec(&state)?;
        let temporary = path.with_extension("tmp");
        std::fs::write(&temporary, bytes)?;
        std::fs::rename(&temporary, &path)?;
        Ok(sent)
    }

    pub async fn run(self) {
        let http = match reqwest::Client::builder().timeout(Duration::from_secs(15)).build() {
            Ok(http) => http,
            Err(error) => return warn!(%error, "Remote logs disabled: no HTTP client"),
        };
        let mut spool = match telemetry::Spool::open(&self.state_dir.join("logs/spool"), self.buffer_max) {
            Ok(spool) => spool,
            Err(error) => return warn!(%error, "Remote logs disabled: spool unavailable"),
        };
        info!(url = %self.url, interval = self.interval_seconds, "Shipping redacted logs");
        loop {
            if let Err(error) = self.collect(&mut spool) {
                warn!(%error, "Log collection failed");
            }
            if let Err(error) = telemetry::flush_as(&http, &self.url, &spool, "Log", "batch").await {
                info!(%error, "Log ingress unreachable; batches stay spooled");
            }
            tokio::time::sleep(Duration::from_secs(self.interval_seconds)).await;
        }
    }
}

/// Split records into batches within the record and byte limits.
fn batches(records: Vec<Record>) -> Vec<Vec<Record>> {
    let mut out: Vec<Vec<Record>> = Vec::new();
    let mut current = Vec::new();
    let mut size = 1024; // headroom for the envelope fields
    for record in records {
        let cost = record.line.len() * 2 + record.source.len() + 32; // worst-case JSON escaping
        if !current.is_empty() && (current.len() == MAX_RECORDS || size + cost > MAX_MESSAGE_BYTES) {
            out.push(std::mem::take(&mut current));
            size = 1024;
        }
        size += cost;
        current.push(record);
    }
    if !current.is_empty() {
        out.push(current);
    }
    out
}

/// Sign a batch's exact canonical bytes.
pub fn seal(signer: &dyn TxSigner, batch: &Batch) -> anyhow::Result<telemetry::Envelope> {
    validate(batch)?;
    let bytes = serde_json::to_vec(batch)?;
    anyhow::ensure!(bytes.len() <= MAX_MESSAGE_BYTES, "log batch exceeds {MAX_MESSAGE_BYTES} bytes");
    Ok(telemetry::Envelope { message: STANDARD.encode(&bytes), signature: STANDARD.encode(signer.sign_personal_message(&bytes)?) })
}

/// `{base}/v1/logs`, keeping any path prefix of a proxied base URL.
pub fn ingress_url(base: &str) -> anyhow::Result<reqwest::Url> {
    let mut url = reqwest::Url::parse(base)?;
    let path = format!("{}/v1/logs", url.path().trim_end_matches('/'));
    url.set_path(&path);
    Ok(url)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("suifleet-logs-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn append(path: &Path, text: &str) {
        std::fs::OpenOptions::new().create(true).append(true).open(path).unwrap().write_all(text.as_bytes()).unwrap();
    }

    #[test]
    fn tail_reads_complete_lines_once_and_follows_rotation() {
        let d = dir("tail");
        let log = d.join("component.log");
        append(&log, "one\ntwo\npart");
        let (lines, cursor, lost) = tail(&log, Cursor::default(), 100).unwrap();
        assert_eq!((lines, lost), (vec!["one".to_string(), "two".to_string()], false));
        append(&log, "ial\nthree\n");
        let (lines, cursor, _) = tail(&log, cursor, 100).unwrap();
        assert_eq!(lines, ["partial", "three"]);
        // Rotation between reads: the rest of the old file, then the new one.
        append(&log, "four\n");
        std::fs::rename(&log, rotated(&log)).unwrap();
        append(&log, "five\n");
        let (lines, cursor, lost) = tail(&log, cursor, 100).unwrap();
        assert_eq!((lines, lost), (vec!["four".to_string(), "five".to_string()], false));
        // Two rotations unseen: the unread old lines are reported lost.
        append(&log, "six\n");
        std::fs::rename(&log, rotated(&log)).unwrap();
        append(&log, "seven\n");
        std::fs::rename(&log, rotated(&log)).unwrap();
        append(&log, "eight\n");
        let (lines, _, lost) = tail(&log, cursor, 100).unwrap();
        assert_eq!((lines, lost), (vec!["eight".to_string()], true));
    }

    #[test]
    fn budget_leaves_the_rest_for_later_and_overlong_lines_are_cut() {
        let d = dir("budget");
        let log = d.join("daemon.log");
        append(&log, &"x\n".repeat(10));
        let (lines, cursor, _) = tail(&log, Cursor::default(), 4).unwrap();
        assert_eq!(lines.len(), 4);
        let (lines, _, _) = tail(&log, cursor, 100).unwrap();
        assert_eq!(lines.len(), 6);
        let long = d.join("long.log");
        append(&long, &"y".repeat(MAX_LINE * 2 + 5));
        let (lines, _, _) = tail(&long, Cursor::default(), 10).unwrap();
        assert_eq!(lines.iter().map(|l| l.len()).collect::<Vec<_>>(), [MAX_LINE, MAX_LINE]);
    }

    #[test]
    fn rotating_file_keeps_one_rotated_file() {
        let d = dir("rotate");
        let mut file = RotatingFile::new(d.join("logs/daemon.log")).unwrap();
        let line = vec![b'z'; 1000];
        for _ in 0..2200 {
            file.write_all(&line).unwrap();
        }
        let current = std::fs::metadata(d.join("logs/daemon.log")).unwrap().len();
        let old = std::fs::metadata(d.join("logs/daemon.log.1")).unwrap().len();
        assert!(current <= ROTATE_BYTES && old <= ROTATE_BYTES, "{current} {old}");
        assert!(!d.join("logs/daemon.log.1.1").exists());
    }

    #[test]
    fn batches_respect_record_and_byte_limits() {
        let many: Vec<Record> = (0..600).map(|i| Record { source: "daemon".into(), line: format!("line {i}") }).collect();
        let chunks = batches(many);
        assert_eq!(chunks.iter().map(Vec::len).collect::<Vec<_>>(), [256, 256, 88]);
        let big: Vec<Record> = (0..100).map(|_| Record { source: "daemon".into(), line: "\"".repeat(MAX_LINE) }).collect();
        for chunk in batches(big) {
            let batch = Batch { schema: 1, kind: "logs".into(), chain_identifier: "c".into(), registry: "r".into(), device_id: "d".into(),
                signer: "s".into(), seq: 1, observed_ms: 1, dropped_lines: 0, redactions: 0, records: chunk };
            assert!(serde_json::to_vec(&batch).unwrap().len() <= MAX_MESSAGE_BYTES);
        }
    }

    struct Key;
    impl TxSigner for Key {
        fn address(&self) -> String {
            "0xa0".into()
        }
        fn sign_digest(&self, _: &[u8]) -> anyhow::Result<Vec<u8>> {
            Ok(vec![1])
        }
    }

    #[test]
    fn collect_redacts_confidential_values_and_persists_cursors() {
        let state = dir("collect");
        let overlays = state.join("overlays");
        std::fs::create_dir_all(&overlays).unwrap();
        std::fs::write(overlays.join("mqtt.json"), r#"{"/password": "pa55w0rd-xyz"}"#).unwrap();
        std::fs::create_dir_all(state.join("supervised/mqtt")).unwrap();
        append(&state.join("supervised/mqtt/component.log"), "login with pa55w0rd-xyz ok\ntoken=abc123\n");
        std::fs::create_dir_all(state.join("logs")).unwrap();
        append(&state.join("logs/daemon.log"), "Daemon started\n");
        let task = Task {
            binding: Binding { chain_identifier: "4c78adac".into(), registry: "0x13".into(), device_id: "0xde".into() },
            url: ingress_url("http://127.0.0.1:1").unwrap(),
            state_dir: state.clone(),
            overlay_dir: overlays,
            interval_seconds: 10,
            max_lines: 100,
            buffer_max: 10,
            signer: Arc::new(Key),
        };
        let mut spool = telemetry::Spool::open(&state.join("logs/spool"), 10).unwrap();
        assert_eq!(task.collect(&mut spool).unwrap(), 3);
        let pending = spool.pending().unwrap();
        let envelope: telemetry::Envelope = serde_json::from_slice(&std::fs::read(&pending[0].1).unwrap()).unwrap();
        let batch: Batch = serde_json::from_slice(&STANDARD.decode(envelope.message).unwrap()).unwrap();
        let lines: Vec<_> = batch.records.iter().map(|r| (r.source.as_str(), r.line.as_str())).collect();
        assert_eq!(lines, [("daemon", "Daemon started"), ("component:mqtt", "login with [REDACTED] ok"), ("component:mqtt", "token=[REDACTED]")]);
        assert_eq!(batch.redactions, 2);
        // Nothing new: nothing sent again.
        assert_eq!(task.collect(&mut spool).unwrap(), 0);
    }
}
