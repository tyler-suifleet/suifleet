//! CloudWatch Logs export (SF-060), the `cloudwatch-logs-export` adapter
//! pinned to `aws.greengrass.LogManager` 2.3.14 (docs/CLOUDWATCH_LOGS.md).
//!
//! A separate process (`suifleet --cloudwatch-logs CONFIG`) under its own
//! Unix user reads the daemon's log and the configured supervised
//! components' logs, redacts every line, and keeps the events in a durable,
//! byte-bounded spool. Every upload interval it sends each source's events
//! with `PutLogEvents` to log group
//! `/aws/greengrass/{componentType}/{region}/{componentName}` and stream
//! `/{yyyy/MM/dd}/thing/{thingName}`, signed (SigV4) with the scoped
//! credentials of the SF-057 container-credential endpoint.
//!
//! Delivery is at least once: a source's events leave the spool only after
//! CloudWatch accepted (or permanently rejected) them. A throttled, failed or
//! unanswered batch stops that source for the cycle and is resent unchanged
//! later; other sources go on.
//!
//! The spool is a journal of JSON lines, `fsync`ed per record and compacted
//! into a snapshot: each read pass is one record holding its events and the
//! source cursors, so a crash either keeps both or neither.
use crate::logs::{self, Cursor, Reader};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{BufRead, Write},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};
use tracing::{info, warn};

/// Bytes CloudWatch counts for each event on top of its UTF-8 message.
pub const EVENT_OVERHEAD: usize = 26;
/// The largest event, overhead included; a longer line is split.
pub const MAX_EVENT_BYTES: usize = 262_144;
pub const MAX_BATCH_EVENTS: usize = 10_000;
pub const MAX_BATCH_BYTES: usize = 1_048_576;
/// Event times are in [0, 10000-01-01T00:00:00Z), so each names a UTC day.
pub const TIME_LIMIT_SECONDS: i64 = 253_402_300_800;
pub const DEFAULT_SPOOL_LIMIT: u64 = 10 * 1024 * 1024;
pub const DEFAULT_INTERVAL_SECONDS: f64 = 300.0;
pub const DEFAULT_READ_SECONDS: u64 = 5;
/// The longest line read from a log; a longer one becomes a marker line.
pub const MAX_LINE_BYTES: usize = 1024 * 1024;
/// Lines read from all sources in one pass; the rest waits for the next.
pub const MAX_LINES_PER_PASS: usize = 10_000;
/// The daemon's log, uploaded as the `GreengrassSystemComponent` `System`.
pub const SYSTEM: &str = "System";
/// Confidential values the spool remembers, at most (see [`Spool`]).
pub const MAX_KNOWN_SECRETS: usize = 65_536;
const DAY_MS: i64 = 86_400_000;
const READER: Reader = Reader { max_scan: MAX_LINE_BYTES, strict: true };

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Level {
    #[serde(rename = "DEBUG")]
    Debug,
    #[serde(rename = "INFO")]
    Info,
    #[serde(rename = "WARN")]
    Warn,
    #[serde(rename = "ERROR")]
    Error,
}

impl Level {
    pub fn parse(text: &str) -> Option<Level> {
        match text {
            "DEBUG" => Some(Level::Debug),
            "INFO" => Some(Level::Info),
            "WARN" => Some(Level::Warn),
            "ERROR" => Some(Level::Error),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ComponentType {
    UserComponent,
    GreengrassSystemComponent,
}

impl ComponentType {
    #[cfg_attr(not(test), allow(dead_code))] // the declared fixture interface
    pub fn parse(text: &str) -> Option<ComponentType> {
        match text {
            "UserComponent" => Some(ComponentType::UserComponent),
            "GreengrassSystemComponent" => Some(ComponentType::GreengrassSystemComponent),
            _ => None,
        }
    }

    fn name(self) -> &'static str {
        match self {
            ComponentType::UserComponent => "UserComponent",
            ComponentType::GreengrassSystemComponent => "GreengrassSystemComponent",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Source {
    pub component_type: ComponentType,
    pub minimum: Level,
}

/// What is uploaded where. Validated by [`Settings::validate`].
#[derive(Debug, Clone, PartialEq)]
pub struct Settings {
    pub region: String,
    pub thing: String,
    pub interval_seconds: f64,
    /// Spooled event bytes (message plus [`EVENT_OVERHEAD`]) kept, at most.
    pub spool_limit: u64,
    /// Literal values replaced by [`crate::redact::MARK`] before spooling.
    pub redact: Vec<String>,
    pub sources: BTreeMap<String, Source>,
}

impl Settings {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(
            !self.region.is_empty() && self.region.len() <= 32 && self.region.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'),
            "region must be 1-32 lowercase letters, digits or '-'"
        );
        anyhow::ensure!(
            !self.thing.is_empty() && self.thing.len() <= 128 && self.thing.bytes().all(|b| b.is_ascii_alphanumeric() || b"_:-".contains(&b)),
            "thing name must be 1-128 letters, digits, '_', ':' or '-'"
        );
        anyhow::ensure!(
            self.interval_seconds.is_finite() && self.interval_seconds > 0.0 && self.interval_seconds <= 86_400.0,
            "upload interval must be more than 0 and at most 86400 seconds"
        );
        anyhow::ensure!(self.spool_limit > 0, "spool limit must be positive");
        anyhow::ensure!(self.redact.iter().all(|value| !value.is_empty()), "redacted values must not be empty");
        for name in self.sources.keys() {
            anyhow::ensure!(
                crate::deps::valid_name(name) && name != "." && name != "..",
                "source name {name:?} must be 1-128 letters, digits, '.', '_' or '-'"
            );
        }
        Ok(())
    }

    fn interval_ms(&self) -> u64 {
        (self.interval_seconds * 1000.0).ceil() as u64
    }

    fn group(&self, source: &str) -> String {
        format!("/aws/greengrass/{}/{}/{}", self.sources[source].component_type.name(), self.region, source)
    }

    fn stream(&self, day: &str) -> String {
        format!("/{day}/thing/{}", self.thing.replace(':', "+"))
    }

    /// Literals and remembered confidential values, longest first, so a
    /// value containing another is replaced whole.
    fn secrets(&self, known: &BTreeSet<String>) -> Vec<String> {
        let mut values: Vec<String> = self.redact.iter().chain(known).cloned().collect();
        values.sort_by_key(|v| std::cmp::Reverse(v.len()));
        values.dedup();
        values
    }
}

// --- Dates -----------------------------------------------------------------

/// (year, month, day) of a day count since 1970-01-01 (proleptic Gregorian).
fn civil(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + i64::from(month <= 2), month, day)
}

/// Days since 1970-01-01 of a civil date.
fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let yoe = year.rem_euclid(400);
    let mp = i64::from((month + 9) % 12);
    let doy = (153 * mp + 2) / 5 + i64::from(day) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// `yyyy/MM/dd` of an event time.
fn day(ms: i64) -> String {
    let (y, m, d) = civil(ms.div_euclid(DAY_MS));
    format!("{y:04}/{m:02}/{d:02}")
}

/// A leading RFC 3339 UTC time (`2026-10-17T12:00:00.123Z`, as written by
/// the daemon and Greengrass-style loggers): its milliseconds and the rest.
fn leading_time(line: &str) -> Option<(i64, &str)> {
    let b = line.as_bytes();
    if b.len() < 20 || b[4] != b'-' || b[7] != b'-' || !matches!(b[10], b'T' | b' ') || b[13] != b':' || b[16] != b':' {
        return None;
    }
    let num = |range: std::ops::Range<usize>| -> Option<i64> {
        let text = line.get(range)?;
        text.bytes().all(|c| c.is_ascii_digit()).then(|| text.parse().ok()).flatten()
    };
    let (year, month, day, hour, minute, second) = (num(0..4)?, num(5..7)?, num(8..10)?, num(11..13)?, num(14..16)?, num(17..19)?);
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) || hour > 23 || minute > 59 || second > 60 {
        return None;
    }
    let mut at = 19;
    let mut millis = 0i64;
    if b.get(at) == Some(&b'.') {
        at += 1;
        let digits = b[at..].iter().take_while(|c| c.is_ascii_digit()).count();
        if digits == 0 {
            return None;
        }
        let text = &line[at..at + digits.min(3)];
        millis = text.parse::<i64>().ok()? * 10i64.pow(3 - text.len() as u32);
        at += digits;
    }
    let rest = if b.get(at) == Some(&b'Z') {
        &line[at + 1..]
    } else if line.get(at..at + 6) == Some("+00:00") {
        &line[at + 6..]
    } else {
        return None;
    };
    let days = days_from_civil(year, month as u32, day as u32);
    let ms = ((days * 24 + hour) * 60 + minute) * 60_000 + second.min(59) * 1000 + millis;
    (0..TIME_LIMIT_SECONDS * 1000).contains(&ms).then_some((ms, rest))
}

/// What a log line says about itself: its level (`None` for `TRACE`, which is
/// below every minimum) and time. A line names its level as its first word,
/// after an optional leading time: `INFO`, `[WARN]`, `ERROR:`. Lines naming
/// none are `INFO`; lines without a time take `fallback_ms` (when read).
pub fn classify(line: &str, fallback_ms: i64) -> (Option<Level>, i64) {
    let (ms, rest) = leading_time(line).unwrap_or((fallback_ms, line));
    let word = rest.split_whitespace().next().unwrap_or("");
    let word = word.trim_start_matches('[').trim_end_matches(':').trim_end_matches(']');
    let level = match word {
        "TRACE" => None,
        "DEBUG" => Some(Level::Debug),
        "WARN" | "WARNING" => Some(Level::Warn),
        "ERROR" => Some(Level::Error),
        _ => Some(Level::Info),
    };
    (level, ms)
}

// --- Events ------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub seq: u64,
    pub source: String,
    pub ms: i64,
    pub message: String,
}

fn size(message: &str) -> u64 {
    (message.len() + EVENT_OVERHEAD) as u64
}

/// Split a (redacted) line into messages within [`MAX_EVENT_BYTES`], cut
/// only at UTF-8 character boundaries, as LogManager 2.2.2 and later do.
pub fn split(message: &str) -> Vec<String> {
    let limit = MAX_EVENT_BYTES - EVENT_OVERHEAD;
    let mut parts = Vec::new();
    let mut rest = message;
    while rest.len() > limit {
        let mut cut = limit;
        while !rest.is_char_boundary(cut) {
            cut -= 1;
        }
        parts.push(rest[..cut].to_owned());
        rest = &rest[cut..];
    }
    parts.push(rest.to_owned());
    parts
}

// --- Spool -------------------------------------------------------------------

/// One journal record.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
enum Record {
    /// A read pass: its events, then oldest events dropped to the limit.
    Pass {
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        events: Vec<Event>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        drop_through: Option<u64>,
        dropped: u64,
        next_seq: u64,
        #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
        cursors: BTreeMap<String, Cursor>,
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        secrets: Vec<String>,
    },
    /// Events CloudWatch accepted or permanently rejected.
    Ack { seqs: Vec<u64>, rejected: u64 },
    Snapshot {
        events: Vec<Event>,
        dropped: u64,
        rejected: u64,
        next_seq: u64,
        cursors: BTreeMap<String, Cursor>,
        secrets: BTreeSet<String>,
    },
}

/// The durable spool: `<dir>/spool.journal` (0600, in a 0700 directory).
///
/// Besides events and cursors it remembers every confidential value it has
/// seen (they may still be in unread lines after the daemon retired them),
/// up to [`MAX_KNOWN_SECRETS`]; beyond that, collection stops.
pub struct Spool {
    path: PathBuf,
    file: std::fs::File,
    length: u64,
    snapshot_length: u64,
    events: BTreeMap<u64, Event>,
    bytes: u64,
    pub dropped: u64,
    pub rejected: u64,
    next_seq: u64,
    pub cursors: BTreeMap<String, Cursor>,
    pub secrets: BTreeSet<String>,
    /// A write failed and could not be undone: reopen before going on.
    poisoned: bool,
}

const JOURNAL: &str = "spool.journal";

impl Spool {
    /// Open (creating) the spool in `dir`. A torn last record (a crash while
    /// writing) is discarded; any other unreadable record fails closed.
    pub fn open(dir: &Path) -> anyhow::Result<Spool> {
        std::fs::create_dir_all(dir)?;
        let meta = std::fs::symlink_metadata(dir)?;
        anyhow::ensure!(meta.is_dir(), "spool directory is not a directory");
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
        let path = dir.join(JOURNAL);
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .append(true)
            .create(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&path)?;
        file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
        let mut spool = Spool {
            path,
            file: file.try_clone()?,
            length: 0,
            snapshot_length: 0,
            events: BTreeMap::new(),
            bytes: 0,
            dropped: 0,
            rejected: 0,
            next_seq: 1,
            cursors: BTreeMap::new(),
            secrets: BTreeSet::new(),
            poisoned: false,
        };
        let mut reader = std::io::BufReader::new(&mut file);
        let mut good = 0u64;
        let mut line = Vec::new();
        loop {
            line.clear();
            let n = reader.read_until(b'\n', &mut line)?;
            if n == 0 {
                break;
            }
            let complete = line.last() == Some(&b'\n');
            match serde_json::from_slice::<Record>(&line) {
                Ok(record) if complete => {
                    let snapshot = matches!(record, Record::Snapshot { .. });
                    spool.apply(record);
                    good += n as u64;
                    if snapshot {
                        spool.snapshot_length = good;
                    }
                }
                // Only the last record can be torn by a crash.
                _ if !complete => break,
                _ => anyhow::bail!("spool journal record at byte {good} is unreadable"),
            }
        }
        drop(reader);
        if good < file.metadata()?.len() {
            warn!("Discarding a torn record at the end of the CloudWatch Logs spool");
            file.set_len(good)?;
            file.sync_all()?;
        }
        spool.length = good;
        Ok(spool)
    }

    fn apply(&mut self, record: Record) {
        match record {
            Record::Pass { events, drop_through, dropped, next_seq, cursors, secrets } => {
                for event in events {
                    self.bytes += size(&event.message);
                    self.events.insert(event.seq, event);
                }
                if let Some(through) = drop_through {
                    let kept = self.events.split_off(&(through + 1));
                    for event in std::mem::replace(&mut self.events, kept).into_values() {
                        self.bytes -= size(&event.message);
                    }
                }
                self.dropped = dropped;
                self.next_seq = next_seq;
                self.cursors.extend(cursors);
                self.secrets.extend(secrets);
            }
            Record::Ack { seqs, rejected } => {
                for seq in seqs {
                    if let Some(event) = self.events.remove(&seq) {
                        self.bytes -= size(&event.message);
                    }
                }
                self.rejected = rejected;
            }
            Record::Snapshot { events, dropped, rejected, next_seq, cursors, secrets } => {
                self.events = events.into_iter().map(|e| (e.seq, e)).collect();
                self.bytes = self.events.values().map(|e| size(&e.message)).sum();
                self.dropped = dropped;
                self.rejected = rejected;
                self.next_seq = next_seq;
                self.cursors = cursors;
                self.secrets = secrets;
            }
        }
    }

    /// Append one record durably, then apply it. On failure nothing changes.
    fn commit(&mut self, record: Record) -> anyhow::Result<()> {
        anyhow::ensure!(!self.poisoned, "spool write failed earlier; restart the adapter");
        let mut bytes = serde_json::to_vec(&record)?;
        bytes.push(b'\n');
        let written = self.file.write_all(&bytes).and_then(|_| self.file.sync_data());
        if let Err(error) = written {
            // Undo a partial record so later records stay readable.
            if self.file.set_len(self.length).and_then(|_| self.file.sync_all()).is_err() {
                self.poisoned = true;
            }
            anyhow::bail!("cannot write the spool: {}", error.kind());
        }
        self.length += bytes.len() as u64;
        self.apply(record);
        if self.length > 2 * self.snapshot_length + (1 << 20) {
            if let Err(error) = self.compact() {
                warn!(%error, "Spool compaction failed; the journal keeps growing until it succeeds");
            }
        }
        Ok(())
    }

    /// Rewrite the journal as one snapshot (atomically, by rename).
    fn compact(&mut self) -> anyhow::Result<()> {
        let snapshot = Record::Snapshot {
            events: self.events.values().cloned().collect(),
            dropped: self.dropped,
            rejected: self.rejected,
            next_seq: self.next_seq,
            cursors: self.cursors.clone(),
            secrets: self.secrets.clone(),
        };
        let mut bytes = serde_json::to_vec(&snapshot)?;
        bytes.push(b'\n');
        let temporary = self.path.with_extension("tmp");
        let _ = std::fs::remove_file(&temporary);
        let mut file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&temporary)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        std::fs::rename(&temporary, &self.path)?;
        std::fs::File::open(self.path.parent().unwrap_or(Path::new(".")))?.sync_all()?;
        self.file = std::fs::OpenOptions::new().append(true).custom_flags(libc::O_NOFOLLOW).open(&self.path)?;
        self.length = bytes.len() as u64;
        self.snapshot_length = self.length;
        Ok(())
    }

    pub fn len(&self) -> usize {
        self.events.len()
    }

    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }

    #[cfg_attr(not(test), allow(dead_code))] // the declared fixture interface
    pub fn bytes(&self) -> u64 {
        self.bytes
    }
}

/// A line's events, about to be spooled.
struct Line {
    source: String,
    ms: i64,
    parts: Vec<String>,
}

/// What became of a line given to [`Exporter::log`].
#[cfg_attr(not(test), allow(dead_code))] // the declared fixture interface
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LogStatus {
    UnknownSource,
    Invalid,
    Filtered,
    Spooled { events: usize },
    /// Larger than the whole spool: dropped by itself, evicting nothing.
    Dropped { events: usize },
}

/// One `PutLogEvents` call made by [`Exporter::tick`].
#[derive(Debug, Clone, PartialEq)]
pub struct Upload {
    pub group: String,
    pub stream: String,
    pub events: Vec<(i64, String)>,
    pub outcome: Outcome,
}

/// How CloudWatch answered a `PutLogEvents` call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// Accepted; `rejected` events were refused as too old or too new.
    Ok { rejected: u64 },
    /// Refused as invalid (`InvalidParameterException`): resending cannot
    /// succeed, so the batch is counted as rejected and leaves the spool.
    Rejected(String),
    Throttled,
    ServerError(u16),
    /// The request could not be sent.
    ConnectionError,
    /// Sent, but no complete answer came back: it may have been accepted.
    ResponseLost,
    /// Any other failure (credentials, authorization, an unexpected answer):
    /// kept and retried.
    Failed(String),
}

impl Outcome {
    /// Whether the batch leaves the spool.
    fn settled(&self) -> bool {
        matches!(self, Outcome::Ok { .. } | Outcome::Rejected(_))
    }

    /// The fixture vocabulary (`interfaces/compat/cloudwatch-logs-cases.json`).
    pub fn label(&self) -> &str {
        match self {
            Outcome::Ok { .. } => "ok",
            Outcome::Rejected(_) => "rejected",
            Outcome::Throttled => "throttled",
            Outcome::ServerError(_) => "server-error",
            Outcome::ConnectionError => "connection-error",
            Outcome::ResponseLost => "response-lost",
            Outcome::Failed(_) => "failed",
        }
    }
}

/// The adapter's state machine over its spool.
pub struct Exporter {
    pub settings: Settings,
    pub spool: Spool,
    last_upload_ms: u64,
}

impl Exporter {
    /// Open the spool in `dir`. Events of sources no longer configured are
    /// dropped and counted.
    pub fn open(dir: &Path, settings: Settings) -> anyhow::Result<Exporter> {
        settings.validate()?;
        let mut spool = Spool::open(dir)?;
        let orphans: Vec<u64> = spool.events.values().filter(|e| !settings.sources.contains_key(&e.source)).map(|e| e.seq).collect();
        if !orphans.is_empty() {
            warn!(events = orphans.len(), "Dropping spooled events of sources no longer configured");
            let rejected = spool.rejected;
            let dropped = spool.dropped + orphans.len() as u64;
            spool.commit(Record::Ack { seqs: orphans, rejected })?;
            spool.commit(Record::Pass { events: Vec::new(), drop_through: None, dropped, next_seq: spool.next_seq, cursors: BTreeMap::new(), secrets: Vec::new() })?;
        }
        Ok(Exporter { settings, spool, last_upload_ms: 0 })
    }

    /// The events of an already redacted line of `source`; `None` if its
    /// level is below the source's minimum.
    fn prepare(&self, source: &str, level: Level, clean: &str, ms: i64) -> Option<Line> {
        (level >= self.settings.sources[source].minimum).then(|| Line { source: source.to_owned(), ms, parts: split(clean) })
    }

    /// Spool lines, with the cursors and confidential values they were read
    /// with, as one durable record; then drop the oldest events to the
    /// limit. Returns each line's status.
    fn spool_lines(&mut self, lines: Vec<Line>, cursors: BTreeMap<String, Cursor>, secrets: Vec<String>) -> anyhow::Result<Vec<LogStatus>> {
        let limit = self.settings.spool_limit;
        let mut next_seq = self.spool.next_seq;
        let mut dropped = self.spool.dropped;
        let mut events = Vec::new();
        let mut statuses = Vec::new();
        for line in lines {
            let count = line.parts.len();
            if line.parts.iter().map(|p| size(p)).sum::<u64>() > limit {
                dropped += count as u64;
                statuses.push(LogStatus::Dropped { events: count });
                continue;
            }
            for message in line.parts {
                events.push(Event { seq: next_seq, source: line.source.clone(), ms: line.ms, message });
                next_seq += 1;
            }
            statuses.push(LogStatus::Spooled { events: count });
        }
        // Oldest first, across what is spooled and what this pass adds.
        let mut total = self.spool.bytes + events.iter().map(|e| size(&e.message)).sum::<u64>();
        let mut drop_through = None;
        let mut oldest = self.spool.events.values().chain(events.iter());
        while total > limit {
            let Some(event) = oldest.next() else { break };
            total -= size(&event.message);
            dropped += 1;
            drop_through = Some(event.seq);
        }
        let known: Vec<String> = secrets.into_iter().filter(|s| !self.spool.secrets.contains(s)).collect();
        self.spool.commit(Record::Pass { events, drop_through, dropped, next_seq, cursors, secrets: known })?;
        Ok(statuses)
    }

    /// Spool one line given directly (the declared fixture interface): a
    /// configured source, a known level, and a time in seconds in
    /// [0, [`TIME_LIMIT_SECONDS`]).
    #[cfg_attr(not(test), allow(dead_code))] // the declared fixture interface
    pub fn log(&mut self, source: &str, level: &str, message: &str, seconds: f64) -> anyhow::Result<LogStatus> {
        if !self.settings.sources.contains_key(source) {
            return Ok(LogStatus::UnknownSource);
        }
        let Some(level) = Level::parse(level) else { return Ok(LogStatus::Invalid) };
        if !(seconds.is_finite() && seconds >= 0.0 && seconds < TIME_LIMIT_SECONDS as f64) {
            return Ok(LogStatus::Invalid);
        }
        let secrets = self.settings.secrets(&self.spool.secrets);
        let (clean, _) = logs::redact_line(message, &secrets, &mut 0);
        let Some(line) = self.prepare(source, level, &clean, (seconds * 1000.0).floor() as i64) else {
            return Ok(LogStatus::Filtered);
        };
        Ok(self.spool_lines(vec![line], BTreeMap::new(), Vec::new())?.remove(0))
    }

    /// Read new lines of every configured source into the spool.
    ///
    /// Lines are read before the confidential values are loaded, so a value
    /// a line can hold was recorded first (docs/LOGS.md). If the values
    /// cannot be loaded, nothing is spooled and no cursor moves.
    pub fn collect(&mut self, files: &SourceFiles, now_ms: i64) -> anyhow::Result<usize> {
        let mut budget = MAX_LINES_PER_PASS;
        let mut read = Vec::new();
        for name in self.settings.sources.keys() {
            if budget == 0 {
                break;
            }
            let path = files.path(name);
            let cursor = self.spool.cursors.get(name).cloned().or_else(|| files.start.get(&files.key(name)).cloned()).unwrap_or_default();
            let block = cursor.key_block;
            match logs::tail_with(&path, cursor, budget, &READER) {
                Ok((lines, next, lost)) => {
                    budget -= lines.len();
                    read.push((name.clone(), lines, next, block, lost));
                }
                Err(error) => warn!(source = %name, error = %error.kind(), "Cannot read this source's log; it is skipped"),
            }
        }
        let current = files.confidential_values()?;
        let mut known = self.spool.secrets.clone();
        known.extend(current.iter().cloned());
        anyhow::ensure!(known.len() <= MAX_KNOWN_SECRETS, "more than {MAX_KNOWN_SECRETS} confidential values to remember; collection stopped");
        let secrets = self.settings.secrets(&known);
        let mut lines = Vec::new();
        let mut cursors = BTreeMap::new();
        for (name, raw, mut next, mut block, lost) in read {
            if lost {
                lines.push(Line { source: name.clone(), ms: now_ms, parts: vec!["[suifleet: earlier lines of this log were rotated away before they could be read]".into()] });
            }
            for text in raw {
                if text.is_empty() {
                    continue;
                }
                // Every line is redacted, filtered or not, to follow key blocks.
                let (clean, _) = logs::redact_line(&text, &secrets, &mut block);
                let (level, ms) = classify(&text, now_ms);
                if let Some(line) = level.and_then(|level| self.prepare(&name, level, &clean, ms)) {
                    lines.push(line);
                }
            }
            next.key_block = block;
            if self.spool.cursors.get(&name) != Some(&next) {
                cursors.insert(name, next);
            }
        }
        let count = lines.len();
        if count > 0 || !cursors.is_empty() || current.iter().any(|v| !self.spool.secrets.contains(v)) {
            self.spool_lines(lines, cursors, current)?;
        }
        Ok(count)
    }

    pub fn due(&self, now_ms: u64) -> bool {
        !self.spool.is_empty() && now_ms.saturating_sub(self.last_upload_ms) >= self.settings.interval_ms()
    }

    /// Upload when due. Each source's events go out in time order (equal
    /// times in arrival order), in batches within the `PutLogEvents` limits
    /// and one UTC day. A source stops at its first unsettled batch.
    pub async fn tick(&mut self, now_ms: u64, uploader: &mut Uploader) -> anyhow::Result<Vec<Upload>> {
        if !self.due(now_ms) {
            return Ok(Vec::new());
        }
        self.last_upload_ms = now_ms;
        let mut uploads = Vec::new();
        let sources: BTreeSet<String> = self.spool.events.values().map(|e| e.source.clone()).collect();
        for source in sources {
            let mut events: Vec<&Event> = self.spool.events.values().filter(|e| e.source == source).collect();
            events.sort_by_key(|e| (e.ms, e.seq));
            let group = self.settings.group(&source);
            let mut settled = Vec::new();
            for (day, batch) in batches(&events) {
                let stream = self.settings.stream(&day);
                let messages: Vec<(i64, String)> = batch.iter().map(|e| (e.ms, e.message.clone())).collect();
                let outcome = uploader.put(&group, &stream, &messages).await;
                let done = outcome.settled();
                if done {
                    settled.push((batch.iter().map(|e| e.seq).collect::<Vec<_>>(), outcome.clone()));
                }
                uploads.push(Upload { group: group.clone(), stream, events: messages, outcome });
                if !done {
                    break;
                }
            }
            for (seqs, outcome) in settled {
                let rejected = self.spool.rejected
                    + match &outcome {
                        Outcome::Ok { rejected } => *rejected,
                        _ => seqs.len() as u64,
                    };
                if let Outcome::Rejected(reason) = &outcome {
                    warn!(source = %source, events = seqs.len(), %reason, "CloudWatch Logs rejected a batch; it is not resent");
                }
                // If this fails, the batch stays and is sent again (at least once).
                self.spool.commit(Record::Ack { seqs, rejected })?;
            }
        }
        Ok(uploads)
    }
}

/// Batches of time-ordered events: at most [`MAX_BATCH_EVENTS`] events and
/// [`MAX_BATCH_BYTES`], all of one UTC day.
fn batches<'a>(events: &[&'a Event]) -> Vec<(String, Vec<&'a Event>)> {
    let mut out = Vec::new();
    let mut batch: Vec<&Event> = Vec::new();
    let mut bytes = 0u64;
    let mut current = String::new();
    for event in events {
        let event_day = day(event.ms);
        let event_size = size(&event.message);
        if !batch.is_empty() && (event_day != current || batch.len() == MAX_BATCH_EVENTS || bytes + event_size > MAX_BATCH_BYTES as u64) {
            out.push((std::mem::take(&mut current), std::mem::take(&mut batch)));
            bytes = 0;
        }
        batch.push(event);
        bytes += event_size;
        current = event_day;
    }
    if !batch.is_empty() {
        out.push((current, batch));
    }
    out
}

// --- Sources -----------------------------------------------------------------

/// Where the configured sources' logs are (docs/CLOUDWATCH_LOGS.md): the
/// daemon's `logs/daemon.log` for [`SYSTEM`], and
/// `supervised/<name>/component.log` for a component, each with its `.1`.
pub struct SourceFiles {
    pub state_dir: PathBuf,
    /// Redact the daemon's confidential values (`logs/confidential.json`).
    pub confidential: bool,
    /// Where each existing component log ended when the daemon first
    /// recorded confidential values; reading starts there.
    pub start: BTreeMap<String, Cursor>,
}

impl SourceFiles {
    fn key(&self, name: &str) -> String {
        if name == SYSTEM {
            logs::DAEMON_SOURCE.to_owned()
        } else {
            format!("component:{name}")
        }
    }

    fn path(&self, name: &str) -> PathBuf {
        if name == SYSTEM {
            self.state_dir.join("logs/daemon.log")
        } else {
            // Settings::validate admits only plain path segments.
            self.state_dir.join("supervised").join(name).join("component.log")
        }
    }

    /// The daemon's current confidential values. Fails closed: if they are
    /// required but cannot be read, nothing may be collected.
    fn confidential_values(&self) -> anyhow::Result<Vec<String>> {
        if !self.confidential {
            return Ok(Vec::new());
        }
        let (values, _) = logs::read_retained(&self.state_dir)?;
        Ok(values)
    }
}

// --- Configuration -------------------------------------------------------------

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct SystemLogs {
    #[serde(default)]
    upload_to_cloud_watch: bool,
    #[serde(default)]
    minimum_log_level: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct ComponentLogs {
    #[serde(default)]
    minimum_log_level: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct UploaderConfiguration {
    #[serde(default)]
    system_logs_configuration: Option<SystemLogs>,
    #[serde(default)]
    component_logs_configuration_map: BTreeMap<String, ComponentLogs>,
}

/// The adapter's JSON configuration: LogManager 2.3.14 key names for what
/// it supports, unknown (and unsupported LogManager) keys rejected.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct FileConfig {
    region: String,
    thing_name: String,
    state_dir: PathBuf,
    spool_dir: PathBuf,
    #[serde(default)]
    endpoint: Option<String>,
    #[serde(default = "default_true")]
    redact_confidential_values: bool,
    #[serde(default)]
    redact: Vec<String>,
    #[serde(default = "default_interval")]
    periodic_upload_interval_sec: f64,
    #[serde(default = "default_read")]
    read_interval_sec: u64,
    #[serde(default = "default_spool_limit")]
    spool_limit_bytes: u64,
    logs_uploader_configuration: UploaderConfiguration,
}

fn default_true() -> bool {
    true
}
fn default_interval() -> f64 {
    DEFAULT_INTERVAL_SECONDS
}
fn default_read() -> u64 {
    DEFAULT_READ_SECONDS
}
fn default_spool_limit() -> u64 {
    DEFAULT_SPOOL_LIMIT
}

fn level(text: &Option<String>) -> anyhow::Result<Level> {
    match text {
        None => Ok(Level::Info),
        Some(text) => Level::parse(text).ok_or_else(|| anyhow::anyhow!("minimumLogLevel must be DEBUG, INFO, WARN or ERROR")),
    }
}

impl FileConfig {
    pub fn load(path: &Path) -> anyhow::Result<FileConfig> {
        let bytes = std::fs::read(path).map_err(|error| anyhow::anyhow!("cannot read {}: {}", path.display(), error.kind()))?;
        let config: FileConfig = serde_json::from_slice(&bytes).map_err(|error| anyhow::anyhow!("invalid CloudWatch Logs configuration: {error}"))?;
        config.settings()?;
        anyhow::ensure!(config.state_dir.is_absolute() && config.spool_dir.is_absolute(), "stateDir and spoolDir must be absolute");
        anyhow::ensure!(!config.spool_dir.starts_with(&config.state_dir), "spoolDir must be outside stateDir");
        anyhow::ensure!((1..=3600).contains(&config.read_interval_sec), "readIntervalSec must be 1..=3600");
        config.endpoint()?;
        Ok(config)
    }

    pub fn settings(&self) -> anyhow::Result<Settings> {
        let mut sources = BTreeMap::new();
        let uploader = &self.logs_uploader_configuration;
        if let Some(system) = uploader.system_logs_configuration.as_ref() {
            let minimum = level(&system.minimum_log_level)?;
            if system.upload_to_cloud_watch {
                sources.insert(SYSTEM.to_owned(), Source { component_type: ComponentType::GreengrassSystemComponent, minimum });
            }
        }
        for (name, component) in &uploader.component_logs_configuration_map {
            anyhow::ensure!(name != SYSTEM, "componentLogsConfigurationMap cannot name {SYSTEM}; use systemLogsConfiguration");
            sources.insert(name.clone(), Source { component_type: ComponentType::UserComponent, minimum: level(&component.minimum_log_level)? });
        }
        let settings = Settings {
            region: self.region.clone(),
            thing: self.thing_name.clone(),
            interval_seconds: self.periodic_upload_interval_sec,
            spool_limit: self.spool_limit_bytes,
            redact: self.redact.clone(),
            sources,
        };
        settings.validate()?;
        Ok(settings)
    }

    /// `https://logs.{region}.amazonaws.com/` unless configured; plain HTTP
    /// only to a loopback address (a local emulator).
    pub fn endpoint(&self) -> anyhow::Result<reqwest::Url> {
        let text = self.endpoint.clone().unwrap_or_else(|| format!("https://logs.{}.amazonaws.com/", self.region));
        let url = reqwest::Url::parse(&text)?;
        anyhow::ensure!(url.path() == "/" && url.query().is_none() && url.username().is_empty() && url.password().is_none(), "endpoint must be a bare origin");
        match url.scheme() {
            "https" => Ok(url),
            "http" if loopback(&url) => Ok(url),
            _ => anyhow::bail!("endpoint must be https (or http to a loopback address)"),
        }
    }
}

fn loopback(url: &reqwest::Url) -> bool {
    let host = url.host_str().unwrap_or_default();
    host == "localhost" || host.trim_start_matches('[').trim_end_matches(']').parse::<std::net::IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

// --- Credentials ---------------------------------------------------------------

/// AWS credentials from the container-credential endpoint (SF-057). Kept in
/// memory only; never printed.
#[derive(Clone)]
pub struct Credentials {
    access_key_id: String,
    secret_access_key: String,
    token: String,
    expires_ms: i64,
}

impl std::fmt::Debug for Credentials {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Credentials([REDACTED])")
    }
}

/// Where scoped credentials come from: `AWS_CONTAINER_CREDENTIALS_FULL_URI`
/// and `AWS_CONTAINER_AUTHORIZATION_TOKEN`, as the SF-057 wrapper sets them
/// for a granted component. No other credential source is used.
pub struct CredentialSource {
    uri: reqwest::Url,
    token: String,
}

impl std::fmt::Debug for CredentialSource {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CredentialSource").field("uri", &self.uri.as_str()).finish_non_exhaustive()
    }
}

/// Refresh credentials this long before they expire.
const REFRESH_MS: i64 = 300_000;

impl CredentialSource {
    pub fn from_env() -> anyhow::Result<CredentialSource> {
        let uri = std::env::var("AWS_CONTAINER_CREDENTIALS_FULL_URI")
            .map_err(|_| anyhow::anyhow!("AWS_CONTAINER_CREDENTIALS_FULL_URI is not set: run the adapter through the SF-057 credential wrapper with a grant"))?;
        let token = std::env::var("AWS_CONTAINER_AUTHORIZATION_TOKEN").map_err(|_| anyhow::anyhow!("AWS_CONTAINER_AUTHORIZATION_TOKEN is not set"))?;
        Self::new(&uri, token)
    }

    pub fn new(uri: &str, token: String) -> anyhow::Result<CredentialSource> {
        let uri = reqwest::Url::parse(uri).map_err(|_| anyhow::anyhow!("the credential URI is not a URL"))?;
        anyhow::ensure!(uri.scheme() == "https" || (uri.scheme() == "http" && loopback(&uri)), "the credential URI must be https or http to a loopback address");
        anyhow::ensure!(!token.is_empty() && !token.contains(['\r', '\n']), "the credential token is empty or malformed");
        Ok(CredentialSource { uri, token })
    }

    async fn fetch(&self, http: &reqwest::Client) -> Result<Credentials, String> {
        #[derive(Deserialize)]
        #[serde(rename_all = "PascalCase")]
        struct Body {
            access_key_id: String,
            secret_access_key: String,
            token: String,
            expiration: String,
        }
        let response = http.get(self.uri.clone()).header("Authorization", &self.token).send().await.map_err(|_| "credential endpoint unreachable".to_owned())?;
        if !response.status().is_success() {
            return Err(format!("credential endpoint answered {}", response.status().as_u16()));
        }
        let bytes = response.bytes().await.map_err(|_| "credential response lost".to_owned())?;
        let body: Body = serde_json::from_slice(&bytes).map_err(|_| "credential response malformed".to_owned())?;
        let expires_ms = leading_time(&body.expiration).filter(|(_, rest)| rest.is_empty()).map(|(ms, _)| ms).ok_or("credential expiration malformed")?;
        if body.access_key_id.is_empty() || body.secret_access_key.is_empty() || body.token.is_empty() {
            return Err("credential response incomplete".into());
        }
        Ok(Credentials { access_key_id: body.access_key_id, secret_access_key: body.secret_access_key, token: body.token, expires_ms })
    }
}

// --- SigV4 ---------------------------------------------------------------------

fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

/// HMAC-SHA256 (RFC 2104).
fn hmac(key: &[u8], message: &[u8]) -> [u8; 32] {
    let mut block = [0u8; 64];
    if key.len() > 64 {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(block.map(|b| b ^ 0x36));
    inner.update(message);
    let mut outer = Sha256::new();
    outer.update(block.map(|b| b ^ 0x5c));
    outer.update(inner.finalize());
    outer.finalize().into()
}

/// The SigV4 signing key for a date (`yyyymmdd`), region and service.
fn signing_key(secret: &str, date: &str, region: &str, service: &str) -> [u8; 32] {
    let key = hmac(format!("AWS4{secret}").as_bytes(), date.as_bytes());
    let key = hmac(&key, region.as_bytes());
    let key = hmac(&key, service.as_bytes());
    hmac(&key, b"aws4_request")
}

/// The SigV4 string to sign for a canonical request.
fn string_to_sign(amz_date: &str, scope: &str, canonical_request: &str) -> String {
    format!("AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{}", sha256_hex(canonical_request.as_bytes()))
}

/// `yyyymmddTHHMMSSZ` of a time.
fn amz_date(ms: i64) -> String {
    let (y, m, d) = civil(ms.div_euclid(DAY_MS));
    let seconds = ms.rem_euclid(DAY_MS) / 1000;
    format!("{y:04}{m:02}{d:02}T{:02}{:02}{:02}Z", seconds / 3600, seconds / 60 % 60, seconds % 60)
}

/// Headers of a signed CloudWatch Logs JSON request.
fn sign(credentials: &Credentials, region: &str, host: &str, target: &str, body: &[u8], now_ms: i64) -> Vec<(&'static str, String)> {
    const CONTENT_TYPE: &str = "application/x-amz-json-1.1";
    let date = amz_date(now_ms);
    let scope = format!("{}/{region}/logs/aws4_request", &date[..8]);
    let signed = "content-type;host;x-amz-date;x-amz-security-token;x-amz-target";
    let canonical = format!(
        "POST\n/\n\ncontent-type:{CONTENT_TYPE}\nhost:{host}\nx-amz-date:{date}\nx-amz-security-token:{}\nx-amz-target:{target}\n\n{signed}\n{}",
        credentials.token,
        sha256_hex(body)
    );
    let key = signing_key(&credentials.secret_access_key, &date[..8], region, "logs");
    let signature = hex::encode(hmac(&key, string_to_sign(&date, &scope, &canonical).as_bytes()));
    vec![
        ("content-type", CONTENT_TYPE.to_owned()),
        ("host", host.to_owned()),
        ("x-amz-date", date),
        ("x-amz-security-token", credentials.token.clone()),
        ("x-amz-target", target.to_owned()),
        (
            "authorization",
            format!("AWS4-HMAC-SHA256 Credential={}/{scope}, SignedHeaders={signed}, Signature={signature}", credentials.access_key_id),
        ),
    ]
}

// --- Uploads -------------------------------------------------------------------

/// Signed `PutLogEvents` calls (creating the group and stream on
/// `ResourceNotFoundException`), with cached scoped credentials.
pub struct Uploader {
    http: reqwest::Client,
    endpoint: reqwest::Url,
    host: String,
    region: String,
    source: CredentialSource,
    cached: Option<Credentials>,
}

enum Answer {
    Status(u16, String, Vec<u8>),
    NotSent,
    Lost,
}

impl Uploader {
    pub fn new(endpoint: reqwest::Url, region: &str, source: CredentialSource) -> anyhow::Result<Uploader> {
        let http = reqwest::Client::builder()
            .http1_only()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(30))
            .build()?;
        let host = match endpoint.port() {
            Some(port) => format!("{}:{port}", endpoint.host_str().unwrap_or_default()),
            None => endpoint.host_str().unwrap_or_default().to_owned(),
        };
        anyhow::ensure!(!host.is_empty(), "endpoint has no host");
        Ok(Uploader { http, endpoint, host, region: region.to_owned(), source, cached: None })
    }

    async fn credentials(&mut self) -> Result<Credentials, String> {
        let now = crate::telemetry::now_ms() as i64;
        if let Some(cached) = &self.cached {
            if cached.expires_ms - now > REFRESH_MS {
                return Ok(cached.clone());
            }
        }
        match self.source.fetch(&self.http).await {
            Ok(fresh) if fresh.expires_ms > now => {
                self.cached = Some(fresh.clone());
                Ok(fresh)
            }
            Ok(_) => Err("credentials already expired".into()),
            // Unexpired cached credentials stay usable while a refresh fails.
            Err(error) => self.cached.clone().filter(|c| c.expires_ms > now).ok_or(error),
        }
    }

    async fn call(&mut self, target: &str, body: &serde_json::Value) -> Result<Answer, String> {
        let credentials = self.credentials().await?;
        let bytes = serde_json::to_vec(body).map_err(|e| e.to_string())?;
        let mut request = self.http.post(self.endpoint.clone());
        // The client sends the same Host itself.
        for (name, value) in sign(&credentials, &self.region, &self.host, target, &bytes, crate::telemetry::now_ms() as i64) {
            if name != "host" {
                request = request.header(name, value);
            }
        }
        let response = match request.body(bytes).send().await {
            Ok(response) => response,
            Err(error) if error.is_connect() => return Ok(Answer::NotSent),
            Err(_) => return Ok(Answer::Lost),
        };
        let status = response.status().as_u16();
        let Ok(body) = response.bytes().await else { return Ok(Answer::Lost) };
        let kind = serde_json::from_slice::<serde_json::Value>(&body)
            .ok()
            .and_then(|v| v.get("__type").and_then(|t| t.as_str()).map(|t| t.rsplit('#').next().unwrap_or(t).to_owned()))
            .unwrap_or_default();
        Ok(Answer::Status(status, kind, body.to_vec()))
    }

    /// One `PutLogEvents` call; events are (milliseconds, message).
    pub async fn put(&mut self, group: &str, stream: &str, events: &[(i64, String)]) -> Outcome {
        let body = serde_json::json!({
            "logGroupName": group,
            "logStreamName": stream,
            "logEvents": events.iter().map(|(ms, message)| serde_json::json!({"timestamp": ms, "message": message})).collect::<Vec<_>>(),
        });
        let mut created = false;
        loop {
            let answer = match self.call("Logs_20140328.PutLogEvents", &body).await {
                Ok(answer) => answer,
                Err(reason) => return Outcome::Failed(reason),
            };
            let (status, kind, response) = match answer {
                Answer::NotSent => return Outcome::ConnectionError,
                Answer::Lost => return Outcome::ResponseLost,
                Answer::Status(status, kind, response) => (status, kind, response),
            };
            match (status, kind.as_str()) {
                (200..=299, _) => return Outcome::Ok { rejected: rejected_events(&response, events.len()) },
                (400, "ResourceNotFoundException") if !created => {
                    created = true;
                    if let Err(outcome) = self.create(group, stream).await {
                        return outcome;
                    }
                }
                (400, "ThrottlingException") | (429, _) => return Outcome::Throttled,
                (400, "InvalidParameterException" | "DataAlreadyAcceptedException" | "SerializationException") => return Outcome::Rejected(kind),
                (500..=599, _) => return Outcome::ServerError(status),
                (_, "ExpiredTokenException" | "UnrecognizedClientException" | "InvalidSignatureException") => {
                    self.cached = None;
                    return Outcome::Failed(format!("{status} {kind}"));
                }
                _ => return Outcome::Failed(format!("{status} {kind}")),
            }
        }
    }

    async fn create(&mut self, group: &str, stream: &str) -> Result<(), Outcome> {
        for (target, body) in [
            ("Logs_20140328.CreateLogGroup", serde_json::json!({"logGroupName": group})),
            ("Logs_20140328.CreateLogStream", serde_json::json!({"logGroupName": group, "logStreamName": stream})),
        ] {
            match self.call(target, &body).await {
                Ok(Answer::Status(200..=299, _, _)) => {}
                Ok(Answer::Status(400, kind, _)) if kind == "ResourceAlreadyExistsException" => {}
                Ok(Answer::Status(400, kind, _)) if kind == "ThrottlingException" => return Err(Outcome::Throttled),
                Ok(Answer::Status(status @ 500..=599, _, _)) => return Err(Outcome::ServerError(status)),
                Ok(Answer::Status(status, kind, _)) => return Err(Outcome::Failed(format!("{target}: {status} {kind}"))),
                Ok(Answer::NotSent) => return Err(Outcome::ConnectionError),
                Ok(Answer::Lost) => return Err(Outcome::ResponseLost),
                Err(reason) => return Err(Outcome::Failed(reason)),
            }
        }
        Ok(())
    }
}

/// Events an accepted `PutLogEvents` call refused (`rejectedLogEventsInfo`).
fn rejected_events(response: &[u8], count: usize) -> u64 {
    let Ok(body) = serde_json::from_slice::<serde_json::Value>(response) else { return 0 };
    let Some(info) = body.get("rejectedLogEventsInfo") else { return 0 };
    let index = |name: &str| info.get(name).and_then(|v| v.as_u64()).map(|v| v.min(count as u64));
    let old = index("tooOldLogEventEndIndex").into_iter().chain(index("expiredLogEventEndIndex")).max();
    let new_from = index("tooNewLogEventStartIndex").unwrap_or(count as u64);
    let old_count = old.map_or(0, |end| end + 1).min(new_from);
    old_count + (count as u64 - new_from)
}

// --- The adapter process -------------------------------------------------------

/// `suifleet --cloudwatch-logs CONFIG`: run until SIGTERM or SIGINT.
pub async fn run(config_path: &Path) -> anyhow::Result<()> {
    // SAFETY: plain getters.
    anyhow::ensure!(unsafe { libc::geteuid() } != 0, "the CloudWatch Logs adapter must not run as root; give it its own user");
    // Keep the credentials (in memory) from other processes of this user.
    // SAFETY: plain prctl on this process.
    anyhow::ensure!(unsafe { libc::prctl(libc::PR_SET_DUMPABLE, 0, 0, 0, 0) } == 0, "cannot make the adapter non-dumpable");
    let config = FileConfig::load(config_path)?;
    let settings = config.settings()?;
    let source = CredentialSource::from_env()?;
    let mut uploader = Uploader::new(config.endpoint()?, &config.region, source)?;
    let start = if config.redact_confidential_values { logs::read_retained(&config.state_dir)?.1 } else { BTreeMap::new() };
    let files = SourceFiles { state_dir: config.state_dir.clone(), confidential: config.redact_confidential_values, start };
    let mut exporter = Exporter::open(&config.spool_dir, settings)?;
    info!(
        sources = exporter.settings.sources.len(),
        spooled = exporter.spool.len(),
        interval = exporter.settings.interval_seconds,
        "Exporting logs to CloudWatch Logs"
    );
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let started = std::time::Instant::now();
    loop {
        if let Err(error) = exporter.collect(&files, crate::telemetry::now_ms() as i64) {
            warn!(%error, "Log collection failed; nothing was spooled and it is retried");
        }
        let now = started.elapsed().as_millis() as u64;
        if exporter.due(now) {
            match exporter.tick(now, &mut uploader).await {
                Ok(uploads) => {
                    let failed: Vec<&str> = uploads.iter().filter(|u| !u.outcome.settled()).map(|u| u.outcome.label()).collect();
                    info!(
                        calls = uploads.len(),
                        unsettled = ?failed,
                        spooled = exporter.spool.len(),
                        dropped = exporter.spool.dropped,
                        rejected = exporter.spool.rejected,
                        "CloudWatch Logs upload"
                    );
                }
                Err(error) => warn!(%error, "CloudWatch Logs upload failed"),
            }
        }
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(config.read_interval_sec)) => {}
            _ = terminate.recv() => break,
            _ = tokio::signal::ctrl_c() => break,
        }
    }
    info!("CloudWatch Logs adapter stopped; the spool is kept");
    Ok(())
}

#[cfg(test)]
#[path = "cloudwatch_logs_tests.rs"]
mod tests;
