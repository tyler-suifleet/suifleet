//! Durable local streams and export (SF-034), modelled on AWS Stream Manager
//! with the semantics fixed in `interfaces/compat/stream-cases.json`.
//!
//! A stream is a bounded sequence of messages numbered from 0, gap-free at
//! append and never reused, even after eviction or restart. `File` streams
//! live in CRC-framed append-only segment files under
//! `<state_dir>/streams/<sha256(name) hex>/` and survive crashes: a torn record at
//! the tail is truncated on open. `Memory` streams keep messages in memory and
//! persist only their definition and sequence counter. When full, a stream
//! rejects new data or overwrites its oldest messages; a time-to-live and
//! discarding exported messages also retire messages. Reads below the oldest
//! retained message report `evicted`, so consumers detect loss.
//!
//! An export sends batches to an HTTP(S) destination that only the operator
//! declares. Each batch is authenticated by the device key signing a small
//! canonical manifest that binds the body's SHA-256 (see [`Manifest`]), and
//! the per-stream checkpoint advances only after a 2xx answer, so delivery is
//! at least once and resumable; receivers deduplicate by (device, stream,
//! sequence).
use crate::identity::TxSigner;
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, VecDeque},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tracing::{info, warn};

pub const MIN_STREAM_BYTES: u64 = 1024;
pub const MAX_STREAM_BYTES: u64 = 1 << 30;
pub const MAX_MESSAGE_BYTES: usize = 1 << 20;
pub const MAX_READ: u64 = 1000;
pub const MANIFEST_SCHEMA: u32 = 1;
const HEADER: usize = 24; // len u32, sequence u64, appended_ms u64, crc32 u32
/// What each message costs against `max_size` besides its bytes: at least
/// its on-disk header and its in-memory index entry, so `max_size` bounds
/// disk and memory use even for tiny messages.
pub const MESSAGE_OVERHEAD: u64 = 64;
const _: () = assert!(HEADER as u64 <= MESSAGE_OVERHEAD && std::mem::size_of::<Entry>() as u64 <= MESSAGE_OVERHEAD);
/// Sequence numbers are reserved in blocks persisted before use, so a crash
/// (or a corrupt segment) skips at most this many numbers instead of reusing
/// any; a clean close persists the exact next number. Receivers deduplicate
/// by sequence, so reuse would silently drop new data.
const RESERVE: u64 = 1024;

#[derive(Debug, Clone)]
pub struct StreamError {
    pub code: &'static str,
    pub oldest: Option<u64>,
    pub detail: String,
}
impl std::fmt::Display for StreamError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.detail)
    }
}
impl std::error::Error for StreamError {}
fn fail<T>(code: &'static str, detail: impl Into<String>) -> anyhow::Result<T> {
    Err(StreamError { code, oldest: None, detail: detail.into() }.into())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Strategy {
    RejectNewData,
    OverwriteOldestData,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub enum Persistence {
    #[default]
    File,
    Memory,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExportConfig {
    /// http(s) destination; batches are POSTed here.
    pub url: String,
    /// Optional `Authorization: Bearer` token for the destination (file, 0600).
    #[serde(default)]
    pub bearer_token_file: Option<PathBuf>,
    #[serde(default = "default_batch_messages")]
    pub batch_max_messages: usize,
    #[serde(default = "default_batch_bytes")]
    pub batch_max_bytes: usize,
    #[serde(default = "default_batch_interval")]
    pub batch_interval_seconds: u64,
    #[serde(default = "default_backoff")]
    pub max_backoff_seconds: u64,
}
fn default_batch_messages() -> usize {
    100
}
fn default_batch_bytes() -> usize {
    1 << 20
}
fn default_batch_interval() -> u64 {
    5
}
fn default_backoff() -> u64 {
    300
}
impl ExportConfig {
    pub fn validate(&self) -> anyhow::Result<()> {
        let url = reqwest::Url::parse(&self.url)?;
        anyhow::ensure!(matches!(url.scheme(), "http" | "https") && url.host().is_some(), "export url must be http(s)");
        anyhow::ensure!(url.username().is_empty() && url.password().is_none(), "export url must not embed credentials; use bearer_token_file");
        anyhow::ensure!((1..=1000).contains(&self.batch_max_messages), "batch_max_messages must be 1..=1000");
        anyhow::ensure!((1024..=16 << 20).contains(&self.batch_max_bytes), "batch_max_bytes must be 1 KiB..=16 MiB");
        anyhow::ensure!((1..=3600).contains(&self.batch_interval_seconds), "batch_interval_seconds must be 1..=3600");
        anyhow::ensure!((1..=3600).contains(&self.max_backoff_seconds), "max_backoff_seconds must be 1..=3600");
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Definition {
    pub name: String,
    pub max_size: u64,
    pub strategy_on_full: Strategy,
    #[serde(default)]
    pub persistence: Persistence,
    #[serde(default)]
    pub time_to_live_seconds: Option<u64>,
    #[serde(default)]
    pub discard_exported: bool,
    #[serde(default)]
    pub export: Option<ExportConfig>,
}

/// AWS-style resource glob: only `*` is special (any run of characters).
pub fn glob(pattern: &str, name: &str) -> bool {
    match pattern.split_once('*') {
        None => pattern == name,
        Some((head, rest)) => name.strip_prefix(head).is_some_and(|tail| (0..=tail.len()).filter(|i| tail.is_char_boundary(*i)).any(|i| glob(rest, &tail[i..]))),
    }
}

/// One IPC stream request (authorized by the caller). `declared` streams come
/// from operator configuration and cannot be deleted locally.
pub fn handle(store: &Mutex<Store>, declared: &[String], operation: &str, name: &str, payload: &Value,
              visible: &dyn Fn(&str) -> bool) -> anyhow::Result<Value> {
    let now = crate::telemetry::now_ms();
    let mut store = store.lock().unwrap();
    match operation {
        "stream_list" => Ok(json!({"streams": store.names().into_iter().filter(|n| visible(n)).collect::<Vec<_>>()})),
        "stream_create" => {
            let mut def = payload.clone();
            let object = def.as_object_mut().ok_or_else(|| StreamError { code: "invalid", oldest: None, detail: "payload is a stream definition".into() })?;
            if object.contains_key("export") {
                return fail("invalid", "exports are declared by the operator, not by components");
            }
            if object.get("name").is_some_and(|n| n != name) {
                return fail("invalid", "payload name differs from topic");
            }
            object.insert("name".into(), json!(name));
            let def: Definition = serde_json::from_value(def).map_err(|e| StreamError { code: "invalid", oldest: None, detail: e.to_string() })?;
            store.create(def)?;
            Ok(json!({"created": name}))
        }
        "stream_delete" => {
            if declared.iter().any(|d| d == name) {
                return fail("declared", "stream is declared by device configuration");
            }
            store.delete(name)?;
            Ok(json!({"deleted": name}))
        }
        "stream_append" => {
            let data = serde_json::to_vec(payload)?;
            Ok(json!({"sequence": store.append(name, &data, now)?}))
        }
        "stream_read" => {
            let number = |key: &str, default: u64| match payload.get(key) {
                None | Some(Value::Null) => Ok(default),
                Some(v) => v.as_u64().ok_or_else(|| anyhow::Error::from(StreamError { code: "invalid", oldest: None, detail: format!("{key} must be a non-negative integer") })),
            };
            let messages = store.read(name, number("start", 0)?, number("min", 1)?, number("max", 1)?, now)?;
            Ok(json!({"messages": messages.into_iter().map(|m| {
                let mut out = json!({"sequence": m.sequence, "appended_ms": m.appended_ms});
                match serde_json::from_slice::<Value>(&m.data) {
                    Ok(value) => out["payload"] = value,
                    Err(_) => out["data_base64"] = json!(STANDARD.encode(&m.data)),
                }
                out
            }).collect::<Vec<_>>()}))
        }
        "stream_describe" => store.describe(name, now),
        _ => anyhow::bail!("unknown stream operation"),
    }
}

pub fn valid_name(name: &str) -> bool {
    (1..=255).contains(&name.len()) && name.bytes().all(|b| b.is_ascii_alphanumeric() || b" ,._-".contains(&b))
}

impl Definition {
    pub fn validate(&self) -> anyhow::Result<()> {
        if !valid_name(&self.name) {
            return fail("invalid", "stream names are 1-255 of [A-Za-z0-9 ,._-]");
        }
        if !(MIN_STREAM_BYTES..=MAX_STREAM_BYTES).contains(&self.max_size) {
            return fail("invalid", "max_size must be 1 KiB..=1 GiB");
        }
        if self.time_to_live_seconds.is_some_and(|t| !(1..=31_536_000).contains(&t)) {
            return fail("invalid", "time_to_live_seconds must be 1..=31536000");
        }
        if self.discard_exported && self.export.is_none() {
            return fail("invalid", "discard_exported needs an export");
        }
        if let Some(export) = &self.export {
            export.validate().map_err(|e| StreamError { code: "invalid", oldest: None, detail: e.to_string() })?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Quota {
    pub max_streams: usize,
    pub max_total_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Message {
    pub sequence: u64,
    pub appended_ms: u64,
    #[serde(skip)]
    pub data: Vec<u8>,
}

#[derive(Serialize, Deserialize)]
struct Meta {
    definition: Definition,
    /// Lowest retained sequence number when last persisted.
    oldest: u64,
    /// No sequence number below this is ever assigned again.
    next_floor: u64,
    exported: i64,
}

enum Location {
    Memory(Vec<u8>),
    Disk { segment: u64, offset: u64 },
}
struct Entry {
    sequence: u64,
    appended_ms: u64,
    len: u32,
    at: Location,
}
impl Entry {
    fn cost(&self) -> u64 {
        cost(self.len as usize)
    }
}
fn cost(len: usize) -> u64 {
    len as u64 + MESSAGE_OVERHEAD
}

struct Stream {
    def: Definition,
    dir: PathBuf,
    entries: VecDeque<Entry>,
    bytes: u64,
    next: u64,
    oldest: u64,
    exported: i64,
    reserved: u64,
    /// First sequence -> file length.
    segments: BTreeMap<u64, u64>,
    corrupt: u64,
    export_status: Value,
    /// A failed append whose undo also failed: the segment to cut back to
    /// its tracked length (`None`: remove it). Appends fail until it is.
    torn: Option<(PathBuf, Option<u64>)>,
}

fn crc32(parts: &[&[u8]]) -> u32 {
    let mut crc = !0u32;
    for part in parts {
        for &byte in *part {
            crc ^= byte as u32;
            for _ in 0..8 {
                crc = (crc >> 1) ^ (0xEDB8_8320 & (!(crc & 1)).wrapping_add(1));
            }
        }
    }
    !crc
}

/// A stream's directory name: fixed length, so every valid name (up to 255
/// bytes) fits the 255-byte file name limit. `Store::open` reads the name from
/// `meta.json`, never from the directory.
fn dir_name(name: &str) -> String {
    hex::encode(Sha256::digest(name.as_bytes()))
}

fn segment_path(dir: &Path, first: u64) -> PathBuf {
    dir.join(format!("{first:020}.seg"))
}

fn write_atomic(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    let tmp = path.with_extension("tmp");
    let mut file = std::fs::File::create(&tmp)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    std::fs::rename(&tmp, path)?;
    if let Some(parent) = path.parent() {
        std::fs::File::open(parent)?.sync_all()?;
    }
    Ok(())
}

/// Restore a segment after a failed append: truncate it to `len`, or remove
/// it if the append created it.
fn undo_write(path: &Path, len: Option<u64>) -> std::io::Result<()> {
    faults::undo()?;
    match len {
        Some(len) => {
            let file = std::fs::OpenOptions::new().write(true).open(path)?;
            file.set_len(len)?;
            file.sync_data()
        }
        None => match std::fs::remove_file(path) {
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => Err(error),
            _ => Ok(()),
        },
    }
}

/// Segment write fault injection for tests; no-ops otherwise.
#[cfg(not(test))]
mod faults {
    use std::io::Write;
    pub fn write_all(file: &mut std::fs::File, bytes: &[u8]) -> std::io::Result<()> {
        file.write_all(bytes)
    }
    pub fn sync(file: &std::fs::File) -> std::io::Result<()> {
        file.sync_data()
    }
    pub fn undo() -> std::io::Result<()> {
        Ok(())
    }
}
#[cfg(test)]
mod faults {
    use std::{cell::Cell, io::Write};
    #[derive(Clone, Copy, Default)]
    pub struct Faults {
        /// Write only this many bytes of the next record, then fail.
        pub partial: Option<usize>,
        /// Fail the next sync after writing the whole record.
        pub sync: bool,
        /// Fail this many undo attempts.
        pub undo: u32,
    }
    thread_local! {
        pub static FAULTS: Cell<Faults> = Cell::new(Faults::default());
    }
    fn injected() -> std::io::Error {
        std::io::Error::other("injected fault")
    }
    pub fn write_all(file: &mut std::fs::File, bytes: &[u8]) -> std::io::Result<()> {
        let mut faults = FAULTS.get();
        if let Some(n) = faults.partial.take() {
            FAULTS.set(faults);
            file.write_all(&bytes[..n.min(bytes.len())])?;
            return Err(injected());
        }
        file.write_all(bytes)
    }
    pub fn sync(file: &std::fs::File) -> std::io::Result<()> {
        let mut faults = FAULTS.get();
        if std::mem::take(&mut faults.sync) {
            FAULTS.set(faults);
            return Err(injected());
        }
        file.sync_data()
    }
    pub fn undo() -> std::io::Result<()> {
        let mut faults = FAULTS.get();
        if faults.undo > 0 {
            faults.undo -= 1;
            FAULTS.set(faults);
            return Err(injected());
        }
        Ok(())
    }
}

impl Stream {
    fn segment_bytes(&self) -> u64 {
        (self.def.max_size / 8).clamp(4096, 4 << 20)
    }

    fn save_meta(&self) -> anyhow::Result<()> {
        let meta = Meta { definition: self.def.clone(), oldest: self.oldest, next_floor: self.reserved.max(self.next), exported: self.exported };
        write_atomic(&self.dir.join("meta.json"), &serde_json::to_vec_pretty(&meta)?)
    }

    fn evict_front(&mut self) {
        if let Some(entry) = self.entries.pop_front() {
            self.bytes -= entry.cost();
        }
        self.oldest = self.entries.front().map_or(self.next, |e| e.sequence);
    }

    /// Retire by time-to-live and exported checkpoint; then drop segment files
    /// that hold only retired messages (persisting the floor first).
    fn retire(&mut self, now_ms: u64) -> anyhow::Result<()> {
        if let Some(ttl) = self.def.time_to_live_seconds {
            while self.entries.front().is_some_and(|e| e.appended_ms.saturating_add(ttl * 1000) <= now_ms) {
                self.evict_front();
            }
        }
        if self.def.discard_exported {
            while self.entries.front().is_some_and(|e| (e.sequence as i64) <= self.exported) {
                self.evict_front();
            }
        }
        self.drop_segments()
    }

    fn drop_segments(&mut self) -> anyhow::Result<()> {
        let firsts: Vec<u64> = self.segments.keys().copied().collect();
        let dead: Vec<u64> = firsts
            .iter()
            .enumerate()
            .filter(|(i, first)| {
                let end = firsts.get(i + 1).copied().unwrap_or(self.next);
                end <= self.oldest && (**first < self.oldest || self.entries.is_empty())
            })
            .map(|(_, first)| *first)
            .collect();
        if dead.is_empty() {
            return Ok(());
        }
        self.save_meta()?;
        for first in dead {
            let path = segment_path(&self.dir, first);
            std::fs::remove_file(&path)?;
            self.segments.remove(&first);
            // A pending undo of this segment went with it: every record in
            // the file was retired, the torn tail included.
            if self.torn.as_ref().is_some_and(|(torn, _)| *torn == path) {
                self.torn = None;
            }
        }
        Ok(())
    }

    fn write_record(&mut self, sequence: u64, appended_ms: u64, data: &[u8]) -> anyhow::Result<Location> {
        if let Some((path, len)) = self.torn.clone() {
            if let Err(error) = undo_write(&path, len) {
                return fail("unavailable", format!("stream {:?} cannot undo an earlier failed write: {error}", self.def.name));
            }
            self.torn = None;
        }
        let record_len = (HEADER + data.len()) as u64;
        let last = self.segments.iter().next_back().map(|(f, l)| (*f, *l));
        let first = match last {
            Some((first, len)) if len == 0 || len + record_len <= self.segment_bytes() => first,
            _ => sequence,
        };
        let path = segment_path(&self.dir, first);
        let tracked = self.segments.get(&first).copied();
        let offset = tracked.unwrap_or(0);
        let mut record = Vec::with_capacity(record_len as usize);
        record.extend_from_slice(&(data.len() as u32).to_le_bytes());
        record.extend_from_slice(&sequence.to_le_bytes());
        record.extend_from_slice(&appended_ms.to_le_bytes());
        record.extend_from_slice(&crc32(&[&sequence.to_le_bytes(), &appended_ms.to_le_bytes(), data]).to_le_bytes());
        record.extend_from_slice(data);
        let written = (|| -> std::io::Result<()> {
            let mut file = std::fs::OpenOptions::new().create(true).write(true).open(&path)?;
            file.seek(SeekFrom::Start(offset))?;
            faults::write_all(&mut file, &record)?;
            faults::sync(&file)?;
            if offset == 0 {
                std::fs::File::open(&self.dir)?.sync_all()?;
            }
            Ok(())
        })();
        if let Err(error) = written {
            // A failed write or sync may leave part or all of the record in
            // the file. Cut the segment back to its tracked length, so a
            // retry and recovery see only acknowledged records; if that fails
            // too, appends fail until it succeeds.
            if let Err(undo) = undo_write(&path, tracked) {
                warn!(stream = self.def.name, %undo, "Could not undo a failed stream write; appends fail until it is undone");
                self.torn = Some((path, tracked));
            }
            return Err(error.into());
        }
        self.segments.insert(first, offset + record_len);
        Ok(Location::Disk { segment: first, offset })
    }

    fn load(dir: &Path, now_ms: u64) -> anyhow::Result<Self> {
        let meta: Meta = serde_json::from_slice(&std::fs::read(dir.join("meta.json"))?)?;
        let mut stream = Stream {
            def: meta.definition,
            dir: dir.to_owned(),
            entries: VecDeque::new(),
            bytes: 0,
            next: meta.next_floor,
            oldest: meta.oldest,
            exported: meta.exported,
            reserved: meta.next_floor,
            segments: BTreeMap::new(),
            corrupt: 0,
            export_status: Value::Null,
            torn: None,
        };
        if stream.def.persistence == Persistence::Memory {
            stream.oldest = stream.next;
            return Ok(stream);
        }
        let mut firsts: Vec<u64> = std::fs::read_dir(dir)?
            .filter_map(|e| e.ok()?.file_name().to_str()?.strip_suffix(".seg")?.parse().ok())
            .collect();
        firsts.sort();
        let mut last_sequence: Option<u64> = None;
        for (index, first) in firsts.iter().enumerate() {
            let path = segment_path(dir, *first);
            let bytes = std::fs::read(&path)?;
            let mut at = 0usize;
            while at + HEADER <= bytes.len() {
                let len = u32::from_le_bytes(bytes[at..at + 4].try_into()?) as usize;
                let sequence = u64::from_le_bytes(bytes[at + 4..at + 12].try_into()?);
                let appended_ms = u64::from_le_bytes(bytes[at + 12..at + 20].try_into()?);
                let crc = u32::from_le_bytes(bytes[at + 20..at + 24].try_into()?);
                let end = at + HEADER + len;
                if len > MAX_MESSAGE_BYTES || end > bytes.len() || last_sequence.is_some_and(|l| sequence <= l)
                    || crc32(&[&sequence.to_le_bytes(), &appended_ms.to_le_bytes(), &bytes[at + HEADER..end]]) != crc
                {
                    break;
                }
                last_sequence = Some(sequence);
                if sequence >= meta.oldest {
                    let entry = Entry { sequence, appended_ms, len: len as u32, at: Location::Disk { segment: *first, offset: at as u64 } };
                    stream.bytes += entry.cost();
                    stream.entries.push_back(entry);
                }
                at = end;
            }
            if at < bytes.len() {
                if index + 1 == firsts.len() {
                    warn!(stream = stream.def.name, bytes = bytes.len() - at, "Truncating a torn record at the end of a stream");
                    let file = std::fs::OpenOptions::new().write(true).open(&path)?;
                    file.set_len(at as u64)?;
                    file.sync_all()?;
                } else {
                    warn!(stream = stream.def.name, segment = first, "Corrupt stream segment; the rest of it is skipped");
                    stream.corrupt += 1;
                }
            }
            stream.segments.insert(*first, at as u64);
        }
        stream.next = stream.next.max(last_sequence.map_or(0, |l| l + 1));
        stream.reserved = stream.next;
        stream.oldest = stream.entries.front().map_or(stream.next, |e| e.sequence).max(meta.oldest.min(stream.next));
        // Re-apply the size bound: the retained set is always the longest
        // suffix that fits, so this reproduces evictions not yet persisted.
        while stream.bytes > stream.def.max_size {
            stream.evict_front();
        }
        stream.retire(now_ms)?;
        Ok(stream)
    }

    fn read_data(&self, entry: &Entry) -> anyhow::Result<Vec<u8>> {
        match &entry.at {
            Location::Memory(data) => Ok(data.clone()),
            Location::Disk { segment, offset } => {
                let mut file = std::fs::File::open(segment_path(&self.dir, *segment))?;
                file.seek(SeekFrom::Start(offset + HEADER as u64))?;
                let mut data = vec![0; entry.len as usize];
                file.read_exact(&mut data)?;
                Ok(data)
            }
        }
    }

    fn describe(&self) -> Value {
        json!({
            "name": self.def.name, "max_size": self.def.max_size, "strategy_on_full": self.def.strategy_on_full,
            "persistence": self.def.persistence, "time_to_live_seconds": self.def.time_to_live_seconds,
            // The last assigned number (numbering may skip ahead after a crash).
            "oldest": self.oldest, "newest": self.entries.back().map_or(self.next as i64 - 1, |e| e.sequence as i64),
            "exported": self.exported, "bytes": self.bytes,
            "messages": self.entries.len(), "corrupt_segments": self.corrupt,
            "export": self.def.export.as_ref().map(|e| json!({"url": redact_url(&e.url), "status": self.export_status})),
        })
    }
}

fn redact_url(url: &str) -> String {
    reqwest::Url::parse(url).map(|mut u| {
        u.set_query(None);
        u.to_string()
    }).unwrap_or_default()
}

pub struct Store {
    dir: PathBuf,
    quota: Quota,
    streams: BTreeMap<String, Stream>,
}

impl Store {
    /// Open the store and apply the operator's definitions. The resulting
    /// inventory, including persisted component-created streams, must fit
    /// the current quota; otherwise startup fails before anything changes.
    pub fn start(dir: &Path, quota: Quota, definitions: &[Definition], now_ms: u64) -> anyhow::Result<Self> {
        let mut store = Self::open(dir, quota, now_ms)?;
        let mut sizes: BTreeMap<&str, u64> = store.streams.iter().map(|(name, s)| (name.as_str(), s.def.max_size)).collect();
        for def in definitions {
            def.validate()?;
            if let Some(stream) = store.streams.get(&def.name) {
                anyhow::ensure!(stream.def.persistence == def.persistence, "stream {:?}: persistence cannot change; delete and recreate it", def.name);
            }
            sizes.insert(&def.name, def.max_size);
        }
        let (count, total) = (sizes.len(), sizes.values().sum::<u64>());
        anyhow::ensure!(count <= quota.max_streams && total <= quota.max_total_bytes,
            "stored and declared streams need {count} streams and {total} bytes, over [streams] max_streams = {} / max_total_bytes = {}; \
             raise the limits, or delete streams under the old limits first (stream data left untouched)",
            quota.max_streams, quota.max_total_bytes);
        // The final inventory fits, so shrink and update before creating.
        let (existing, new): (Vec<_>, Vec<_>) = definitions.iter().partition(|d| store.streams.contains_key(&d.name));
        for def in existing {
            store.apply(def.clone(), now_ms)?;
        }
        for def in new {
            store.create(def.clone())?;
        }
        Ok(store)
    }

    fn open(dir: &Path, quota: Quota, now_ms: u64) -> anyhow::Result<Self> {
        std::fs::create_dir_all(dir)?;
        let mut streams = BTreeMap::new();
        for entry in std::fs::read_dir(dir)? {
            let path = entry?.path();
            if !path.join("meta.json").exists() {
                continue;
            }
            match Stream::load(&path, now_ms) {
                Ok(stream) => {
                    streams.insert(stream.def.name.clone(), stream);
                }
                Err(error) => warn!(path = %path.display(), %error, "Stream could not be opened; left untouched"),
            }
        }
        Ok(Self { dir: dir.to_owned(), quota, streams })
    }

    fn get(&mut self, name: &str) -> anyhow::Result<&mut Stream> {
        match self.streams.get_mut(name) {
            Some(stream) => Ok(stream),
            None => fail("not-found", format!("no stream {name:?}")),
        }
    }

    pub fn names(&self) -> Vec<String> {
        self.streams.keys().cloned().collect()
    }

    fn reserved_bytes(&self) -> u64 {
        self.streams.values().map(|s| s.def.max_size).sum()
    }

    pub fn create(&mut self, def: Definition) -> anyhow::Result<()> {
        if self.streams.contains_key(&def.name) {
            return fail("exists", format!("stream {:?} exists", def.name));
        }
        def.validate()?;
        if self.streams.len() >= self.quota.max_streams || self.reserved_bytes() + def.max_size > self.quota.max_total_bytes {
            return fail("quota", "stream quota exceeded (max_streams / max_total_bytes)");
        }
        let dir = self.dir.join(dir_name(&def.name));
        if dir.join("meta.json").exists() {
            // A stream that failed to open is left untouched, not overwritten.
            return fail("exists", format!("stream {:?} has unreadable state on disk", def.name));
        }
        std::fs::create_dir_all(&dir)?;
        let stream = Stream {
            def: def.clone(), dir, entries: VecDeque::new(), bytes: 0, next: 0, oldest: 0, exported: -1, reserved: 0,
            segments: BTreeMap::new(), corrupt: 0, export_status: Value::Null, torn: None,
        };
        stream.save_meta()?;
        std::fs::File::open(&self.dir)?.sync_all()?;
        self.streams.insert(def.name, stream);
        Ok(())
    }

    /// Apply an operator definition to an existing stream; `start` has
    /// checked its persistence and the quota.
    fn apply(&mut self, def: Definition, now_ms: u64) -> anyhow::Result<()> {
        let stream = self.get(&def.name)?;
        if stream.def == def {
            return Ok(());
        }
        stream.def = def;
        while stream.bytes > stream.def.max_size {
            stream.evict_front();
        }
        stream.save_meta()?;
        stream.retire(now_ms)
    }

    pub fn delete(&mut self, name: &str) -> anyhow::Result<()> {
        let stream = self.get(name)?;
        let dir = stream.dir.clone();
        self.streams.remove(name);
        std::fs::remove_dir_all(dir)?;
        Ok(())
    }

    pub fn append(&mut self, name: &str, data: &[u8], now_ms: u64) -> anyhow::Result<u64> {
        let stream = self.get(name)?;
        if data.is_empty() || data.len() > MAX_MESSAGE_BYTES || cost(data.len()) > stream.def.max_size {
            return fail("invalid", "message must be 1 byte to min(max_size - 64, 1 MiB)");
        }
        stream.retire(now_ms)?;
        while stream.bytes + cost(data.len()) > stream.def.max_size {
            if stream.def.strategy_on_full == Strategy::RejectNewData {
                return fail("full", format!("stream {name:?} is full"));
            }
            stream.evict_front();
        }
        let sequence = stream.next;
        if sequence + 1 > stream.reserved {
            // Hand out a number only once its reservation is durable; on a
            // failed write the next append retries it.
            let previous = stream.reserved;
            stream.reserved = sequence + RESERVE;
            if let Err(error) = stream.save_meta() {
                stream.reserved = previous;
                return Err(error);
            }
        }
        let at = match stream.def.persistence {
            Persistence::Memory => Location::Memory(data.to_vec()),
            Persistence::File => stream.write_record(sequence, now_ms, data)?,
        };
        stream.entries.push_back(Entry { sequence, appended_ms: now_ms, len: data.len() as u32, at });
        stream.bytes += cost(data.len());
        stream.next += 1;
        if stream.entries.len() == 1 {
            stream.oldest = sequence;
        }
        stream.drop_segments()?;
        Ok(sequence)
    }

    pub fn read(&mut self, name: &str, start: u64, min: u64, max: u64, now_ms: u64) -> anyhow::Result<Vec<Message>> {
        let stream = self.get(name)?;
        if min < 1 || max < min || max > MAX_READ {
            return fail("invalid", "need 1 <= min <= max <= 1000");
        }
        stream.retire(now_ms)?;
        if start < stream.oldest {
            return Err(StreamError { code: "evicted", oldest: Some(stream.oldest), detail: format!("oldest retained is {}", stream.oldest) }.into());
        }
        let found: Vec<&Entry> = stream.entries.iter().filter(|e| e.sequence >= start).take(max as usize).collect();
        if (found.len() as u64) < min {
            return fail("not-enough-messages", format!("{} available", found.len()));
        }
        found.into_iter().map(|e| Ok(Message { sequence: e.sequence, appended_ms: e.appended_ms, data: stream.read_data(e)? })).collect()
    }

    pub fn describe(&mut self, name: &str, now_ms: u64) -> anyhow::Result<Value> {
        let stream = self.get(name)?;
        stream.retire(now_ms)?;
        Ok(stream.describe())
    }

    /// Record that everything up to `sequence` reached the destination.
    /// Idempotent and monotonic; never beyond the newest message.
    pub fn export_ack(&mut self, name: &str, sequence: u64, now_ms: u64) -> anyhow::Result<i64> {
        let stream = self.get(name)?;
        if sequence >= stream.next {
            return fail("invalid", "cannot acknowledge beyond the newest message");
        }
        if sequence as i64 > stream.exported {
            stream.exported = sequence as i64;
            stream.save_meta()?;
            stream.retire(now_ms)?;
        }
        Ok(stream.exported)
    }

    /// The next batch after the checkpoint: messages, and how many messages
    /// were retired before they could be exported.
    pub fn export_batch(&mut self, name: &str, max_messages: usize, max_bytes: usize, now_ms: u64) -> anyhow::Result<Option<(u64, Vec<Message>)>> {
        let stream = self.get(name)?;
        stream.retire(now_ms)?;
        let start = (stream.exported + 1) as u64;
        let lost = stream.oldest.saturating_sub(start);
        let mut messages = Vec::new();
        let mut bytes = 0usize;
        for entry in stream.entries.iter().filter(|e| e.sequence >= start) {
            if messages.len() == max_messages || (!messages.is_empty() && bytes + entry.len as usize > max_bytes) {
                break;
            }
            bytes += entry.len as usize;
            messages.push(Message { sequence: entry.sequence, appended_ms: entry.appended_ms, data: stream.read_data(entry)? });
        }
        Ok(if messages.is_empty() { None } else { Some((lost, messages)) })
    }

    pub fn set_export_status(&mut self, name: &str, status: Value) {
        if let Some(stream) = self.streams.get_mut(name) {
            stream.export_status = status;
        }
    }

    /// Persist exact sequence counters (clean shutdown).
    pub fn close(&mut self) {
        for stream in self.streams.values_mut() {
            stream.reserved = stream.next;
            if let Err(error) = stream.save_meta() {
                warn!(stream = stream.def.name, %error, "Could not persist a memory stream's counter");
            }
        }
    }
}

impl Drop for Store {
    fn drop(&mut self) {
        self.close();
    }
}

/// Signed by the device key as a Sui personal message: binds an export batch
/// body to this device, chain, registry, stream and sequence range.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub schema: u32,
    pub kind: String,
    pub chain_identifier: String,
    pub registry: String,
    pub device_id: String,
    pub signer: String,
    pub stream: String,
    pub first_sequence: u64,
    pub last_sequence: u64,
    pub messages: u64,
    pub lost_before: u64,
    pub body_sha256: String,
    pub created_ms: u64,
}
pub const MANIFEST_KIND: &str = "stream-export";
pub const MAX_MANIFEST_BYTES: usize = 2048;

#[derive(Debug, Clone)]
pub struct Binding {
    pub chain_identifier: String,
    pub registry: String,
    pub device_id: String,
}

pub struct Batch {
    pub idempotency_key: String,
    pub body: Vec<u8>,
    pub manifest: Vec<u8>,
    pub signature: Vec<u8>,
    pub last_sequence: u64,
}

pub fn build_batch(binding: &Binding, signer: &dyn TxSigner, stream: &str, lost: u64, messages: &[Message], now_ms: u64) -> anyhow::Result<Batch> {
    let first = messages.first().ok_or_else(|| anyhow::anyhow!("empty batch"))?.sequence;
    let last = messages.last().unwrap().sequence;
    let body = serde_json::to_vec(&json!({
        "schema": MANIFEST_SCHEMA, "device_id": binding.device_id, "stream": stream, "lost_before": lost,
        "messages": messages.iter().map(|m| json!({"sequence": m.sequence, "appended_ms": m.appended_ms, "data": STANDARD.encode(&m.data)})).collect::<Vec<_>>(),
    }))?;
    let manifest = Manifest {
        schema: MANIFEST_SCHEMA,
        kind: MANIFEST_KIND.into(),
        chain_identifier: binding.chain_identifier.clone(),
        registry: binding.registry.clone(),
        device_id: binding.device_id.clone(),
        signer: signer.address(),
        stream: stream.into(),
        first_sequence: first,
        last_sequence: last,
        messages: messages.len() as u64,
        lost_before: lost,
        body_sha256: hex::encode(Sha256::digest(&body)),
        created_ms: now_ms,
    };
    let manifest = serde_json::to_vec(&manifest)?;
    anyhow::ensure!(manifest.len() <= MAX_MANIFEST_BYTES, "export manifest too large");
    let signature = signer.sign_personal_message(&manifest)?;
    Ok(Batch { idempotency_key: format!("{}:{}:{first}-{last}", binding.device_id, hex::encode(stream.as_bytes())), body, manifest, signature, last_sequence: last })
}

/// Exports one stream until aborted.
pub struct Exporter {
    pub store: Arc<Mutex<Store>>,
    pub stream: String,
    pub config: ExportConfig,
    pub binding: Binding,
    pub signer: Arc<dyn TxSigner>,
}

impl Exporter {
    async fn send(&self, http: &reqwest::Client, batch: &Batch) -> anyhow::Result<()> {
        let mut request = http
            .post(&self.config.url)
            .header("content-type", "application/json")
            .header("idempotency-key", &batch.idempotency_key)
            .header("x-suifleet-manifest", STANDARD.encode(&batch.manifest))
            .header("x-suifleet-signature", STANDARD.encode(&batch.signature))
            .body(batch.body.clone());
        if let Some(path) = &self.config.bearer_token_file {
            let token = std::fs::read_to_string(path).map_err(|e| anyhow::anyhow!("cannot read bearer_token_file: {}", e.kind()))?;
            request = request.bearer_auth(token.trim());
        }
        // Redirects are not followed (see `http_client`), so a 3xx is a failure.
        // Errors leave out the URL: its query may carry a credential, and
        // they reach the log and components' stream_describe.
        let status = request.send().await.map_err(|e| anyhow::anyhow!("{}", sanitized(e, &self.config.url)))?.status();
        anyhow::ensure!(status.is_success(), "destination answered {status}");
        Ok(())
    }

    /// One attempt: Ok(true) when a batch was delivered and checkpointed.
    pub async fn step(&self, http: &reqwest::Client) -> anyhow::Result<bool> {
        let now = crate::telemetry::now_ms();
        let next = self.store.lock().unwrap().export_batch(&self.stream, self.config.batch_max_messages, self.config.batch_max_bytes, now)?;
        let Some((lost, messages)) = next else { return Ok(false) };
        if lost > 0 {
            warn!(stream = self.stream, lost, "Stream messages were retired before export");
        }
        let batch = build_batch(&self.binding, self.signer.as_ref(), &self.stream, lost, &messages, now)?;
        self.send(http, &batch).await?;
        let exported = self.store.lock().unwrap().export_ack(&self.stream, batch.last_sequence, crate::telemetry::now_ms())?;
        info!(stream = self.stream, exported, messages = messages.len(), "Exported stream batch");
        Ok(true)
    }

    /// Only a 2xx from the declared URL itself acknowledges a batch: a
    /// followed redirect would turn the POST into a GET elsewhere (a login
    /// page, say) whose 2xx would advance the checkpoint for undelivered data.
    pub fn http_client() -> reqwest::Result<reqwest::Client> {
        reqwest::Client::builder().timeout(Duration::from_secs(30)).redirect(reqwest::redirect::Policy::none()).build()
    }

    pub async fn run(self) {
        let http = match Self::http_client() {
            Ok(http) => http,
            Err(error) => return warn!(%error, "Stream export disabled: no HTTP client"),
        };
        let mut backoff = 1u64;
        let (mut batches, mut failures) = (0u64, 0u64);
        loop {
            let result = self.step(&http).await;
            let delay = match &result {
                Ok(true) => {
                    batches += 1;
                    backoff = 1;
                    0
                }
                Ok(false) => self.config.batch_interval_seconds,
                Err(error) => {
                    failures += 1;
                    warn!(stream = self.stream, %error, retry_seconds = backoff, "Stream export failed; checkpoint unchanged");
                    let delay = backoff;
                    backoff = (backoff * 2).min(self.config.max_backoff_seconds);
                    delay
                }
            };
            self.store.lock().unwrap().set_export_status(&self.stream, json!({
                "batches": batches, "failures": failures,
                "last_error": result.as_ref().err().map(|e| crate::telemetry::bounded(&e.to_string(), 300)),
                "updated_ms": crate::telemetry::now_ms(),
            }));
            if delay > 0 {
                tokio::time::sleep(Duration::from_secs(delay)).await;
            }
        }
    }
}

/// A transport error without its URL, with its cause chain; any remaining
/// copy of the query string is masked too.
fn sanitized(error: reqwest::Error, url: &str) -> String {
    let error = error.without_url();
    let mut text = error.to_string();
    let mut source = std::error::Error::source(&error);
    while let Some(cause) = source {
        text = format!("{text}: {cause}");
        source = cause.source();
    }
    match reqwest::Url::parse(url).ok().and_then(|u| u.query().map(str::to_owned)) {
        Some(query) if !query.is_empty() => text.replace(&query, "<redacted>"),
        _ => text,
    }
}

fn same_address(a: &str, b: &str) -> bool {
    let canonical = |x: &str| {
        let hex = x.strip_prefix("0x").unwrap_or(x).to_ascii_lowercase();
        (hex.len() <= 64 && !hex.is_empty() && hex.bytes().all(|c| c.is_ascii_hexdigit())).then(|| format!("{hex:0>64}"))
    };
    canonical(a).is_some() && canonical(a) == canonical(b)
}

/// Validate a manifest for the signing service: canonical, bounded, this
/// chain/registry/signer, and the declared kind.
pub fn check_manifest(message: &[u8], chain_identifier: &str, registry: &str, signer: &str) -> anyhow::Result<()> {
    anyhow::ensure!(message.len() <= MAX_MANIFEST_BYTES, "export manifest exceeds {MAX_MANIFEST_BYTES} bytes");
    let manifest: Manifest = serde_json::from_slice(message).map_err(|e| anyhow::anyhow!("not an export manifest: {e}"))?;
    anyhow::ensure!(serde_json::to_vec(&manifest)? == message, "export manifest is not in canonical form");
    anyhow::ensure!(manifest.schema == MANIFEST_SCHEMA && manifest.kind == MANIFEST_KIND, "unsupported export manifest");
    anyhow::ensure!(manifest.chain_identifier == chain_identifier, "export manifest is for another chain");
    anyhow::ensure!(same_address(&manifest.registry, registry), "export manifest is not for this fleet's registry");
    anyhow::ensure!(same_address(&manifest.signer, signer), "export manifest names another signer");
    anyhow::ensure!(valid_name(&manifest.stream) && manifest.first_sequence <= manifest.last_sequence
        && manifest.messages >= 1 && manifest.messages <= manifest.last_sequence - manifest.first_sequence + 1
        && manifest.body_sha256.len() == 64 && manifest.body_sha256.bytes().all(|b| b.is_ascii_hexdigit()), "malformed export manifest");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(tag: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("suifleet-streams-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        path
    }
    const QUOTA: Quota = Quota { max_streams: 32, max_total_bytes: 1 << 26 };

    fn code(error: &anyhow::Error) -> &'static str {
        error.downcast_ref::<StreamError>().map_or("other", |e| e.code)
    }

    #[test]
    fn shared_stream_cases() {
        let cases: Value = serde_json::from_str(include_str!("../../interfaces/compat/stream-cases.json")).unwrap();
        for case in cases.as_array().unwrap() {
            let path = dir(case["id"].as_str().unwrap());
            let mut store = Some(Store::open(&path, QUOTA, 1).unwrap());
            for step in case["steps"].as_array().unwrap() {
                let op = &step["op"];
                let s = store.as_mut().unwrap();
                let name = op["stream"].as_str().unwrap_or("");
                let outcome: anyhow::Result<Value> = match op["op"].as_str().unwrap() {
                    "restart" => {
                        drop(store.take());
                        store = Some(Store::open(&path, QUOTA, 1).unwrap());
                        Ok(json!({}))
                    }
                    "create" => (|| {
                        let def: Definition = serde_json::from_value(json!({
                            "name": name, "max_size": op["max_size"], "strategy_on_full": op["strategy_on_full"],
                            "persistence": op.get("persistence").cloned().unwrap_or(json!("File"))}))
                            .map_err(|e| StreamError { code: "invalid", oldest: None, detail: e.to_string() })?;
                        s.create(def).map(|_| json!({}))
                    })(),
                    "append" => s.append(name, &vec![7u8; op["size"].as_u64().unwrap() as usize], 1).map(|q| json!({"sequence": q})),
                    "read" => s.read(name, op["start"].as_u64().unwrap_or(0), op["min"].as_u64().unwrap_or(1), op["max"].as_u64().unwrap_or(1), 1)
                        .map(|m| json!({"sequences": m.iter().map(|m| m.sequence).collect::<Vec<_>>()})),
                    "export-ack" => s.export_ack(name, op["sequence"].as_u64().unwrap(), 1).map(|e| json!({"exported": e})),
                    "describe" => s.describe(name, 1).map(|d| json!({"oldest": d["oldest"], "newest": d["newest"], "exported": d["exported"], "bytes": d["bytes"]})),
                    other => panic!("unknown op {other}"),
                };
                let got = match outcome {
                    Ok(value) => value,
                    Err(error) => {
                        let e = error.downcast_ref::<StreamError>().unwrap_or_else(|| panic!("{}: {error:#}", case["id"]));
                        match e.oldest {
                            Some(oldest) => json!({"error": e.code, "oldest": oldest}),
                            None => json!({"error": e.code}),
                        }
                    }
                };
                assert_eq!(got, step["expect"], "{} step {op}", case["id"]);
            }
            drop(store);
            let _ = std::fs::remove_dir_all(&path);
        }
    }

    fn def(name: &str, max: u64, strategy: Strategy) -> Definition {
        Definition { name: name.into(), max_size: max, strategy_on_full: strategy, persistence: Persistence::File,
            time_to_live_seconds: None, discard_exported: false, export: None }
    }

    #[test]
    fn file_streams_survive_crashes_and_truncate_torn_tails() {
        let path = dir("crash");
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        store.create(def("s", 64 * 1024, Strategy::RejectNewData)).unwrap();
        for i in 0..50u32 {
            store.append("s", format!("message {i}").as_bytes(), 10).unwrap();
        }
        std::mem::forget(store); // a crash: nothing closed
        let seg = std::fs::read_dir(path.join(dir_name("s"))).unwrap().map(|e| e.unwrap().path())
            .filter(|p| p.extension().is_some_and(|e| e == "seg")).max().unwrap();
        // A torn append: half a header and garbage.
        std::fs::OpenOptions::new().append(true).open(&seg).unwrap().write_all(&[9, 0, 0, 0, 1, 2, 3]).unwrap();
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        let all = store.read("s", 0, 50, 100, 10).unwrap();
        assert_eq!(all.len(), 50);
        assert_eq!(all[49].data, b"message 49");
        // After a crash numbering skips past the reservation, never back.
        let after = store.append("s", b"after", 11).unwrap();
        assert_eq!(after, RESERVE);
        drop(store);
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        assert_eq!(store.read("s", after, 1, 1, 12).unwrap()[0].data, b"after");
        assert_eq!(store.append("s", b"clean", 12).unwrap(), after + 1, "a clean close is exact");
        // A flipped bit in the middle of the last segment drops what follows it.
        drop(store);
        let mut bytes = std::fs::read(&seg).unwrap();
        let middle = bytes.len() / 2;
        bytes[middle] ^= 0xff;
        std::fs::write(&seg, bytes).unwrap();
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        let d = store.describe("s", 13).unwrap();
        assert!(d["messages"].as_u64().unwrap() < 52, "records after the corruption are dropped");
        assert_eq!(store.append("s", b"x", 14).unwrap(), after + 2, "and their numbers are not reused");
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn overwrite_segments_are_reclaimed_and_eviction_survives_restart() {
        let path = dir("overwrite");
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        store.create(def("o", 8192, Strategy::OverwriteOldestData)).unwrap();
        for i in 0..1000u64 {
            store.append("o", &[i as u8; 100], i).unwrap();
        }
        let d = store.describe("o", 1000).unwrap();
        assert_eq!((d["newest"].as_i64(), d["bytes"].as_u64()), (Some(999), Some(49 * (100 + MESSAGE_OVERHEAD))));
        let disk: u64 = std::fs::read_dir(path.join(dir_name("o"))).unwrap().map(|e| e.unwrap().metadata().unwrap().len()).sum();
        assert!(disk < 8192 * 3, "evicted segments are deleted ({disk} bytes on disk)");
        let oldest = d["oldest"].as_u64().unwrap();
        std::mem::forget(store);
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        assert_eq!(store.describe("o", 1000).unwrap()["oldest"].as_u64(), Some(oldest));
        let e = store.read("o", oldest - 1, 1, 1, 1000).unwrap_err();
        assert_eq!((code(&e), e.downcast_ref::<StreamError>().unwrap().oldest), ("evicted", Some(oldest)));
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn retention_quota_and_export_batches() {
        let path = dir("retention");
        let mut store = Store::open(&path, Quota { max_streams: 2, max_total_bytes: 4096 }, 0).unwrap();
        let mut ttl = def("ttl", 2048, Strategy::RejectNewData);
        ttl.time_to_live_seconds = Some(10);
        store.create(ttl).unwrap();
        assert_eq!(code(&store.create(def("big", 4096, Strategy::RejectNewData)).unwrap_err()), "quota");
        store.create(def("second", 1024, Strategy::RejectNewData)).unwrap();
        assert_eq!(code(&store.create(def("third", 1024, Strategy::RejectNewData)).unwrap_err()), "quota");
        store.append("ttl", b"old", 1_000).unwrap();
        store.append("ttl", b"new", 9_000).unwrap();
        assert_eq!(store.describe("ttl", 11_000).unwrap()["oldest"].as_u64(), Some(1), "expired by time-to-live");
        // Export batches resume from the checkpoint and report retired messages.
        let (lost, batch) = store.export_batch("ttl", 10, 1 << 20, 11_000).unwrap().unwrap();
        assert_eq!((lost, batch.len(), batch[0].sequence), (1, 1, 1));
        assert_eq!(store.export_ack("ttl", 1, 11_000).unwrap(), 1);
        assert!(store.export_batch("ttl", 10, 1 << 20, 11_000).unwrap().is_none());
        // discard_exported needs an export; with one, acknowledged messages go.
        let mut discard = def("second", 1024, Strategy::RejectNewData);
        discard.discard_exported = true;
        assert_eq!(code(&discard.validate().unwrap_err()), "invalid");
        discard.export = Some(ExportConfig { url: "http://127.0.0.1:9/x".into(), bearer_token_file: None, batch_max_messages: 2,
            batch_max_bytes: 1024, batch_interval_seconds: 1, max_backoff_seconds: 5 });
        drop(store);
        let mut store = Store::start(&path, Quota { max_streams: 2, max_total_bytes: 4096 }, &[discard], 0).unwrap();
        for i in 0..3u8 {
            store.append("second", &[i; 250], 0).unwrap();
        }
        assert_eq!(code(&store.append("second", &[9; 250], 0).unwrap_err()), "full");
        let (_, first) = store.export_batch("second", 2, 1024, 0).unwrap().unwrap();
        assert_eq!(first.iter().map(|m| m.sequence).collect::<Vec<_>>(), vec![0, 1], "batch_max_messages");
        store.export_ack("second", 1, 0).unwrap();
        assert_eq!(store.append("second", &[9; 250], 0).unwrap(), 3, "exported messages discarded to make room");
        let bad = ExportConfig { url: "http://user:pw@host/x".into(), ..store.streams["second"].def.export.clone().unwrap() };
        assert!(bad.validate().is_err(), "no credentials in URLs");
        assert_eq!(code(&store.append("missing", b"x", 0).unwrap_err()), "not-found");
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn startup_enforces_the_current_quota_on_stored_streams() {
        let path = dir("restored-quota");
        let quota = Quota { max_streams: 2, max_total_bytes: 16384 };
        let mut store = Store::start(&path, quota, &[], 0).unwrap();
        // Component-created streams, absent from the operator definitions.
        store.create(def("a", 8192, Strategy::RejectNewData)).unwrap();
        store.create(def("b", 8192, Strategy::RejectNewData)).unwrap();
        store.append("a", b"kept", 0).unwrap();
        drop(store);
        let snapshot = || std::fs::read_dir(&path).unwrap().flat_map(|e| std::fs::read_dir(e.unwrap().path()).unwrap())
            .map(|e| { let p = e.unwrap().path(); (p.clone(), std::fs::read(p).unwrap()) }).collect::<BTreeMap<_, _>>();
        let before = snapshot();
        // Reduced count or byte budgets fail startup and change nothing.
        for reduced in [Quota { max_streams: 1, max_total_bytes: 16384 }, Quota { max_streams: 2, max_total_bytes: 1024 }] {
            let error = Store::start(&path, reduced, &[], 0).err().expect("over-quota inventory refused");
            assert!(error.to_string().contains("need 2 streams and 16384 bytes"), "{error}");
        }
        // A declared definition is counted with its new size, as is a new one.
        let shrink = [def("a", 4096, Strategy::RejectNewData)];
        assert!(Store::start(&path, Quota { max_streams: 2, max_total_bytes: 8192 }, &shrink, 0).is_err());
        let grow = [def("c", 1024, Strategy::RejectNewData)];
        assert!(Store::start(&path, quota, &grow, 0).is_err());
        let mut memory = def("a", 8192, Strategy::RejectNewData);
        memory.persistence = Persistence::Memory;
        assert!(Store::start(&path, quota, &[memory], 0).is_err(), "persistence change refused before any change");
        assert_eq!(snapshot(), before, "refused startups leave stream data untouched");
        // Unchanged budgets reopen; shrinking declared streams into a smaller budget works.
        let mut store = Store::start(&path, quota, &[], 0).unwrap();
        assert_eq!(store.read("a", 0, 1, 1, 0).unwrap()[0].data, b"kept");
        drop(store);
        let both = [def("a", 4096, Strategy::RejectNewData), def("b", 4096, Strategy::RejectNewData)];
        let mut store = Store::start(&path, Quota { max_streams: 2, max_total_bytes: 8192 }, &both, 0).unwrap();
        assert_eq!(store.reserved_bytes(), 8192);
        store.append("b", &[0; 1500], 0).unwrap();
        store.append("b", &[0; 1500], 0).unwrap();
        assert_eq!(code(&store.append("b", &[0; 1500], 0).unwrap_err()), "full", "the new size is enforced");
        drop(store);
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn memory_streams_never_reuse_sequences_after_a_crash() {
        let path = dir("memory");
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        let mut m = def("m", 4096, Strategy::RejectNewData);
        m.persistence = Persistence::Memory;
        store.create(m).unwrap();
        for _ in 0..5 {
            store.append("m", b"x", 0).unwrap();
        }
        std::mem::forget(store);
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        let next = store.append("m", b"y", 0).unwrap();
        assert!((5..=RESERVE).contains(&next), "after a crash numbering skips ahead, never back ({next})");
        assert!(!std::fs::read_dir(path.join(dir_name("m"))).unwrap().any(|e| e.unwrap().path().extension().is_some_and(|x| x == "seg")));
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn a_failed_reservation_write_hands_out_no_number() {
        for persistence in [Persistence::Memory, Persistence::File] {
            let path = dir(&format!("reserve-{persistence:?}"));
            let mut store = Store::open(&path, QUOTA, 0).unwrap();
            store.create(Definition { persistence, ..def("r", 4096, Strategy::RejectNewData) }).unwrap();
            // The atomic write's temporary file cannot be created.
            let blocker = path.join(dir_name("r")).join("meta.tmp");
            std::fs::create_dir(&blocker).unwrap();
            assert!(store.append("r", b"x", 0).is_err(), "no durable reservation, no number");
            std::fs::remove_dir(&blocker).unwrap();
            assert_eq!(store.append("r", b"x", 0).unwrap(), 0);
            std::mem::forget(store); // a crash after the retried reservation
            let mut store = Store::open(&path, QUOTA, 0).unwrap();
            assert_eq!(store.append("r", b"y", 0).unwrap(), RESERVE, "{persistence:?}: the retried reservation was persisted");
            drop(store);
            let _ = std::fs::remove_dir_all(&path);
        }
    }

    #[test]
    fn a_failed_segment_write_leaves_only_acknowledged_records() {
        use faults::{Faults, FAULTS};
        let path = dir("torn-write");
        let seg_dir = path.join(dir_name("w"));
        let seg_len = |first: u64| std::fs::metadata(segment_path(&seg_dir, first)).map(|m| m.len()).ok();
        let all = |store: &mut Store| -> Vec<(u64, Vec<u8>)> {
            let n = store.describe("w", 0).unwrap()["messages"].as_u64().unwrap();
            store.read("w", 0, n, n, 0).unwrap().into_iter().map(|m| (m.sequence, m.data)).collect()
        };
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        // 1000-byte messages are 1024-byte records; a segment holds four.
        store.create(def("w", 1 << 15, Strategy::RejectNewData)).unwrap();
        let big = |b: u8| vec![b; 1000];
        assert_eq!(store.append("w", &big(0), 0).unwrap(), 0);

        // A partial write (e.g. disk full) is cut back; the retry is intact.
        FAULTS.set(Faults { partial: Some(30), ..Faults::default() });
        assert!(store.append("w", &big(b'A'), 0).is_err());
        assert_eq!(seg_len(0), Some(1024));
        assert_eq!(store.append("w", &big(1), 0).unwrap(), 1);
        // A failed sync after the whole record was written is undone too.
        FAULTS.set(Faults { sync: true, ..Faults::default() });
        assert!(store.append("w", &big(b'B'), 0).is_err());
        assert_eq!(seg_len(0), Some(2048));
        assert_eq!(store.append("w", &big(2), 0).unwrap(), 2);
        // If the undo fails as well, appends fail until it succeeds.
        FAULTS.set(Faults { partial: Some(500), undo: 2, ..Faults::default() });
        assert!(store.append("w", &big(b'C'), 0).is_err());
        assert_eq!(code(&store.append("w", &big(3), 0).unwrap_err()), "unavailable");
        assert_eq!(seg_len(0), Some(3072 + 500));
        assert_eq!(store.append("w", &big(3), 0).unwrap(), 3);
        assert_eq!(seg_len(0), Some(4096));
        // A failed write that started a segment removes it.
        FAULTS.set(Faults { partial: Some(10), ..Faults::default() });
        assert!(store.append("w", &big(b'D'), 0).is_err());
        assert_eq!(seg_len(4), None);
        let expected: Vec<(u64, Vec<u8>)> = (0..4).map(|i| (i, big(i as u8))).collect();
        assert_eq!(all(&mut store), expected);
        drop(store);
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        assert_eq!(all(&mut store), expected, "every acknowledged record survives reopening");
        assert_eq!(store.append("w", &big(4), 0).unwrap(), 4);

        // A crash with an undo still pending: recovery cuts the torn tail.
        FAULTS.set(Faults { partial: Some(700), undo: 1, ..Faults::default() });
        assert!(store.append("w", &big(b'E'), 0).is_err());
        assert_eq!(seg_len(4), Some(1024 + 700));
        std::mem::forget(store);
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        assert_eq!(seg_len(4), Some(1024));
        assert_eq!(all(&mut store).len(), 5);
        assert!(store.append("w", &big(5), 0).unwrap() > 4);
        FAULTS.set(Faults::default());
        drop(store);
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn retiring_a_segment_with_a_pending_undo_lets_appends_resume() {
        use faults::{Faults, FAULTS};
        let path = dir("torn-retired");
        let seg_dir = path.join(dir_name("t"));
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        let mut ttl = def("t", 1 << 15, Strategy::RejectNewData);
        ttl.time_to_live_seconds = Some(1);
        store.create(ttl).unwrap();
        let mut discard = def("d", 1 << 15, Strategy::RejectNewData);
        discard.discard_exported = true;
        discard.export = Some(ExportConfig { url: "http://127.0.0.1:9/x".into(), bearer_token_file: None, batch_max_messages: 2,
            batch_max_bytes: 1024, batch_interval_seconds: 1, max_backoff_seconds: 5 });
        store.create(discard).unwrap();
        store.append("t", b"expires", 0).unwrap();
        store.append("d", b"exported", 0).unwrap();
        // A failed write whose undo also fails leaves a torn tail pending.
        for name in ["t", "d"] {
            FAULTS.set(Faults { partial: Some(10), undo: 1, ..Faults::default() });
            assert!(store.append(name, b"torn", 0).is_err());
        }
        // Retention retires the whole segment, torn tail included.
        assert_eq!(store.describe("t", 2_000).unwrap()["messages"].as_u64(), Some(0));
        store.export_ack("d", 0, 0).unwrap();
        assert!(std::fs::metadata(segment_path(&seg_dir, 0)).is_err());
        let t = store.append("t", b"after", 2_000).unwrap();
        let d = store.append("d", b"after", 0).unwrap();
        assert!(t >= 1 && d >= 1);
        assert_eq!(store.read("t", t, 1, 1, 2_000).unwrap()[0].data, b"after");
        assert_eq!(store.read("d", d, 1, 1, 0).unwrap()[0].data, b"after");
        drop(store);
        let mut store = Store::open(&path, QUOTA, 2_000).unwrap();
        assert_eq!(store.read("t", t, 1, 1, 2_000).unwrap()[0].data, b"after", "survives reopening");
        assert_eq!(store.read("d", d, 1, 1, 2_000).unwrap()[0].data, b"after");
        assert!(store.append("t", b"more", 2_000).unwrap() > t);
        FAULTS.set(Faults::default());
        drop(store);
        let _ = std::fs::remove_dir_all(&path);
    }

    fn disk_use(path: &Path) -> u64 {
        std::fs::read_dir(path).unwrap().map(|e| e.unwrap().metadata().unwrap().len()).sum()
    }

    /// Per-message overhead counts against max_size, so tiny messages cannot
    /// multiply disk use: the documented bound is max_size plus one segment
    /// of retired records (and meta.json).
    #[test]
    fn small_messages_stay_within_the_disk_bound() {
        let path = dir("small");
        let mut store = Store::open(&path, Quota { max_streams: 4, max_total_bytes: 3 * 8192 }, 0).unwrap();
        store.create(def("reject", 8192, Strategy::RejectNewData)).unwrap();
        let mut accepted = 0;
        while store.append("reject", b"x", 0).is_ok() {
            accepted += 1;
        }
        assert_eq!(accepted, 8192 / (1 + MESSAGE_OVERHEAD), "a 1-byte message costs 1 + {MESSAGE_OVERHEAD}");
        assert_eq!(store.describe("reject", 0).unwrap()["bytes"].as_u64(), Some(accepted * (1 + MESSAGE_OVERHEAD)));
        store.create(def("ring", 8192, Strategy::OverwriteOldestData)).unwrap();
        let mut memory = def("memory", 8192, Strategy::OverwriteOldestData);
        memory.persistence = Persistence::Memory;
        store.create(memory).unwrap();
        let mut peak = 0;
        for i in 0..5000u64 {
            store.append("ring", &vec![i as u8; 1 + (i % 7) as usize], i).unwrap();
            store.append("memory", b"m", i).unwrap();
            peak = peak.max(disk_use(&path.join(dir_name("ring"))));
        }
        let segment = 4096; // max_size / 8 clamped to 4 KiB..4 MiB
        let meta = std::fs::metadata(path.join(dir_name("ring")).join("meta.json")).unwrap().len();
        assert!(peak <= 8192 + segment + meta, "ring peaked at {peak} bytes on disk");
        assert!(disk_use(&path.join(dir_name("reject"))) <= 8192 + meta);
        assert!(store.describe("memory", 0).unwrap()["bytes"].as_u64().unwrap() <= 8192);
        assert_eq!(code(&store.append("reject", &vec![1; (8192 - MESSAGE_OVERHEAD + 1) as usize], 0).unwrap_err()), "invalid");
        drop(store);
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn longest_names_create_reopen_and_delete() {
        let path = dir("names");
        let long = "n".repeat(255);
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        store.create(def(&long, 4096, Strategy::RejectNewData)).unwrap();
        store.append(&long, b"kept", 0).unwrap();
        drop(store);
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        assert_eq!(store.read(&long, 0, 1, 1, 0).unwrap()[0].data, b"kept");
        store.delete(&long).unwrap();
        assert!(store.names().is_empty() && std::fs::read_dir(&path).unwrap().next().is_none());
        // State that cannot be opened is left untouched, not recreated over.
        let broken = path.join(dir_name("b"));
        std::fs::create_dir(&broken).unwrap();
        std::fs::write(broken.join("meta.json"), b"not json").unwrap();
        let mut store = Store::open(&path, QUOTA, 0).unwrap();
        assert_eq!(code(&store.create(def("b", 4096, Strategy::RejectNewData)).unwrap_err()), "exists");
        assert_eq!(std::fs::read(broken.join("meta.json")).unwrap(), b"not json");
        drop(store);
        let _ = std::fs::remove_dir_all(&path);
    }

    /// An HTTP server answering POSTs with `post_status` and anything else
    /// with 200; it records request lines.
    fn http_server(post_status: &'static str) -> (String, Arc<Mutex<Vec<String>>>) {
        use std::io::BufRead;
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/ingest", listener.local_addr().unwrap());
        let seen = Arc::new(Mutex::new(Vec::new()));
        let log = seen.clone();
        std::thread::spawn(move || {
            for connection in listener.incoming() {
                let Ok(mut connection) = connection else { return };
                let mut reader = std::io::BufReader::new(connection.try_clone().unwrap());
                let (mut line, mut length) = (String::new(), 0usize);
                reader.read_line(&mut line).unwrap();
                let request = line.trim().to_string();
                loop {
                    line.clear();
                    reader.read_line(&mut line).unwrap();
                    if line.trim().is_empty() {
                        break;
                    }
                    if let Some((key, value)) = line.split_once(':') {
                        if key.eq_ignore_ascii_case("content-length") {
                            length = value.trim().parse().unwrap();
                        }
                    }
                }
                reader.read_exact(&mut vec![0; length]).unwrap();
                let status = if request.starts_with("POST") { post_status } else { "200 OK" };
                log.lock().unwrap().push(request);
                write!(connection, "HTTP/1.1 {status}\r\nLocation: /login\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok").unwrap();
            }
        });
        (url, seen)
    }

    #[tokio::test]
    async fn only_a_2xx_from_the_declared_url_advances_the_checkpoint() {
        for (status, delivered) in [("302 Found", false), ("307 Temporary Redirect", false), ("200 OK", true)] {
            let path = dir(&format!("redirect-{}", &status[..3]));
            let (url, seen) = http_server(status);
            let store = Arc::new(Mutex::new(Store::open(&path, QUOTA, 0).unwrap()));
            let mut d = def("e", 4096, Strategy::RejectNewData);
            d.discard_exported = true;
            d.export = Some(ExportConfig { url, bearer_token_file: None, batch_max_messages: 10, batch_max_bytes: 1024,
                batch_interval_seconds: 1, max_backoff_seconds: 1 });
            store.lock().unwrap().create(d.clone()).unwrap();
            store.lock().unwrap().append("e", b"data", 0).unwrap();
            let exporter = Exporter { store: store.clone(), stream: "e".into(), config: d.export.unwrap(),
                binding: Binding { chain_identifier: "chain".into(), registry: "0x5".into(), device_id: "0xd".into() }, signer: Arc::new(Key) };
            let result = exporter.step(&Exporter::http_client().unwrap()).await;
            assert_eq!(result.is_ok(), delivered, "{status}: {result:?}");
            let described = store.lock().unwrap().describe("e", 0).unwrap();
            assert_eq!((described["exported"].as_i64(), described["messages"].as_u64()),
                if delivered { (Some(0), Some(0)) } else { (Some(-1), Some(1)) }, "{status}");
            assert_eq!(*seen.lock().unwrap(), vec!["POST /ingest HTTP/1.1"], "{status}: the redirect is not followed");
            drop(exporter);
            drop(store);
            let _ = std::fs::remove_dir_all(&path);
        }
    }

    /// Export errors reach the log and components' stream_describe, so they
    /// must not carry the destination's query (a signed-URL credential) or
    /// the token file's path.
    #[tokio::test]
    async fn export_errors_do_not_disclose_the_destination_query_or_token_path() {
        let path = dir("redact");
        let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap();
        let store = Arc::new(Mutex::new(Store::open(&path, QUOTA, 0).unwrap()));
        let mut d = def("e", 4096, Strategy::RejectNewData);
        d.export = Some(ExportConfig { url: format!("http://{closed}/ingest?token=REVIEW_SECRET"), bearer_token_file: None,
            batch_max_messages: 10, batch_max_bytes: 1024, batch_interval_seconds: 1, max_backoff_seconds: 1 });
        store.lock().unwrap().create(d.clone()).unwrap();
        store.lock().unwrap().append("e", b"data", 0).unwrap();
        let exporter = |config: ExportConfig| Exporter { store: store.clone(), stream: "e".into(), config,
            binding: Binding { chain_identifier: "chain".into(), registry: "0x5".into(), device_id: "0xd".into() }, signer: Arc::new(Key) };
        let error = exporter(d.export.clone().unwrap()).step(&Exporter::http_client().unwrap()).await.unwrap_err();
        assert!(!format!("{error:#}").contains("REVIEW_SECRET"), "{error:#}");
        let mut with_token = d.export.clone().unwrap();
        with_token.bearer_token_file = Some(PathBuf::from("/private/REVIEW_TOKEN_PATH"));
        let error = exporter(with_token).step(&Exporter::http_client().unwrap()).await.unwrap_err();
        assert!(!format!("{error:#}").contains("REVIEW_TOKEN_PATH"), "{error:#}");
        // The status a component sees through IPC after a failed attempt.
        let run = tokio::spawn(exporter(d.export.clone().unwrap()).run());
        let mut status = Value::Null;
        for _ in 0..200 {
            let all = |_: &str| true;
            status = handle(&store, &[], "stream_describe", "e", &Value::Null, &all).unwrap();
            if status["export"]["status"]["failures"].as_u64().unwrap_or(0) > 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        run.abort();
        assert!(status["export"]["status"]["last_error"].is_string(), "{status}");
        assert_eq!(status["export"]["url"], format!("http://{closed}/ingest"));
        assert!(!status.to_string().contains("REVIEW_SECRET"), "{status}");
        drop(store);
        let _ = std::fs::remove_dir_all(&path);
    }

    struct Key;
    impl TxSigner for Key {
        fn address(&self) -> String {
            "0xabc".into()
        }
        fn sign_digest(&self, _: &[u8]) -> anyhow::Result<Vec<u8>> {
            Ok(vec![1])
        }
    }

    #[test]
    fn manifests_bind_the_body_and_the_signer_service_checks_them() {
        let binding = Binding { chain_identifier: "chain".into(), registry: "0x5".into(), device_id: "0xd".into() };
        assert!(same_address("0x05", &format!("0x{:0>64}", "5")) && !same_address("0x5", "0x6") && !same_address("zz", "zz"));
        let messages = vec![Message { sequence: 4, appended_ms: 1, data: b"hi".to_vec() }, Message { sequence: 6, appended_ms: 2, data: b"yo".to_vec() }];
        let batch = build_batch(&binding, &Key, "telemetry data", 2, &messages, 3).unwrap();
        let manifest: Manifest = serde_json::from_slice(&batch.manifest).unwrap();
        assert_eq!(manifest.body_sha256, hex::encode(Sha256::digest(&batch.body)));
        assert_eq!((manifest.first_sequence, manifest.last_sequence, manifest.messages, manifest.lost_before), (4, 6, 2, 2));
        assert_eq!(batch.idempotency_key, format!("0xd:{}:4-6", hex::encode("telemetry data")));
        check_manifest(&batch.manifest, "chain", "0x5", "0xabc").unwrap();
        assert!(check_manifest(&batch.manifest, "other", "0x5", "0xabc").is_err());
        assert!(check_manifest(&batch.manifest, "chain", "0x6", "0xabc").is_err());
        assert!(check_manifest(&batch.manifest, "chain", "0x5", "0xdef").is_err());
        let mut pretty = serde_json::to_vec_pretty(&manifest).unwrap();
        assert!(check_manifest(&pretty, "chain", "0x5", "0xabc").is_err(), "non-canonical");
        pretty.clear();
        assert!(check_manifest(b"{\"anything\":1}", "chain", "0x5", "0xabc").is_err());
    }

    #[test]
    fn ipc_requests() {
        let path = dir("ipc");
        let store = Mutex::new(Store::open(&path, QUOTA, 0).unwrap());
        let all = |_: &str| true;
        let call = |op: &str, name: &str, payload: Value| handle(&store, &["declared".to_string()], op, name, &payload, &all);
        call("stream_create", "local", json!({"max_size": 4096, "strategy_on_full": "RejectNewData"})).unwrap();
        let e = call("stream_create", "x", json!({"max_size": 4096, "strategy_on_full": "RejectNewData", "export": {"url": "http://h/x"}})).unwrap_err();
        assert_eq!(code(&e), "invalid", "components cannot declare exports");
        assert_eq!(code(&call("stream_create", "y", json!({"name": "z", "max_size": 4096, "strategy_on_full": "RejectNewData"})).unwrap_err()), "invalid");
        assert_eq!(call("stream_append", "local", json!({"t": 21})).unwrap()["sequence"], 0);
        assert_eq!(call("stream_append", "local", json!("text")).unwrap()["sequence"], 1);
        let read = call("stream_read", "local", json!({"start": 0, "max": 5})).unwrap();
        assert_eq!(read["messages"][0]["payload"], json!({"t": 21}));
        assert_eq!(read["messages"][1]["payload"], json!("text"));
        assert_eq!(code(&call("stream_read", "local", json!({"start": "0"})).unwrap_err()), "invalid");
        store.lock().unwrap().create(def("declared", 4096, Strategy::RejectNewData)).unwrap();
        assert_eq!(code(&call("stream_delete", "declared", Value::Null).unwrap_err()), "declared");
        assert_eq!(call("stream_list", "", Value::Null).unwrap()["streams"], json!(["declared", "local"]));
        call("stream_delete", "local", Value::Null).unwrap();
        assert_eq!(code(&call("stream_describe", "local", Value::Null).unwrap_err()), "not-found");
        assert!(glob("sensor-*", "sensor-a") && glob("*", "x") && glob("a*b*c", "aXbYc") && !glob("sensor-*", "other") && !glob("a", "ab"));
        drop(store);
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn crc_matches_the_standard_check_value() {
        assert_eq!(crc32(&[b"123456789"]), 0xCBF4_3926);
    }
}
