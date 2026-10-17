//! Remote logs (SF-020): bounded, redacted, signed log batches.
//!
//! Sources are the daemon's own log (written by [`RotatingFile`] to
//! `<state_dir>/logs/daemon.log`, 1 MiB, one rotated file kept) and each
//! supervised component's `component.log` (rotated by the supervisor). A
//! shipper tails them with per-source cursors (inode and offset) that follow
//! rotation into the `.1` file, so a line is read once. Every whole line is
//! redacted (by the [`redact`] module, including the device's confidential
//! configuration values) and only then truncated, so a secret is never split
//! across records. A line longer than [`MAX_SCAN`] is replaced by a marker.
//! A confidential value spanning lines is also matched line by line, and
//! every line of a PEM private-key block is dropped, its state kept in the
//! cursor across intervals and restarts.
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
/// The longest physical line redacted and shipped (truncated to [`MAX_LINE`]
/// after redaction); a longer one is dropped whole, never cut into pieces
/// that would each be redacted without the other's context.
pub const MAX_SCAN: usize = 16 * 1024;
pub const OMITTED: &str = "[suifleet: line longer than 16384 bytes omitted]";
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
    /// Inside a line longer than [`MAX_SCAN`]: skip to its end.
    #[serde(default)]
    pub skipping: bool,
    /// Lines read so far inside a PEM private-key block (0 outside one).
    #[serde(default)]
    pub key_block: u32,
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
/// the offset after the last line consumed. A line longer than [`MAX_SCAN`]
/// becomes [`OMITTED`] and `skipping` holds until its end. With `last` (a
/// rotated file nobody writes to), an unterminated final line is a line too.
fn read_lines(path: &Path, offset: u64, budget: usize, skipping: &mut bool, last: bool, lines: &mut Vec<String>) -> std::io::Result<u64> {
    let mut file = std::fs::File::open(path)?;
    let length = file.metadata()?.len();
    if offset >= length {
        return Ok(offset.min(length));
    }
    file.seek(SeekFrom::Start(offset))?;
    let mut bytes = Vec::new();
    // Bounded read: enough for the budget at the line limit, and for one
    // line up to the scan limit.
    let window = (budget as u64).saturating_mul(MAX_LINE as u64 + 1).saturating_add(MAX_SCAN as u64 + 1);
    file.take(window.min(length - offset)).read_to_end(&mut bytes)?;
    let at_end = offset + bytes.len() as u64 >= length;
    let mut start = 0usize;
    while start < bytes.len() && (*skipping || lines.len() < budget) {
        let newline = bytes[start..].iter().position(|b| *b == b'\n');
        if *skipping {
            match newline {
                Some(n) => {
                    start += n + 1;
                    *skipping = false;
                }
                None => start = bytes.len(),
            }
            continue;
        }
        match newline {
            Some(n) if n <= MAX_SCAN => {
                lines.push(String::from_utf8_lossy(&bytes[start..start + n]).into_owned());
                start += n + 1;
            }
            Some(n) => {
                lines.push(OMITTED.into());
                start += n + 1;
            }
            None if bytes.len() - start > MAX_SCAN => {
                lines.push(OMITTED.into());
                start = bytes.len();
                *skipping = true;
            }
            None if last && at_end => {
                lines.push(String::from_utf8_lossy(&bytes[start..]).into_owned());
                start = bytes.len();
            }
            None => break,
        }
    }
    Ok(offset + start as u64)
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
    let mut skipping = false;
    if same_file(path, &current, &cursor) && cursor.offset <= current.len() {
        offset = cursor.offset;
        skipping = cursor.skipping;
    } else if cursor.inode != 0 {
        // Rotated since the last read: finish the old file if it is `.1`,
        // including an unterminated last line (nothing will complete it).
        let old_path = rotated(path);
        match std::fs::metadata(&old_path) {
            Ok(old) if same_file(&old_path, &old, &cursor) => {
                let mut old_skipping = cursor.skipping;
                let end = read_lines(&old_path, cursor.offset, budget, &mut old_skipping, true, &mut lines)?;
                if end < old.len() {
                    // Budget spent inside the rotated file: continue there next time.
                    let next = Cursor { inode: cursor.inode, offset: end, head: head(&old_path), skipping: old_skipping, key_block: 0 };
                    return Ok((lines, next, false));
                }
            }
            _ => lost = true,
        }
    }
    let remaining = budget.saturating_sub(lines.len());
    let end = read_lines(path, offset, remaining, &mut skipping, false, &mut lines)?;
    Ok((lines, Cursor { inode: current.ino(), offset: end, head: head(path), skipping, key_block: 0 }, lost))
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
///
/// Fails closed: a component keeps its materialized secrets while its
/// overlay is being edited, so an overlay that cannot be listed, read or
/// parsed must stop collection rather than ship lines unredacted. Errors
/// name the file only, never its content.
pub fn confidential_values(overlay_dir: &Path) -> anyhow::Result<Vec<String>> {
    let mut values = Vec::new();
    let entries = match std::fs::read_dir(overlay_dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(values),
        Err(error) => anyhow::bail!("cannot list configuration overlays: {}", error.kind()),
    };
    for entry in entries {
        let path = entry.map_err(|error| anyhow::anyhow!("cannot list configuration overlays: {}", error.kind()))?.path();
        if !path.extension().is_some_and(|e| e == "json") {
            continue;
        }
        let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let bytes = std::fs::read(&path).map_err(|error| anyhow::anyhow!("cannot read configuration overlay {name}: {}", error.kind()))?;
        // The overlay holds secrets: a parse error must not quote its content.
        let value: serde_json::Value =
            serde_json::from_slice(&bytes).map_err(|_| anyhow::anyhow!("configuration overlay {name} is not valid JSON"))?;
        let mut leaves = Vec::new();
        redact::string_leaves(&value, &mut leaves);
        for leaf in leaves {
            // The shipper reads physical lines, so a value spanning lines
            // is matched line by line too.
            if leaf.contains(['\n', '\r']) {
                for part in leaf.split(['\n', '\r']) {
                    values.push(part.to_owned());
                    values.push(part.trim().to_owned());
                }
            }
            values.push(leaf);
        }
    }
    // However short: a short value is over-redacted rather than exported.
    // Blank fragments of a multiline value carry nothing to protect.
    values.retain(|v| !v.trim().is_empty());
    // Longest first, so a value containing another is replaced whole.
    values.sort_by_key(|v| std::cmp::Reverse(v.len()));
    values.dedup();
    Ok(values)
}

/// How long a confidential value stays redacted after it left every overlay
/// and materialized configuration, at least: covers a component still
/// stopping with it. It is also kept until every log was read to its end.
pub const RETAIN_MS: u64 = 60 * 60 * 1000;
const RETAINED: &str = "logs/confidential.json";

/// One confidential value known to the shipper.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Known {
    /// When the value was first seen in no overlay and no materialized
    /// configuration; `None` while one still holds it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    unused_since_ms: Option<u64>,
}

/// Every confidential value the shipper has redacted and may still meet:
/// a replaced or removed overlay value stays in unread log lines and in a
/// running component's materialized configuration. Kept (0600) in the state
/// directory, never shipped.
#[derive(Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Retained {
    values: BTreeMap<String, Known>,
}

impl Retained {
    /// Fails closed like the overlays: a retained set that exists but cannot
    /// be read would forget previous values.
    fn load(path: &Path) -> anyhow::Result<Self> {
        match std::fs::read(path) {
            Ok(bytes) => serde_json::from_slice(&bytes).map_err(|_| anyhow::anyhow!("retained confidential values are not valid JSON")),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(error) => anyhow::bail!("cannot read retained confidential values: {}", error.kind()),
        }
    }

    fn save(&self, path: &Path) -> anyhow::Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        crate::configuration::write_private(path, self)
            .map_err(|error| anyhow::anyhow!("cannot store retained confidential values: {}", error.root_cause()))
    }

    /// Add `current` and restart the clock of a value still in use; start it
    /// for one no longer used. `in_use` is `None` when the materialized
    /// configurations could not all be read: then every value counts as used.
    fn update(&mut self, current: &[String], in_use: Option<&[String]>, now_ms: u64) {
        for value in current {
            self.values.insert(value.clone(), Known::default());
        }
        for (value, known) in &mut self.values {
            let used = current.contains(value) || in_use.is_none_or(|leaves| leaves.iter().any(|leaf| leaf.contains(value.as_str())));
            known.unused_since_ms = if used { None } else { Some(known.unused_since_ms.unwrap_or(now_ms)) };
        }
    }

    /// After every log was read to its end: forget values unused for
    /// [`RETAIN_MS`].
    fn prune(&mut self, now_ms: u64) {
        self.values.retain(|_, known| known.unused_since_ms.is_none_or(|since| now_ms.saturating_sub(since) < RETAIN_MS));
    }

    /// Longest first, so a value containing another is replaced whole.
    fn secrets(&self) -> Vec<String> {
        let mut values: Vec<String> = self.values.keys().cloned().collect();
        values.sort_by_key(|v| std::cmp::Reverse(v.len()));
        values
    }
}

/// String leaves of every component version's materialized configuration
/// (`supervised/<name>/versions/<version>/configuration.json`), which a
/// running component may hold after its overlay changed. `None` if one
/// cannot be read.
fn materialized_leaves(state_dir: &Path) -> Option<Vec<String>> {
    let mut leaves = Vec::new();
    let components = match std::fs::read_dir(state_dir.join("supervised")) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Some(leaves),
        Err(_) => return None,
    };
    for component in components {
        let versions = match std::fs::read_dir(component.ok()?.path().join("versions")) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound || error.raw_os_error() == Some(libc::ENOTDIR) => continue,
            Err(_) => return None,
        };
        for version in versions {
            let path = version.ok()?.path().join("configuration.json");
            match std::fs::read(&path) {
                Ok(bytes) => redact::string_leaves(&serde_json::from_slice(&bytes).ok()?, &mut leaves),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(_) => return None,
            }
        }
    }
    Some(leaves)
}

/// Whether `file` was read to its end, as far as `cursor` shows.
fn drained(file: &Path, cursor: &Cursor) -> bool {
    match std::fs::metadata(file) {
        Ok(meta) => meta.ino() == cursor.inode && cursor.offset >= meta.len() && !cursor.skipping,
        Err(error) => error.kind() == std::io::ErrorKind::NotFound,
    }
}

/// The most lines one PEM private-key block may hold (an 8192-bit RSA key
/// is about 100); past it, a block whose end marker was lost is closed.
pub const MAX_KEY_BLOCK_LINES: u32 = 256;

/// Redact one line of a source. `block` follows PEM private-key blocks,
/// whose body lines carry no marker of their own: from a `BEGIN … PRIVATE
/// KEY` line to its `END` line, every line is dropped.
fn redact_line(line: &str, secrets: &[String], block: &mut u32) -> (String, usize) {
    if line.contains("PRIVATE KEY") && line.contains("-----") {
        if line.contains("BEGIN") {
            *block = 1;
        } else if line.contains("END") {
            *block = 0;
        }
        return redact::redact(line, secrets);
    }
    if *block > 0 && *block < MAX_KEY_BLOCK_LINES {
        *block += 1;
        return (redact::key_block_line(), 1);
    }
    *block = 0;
    redact::redact(line, secrets)
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
    /// upload. Cursors advance only after the batches are spooled; nothing
    /// is read when the confidential values cannot be loaded. Values that
    /// left the overlays are still redacted (see [`Retained`]).
    pub fn collect(&self, spool: &mut telemetry::Spool) -> anyhow::Result<usize> {
        let current = confidential_values(&self.overlay_dir)?;
        let retained_path = self.state_dir.join(RETAINED);
        let mut retained = Retained::load(&retained_path)?;
        let before = retained.values.clone();
        let now = telemetry::now_ms();
        retained.update(&current, materialized_leaves(&self.state_dir).as_deref(), now);
        // Stored before any line is read, so no restart can forget a value.
        if retained.values != before {
            retained.save(&retained_path)?;
        }
        let secrets = retained.secrets();
        let path = self.state_dir.join("logs/shipper.json");
        let mut state: ShipperState = std::fs::read(&path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        let mut records = Vec::new();
        let mut redactions = 0u64;
        let mut budget = self.max_lines;
        let mut cursors = state.cursors.clone();
        let mut lost_sources = Vec::new();
        let mut all_read = true;
        for (source, file) in sources(&self.state_dir) {
            if budget == 0 {
                all_read = false;
                break;
            }
            let cursor = cursors.get(&source).cloned().unwrap_or_default();
            let mut block = cursor.key_block;
            let (lines, mut next, lost) = tail(&file, cursor, budget)?;
            if lost {
                lost_sources.push(source.clone());
            }
            budget -= lines.len();
            for line in lines {
                let (clean, n) = redact_line(&line, &secrets, &mut block);
                redactions += n as u64;
                records.push(Record { source: bound(&source), line: bound(&clean) });
            }
            next.key_block = block;
            all_read &= drained(&file, &next);
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
        if all_read {
            let before = retained.values.len();
            retained.prune(now);
            if retained.values.len() != before {
                retained.save(&retained_path)?;
            }
        }
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
        // The record's exact JSON (a control byte escapes to six bytes), and a comma.
        let cost = serde_json::to_vec(&record).map_or(MAX_MESSAGE_BYTES, |b| b.len()) + 1;
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
    use std::os::unix::fs::PermissionsExt;

    /// What fits of the mark after 1020 bytes.
    const MARK_PREFIX: &str = "[RED";

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
    fn budget_leaves_the_rest_for_later_and_overlong_lines_are_whole_or_omitted() {
        let d = dir("budget");
        let log = d.join("daemon.log");
        append(&log, &"x\n".repeat(10));
        let (lines, cursor, _) = tail(&log, Cursor::default(), 4).unwrap();
        assert_eq!(lines.len(), 4);
        let (lines, _, _) = tail(&log, cursor, 100).unwrap();
        assert_eq!(lines.len(), 6);
        // A line over MAX_LINE but within MAX_SCAN waits for its end and is
        // read whole (collection redacts, then truncates).
        let long = d.join("long.log");
        append(&long, &"y".repeat(MAX_LINE * 2 + 5));
        let (lines, cursor, _) = tail(&long, Cursor::default(), 10).unwrap();
        assert!(lines.is_empty());
        append(&long, "\nnext\n");
        let (lines, _, _) = tail(&long, cursor, 10).unwrap();
        assert_eq!(lines.iter().map(|l| l.len()).collect::<Vec<_>>(), [MAX_LINE * 2 + 5, 4]);
        // A longer line is omitted whole, also when its end arrives in a
        // later interval, and never shipped as fragments.
        let huge = d.join("huge.log");
        append(&huge, &format!("a\n{}", "z".repeat(MAX_SCAN + 10)));
        let (lines, cursor, _) = tail(&huge, Cursor::default(), 10).unwrap();
        assert_eq!(lines, ["a", OMITTED]);
        assert!(cursor.skipping);
        append(&huge, &"z".repeat(MAX_SCAN * 3));
        let (lines, cursor, _) = tail(&huge, cursor, 10).unwrap();
        assert!(lines.is_empty() && cursor.skipping);
        append(&huge, "zzz-end\nafter\n");
        let (lines, cursor, _) = tail(&huge, cursor, 10).unwrap();
        assert_eq!((lines, cursor.skipping), (vec!["after".to_string()], false));
        // Terminated in the same read.
        let both = d.join("both.log");
        append(&both, &format!("{}\nb\n", "q".repeat(MAX_SCAN + 1)));
        let (lines, _, _) = tail(&both, Cursor::default(), 10).unwrap();
        assert_eq!(lines, [OMITTED, "b"]);
    }

    #[test]
    fn unterminated_rotated_tail_is_flushed_and_the_current_file_read() {
        let d = dir("rotated-tail");
        let log = d.join("component.log");
        append(&log, "old\n");
        let (_, cursor, _) = tail(&log, Cursor::default(), 100).unwrap();
        append(&log, "partial");
        std::fs::rename(&log, rotated(&log)).unwrap();
        append(&log, "new\n");
        let (lines, cursor, lost) = tail(&log, cursor, 100).unwrap();
        assert_eq!((lines, lost), (vec!["partial".to_string(), "new".to_string()], false));
        append(&log, "newer\n");
        let (lines, _, _) = tail(&log, cursor, 100).unwrap();
        assert_eq!(lines, ["newer"]);
        // With the budget spent in the rotated file, it continues there.
        let log = d.join("budget.log");
        append(&log, "x\n");
        let (_, cursor, _) = tail(&log, Cursor::default(), 100).unwrap();
        append(&log, "1\n2\n3");
        std::fs::rename(&log, rotated(&log)).unwrap();
        append(&log, "4\n");
        let (lines, cursor, _) = tail(&log, cursor, 2).unwrap();
        assert_eq!(lines, ["1", "2"]);
        let (lines, _, _) = tail(&log, cursor, 100).unwrap();
        assert_eq!(lines, ["3", "4"]);
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
        // Quotes escape to two bytes, control bytes to six.
        let mut big: Vec<Record> = (0..100).map(|_| Record { source: "daemon".into(), line: "\"".repeat(MAX_LINE) }).collect();
        big.extend((0..50).map(|_| Record { source: "daemon".into(), line: "\u{1}".repeat(MAX_LINE) }));
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

    fn records(spool: &telemetry::Spool) -> Vec<Record> {
        let mut out = Vec::new();
        for (_, path) in spool.pending().unwrap() {
            let envelope: telemetry::Envelope = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
            let batch: Batch = serde_json::from_slice(&STANDARD.decode(envelope.message).unwrap()).unwrap();
            out.extend(batch.records);
        }
        out
    }

    #[test]
    fn collect_never_splits_a_confidential_value_across_records() {
        let state = dir("collect-split");
        let overlays = state.join("overlays");
        std::fs::create_dir_all(&overlays).unwrap();
        std::fs::write(overlays.join("app.json"), r#"{"/key": "ConfidentialValueQ9", "/pin": "123"}"#).unwrap();
        std::fs::create_dir_all(state.join("supervised/app")).unwrap();
        // The value crosses MAX_LINE; then a short value; then a line over MAX_SCAN.
        let log = state.join("supervised/app/component.log");
        append(&log, &format!("{}ConfidentialValueQ9\nlogin with 123\n{}ConfidentialValueQ9\nend\n", ".".repeat(1020), "-".repeat(MAX_SCAN)));
        let task = Task {
            binding: Binding { chain_identifier: "4c78adac".into(), registry: "0x13".into(), device_id: "0xde".into() },
            url: ingress_url("http://127.0.0.1:1").unwrap(),
            state_dir: state.clone(),
            overlay_dir: overlays,
            interval_seconds: 10,
            max_lines: 1,
            buffer_max: 10,
            signer: Arc::new(Key),
        };
        let mut spool = telemetry::Spool::open(&state.join("logs/spool"), 10).unwrap();
        while task.collect(&mut spool).unwrap() > 0 {}
        let lines: Vec<String> = records(&spool).into_iter().map(|r| r.line).collect();
        assert_eq!(lines.len(), 4, "{lines:?}");
        assert_eq!(lines[0], format!("{}{}", ".".repeat(1020), &MARK_PREFIX));
        assert_eq!(lines[1], "login with [REDACTED]");
        assert_eq!(lines[2], OMITTED);
        assert_eq!(lines[3], "end");
        let all = lines.concat();
        for fragment in ["Conf", "Q9", "123"] {
            assert!(!all.contains(fragment), "{fragment} exported");
        }
    }

    #[test]
    fn collect_stops_while_an_overlay_cannot_be_loaded() {
        let state = dir("collect-overlay-failure");
        let overlays = state.join("overlays");
        std::fs::create_dir_all(&overlays).unwrap();
        std::fs::write(overlays.join("app.json"), r#"{"/key": "ConfidentialValueQ9"}"#).unwrap();
        std::fs::create_dir_all(state.join("supervised/app")).unwrap();
        let log = state.join("supervised/app/component.log");
        append(&log, "first ConfidentialValueQ9\n");
        let mut spool = telemetry::Spool::open(&state.join("logs/spool"), 100).unwrap();
        assert_eq!(task(&state, 100).collect(&mut spool).unwrap(), 1);
        let cursors = std::fs::read(state.join("logs/shipper.json")).unwrap();
        append(&log, "login with ConfidentialValueQ9\n");
        // Mid-edit (truncated), unreadable (a directory) and unlistable
        // overlays: nothing is collected, spooled or advanced, across restarts.
        let broken: [(&str, fn(&Path)); 3] = [
            ("truncated", |o| std::fs::write(o.join("app.json"), "{").unwrap()),
            ("unreadable", |o| {
                std::fs::remove_file(o.join("app.json")).unwrap();
                std::fs::create_dir(o.join("app.json")).unwrap();
            }),
            ("unlistable", |o| {
                std::fs::remove_dir(o.join("app.json")).unwrap();
                std::fs::remove_dir(o).unwrap();
                std::fs::write(o, r#"{"/key": "ConfidentialValueQ9"}"#).unwrap();
            }),
        ];
        for (case, breaks) in broken {
            breaks(&overlays);
            for _ in 0..2 {
                let mut spool = telemetry::Spool::open(&state.join("logs/spool"), 100).unwrap();
                let error = format!("{:#}", task(&state, 100).collect(&mut spool).unwrap_err());
                assert!(!error.contains("Confidential") && !error.contains("Q9") && !error.contains('{'), "{case}: {error}");
                assert_eq!(spool.pending().unwrap().len(), 1, "{case}");
                assert_eq!(std::fs::read(state.join("logs/shipper.json")).unwrap(), cursors, "{case}");
            }
        }
        // Restored: the held line ships redacted.
        std::fs::remove_file(&overlays).unwrap();
        std::fs::create_dir(&overlays).unwrap();
        std::fs::write(overlays.join("app.json"), r#"{"/key": "ConfidentialValueQ9"}"#).unwrap();
        let mut spool = telemetry::Spool::open(&state.join("logs/spool"), 100).unwrap();
        assert_eq!(task(&state, 100).collect(&mut spool).unwrap(), 1);
        let lines: Vec<String> = records(&spool).into_iter().map(|r| r.line).collect();
        assert_eq!(lines, ["first [REDACTED]", "login with [REDACTED]"]);
    }

    fn shipped(state: &Path) -> Vec<String> {
        let mut spool = telemetry::Spool::open(&state.join("logs/spool"), 100).unwrap();
        while task(state, 100).collect(&mut spool).unwrap() > 0 {}
        records(&spool).into_iter().map(|r| r.line).collect()
    }

    fn retained(state: &Path) -> Retained {
        Retained::load(&state.join(RETAINED)).unwrap()
    }

    #[test]
    fn replaced_or_removed_overlay_values_stay_redacted_across_restarts() {
        let state = dir("collect-rotation");
        let overlays = state.join("overlays");
        std::fs::create_dir_all(&overlays).unwrap();
        std::fs::write(overlays.join("app.json"), r#"{"/key": "ConfidentialValueQ9"}"#).unwrap();
        std::fs::create_dir_all(state.join("supervised/app")).unwrap();
        let log = state.join("supervised/app/component.log");
        append(&log, "first ConfidentialValueQ9\n");
        assert_eq!(shipped(&state), ["first [REDACTED]"]);
        let mode = std::fs::metadata(state.join(RETAINED)).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
        // Each step: a queued old line and the old component still writing,
        // collected by a fresh task (a daemon restart).
        let steps: [(&str, fn(&Path)); 3] = [
            ("replaced", |o| std::fs::write(o.join("app.json"), r#"{"/key": "ReplacementValueR8"}"#).unwrap()),
            ("file removed", |o| std::fs::remove_file(o.join("app.json")).unwrap()),
            ("directory removed", |o| std::fs::remove_dir_all(o).unwrap()),
        ];
        let mut expected = vec!["first [REDACTED]".to_string()];
        for (case, step) in steps {
            append(&log, &format!("{case}: queued ConfidentialValueQ9\n"));
            step(&overlays);
            append(&log, &format!("{case}: still running with ConfidentialValueQ9 and ReplacementValueR8\n"));
            expected.push(format!("{case}: queued [REDACTED]"));
            // R8 is a current value only while the replacement overlay exists,
            // and retained after it is removed.
            expected.push(format!("{case}: still running with [REDACTED] and [REDACTED]"));
            assert_eq!(shipped(&state), expected, "{case}");
        }
        let all = expected.concat();
        assert!(!all.contains("Q9") && !all.contains("R8"));
        // Both are unused now, but still within the retention.
        let known = retained(&state);
        assert!(known.values.values().all(|k| k.unused_since_ms.is_some()), "{known:?}");
        assert_eq!(known.secrets(), ["ConfidentialValueQ9", "ReplacementValueR8"]);
    }

    #[test]
    fn a_value_is_retained_while_materialized_and_until_every_log_was_read() {
        let state = dir("collect-retention");
        let overlays = state.join("overlays");
        std::fs::create_dir_all(&overlays).unwrap();
        std::fs::write(overlays.join("app.json"), r#"{"/key": "ConfidentialValueQ9"}"#).unwrap();
        let version = state.join("supervised/app/versions/1-ab");
        std::fs::create_dir_all(&version).unwrap();
        std::fs::write(version.join("configuration.json"), r#"{"port": 80, "key": "ConfidentialValueQ9"}"#).unwrap();
        let log = state.join("supervised/app/component.log");
        append(&log, "one\n");
        assert_eq!(shipped(&state), ["one"]);
        std::fs::remove_file(overlays.join("app.json")).unwrap();
        let expire = |state: &Path| {
            let mut known = retained(state);
            known.values.values_mut().for_each(|k| k.unused_since_ms = k.unused_since_ms.map(|_| 1));
            known.save(&state.join(RETAINED)).unwrap();
        };
        // The running version still holds it: in use, however long.
        expire(&state);
        assert_eq!(shipped(&state), ["one"]);
        assert_eq!(retained(&state).values["ConfidentialValueQ9"].unused_since_ms, None);
        // An unreadable materialized configuration counts as using every value.
        std::fs::write(version.join("configuration.json"), "{").unwrap();
        append(&log, "two ConfidentialValueQ9\n");
        assert_eq!(shipped(&state)[1], "two [REDACTED]");
        assert_eq!(retained(&state).values["ConfidentialValueQ9"].unused_since_ms, None);
        // Restarted with a new configuration: unused from now, kept for the retention.
        std::fs::write(version.join("configuration.json"), r#"{"port": 80}"#).unwrap();
        append(&log, "three ConfidentialValueQ9\n");
        assert_eq!(shipped(&state)[2], "three [REDACTED]");
        assert!(retained(&state).values["ConfidentialValueQ9"].unused_since_ms.is_some());
        // Past the retention, but an unread line is left (the budget is
        // spent): kept, and the line ships redacted.
        expire(&state);
        append(&log, "four ConfidentialValueQ9\nfive\n");
        let mut spool = telemetry::Spool::open(&state.join("logs/spool"), 100).unwrap();
        assert_eq!(task(&state, 1).collect(&mut spool).unwrap(), 1);
        assert!(retained(&state).values.contains_key("ConfidentialValueQ9"));
        // Every log read to its end: forgotten.
        assert_eq!(task(&state, 100).collect(&mut spool).unwrap(), 1);
        assert!(retained(&state).values.is_empty());
        let lines: Vec<String> = records(&spool).into_iter().map(|r| r.line).collect();
        assert_eq!(&lines[3..], ["four [REDACTED]", "five"]);
    }

    #[test]
    fn collect_stops_while_the_retained_values_cannot_be_read() {
        let state = dir("collect-retained-failure");
        std::fs::create_dir_all(state.join("supervised/app")).unwrap();
        std::fs::create_dir_all(state.join("logs")).unwrap();
        std::fs::write(state.join(RETAINED), r#"{"values": {"ConfidentialValueQ9": {}"#).unwrap();
        append(&state.join("supervised/app/component.log"), "login with ConfidentialValueQ9\n");
        let mut spool = telemetry::Spool::open(&state.join("logs/spool"), 100).unwrap();
        let error = format!("{:#}", task(&state, 100).collect(&mut spool).unwrap_err());
        assert!(!error.contains("Q9") && !error.contains('{'), "{error}");
        assert!(spool.pending().unwrap().is_empty());
        assert!(!state.join("logs/shipper.json").exists());
        std::fs::write(state.join(RETAINED), r#"{"values": {"ConfidentialValueQ9": {}}}"#).unwrap();
        assert_eq!(shipped(&state), ["login with [REDACTED]"]);
    }

    #[test]
    fn a_missing_overlay_directory_holds_no_confidential_values() {
        assert!(confidential_values(&dir("no-overlays").join("absent")).unwrap().is_empty());
    }

    fn task(state: &Path, max_lines: usize) -> Task {
        Task {
            binding: Binding { chain_identifier: "4c78adac".into(), registry: "0x13".into(), device_id: "0xde".into() },
            url: ingress_url("http://127.0.0.1:1").unwrap(),
            state_dir: state.to_path_buf(),
            overlay_dir: state.join("overlays"),
            interval_seconds: 10,
            max_lines,
            buffer_max: 100,
            signer: Arc::new(Key),
        }
    }

    /// Collect one line per interval, with a fresh task (a daemon restart)
    /// each time, until nothing is left; returns every shipped line.
    fn collect_one_line_per_restart(state: &Path) -> Vec<String> {
        let mut spool = telemetry::Spool::open(&state.join("logs/spool"), 100).unwrap();
        while task(state, 1).collect(&mut spool).unwrap() > 0 {
            spool = telemetry::Spool::open(&state.join("logs/spool"), 100).unwrap();
        }
        records(&spool).into_iter().map(|r| r.line).collect()
    }

    #[test]
    fn collect_redacts_each_line_of_a_multiline_confidential_value() {
        let state = dir("collect-multiline");
        std::fs::create_dir_all(state.join("overlays")).unwrap();
        std::fs::write(state.join("overlays/app.json"), r#"{"/cert": "AlphaLineQ9\r\n  BetaLineZ8\n\n"}"#).unwrap();
        std::fs::create_dir_all(state.join("supervised/app")).unwrap();
        append(&state.join("supervised/app/component.log"), "config AlphaLineQ9\n  BetaLineZ8\nBetaLineZ8 again\nok\n");
        let lines = collect_one_line_per_restart(&state);
        assert_eq!(lines, ["config [REDACTED]", "[REDACTED]", "[REDACTED] again", "ok"]);
    }

    /// A synthetic Ed25519 PKCS#8 key (all-zero seed): its body holds no `+`, `/` or `=`.
    const ZERO_SEED_PKCS8: &str = "MC4CAQAwBQYDK2VwBCIEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

    #[test]
    fn collect_drops_every_line_of_a_private_key_block_across_restarts() {
        let state = dir("collect-pem");
        std::fs::create_dir_all(state.join("supervised/app")).unwrap();
        // Body lines shorter than the PEM width carry nothing the line rules see.
        append(
            &state.join("supervised/app/component.log"),
            &format!("before\n-----BEGIN PRIVATE KEY-----\n{ZERO_SEED_PKCS8}\nAbCd1234EfGh\n-----END PRIVATE KEY-----\nafter AbCd\n"),
        );
        let lines = collect_one_line_per_restart(&state);
        let mark = redact::key_block_line();
        assert_eq!(lines, ["before", &mark, &mark, &mark, &mark, "after AbCd"]);
        let all = lines.concat();
        assert!(!all.contains("MC4C") && !all.contains("1234"), "{all}");
    }

    #[test]
    fn a_key_block_without_an_end_marker_closes() {
        let mut block = 0;
        assert_eq!(redact_line("-----BEGIN RSA PRIVATE KEY-----", &[], &mut block).0, redact::key_block_line());
        for _ in 1..MAX_KEY_BLOCK_LINES {
            assert_eq!(redact_line("short", &[], &mut block).0, redact::key_block_line());
        }
        assert_eq!(redact_line("short", &[], &mut block), ("short".to_string(), 0));
        assert_eq!(block, 0);
    }

    #[test]
    fn control_character_backlog_is_sealed_and_shipped() {
        let state = dir("collect-control");
        std::fs::create_dir_all(state.join("logs")).unwrap();
        append(&state.join("logs/daemon.log"), &format!("{}after\n", format!("{}\n", "\u{1}".repeat(MAX_LINE)).repeat(50)));
        let mut spool = telemetry::Spool::open(&state.join("logs/spool"), 100).unwrap();
        assert_eq!(task(&state, 100).collect(&mut spool).unwrap(), 51);
        assert_eq!(task(&state, 100).collect(&mut spool).unwrap(), 0);
        let lines: Vec<String> = records(&spool).into_iter().map(|r| r.line).collect();
        assert_eq!((lines.len(), lines[50].as_str()), (51, "after"));
    }
}
