//! Local device shadows and conflict-aware cloud synchronization (SF-033;
//! docs/SHADOWS.md), with the document semantics fixed in
//! `interfaces/compat/shadow-cases.json`.
//!
//! A shadow is a named `{desired?, reported?}` JSON document with a version.
//! Updates merge recursively: `null` deletes a key (or a whole section),
//! arrays are replaced whole and objects left empty are removed. A supplied
//! version must equal the current one (`conflict` otherwise), and every
//! accepted change adds one. Deletion keeps the version, so a recreated
//! shadow continues the sequence. Each shadow is one synced file under
//! `<state_dir>/shadows/`, replaced atomically, so it survives crashes.
//!
//! Components reach shadows through IPC with AWS-shaped `shadow_policies`.
//! Changes are announced on AWS-shaped local pub/sub topics
//! (`$aws/things/<thing>/shadow[/name/<name>]/update/delta`, ...), when
//! `[pubsub]` is enabled.
//!
//! An optional sync adapter reconciles selected shadows with an HTTP(S)
//! service, or with AWS IoT Device Shadow over the AWS IoT Core connection
//! (`iotcore.rs`, SF-061), through compare-and-swap on the cloud version. Each side's
//! changes since the last agreed document (the base) are merged three ways;
//! a key both sides changed differently is a conflict, settled per section
//! by the configured owner (by default the cloud owns `desired` and the
//! device `reported`). Requests carry a device-signed manifest like stream
//! exports (SF-034).
use crate::identity::TxSigner;
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tracing::{info, warn};

pub const MAX_STATE_BYTES: usize = 8192;
pub const MAX_DEPTH: usize = 8;
pub const MAX_CLIENT_TOKEN: usize = 64;
pub const MAX_NAME: usize = 64;
pub const MAX_SHADOWS: usize = 128;
pub const SYNC_SCHEMA: u32 = 1;
pub const MANIFEST_KIND: &str = "shadow-sync";
pub const MAX_MANIFEST_BYTES: usize = 2048;
const MAX_RESPONSE_BYTES: usize = 65536;
const SECTIONS: [&str; 2] = ["desired", "reported"];
/// The sender of shadow notifications in local pub/sub.
pub const SENDER: &str = "@shadows";

#[derive(Debug, Clone)]
pub struct ShadowError {
    pub code: &'static str,
    pub detail: String,
}
impl std::fmt::Display for ShadowError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.detail)
    }
}
impl std::error::Error for ShadowError {}
fn fail<T>(code: &'static str, detail: impl Into<String>) -> anyhow::Result<T> {
    Err(ShadowError { code, detail: detail.into() }.into())
}

/// `""` is the classic shadow; named shadows are 1-64 of `[A-Za-z0-9:_-]`.
pub fn valid_name(name: &str) -> bool {
    name.len() <= MAX_NAME && name.bytes().all(|b| b.is_ascii_alphanumeric() || b":_-".contains(&b))
}

/// An AWS IoT thing name: 1-128 of `[A-Za-z0-9:_-]`.
pub fn valid_thing(thing: &str) -> bool {
    !thing.is_empty() && thing.len() <= 128 && thing.bytes().all(|b| b.is_ascii_alphanumeric() || b":_-".contains(&b))
}

/// The AWS resource naming a shadow, used by `shadow_policies`.
pub fn resource(thing: &str, name: &str) -> String {
    if name.is_empty() {
        format!("$aws/things/{thing}/shadow")
    } else {
        format!("$aws/things/{thing}/shadow/name/{name}")
    }
}

/// Nesting levels: the section object is level 1, every object or array adds one.
fn depth(value: &Value) -> usize {
    match value {
        Value::Object(map) => 1 + map.values().map(depth).max().unwrap_or(0),
        Value::Array(items) => 1 + items.iter().map(depth).max().unwrap_or(0),
        _ => 0,
    }
}

/// A `null` anywhere under an array, including in objects nested in it:
/// arrays replace whole, so it would be stored and fail `check_document`.
fn null_in_array(value: &Value) -> bool {
    match value {
        Value::Array(items) => items.iter().any(contains_null),
        Value::Object(map) => map.values().any(null_in_array),
        _ => false,
    }
}

fn contains_null(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Array(items) => items.iter().any(contains_null),
        Value::Object(map) => map.values().any(contains_null),
        _ => false,
    }
}

/// JSON value equality: booleans never equal numbers, and 1 equals 1.0.
pub fn json_equal(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => match (x.as_u64(), y.as_u64(), x.as_i64(), y.as_i64()) {
            (Some(x), Some(y), _, _) => x == y,
            (_, _, Some(x), Some(y)) => x == y,
            _ => x.as_f64() == y.as_f64(),
        },
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).is_some_and(|w| json_equal(v, w)))
        }
        (Value::Array(x), Value::Array(y)) => x.len() == y.len() && x.iter().zip(y).all(|(v, w)| json_equal(v, w)),
        _ => a == b,
    }
}

fn same(a: Option<&Value>, b: Option<&Value>) -> bool {
    match (a, b) {
        (None, None) => true,
        (Some(a), Some(b)) => json_equal(a, b),
        _ => false,
    }
}

fn same_document(a: Option<&Map<String, Value>>, b: Option<&Map<String, Value>>) -> bool {
    match (a, b) {
        (None, None) => true,
        (Some(a), Some(b)) => json_equal(&Value::Object(a.clone()), &Value::Object(b.clone())),
        _ => false,
    }
}

/// Recursive merge: `null` deletes, objects merge, anything else replaces;
/// objects left empty are removed.
fn merge(target: &mut Map<String, Value>, patch: &Map<String, Value>) {
    for (key, value) in patch {
        match value {
            Value::Null => {
                target.remove(key);
            }
            Value::Object(patch) => {
                let mut nested = match target.remove(key) {
                    Some(Value::Object(map)) => map,
                    _ => Map::new(),
                };
                merge(&mut nested, patch);
                if !nested.is_empty() {
                    target.insert(key.clone(), Value::Object(nested));
                }
            }
            other => {
                target.insert(key.clone(), other.clone());
            }
        }
    }
}

/// The desired keys that differ from reported, with their path from the
/// root; keys only in reported are ignored.
pub fn delta(desired: Option<&Map<String, Value>>, reported: Option<&Map<String, Value>>) -> Map<String, Value> {
    let mut out = Map::new();
    for (key, value) in desired.into_iter().flatten() {
        let other = reported.and_then(|r| r.get(key));
        match (value, other) {
            (Value::Object(d), Some(Value::Object(r))) => {
                let nested = delta(Some(d), Some(r));
                if !nested.is_empty() {
                    out.insert(key.clone(), Value::Object(nested));
                }
            }
            _ if other.is_some_and(|o| json_equal(value, o)) => {}
            _ => {
                out.insert(key.clone(), value.clone());
            }
        }
    }
    out
}

fn section<'a>(document: &'a Map<String, Value>, name: &str) -> Option<&'a Map<String, Value>> {
    document.get(name).and_then(Value::as_object)
}

/// The `state` of a document as returned to clients: its sections and a
/// non-empty delta.
fn state_view(document: &Map<String, Value>) -> Value {
    let mut view = Map::new();
    for name in SECTIONS {
        if let Some(value) = document.get(name) {
            view.insert(name.into(), value.clone());
        }
    }
    let delta = delta(section(document, "desired"), section(document, "reported"));
    if !delta.is_empty() {
        view.insert("delta".into(), Value::Object(delta));
    }
    Value::Object(view)
}

/// A stored or received document: only object sections, no nulls, within
/// the depth and size limits.
pub fn check_document(document: &Map<String, Value>) -> anyhow::Result<()> {
    anyhow::ensure!(document.iter().all(|(k, v)| SECTIONS.contains(&k.as_str()) && v.is_object()),
        "a shadow document has only desired/reported objects");
    anyhow::ensure!(document.values().all(|v| depth(v) <= MAX_DEPTH && !contains_null(v)), "shadow document too deep or contains null");
    anyhow::ensure!(serde_json::to_vec(document)?.len() <= MAX_STATE_BYTES, "shadow document exceeds {MAX_STATE_BYTES} bytes");
    Ok(())
}

/// What the cloud and this device last agreed on.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SyncState {
    /// The agreed document; None when it was absent on both sides.
    pub base: Option<Map<String, Value>>,
    /// The cloud's version of that document.
    pub cloud_version: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    name: String,
    version: u64,
    /// `{desired?, reported?}`; None after deletion, which keeps the version.
    document: Option<Map<String, Value>>,
    updated_ms: u64,
    #[serde(default)]
    sync: Option<SyncState>,
}

pub struct Store {
    dir: PathBuf,
    max_shadows: usize,
    thing: String,
    records: BTreeMap<String, Record>,
    broker: Option<Arc<Mutex<crate::pubsub::Broker>>>,
    published: u64,
    refused: u64,
    sync_status: BTreeMap<String, Value>,
    /// Woken by every local change, so the sync adapter pushes promptly.
    pub changed: Arc<tokio::sync::Notify>,
}

impl Store {
    /// Open the shadows under `dir`. A file that cannot be read stops the
    /// daemon rather than silently losing a document or its version.
    pub fn set_broker(&mut self, broker: Option<Arc<Mutex<crate::pubsub::Broker>>>) { self.broker = broker; }

    pub fn open(dir: &Path, max_shadows: usize, thing: &str, broker: Option<Arc<Mutex<crate::pubsub::Broker>>>) -> anyhow::Result<Self> {
        std::fs::create_dir_all(dir)?;
        let mut records = BTreeMap::new();
        for entry in std::fs::read_dir(dir)? {
            let path = entry?.path();
            let file = path.file_name().and_then(|f| f.to_str()).unwrap_or_default().to_owned();
            if file.ends_with(".tmp") {
                // An interrupted replacement; the previous file is intact.
                std::fs::remove_file(&path)?;
                continue;
            }
            if !(file.starts_with("s-") && file.ends_with(".json")) {
                continue;
            }
            let record: Record = serde_json::from_slice(&std::fs::read(&path)?)
                .map_err(|e| anyhow::anyhow!("shadow file {} is unreadable ({e}); inspect or remove it", path.display()))?;
            anyhow::ensure!(valid_name(&record.name) && file == file_name(&record.name)
                && record.document.as_ref().is_none_or(|d| check_document(d).is_ok()),
                "shadow file {} is invalid; inspect or remove it", path.display());
            records.insert(record.name.clone(), record);
        }
        let live = records.values().filter(|r| r.document.is_some()).count();
        anyhow::ensure!(live <= max_shadows,
            "{live} stored shadows exceed shadows.max_shadows ({max_shadows}); raise it or delete shadows under the old limit");
        Ok(Self { dir: dir.to_owned(), max_shadows, thing: thing.to_owned(), records, broker, published: 0, refused: 0,
            sync_status: BTreeMap::new(), changed: Arc::new(tokio::sync::Notify::new()) })
    }

    pub fn thing(&self) -> &str {
        &self.thing
    }

    fn live(&self) -> usize {
        self.records.values().filter(|r| r.document.is_some()).count()
    }

    fn save(&self, record: &Record) -> anyhow::Result<()> {
        crate::pubsub::write_synced(&self.dir.join(file_name(&record.name)), &serde_json::to_vec(record)?)
            .map_err(|e| ShadowError { code: "unavailable", detail: format!("cannot store shadow: {e}") }.into())
    }

    /// Store a new document (or deletion) as the next version, then announce it.
    fn commit(&mut self, name: &str, document: Option<Map<String, Value>>, sync: Option<SyncState>, now: u64) -> anyhow::Result<u64> {
        let previous = self.records.get(name).cloned();
        let creating = document.is_some() && previous.as_ref().is_none_or(|r| r.document.is_none());
        if creating {
            if self.live() >= self.max_shadows {
                return fail("quota", format!("shadows.max_shadows ({}) reached", self.max_shadows));
            }
            // Deleted shadows keep their version; bound how many are kept.
            if previous.is_none() && self.records.len() >= 2 * self.max_shadows {
                return fail("quota", "too many deleted shadows are retained");
            }
        }
        let record = Record {
            name: name.to_owned(),
            version: previous.as_ref().map_or(0, |r| r.version) + 1,
            document,
            updated_ms: now,
            sync: sync.or_else(|| previous.as_ref().and_then(|r| r.sync.clone())),
        };
        self.save(&record)?;
        let version = record.version;
        self.records.insert(name.to_owned(), record.clone());
        self.notify(name, previous.as_ref(), &record, now);
        self.changed.notify_one();
        Ok(version)
    }

    fn topic(&self, name: &str, suffix: &str) -> String {
        format!("{}/{suffix}", resource(&self.thing, name))
    }

    /// Announce a change on local pub/sub. The change is already stored; a
    /// refused notification is counted, and readers recover by version.
    fn notify(&mut self, name: &str, previous: Option<&Record>, current: &Record, now: u64) {
        let Some(broker) = self.broker.clone() else { return };
        let mut publications = Vec::new();
        let bounded = |payload: Value, fallback: Value| {
            if serde_json::to_vec(&payload).map_or(true, |b| b.len() > crate::pubsub::MAX_PAYLOAD) { fallback } else { payload }
        };
        match &current.document {
            None => publications.push((self.topic(name, "delete/accepted"), json!({"version": current.version, "timestamp": now / 1000}))),
            Some(document) => {
                let before = previous.and_then(|r| r.document.as_ref());
                let old = before.map(|d| json!({"state": d, "version": previous.unwrap().version}));
                publications.push((self.topic(name, "update/documents"), bounded(
                    json!({"previous": old, "current": {"state": document, "version": current.version}, "timestamp": now / 1000}),
                    json!({"previous": old.as_ref().map(|_| json!({"version": previous.unwrap().version})),
                        "current": {"version": current.version}, "timestamp": now / 1000, "truncated": true}))));
                let delta = delta(section(document, "desired"), section(document, "reported"));
                let old_delta = before.map(|d| delta_of(d));
                if !delta.is_empty() && old_delta.as_ref() != Some(&delta) {
                    publications.push((self.topic(name, "update/delta"), bounded(
                        json!({"state": delta, "version": current.version, "timestamp": now / 1000}),
                        json!({"version": current.version, "timestamp": now / 1000, "truncated": true}))));
                }
            }
        }
        let mut broker = broker.lock().unwrap();
        for (topic, payload) in publications {
            match broker.publish(SENDER, &topic, &payload, None) {
                Ok(_) => self.published += 1,
                Err(error) => {
                    self.refused += 1;
                    warn!(shadow = name, topic, %error, "Shadow notification refused; subscribers must re-read the shadow");
                }
            }
        }
    }

    pub fn get(&self, name: &str) -> anyhow::Result<Value> {
        if !valid_name(name) {
            return fail("invalid", "invalid shadow name");
        }
        match self.records.get(name) {
            Some(Record { document: Some(document), version, .. }) => Ok(json!({"state": state_view(document), "version": version})),
            _ => fail("not-found", "no such shadow"),
        }
    }

    /// An AWS-shaped update request: `{state: {desired?, reported?}, version?, clientToken?}`.
    pub fn update(&mut self, name: &str, request: &Value, now: u64) -> anyhow::Result<Value> {
        if !valid_name(name) {
            return fail("invalid", "invalid shadow name");
        }
        let Some(request) = request.as_object() else { return fail("invalid", "the request is an object") };
        let token = match request.get("clientToken") {
            None | Some(Value::Null) => None,
            Some(Value::String(token)) if token.len() <= MAX_CLIENT_TOKEN => Some(token.clone()),
            Some(_) => return fail("invalid", "clientToken is a string of at most 64 bytes"),
        };
        if request.keys().any(|k| !["state", "version", "clientToken"].contains(&k.as_str())) {
            return fail("invalid", "unknown request key");
        }
        let Some(state) = request.get("state").and_then(Value::as_object) else { return fail("invalid", "state is an object") };
        if state.iter().any(|(k, v)| !SECTIONS.contains(&k.as_str()) || !(v.is_null() || v.is_object())) {
            return fail("invalid", "state has only desired/reported objects or null");
        }
        let state_value = Value::Object(state.clone());
        if state.values().any(|v| v.as_object().is_some_and(|m| !m.is_empty()) && depth(v) > MAX_DEPTH) || null_in_array(&state_value) {
            return fail("invalid", "too deep, or null inside an array");
        }
        let existing = self.records.get(name);
        if let Some(version) = request.get("version") {
            let Some(version) = version.as_u64().filter(|v| *v >= 1) else { return fail("invalid", "version is an integer of at least 1") };
            if existing.is_none_or(|r| r.document.is_none() || r.version != version) {
                return fail("conflict", "version mismatch");
            }
        }
        let mut document = existing.and_then(|r| r.document.clone()).unwrap_or_default();
        for (name, patch) in state {
            match patch {
                Value::Object(patch) => {
                    let mut merged = match document.remove(name) {
                        Some(Value::Object(map)) => map,
                        _ => Map::new(),
                    };
                    merge(&mut merged, patch);
                    if !merged.is_empty() {
                        document.insert(name.clone(), Value::Object(merged));
                    }
                }
                _ => {
                    document.remove(name);
                }
            }
        }
        if serde_json::to_vec(&document)?.len() > MAX_STATE_BYTES {
            return fail("too-large", format!("the state would exceed {MAX_STATE_BYTES} bytes"));
        }
        // Never store a document that would stop the next startup.
        if let Err(error) = check_document(&document) {
            return fail("invalid", error.to_string());
        }
        let version = self.commit(name, Some(document.clone()), None, now)?;
        let mut result = json!({"state": state_view(&document), "version": version});
        if let Some(token) = token {
            result["clientToken"] = json!(token);
        }
        Ok(result)
    }

    /// `request` is null or `{version?}`.
    pub fn delete(&mut self, name: &str, request: &Value, now: u64) -> anyhow::Result<Value> {
        if !valid_name(name) {
            return fail("invalid", "invalid shadow name");
        }
        let expected = match request {
            Value::Null => None,
            Value::Object(map) if map.keys().all(|k| k == "version") => match map.get("version") {
                None => None,
                Some(version) => match version.as_u64().filter(|v| *v >= 1) {
                    Some(version) => Some(version),
                    None => return fail("invalid", "version is an integer of at least 1"),
                },
            },
            _ => return fail("invalid", "the request is null or {version}"),
        };
        let Some(record) = self.records.get(name).filter(|r| r.document.is_some()) else { return fail("not-found", "no such shadow") };
        if expected.is_some_and(|v| v != record.version) {
            return fail("conflict", "version mismatch");
        }
        let version = self.commit(name, None, None, now)?;
        Ok(json!({"version": version}))
    }

    /// Existing named shadows (the classic shadow is not listed, as in AWS).
    pub fn names(&self) -> Vec<String> {
        self.records.values().filter(|r| r.document.is_some() && !r.name.is_empty()).map(|r| r.name.clone()).collect()
    }

    /// Administrator status. Without a name: every existing shadow with its
    /// version, size and sync counters, bounded to fit an IPC frame. With a
    /// name (deleted shadows included): also its last sync error and conflicts.
    pub fn status(&self, name: Option<&str>) -> anyhow::Result<Value> {
        let entry = |r: &Record, detail: bool| {
            let mut sync = self.sync_status.get(&r.name).cloned().unwrap_or(Value::Null);
            if !detail {
                if let Some(sync) = sync.as_object_mut() {
                    sync.retain(|k, _| ["pushes", "pulls", "conflicts", "failures"].contains(&k.as_str()));
                }
            }
            json!({
                "name": r.name, "version": r.version, "exists": r.document.is_some(),
                "bytes": r.document.as_ref().map_or(0, |d| serde_json::to_vec(d).map_or(0, |b| b.len())),
                "cloud_version": r.sync.as_ref().map(|s| s.cloud_version),
                "sync": sync,
            })
        };
        if let Some(name) = name {
            let Some(record) = self.records.get(name) else { return fail("not-found", "no such shadow") };
            return Ok(entry(record, true));
        }
        Ok(json!({
            "thing": self.thing,
            "max_shadows": self.max_shadows,
            "shadows": self.records.values().filter(|r| r.document.is_some()).map(|r| entry(r, false)).collect::<Vec<_>>(),
            "deleted_retained": self.records.len() - self.live(),
            "notifications": {"published": self.published, "refused": self.refused},
        }))
    }

    pub fn sync_snapshot(&self, name: &str) -> (Option<Map<String, Value>>, SyncState) {
        let record = self.records.get(name);
        (record.and_then(|r| r.document.clone()), record.and_then(|r| r.sync.clone()).unwrap_or_default())
    }

    /// Record a document the cloud now holds at `cloud_version`. `observed`
    /// is the local document the merge started from; local edits made since
    /// then are kept on top of the agreed one (and pushed next cycle).
    /// Returns whether the local document changed.
    pub fn sync_apply(&mut self, name: &str, observed: Option<&Map<String, Value>>, agreed: Option<Map<String, Value>>,
                      cloud_version: u64, now: u64) -> anyhow::Result<bool> {
        let current = self.records.get(name).and_then(|r| r.document.clone());
        let target = if same_document(current.as_ref(), observed) {
            agreed.clone()
        } else {
            merge3(observed, current.as_ref(), agreed.as_ref(), Owners { desired: Side::Device, reported: Side::Device }, &mut Vec::new())
        };
        // Keep the old base: recording the agreed one without its changes
        // here would let the next cycle overwrite them in the cloud.
        if let Some(Err(error)) = target.as_ref().map(check_document) {
            anyhow::bail!("local edits made during synchronization cannot be merged with the cloud document: {error}");
        }
        let sync = SyncState { base: agreed, cloud_version };
        if same_document(current.as_ref(), target.as_ref()) {
            if self.records.get(name).and_then(|r| r.sync.as_ref()) == Some(&sync) || (current.is_none() && sync == SyncState::default()) {
                return Ok(false);
            }
            let record = match self.records.get(name) {
                Some(record) => Record { sync: Some(sync), ..record.clone() },
                None => Record { name: name.into(), version: 0, document: None, updated_ms: now, sync: Some(sync) },
            };
            self.save(&record)?;
            self.records.insert(name.into(), record);
            return Ok(false);
        }
        self.commit(name, target, Some(sync), now)?;
        Ok(true)
    }

    pub fn set_sync_status(&mut self, name: &str, status: Value) {
        self.sync_status.insert(name.into(), status);
    }
}

/// A received document as an update would store it: empty objects removed.
/// If that differs from the cloud's copy, the next merge pushes it back.
fn normalize(document: &Map<String, Value>) -> Map<String, Value> {
    let mut out = Map::new();
    merge(&mut out, document);
    out
}

fn delta_of(document: &Map<String, Value>) -> Map<String, Value> {
    delta(section(document, "desired"), section(document, "reported"))
}

/// One shadow per file, named by the hex of its name (`s-.json` is classic).
fn file_name(name: &str) -> String {
    format!("s-{}.json", hex::encode(name))
}

// ---------------------------------------------------------------------------
// Authorization: AWS ShadowManager-style policies.
// ---------------------------------------------------------------------------

pub const OPERATIONS: [&str; 4] = [
    "aws.greengrass#GetThingShadow",
    "aws.greengrass#UpdateThingShadow",
    "aws.greengrass#DeleteThingShadow",
    "aws.greengrass#ListNamedShadowsForThing",
];

/// One policy statement. Resources are `$aws/things/<thing>/shadow` (the
/// classic shadow) or `$aws/things/<thing>/shadow/name/<name>`, globs in
/// which only `*` is special. `sections` (SuiFleet) limits an update grant
/// to `desired` or `reported`; without it both may be written.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub operations: Vec<String>,
    pub resources: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sections: Option<Vec<String>>,
}

pub fn validate_policies(policies: &[Policy]) -> anyhow::Result<()> {
    anyhow::ensure!(policies.len() <= 32, "too many shadow policies");
    for policy in policies {
        anyhow::ensure!(!policy.operations.is_empty() && policy.operations.len() <= 5 && !policy.resources.is_empty() && policy.resources.len() <= 64,
            "a shadow policy needs 1-5 operations and 1-64 resources");
        anyhow::ensure!(policy.operations.iter().all(|o| o == "*" || OPERATIONS.contains(&o.as_str())),
            "shadow operations are aws.greengrass#GetThingShadow, UpdateThingShadow, DeleteThingShadow, ListNamedShadowsForThing or *");
        anyhow::ensure!(policy.resources.iter().all(|r| !r.is_empty() && r.len() <= 256 && !r.contains('\0')), "invalid shadow resource");
        anyhow::ensure!(policy.sections.as_ref().is_none_or(|s| !s.is_empty() && s.iter().all(|s| SECTIONS.contains(&s.as_str()))),
            "shadow policy sections are desired and/or reported");
    }
    Ok(())
}

/// Whether one policy grants `operation` (unqualified, e.g. `GetThingShadow`)
/// on `resource` for every section in `sections`.
pub fn authorized(policies: &[Policy], operation: &str, resource: &str, sections: &[&str]) -> bool {
    let qualified = format!("aws.greengrass#{operation}");
    policies.iter().any(|p| {
        p.operations.iter().any(|o| o == "*" || *o == qualified)
            && p.resources.iter().any(|r| crate::pubsub::glob(r, resource))
            && sections.iter().all(|s| p.sections.as_ref().is_none_or(|allowed| allowed.iter().any(|a| a == s)))
    })
}

/// An IPC shadow operation. `policies` is None for administrators.
pub fn handle(store: &Mutex<Store>, policies: Option<&[Policy]>, op: &str, name: &str, payload: &Value) -> anyhow::Result<Value> {
    let mut store = store.lock().unwrap();
    let resource = resource(store.thing(), name);
    let allow = |operation: &str, sections: &[&str]| policies.is_none_or(|p| authorized(p, operation, &resource, sections));
    let now = crate::telemetry::now_ms();
    match op {
        "shadow_get" => {
            anyhow::ensure!(allow("GetThingShadow", &[]), "forbidden_shadow");
            store.get(name)
        }
        "shadow_update" => {
            let sections: Vec<&str> = payload.get("state").and_then(Value::as_object).map(|s| s.keys().map(String::as_str).collect()).unwrap_or_default();
            anyhow::ensure!(allow("UpdateThingShadow", &sections), "forbidden_shadow");
            let mut result = store.update(name, payload, now)?;
            // The stored document is only for readers: a write-only caller
            // gets its own request state back, as in AWS update/accepted.
            if !allow("GetThingShadow", &[]) {
                result["state"] = payload["state"].clone();
            }
            Ok(result)
        }
        "shadow_delete" => {
            anyhow::ensure!(allow("DeleteThingShadow", &[]), "forbidden_shadow");
            store.delete(name, payload, now)
        }
        "shadow_list" => {
            let thing = store.thing().to_owned();
            let names: Vec<String> = store.names().into_iter()
                .filter(|n| policies.is_none_or(|p| authorized(p, "ListNamedShadowsForThing", &crate::shadows::resource(&thing, n), &[])))
                .collect();
            Ok(json!({"shadows": names}))
        }
        "shadows" => {
            anyhow::ensure!(policies.is_none(), "administrator only");
            // null: every shadow; {"shadow": name}: one, with its last error and conflicts.
            match payload {
                Value::Null => store.status(None),
                Value::Object(map) if map.len() == 1 && map.get("shadow").is_some_and(Value::is_string) => store.status(map["shadow"].as_str()),
                _ => fail("invalid", "the payload is null or {shadow}"),
            }
        }
        _ => anyhow::bail!("unknown shadow operation"),
    }
}

// ---------------------------------------------------------------------------
// Three-way merge and the cloud sync adapter.
// ---------------------------------------------------------------------------

/// Which side's value a conflicting key keeps.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Side {
    Device,
    Cloud,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Owners {
    pub desired: Side,
    pub reported: Side,
}

/// Merge each side's changes since `base`. A key changed differently on
/// both sides is a conflict (its path is recorded) and keeps the section
/// owner's value; objects merge key by key, other values are whole. A
/// document deleted on one side and changed on the other keeps the change.
pub fn merge3(base: Option<&Map<String, Value>>, local: Option<&Map<String, Value>>, cloud: Option<&Map<String, Value>>,
              owners: Owners, conflicts: &mut Vec<String>) -> Option<Map<String, Value>> {
    if same_document(local, cloud) || same_document(local, base) {
        return cloud.cloned();
    }
    if same_document(cloud, base) {
        return local.cloned();
    }
    match (local, cloud) {
        (Some(local), Some(cloud)) => {
            let mut out = Map::new();
            for name in SECTIONS {
                let side = if name == "desired" { owners.desired } else { owners.reported };
                if let Some(value) = merge_value(base.and_then(|b| b.get(name)), local.get(name), cloud.get(name), side, name.into(), conflicts) {
                    out.insert(name.into(), value);
                }
            }
            Some(out)
        }
        (Some(kept), None) | (None, Some(kept)) => {
            conflicts.push("(deleted on one side, changed on the other)".into());
            Some(kept.clone())
        }
        (None, None) => None,
    }
}

fn merge_value(base: Option<&Value>, local: Option<&Value>, cloud: Option<&Value>, side: Side, path: String,
               conflicts: &mut Vec<String>) -> Option<Value> {
    if same(local, cloud) || same(local, base) {
        return cloud.cloned();
    }
    if same(cloud, base) {
        return local.cloned();
    }
    if let (Some(Value::Object(l)), Some(Value::Object(c))) = (local, cloud) {
        let b = base.and_then(Value::as_object);
        let keys: BTreeSet<&String> = l.keys().chain(c.keys()).collect();
        let mut out = Map::new();
        for key in keys {
            if let Some(value) = merge_value(b.and_then(|b| b.get(key)), l.get(key), c.get(key), side, format!("{path}.{key}"), conflicts) {
                out.insert(key.clone(), value);
            }
        }
        return (!out.is_empty()).then_some(Value::Object(out));
    }
    conflicts.push(path);
    match side {
        Side::Cloud => cloud.cloned(),
        Side::Device => local.cloned(),
    }
}

/// `[shadows.sync]`: the HTTP(S) shadow sync adapter.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SyncConfig {
    /// http(s) endpoint; every request is a signed POST here.
    pub url: String,
    /// Optional `Authorization: Bearer` token for the service (file, 0600).
    #[serde(default)]
    pub bearer_token_file: Option<PathBuf>,
    /// Names of the shadows to synchronize (`""` is the classic shadow).
    pub shadows: Vec<String>,
    #[serde(default = "default_sync_interval")]
    pub interval_seconds: u64,
    #[serde(default = "default_sync_backoff")]
    pub max_backoff_seconds: u64,
    /// Who wins a conflicting `desired` key.
    #[serde(default = "cloud_side")]
    pub desired_conflict: Side,
    /// Who wins a conflicting `reported` key.
    #[serde(default = "device_side")]
    pub reported_conflict: Side,
}
fn default_sync_interval() -> u64 {
    30
}
fn default_sync_backoff() -> u64 {
    300
}
fn cloud_side() -> Side {
    Side::Cloud
}
fn device_side() -> Side {
    Side::Device
}

/// The settings both sync adapters share.
fn validate_sync(table: &str, shadows: &[String], interval: u64, backoff: u64, max_shadows: usize) -> anyhow::Result<()> {
    anyhow::ensure!(!shadows.is_empty() && shadows.len() <= max_shadows, "{table}.shadows needs 1..=max_shadows names");
    let mut names = BTreeSet::new();
    for name in shadows {
        anyhow::ensure!(valid_name(name), "invalid shadow name {name:?} in {table}.shadows");
        anyhow::ensure!(names.insert(name), "shadow {name:?} is listed twice in {table}.shadows");
    }
    anyhow::ensure!((1..=3600).contains(&interval), "{table}.interval_seconds must be 1..=3600");
    anyhow::ensure!((1..=3600).contains(&backoff), "{table}.max_backoff_seconds must be 1..=3600");
    Ok(())
}

impl SyncConfig {
    pub fn validate(&self, max_shadows: usize) -> anyhow::Result<()> {
        let url = reqwest::Url::parse(&self.url)?;
        anyhow::ensure!(matches!(url.scheme(), "http" | "https") && url.host().is_some(), "shadows.sync.url must be http(s)");
        anyhow::ensure!(url.username().is_empty() && url.password().is_none(), "shadows.sync.url must not embed credentials; use bearer_token_file");
        validate_sync("shadows.sync", &self.shadows, self.interval_seconds, self.max_backoff_seconds, max_shadows)
    }
    fn owners(&self) -> Owners {
        Owners { desired: self.desired_conflict, reported: self.reported_conflict }
    }
}

/// `[shadows.aws_iot]`: synchronization with AWS IoT Device Shadow over
/// the `[aws_iot]` connection (SF-061).
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AwsSyncConfig {
    /// Names of the shadows to synchronize (`""` is the classic shadow).
    pub shadows: Vec<String>,
    /// Cloud poll; changes from other writers also arrive as notifications.
    #[serde(default = "default_aws_interval")]
    pub interval_seconds: u64,
    #[serde(default = "default_sync_backoff")]
    pub max_backoff_seconds: u64,
    #[serde(default = "cloud_side")]
    pub desired_conflict: Side,
    #[serde(default = "device_side")]
    pub reported_conflict: Side,
}
fn default_aws_interval() -> u64 {
    300
}

impl AwsSyncConfig {
    pub fn validate(&self, max_shadows: usize) -> anyhow::Result<()> {
        validate_sync("shadows.aws_iot", &self.shadows, self.interval_seconds, self.max_backoff_seconds, max_shadows)
    }
    pub fn owners(&self) -> Owners {
        Owners { desired: self.desired_conflict, reported: self.reported_conflict }
    }
}

/// Signed by the device key as a Sui personal message: binds one sync
/// request body to this device, chain, registry and shadow.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub schema: u32,
    pub kind: String,
    pub chain_identifier: String,
    pub registry: String,
    pub device_id: String,
    pub signer: String,
    pub shadow: String,
    pub op: String,
    pub body_sha256: String,
    pub created_ms: u64,
}

/// Validate a sync manifest for the signing service: canonical, bounded,
/// this chain/registry/signer, and a well-formed request.
pub fn check_manifest(message: &[u8], chain_identifier: &str, registry: &str, signer: &str) -> anyhow::Result<()> {
    anyhow::ensure!(message.len() <= MAX_MANIFEST_BYTES, "shadow sync manifest exceeds {MAX_MANIFEST_BYTES} bytes");
    let manifest: Manifest = serde_json::from_slice(message).map_err(|e| anyhow::anyhow!("not a shadow sync manifest: {e}"))?;
    anyhow::ensure!(serde_json::to_vec(&manifest)? == message, "shadow sync manifest is not in canonical form");
    anyhow::ensure!(manifest.schema == SYNC_SCHEMA && manifest.kind == MANIFEST_KIND, "unsupported shadow sync manifest");
    anyhow::ensure!(manifest.chain_identifier == chain_identifier, "shadow sync manifest is for another chain");
    anyhow::ensure!(crate::streams::same_address(&manifest.registry, registry), "shadow sync manifest is not for this fleet's registry");
    anyhow::ensure!(crate::streams::same_address(&manifest.signer, signer), "shadow sync manifest names another signer");
    anyhow::ensure!(valid_name(&manifest.shadow) && matches!(manifest.op.as_str(), "get" | "put")
        && manifest.body_sha256.len() == 64 && manifest.body_sha256.bytes().all(|b| b.is_ascii_hexdigit()), "malformed shadow sync manifest");
    Ok(())
}

/// Synchronizes the configured shadows until aborted.
pub struct Syncer {
    pub store: Arc<Mutex<Store>>,
    pub config: SyncConfig,
    pub binding: crate::streams::Binding,
    pub signer: Arc<dyn TxSigner>,
}

/// The cloud's answer to a put.
pub(crate) enum Put {
    Stored(u64),
    Conflict,
}

/// A cloud shadow service the synchronizer reconciles with.
pub(crate) trait Cloud {
    /// The cloud's version and document (None when absent there).
    async fn fetch(&self, shadow: &str) -> anyhow::Result<(u64, Option<Map<String, Value>>)>;
    /// Store `document` (None deletes) if the cloud is still at `expected`,
    /// whose document was `cloud`.
    async fn put(&self, shadow: &str, expected: u64, cloud: Option<&Map<String, Value>>, document: Option<&Map<String, Value>>) -> anyhow::Result<Put>;
}

/// One reconciliation of `shadow`: fetch, merge, compare-and-swap, apply.
pub(crate) async fn sync_one<C: Cloud>(store: &Mutex<Store>, owners: Owners, cloud: &C, shadow: &str) -> anyhow::Result<Value> {
    let (local, state) = store.lock().unwrap().sync_snapshot(shadow);
    let (cloud_version, remote) = cloud.fetch(shadow).await?;
    let mut conflicts = Vec::new();
    let merged = merge3(state.base.as_ref(), local.as_ref(), remote.as_ref(), owners, &mut conflicts);
    if let Some(document) = &merged {
        check_document(document).map_err(|e| anyhow::anyhow!("merged shadow refused: {e}"))?;
    }
    let pushed = !same_document(merged.as_ref(), remote.as_ref());
    let agreed_version = if pushed {
        match cloud.put(shadow, cloud_version, remote.as_ref(), merged.as_ref()).await? {
            Put::Stored(version) => version,
            // Another writer got there first: merge again from a fresh read.
            Put::Conflict => anyhow::bail!("the cloud shadow changed during synchronization; retrying"),
        }
    } else {
        cloud_version
    };
    let pulled = store.lock().unwrap().sync_apply(shadow, local.as_ref(), merged, agreed_version, crate::telemetry::now_ms())?;
    Ok(json!({"pushed": pushed, "pulled": pulled, "conflicts": conflicts, "cloud_version": agreed_version}))
}

/// Synchronize `shadows` until aborted. Local changes and `cloud_changed`
/// wake a cycle at once; otherwise the cloud is polled every `interval`.
pub(crate) async fn run_sync<C: Cloud>(store: Arc<Mutex<Store>>, shadows: &[String], interval: u64, max_backoff: u64, owners: Owners,
                                       cloud: &C, cloud_changed: Option<Arc<tokio::sync::Notify>>) {
    let changed = store.lock().unwrap().changed.clone();
    let cloud_changed = cloud_changed.unwrap_or_default();
    let mut counters: BTreeMap<String, (u64, u64, u64, u64)> = BTreeMap::new();
    let mut backoff = 1u64;
    loop {
        let mut failed = false;
        for shadow in shadows {
            let result = sync_one(&store, owners, cloud, shadow).await;
            let (pushes, pulls, conflicts, failures) = counters.entry(shadow.clone()).or_default();
            match &result {
                Ok(outcome) => {
                    *pushes += outcome["pushed"].as_bool().unwrap_or(false) as u64;
                    *pulls += outcome["pulled"].as_bool().unwrap_or(false) as u64;
                    let found = outcome["conflicts"].as_array().map_or(0, Vec::len) as u64;
                    *conflicts += found;
                    if found > 0 {
                        info!(%shadow, conflicts = %outcome["conflicts"], "Shadow sync settled conflicts by the configured owner");
                    }
                }
                Err(error) => {
                    failed = true;
                    *failures += 1;
                    warn!(%shadow, %error, "Shadow sync failed; local shadow unchanged");
                }
            }
            store.lock().unwrap().set_sync_status(shadow, json!({
                "pushes": *pushes, "pulls": *pulls, "conflicts": *conflicts, "failures": *failures,
                "last_error": result.as_ref().err().map(|e| crate::telemetry::bounded(&e.to_string(), 300)),
                // At most 16 paths: the status must fit an IPC frame.
                "last_conflicts": result.as_ref().ok().map(|o| o["conflicts"].as_array().map_or(&[][..], Vec::as_slice)
                    .iter().take(16).cloned().collect::<Vec<_>>()),
                "updated_ms": crate::telemetry::now_ms(),
            }));
        }
        if failed {
            tokio::time::sleep(Duration::from_secs(backoff)).await;
            backoff = (backoff * 2).min(max_backoff);
        } else {
            backoff = 1;
            // A local change syncs at once; cloud changes are polled or notified.
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(interval)) => {}
                _ = changed.notified() => {}
                _ = cloud_changed.notified() => {}
            }
        }
    }
}

/// The HTTP(S) service, through one client.
struct Http<'a> {
    syncer: &'a Syncer,
    client: reqwest::Client,
}

impl Cloud for Http<'_> {
    async fn fetch(&self, shadow: &str) -> anyhow::Result<(u64, Option<Map<String, Value>>)> {
        self.syncer.fetch(&self.client, shadow).await
    }
    async fn put(&self, shadow: &str, expected: u64, _: Option<&Map<String, Value>>, document: Option<&Map<String, Value>>) -> anyhow::Result<Put> {
        self.syncer.put(&self.client, shadow, expected, document).await
    }
}

impl Syncer {
    /// Build the signed request for `body` (without the shared fields).
    pub fn request(&self, op: &str, shadow: &str, mut body: Map<String, Value>, now_ms: u64) -> anyhow::Result<(Vec<u8>, Vec<u8>, Vec<u8>)> {
        body.insert("schema".into(), json!(SYNC_SCHEMA));
        body.insert("op".into(), json!(op));
        body.insert("device_id".into(), json!(self.binding.device_id));
        body.insert("shadow".into(), json!(shadow));
        let body = serde_json::to_vec(&body)?;
        let manifest = serde_json::to_vec(&Manifest {
            schema: SYNC_SCHEMA,
            kind: MANIFEST_KIND.into(),
            chain_identifier: self.binding.chain_identifier.clone(),
            registry: self.binding.registry.clone(),
            device_id: self.binding.device_id.clone(),
            signer: self.signer.address(),
            shadow: shadow.into(),
            op: op.into(),
            body_sha256: hex::encode(Sha256::digest(&body)),
            created_ms: now_ms,
        })?;
        anyhow::ensure!(manifest.len() <= MAX_MANIFEST_BYTES, "shadow sync manifest too large");
        let signature = self.signer.sign_personal_message(&manifest)?;
        Ok((body, manifest, signature))
    }

    async fn call(&self, http: &reqwest::Client, op: &str, shadow: &str, body: Map<String, Value>) -> anyhow::Result<(reqwest::StatusCode, Value)> {
        let (body, manifest, signature) = self.request(op, shadow, body, crate::telemetry::now_ms())?;
        let mut request = http
            .post(&self.config.url)
            .header("content-type", "application/json")
            .header("x-suifleet-manifest", STANDARD.encode(&manifest))
            .header("x-suifleet-signature", STANDARD.encode(&signature))
            .body(body);
        if let Some(path) = &self.config.bearer_token_file {
            let token = std::fs::read_to_string(path).map_err(|e| anyhow::anyhow!("cannot read bearer_token_file: {}", e.kind()))?;
            request = request.bearer_auth(token.trim());
        }
        // Errors leave out the URL: its query may carry a credential.
        let sanitize = |e: reqwest::Error| anyhow::anyhow!("{}", crate::streams::sanitized(e, &self.config.url));
        let mut response = request.send().await.map_err(sanitize)?;
        let status = response.status();
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(sanitize)? {
            bytes.extend_from_slice(&chunk);
            anyhow::ensure!(bytes.len() <= MAX_RESPONSE_BYTES, "shadow service answer exceeds {MAX_RESPONSE_BYTES} bytes");
        }
        let value = if bytes.is_empty() { Value::Null } else { serde_json::from_slice(&bytes).unwrap_or(Value::Null) };
        Ok((status, value))
    }

    /// The cloud's version and document (None when absent there).
    async fn fetch(&self, http: &reqwest::Client, shadow: &str) -> anyhow::Result<(u64, Option<Map<String, Value>>)> {
        let (status, value) = self.call(http, "get", shadow, Map::new()).await?;
        anyhow::ensure!(status == reqwest::StatusCode::OK, "shadow service answered {status}");
        let version = value.get("version").and_then(Value::as_u64).ok_or_else(|| anyhow::anyhow!("shadow service answer has no version"))?;
        let document = match value.get("document") {
            None | Some(Value::Null) => None,
            Some(Value::Object(document)) => {
                check_document(document).map_err(|e| anyhow::anyhow!("cloud shadow refused: {e}"))?;
                Some(normalize(document))
            }
            Some(_) => anyhow::bail!("cloud shadow refused: not an object"),
        };
        Ok((version, document))
    }

    async fn put(&self, http: &reqwest::Client, shadow: &str, expected: u64, document: Option<&Map<String, Value>>) -> anyhow::Result<Put> {
        let mut body = Map::new();
        body.insert("expected_version".into(), json!(expected));
        body.insert("document".into(), json!(document));
        let (status, value) = self.call(http, "put", shadow, body).await?;
        if status == reqwest::StatusCode::CONFLICT {
            return Ok(Put::Conflict);
        }
        anyhow::ensure!(status == reqwest::StatusCode::OK, "shadow service answered {status}");
        let version = value.get("version").and_then(Value::as_u64).ok_or_else(|| anyhow::anyhow!("shadow service answer has no version"))?;
        anyhow::ensure!(version > expected, "shadow service did not advance the version");
        Ok(Put::Stored(version))
    }

    pub async fn run(self) {
        let client = match crate::streams::Exporter::http_client() {
            Ok(client) => client,
            Err(error) => return warn!(%error, "Shadow sync disabled: no HTTP client"),
        };
        let cloud = Http { syncer: &self, client };
        run_sync(self.store.clone(), &self.config.shadows, self.config.interval_seconds, self.config.max_backoff_seconds,
                 self.config.owners(), &cloud, None).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(tag: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("suifleet-shadows-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        path
    }

    fn code(error: &anyhow::Error) -> &'static str {
        error.downcast_ref::<ShadowError>().map_or("other", |e| e.code)
    }

    fn outcome(result: anyhow::Result<Value>) -> Value {
        result.unwrap_or_else(|e| json!({"error": code(&e)}))
    }

    /// The fixture's ops against the store; `restart` reopens it from disk.
    fn apply(store: &mut Store, op: &Value) -> Value {
        let name = op["shadow"].as_str().unwrap_or("");
        match op["op"].as_str().unwrap() {
            "get" => outcome(store.get(name)),
            "update" => outcome(store.update(name, &op["request"], 1)),
            "delete" => outcome(store.delete(name, &op.get("version").map_or(Value::Null, |v| json!({"version": v})), 1)),
            other => panic!("unknown op {other}"),
        }
    }

    #[test]
    fn shared_shadow_cases() {
        let cases: Value = serde_json::from_str(include_str!("../../interfaces/compat/shadow-cases.json")).unwrap();
        let mut steps = 0;
        for case in cases.as_array().unwrap() {
            let path = dir(case["id"].as_str().unwrap());
            let mut store = Store::open(&path, 16, "thing", None).unwrap();
            for step in case["steps"].as_array().unwrap() {
                let got = apply(&mut store, &step["op"]);
                assert!(json_equal(&got, &step["expect"]), "{} step {}: {got} != {}", case["id"], step["op"], step["expect"]);
                // Every step's outcome also holds after a restart.
                let reopened = Store::open(&path, 16, "thing", None).unwrap();
                for name in store.records.keys() {
                    assert_eq!(outcome(reopened.get(name)), outcome(store.get(name)), "{} after reopen", case["id"]);
                    assert_eq!(reopened.records[name].version, store.records[name].version);
                }
                steps += 1;
            }
            let _ = std::fs::remove_dir_all(&path);
        }
        assert_eq!(steps, 55, "shadow-cases.json has 55 steps");
    }

    #[test]
    fn delete_request_versions_follow_the_fixture_order() {
        // The fixture passes delete versions at the op level; IPC passes {version}.
        let path = dir("delete");
        let mut store = Store::open(&path, 4, "thing", None).unwrap();
        store.update("a", &json!({"state": {"desired": {"x": 1}}}), 1).unwrap();
        for (request, expected) in [(json!({"version": true}), "invalid"), (json!({"version": 1.5}), "invalid"),
                                    (json!({"other": 1}), "invalid"), (json!("1"), "invalid"), (json!({"version": 2}), "conflict")] {
            assert_eq!(code(&store.delete("a", &request, 1).unwrap_err()), expected, "{request}");
        }
        assert_eq!(store.delete("a", &json!({"version": 1}), 1).unwrap(), json!({"version": 2}));
        assert_eq!(code(&store.delete("a", &Value::Null, 1).unwrap_err()), "not-found");
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn quota_bounds_live_and_retained_shadows_and_startup() {
        let path = dir("quota");
        let mut store = Store::open(&path, 2, "thing", None).unwrap();
        for name in ["a", "b"] {
            store.update(name, &json!({"state": {"reported": {"x": 1}}}), 1).unwrap();
        }
        assert_eq!(code(&store.update("c", &json!({"state": {"reported": {"x": 1}}}), 1).unwrap_err()), "quota");
        store.delete("a", &Value::Null, 1).unwrap();
        store.update("c", &json!({"state": {"reported": {"x": 1}}}), 1).unwrap();
        // A deleted shadow is recreated within the live quota, continuing its versions.
        store.delete("c", &Value::Null, 1).unwrap();
        assert_eq!(store.update("a", &json!({"state": {"reported": {"x": 2}}}), 1).unwrap()["version"], 3);
        // Live b, a; retained c: a fourth name exceeds 2 * max_shadows retained records only after one more.
        store.delete("b", &Value::Null, 1).unwrap();
        store.update("d", &json!({"state": {"reported": {"x": 1}}}), 1).unwrap();
        store.delete("d", &Value::Null, 1).unwrap();
        assert_eq!(code(&store.update("e", &json!({"state": {"reported": {"x": 1}}}), 1).unwrap_err()), "quota");
        drop(store);
        // Lowering the limit below the stored shadows refuses to open, keeping them.
        let mut store = Store::open(&path, 2, "thing", None).unwrap();
        store.update("b", &json!({"state": {"reported": {"x": 1}}}), 1).unwrap();
        drop(store);
        assert!(Store::open(&path, 1, "thing", None).is_err());
        assert_eq!(Store::open(&path, 2, "thing", None).unwrap().get("b").unwrap()["version"], 3);
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn null_nested_in_an_array_is_refused_and_restart_still_works() {
        let path = dir("nested-null");
        let mut store = Store::open(&path, 4, "thing", None).unwrap();
        store.update("a", &json!({"state": {"reported": {"x": 1}}}), 1).unwrap();
        for items in [json!([{"x": null}]), json!([[null]]), json!([{"x": [{"y": null}]}])] {
            let error = store.update("a", &json!({"state": {"reported": {"items": items}}}), 1).unwrap_err();
            assert_eq!(code(&error), "invalid", "{items}");
        }
        assert_eq!(store.get("a").unwrap(), json!({"state": {"reported": {"x": 1}}, "version": 1}));
        drop(store);
        let store = Store::open(&path, 4, "thing", None).unwrap();
        assert_eq!(store.get("a").unwrap(), json!({"state": {"reported": {"x": 1}}, "version": 1}));
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn interrupted_writes_keep_the_last_document_and_corruption_stops_startup() {
        let path = dir("crash");
        let mut store = Store::open(&path, 4, "thing", None).unwrap();
        store.update("a", &json!({"state": {"desired": {"x": 1}}}), 1).unwrap();
        drop(store);
        // A crash mid-replacement leaves a temporary file; the old file is intact.
        std::fs::write(path.join(format!("{}.tmp", file_name("a").trim_end_matches(".json"))), b"{torn").unwrap();
        let store = Store::open(&path, 4, "thing", None).unwrap();
        assert_eq!(store.get("a").unwrap()["state"]["desired"], json!({"x": 1}));
        assert_eq!(std::fs::read_dir(&path).unwrap().count(), 1);
        drop(store);
        std::fs::write(path.join(file_name("a")), b"{\"name\":").unwrap();
        let error = Store::open(&path, 4, "thing", None).err().unwrap();
        assert!(error.to_string().contains("inspect or remove it"), "{error}");
        assert!(path.join(file_name("a")).exists(), "the corrupt file is kept for inspection");
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn policies_scope_operations_resources_and_sections() {
        let policies: Vec<Policy> = serde_json::from_value(json!([
            {"operations": ["aws.greengrass#GetThingShadow", "aws.greengrass#UpdateThingShadow"],
             "resources": ["$aws/things/dev/shadow/name/config"], "sections": ["reported"]},
            {"operations": ["aws.greengrass#ListNamedShadowsForThing", "aws.greengrass#GetThingShadow"],
             "resources": ["$aws/things/dev/shadow/name/pub-*"]},
        ])).unwrap();
        validate_policies(&policies).unwrap();
        let config = resource("dev", "config");
        assert!(authorized(&policies, "GetThingShadow", &config, &[]));
        assert!(authorized(&policies, "UpdateThingShadow", &config, &["reported"]));
        assert!(!authorized(&policies, "UpdateThingShadow", &config, &["desired"]));
        assert!(!authorized(&policies, "UpdateThingShadow", &config, &["reported", "desired"]));
        assert!(!authorized(&policies, "DeleteThingShadow", &config, &[]));
        assert!(!authorized(&policies, "GetThingShadow", &resource("dev", ""), &[]));
        assert!(!authorized(&policies, "GetThingShadow", &resource("other", "config"), &[]));
        assert!(authorized(&policies, "GetThingShadow", &resource("dev", "pub-a"), &[]));
        for bad in [json!([{"operations": ["aws.greengrass#PublishToTopic"], "resources": ["x"]}]),
                    json!([{"operations": ["*"], "resources": []}]),
                    json!([{"operations": ["*"], "resources": ["x"], "sections": ["delta"]}]),
                    json!([{"operations": ["*"], "resources": ["x"], "sections": []}])] {
            assert!(validate_policies(&serde_json::from_value::<Vec<Policy>>(bad.clone()).unwrap()).is_err(), "{bad}");
        }

        let path = dir("acl");
        let store = Mutex::new(Store::open(&path, 8, "dev", None).unwrap());
        let p = Some(policies.as_slice());
        let update = |section: &str| json!({"state": {section: {"x": 1}}});
        handle(&store, None, "shadow_update", "pub-a", &update("desired")).unwrap();
        handle(&store, None, "shadow_update", "secret", &update("desired")).unwrap();
        handle(&store, p, "shadow_update", "config", &update("reported")).unwrap();
        assert!(handle(&store, p, "shadow_update", "config", &update("desired")).is_err());
        assert!(handle(&store, p, "shadow_delete", "config", &Value::Null).is_err());
        assert!(handle(&store, p, "shadow_get", "secret", &Value::Null).is_err());
        assert!(handle(&store, p, "shadows", "", &Value::Null).is_err());
        assert_eq!(handle(&store, p, "shadow_list", "", &Value::Null).unwrap(), json!({"shadows": ["pub-a"]}));
        assert_eq!(handle(&store, None, "shadow_list", "", &Value::Null).unwrap(), json!({"shadows": ["config", "pub-a", "secret"]}));

        // A write-only grant updates without reading: the response echoes the
        // request, never the stored desired state or delta.
        let writer: Vec<Policy> = serde_json::from_value(json!([
            {"operations": ["aws.greengrass#UpdateThingShadow"], "resources": ["$aws/things/dev/shadow/name/secret"], "sections": ["reported"]},
        ])).unwrap();
        let w = Some(writer.as_slice());
        assert!(handle(&store, w, "shadow_get", "secret", &Value::Null).is_err());
        let r = handle(&store, w, "shadow_update", "secret", &json!({"state": {"reported": {"y": 2}}, "clientToken": "c"})).unwrap();
        assert_eq!(r, json!({"state": {"reported": {"y": 2}}, "version": 2, "clientToken": "c"}));
        assert_eq!(handle(&store, w, "shadow_update", "secret", &json!({"state": {}})).unwrap(), json!({"state": {}, "version": 3}));
        // Readers still get the whole document.
        let r = handle(&store, p, "shadow_update", "config", &update("reported")).unwrap();
        assert_eq!(r["state"], handle(&store, None, "shadow_get", "config", &Value::Null).unwrap()["state"]);
        assert_eq!(handle(&store, None, "shadow_get", "secret", &Value::Null).unwrap()["state"]["desired"], json!({"x": 1}));
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn changes_are_announced_on_local_pubsub() {
        let path = dir("notify");
        let broker = Arc::new(Mutex::new(crate::pubsub::Broker::open(&path.join("pubsub"), crate::pubsub::Limits::default()).unwrap()));
        for suffix in ["update/delta", "update/documents", "delete/accepted"] {
            broker.lock().unwrap().subscribe("c", &format!("$aws/things/dev/shadow/name/cfg/{suffix}"), 1).unwrap();
        }
        let mut store = Store::open(&path.join("shadows"), 8, "dev", Some(broker.clone())).unwrap();
        let receive = |suffix: &str| {
            let filter = format!("$aws/things/dev/shadow/name/cfg/{suffix}");
            let mut broker = broker.lock().unwrap();
            let message = broker.receive("c", &filter).unwrap();
            if let Some(m) = &message {
                broker.ack("c", &filter, m.id).unwrap();
            }
            message.map(|m| m.payload)
        };
        store.update("cfg", &json!({"state": {"desired": {"mode": "on"}}}), 5000).unwrap();
        assert_eq!(receive("update/delta").unwrap(), json!({"state": {"mode": "on"}, "version": 1, "timestamp": 5}));
        let documents = receive("update/documents").unwrap();
        assert_eq!((documents["previous"].clone(), documents["current"]["version"].clone()), (Value::Null, json!(1)));
        // Reported catching up clears the delta: documents only, no delta message.
        store.update("cfg", &json!({"state": {"reported": {"mode": "on"}}}), 6000).unwrap();
        assert!(receive("update/delta").is_none());
        assert_eq!(receive("update/documents").unwrap()["previous"]["version"], 1);
        // A change in desired that keeps the same delta is not announced twice.
        store.update("cfg", &json!({"state": {"desired": {"mode": "off"}}}), 7000).unwrap();
        store.update("cfg", &json!({"state": {"desired": {"other": null}}}), 7000).unwrap();
        assert_eq!(receive("update/delta").unwrap()["version"], 3);
        assert!(receive("update/delta").is_none());
        // Large documents are announced by version only (pub/sub payloads are <= 4 KiB).
        store.update("cfg", &json!({"state": {"desired": {"big": "x".repeat(6000)}}}), 8000).unwrap();
        assert_eq!(receive("update/delta").unwrap(), json!({"version": 5, "timestamp": 8, "truncated": true}));
        store.delete("cfg", &Value::Null, 9000).unwrap();
        assert_eq!(receive("delete/accepted").unwrap(), json!({"version": 6, "timestamp": 9}));
        assert_eq!(store.status(None).unwrap()["notifications"]["refused"], 0);
        assert_eq!(store.status(None).unwrap()["deleted_retained"], 1);
        assert_eq!(store.status(Some("cfg")).unwrap()["version"], 6);
        let _ = std::fs::remove_dir_all(&path);
    }

    const DEVICE_WINS_REPORTED: Owners = Owners { desired: Side::Cloud, reported: Side::Device };

    fn doc(value: Value) -> Option<Map<String, Value>> {
        value.as_object().cloned()
    }

    #[test]
    fn three_way_merge_keeps_both_sides_and_settles_conflicts_by_owner() {
        let base = doc(json!({"desired": {"a": 1, "b": 1, "n": {"x": 1}}, "reported": {"r": 1}}));
        let local = doc(json!({"desired": {"a": 2, "b": 1, "n": {"x": 1, "y": 2}}, "reported": {"r": 2, "s": 1}}));
        let cloud = doc(json!({"desired": {"a": 3, "b": 9, "n": {"x": 5}}, "reported": {"r": 7}}));
        let mut conflicts = Vec::new();
        let merged = merge3(base.as_ref(), local.as_ref(), cloud.as_ref(), DEVICE_WINS_REPORTED, &mut conflicts);
        assert_eq!(Value::Object(merged.unwrap()),
            json!({"desired": {"a": 3, "b": 9, "n": {"x": 5, "y": 2}}, "reported": {"r": 2, "s": 1}}));
        assert_eq!(conflicts, ["desired.a", "reported.r"]);
        // The reverse owners take the other values.
        let mut conflicts = Vec::new();
        let merged = merge3(base.as_ref(), local.as_ref(), cloud.as_ref(), Owners { desired: Side::Device, reported: Side::Cloud }, &mut conflicts);
        assert_eq!(Value::Object(merged.unwrap()),
            json!({"desired": {"a": 2, "b": 9, "n": {"x": 5, "y": 2}}, "reported": {"r": 7, "s": 1}}));
        // One-sided changes need no conflict resolution, deletions included.
        let mut conflicts = Vec::new();
        assert_eq!(merge3(base.as_ref(), None, base.as_ref(), DEVICE_WINS_REPORTED, &mut conflicts), None);
        let edited = doc(json!({"desired": {"a": 1, "b": 1, "n": {"x": 1}}}));
        assert_eq!(merge3(base.as_ref(), base.as_ref(), edited.as_ref(), DEVICE_WINS_REPORTED, &mut conflicts), edited);
        assert!(conflicts.is_empty());
        // Deleted on one side and changed on the other: the change survives.
        assert_eq!(merge3(base.as_ref(), None, cloud.as_ref(), DEVICE_WINS_REPORTED, &mut conflicts), cloud);
        assert_eq!(conflicts.len(), 1);
        // Never synced: both documents are unioned.
        let mut conflicts = Vec::new();
        let merged = merge3(None, doc(json!({"reported": {"r": 1}})).as_ref(), doc(json!({"desired": {"a": 1}})).as_ref(), DEVICE_WINS_REPORTED, &mut conflicts);
        assert_eq!(Value::Object(merged.unwrap()), json!({"desired": {"a": 1}, "reported": {"r": 1}}));
        // A value replaced by an object on one side and another value on the other is one conflict.
        let mut conflicts = Vec::new();
        let merged = merge3(doc(json!({"desired": {"a": 1}})).as_ref(), doc(json!({"desired": {"a": {"x": 1}}})).as_ref(),
            doc(json!({"desired": {"a": 2}})).as_ref(), DEVICE_WINS_REPORTED, &mut conflicts);
        assert_eq!((Value::Object(merged.unwrap()), conflicts), (json!({"desired": {"a": 2}}), vec!["desired.a".to_string()]));
    }

    #[test]
    fn sync_apply_keeps_local_edits_made_during_a_sync() {
        let path = dir("apply");
        let mut store = Store::open(&path, 4, "thing", None).unwrap();
        store.update("s", &json!({"state": {"reported": {"r": 1}}}), 1).unwrap();
        let (observed, _) = store.sync_snapshot("s");
        // Edited locally while the cloud round trip was in flight.
        store.update("s", &json!({"state": {"reported": {"r": 2}}}), 1).unwrap();
        let agreed = doc(json!({"desired": {"a": 1}, "reported": {"r": 1}}));
        assert!(store.sync_apply("s", observed.as_ref(), agreed.clone(), 7, 1).unwrap());
        assert_eq!(store.get("s").unwrap()["state"], json!({"desired": {"a": 1}, "reported": {"r": 2}, "delta": {"a": 1}}));
        assert_eq!(store.sync_snapshot("s").1, SyncState { base: agreed, cloud_version: 7 });
        // The sync state survives a restart.
        drop(store);
        let store = Store::open(&path, 4, "thing", None).unwrap();
        assert_eq!(store.sync_snapshot("s").1.cloud_version, 7);
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn sync_apply_refuses_an_unmergeable_edit_and_keeps_the_old_base() {
        let path = dir("apply-limit");
        let mut store = Store::open(&path, 4, "thing", None).unwrap();
        store.update("s", &json!({"state": {"reported": {"r": 1}}}), 1).unwrap();
        let (observed, before) = store.sync_snapshot("s");
        store.update("s", &json!({"state": {"reported": {"big": "x".repeat(5000)}}}), 1).unwrap();
        let agreed = doc(json!({"desired": {"other": "y".repeat(5000)}, "reported": {"r": 1}}));
        assert!(store.sync_apply("s", observed.as_ref(), agreed, 9, 1).is_err());
        assert_eq!(store.sync_snapshot("s").1, before);
        assert_eq!(store.get("s").unwrap()["version"], 2);
        // Received documents are stored as updates would store them.
        assert_eq!(Value::Object(normalize(&doc(json!({"desired": {"a": {}, "b": {"c": {}}, "d": 1}, "reported": {}})).unwrap())),
            json!({"desired": {"d": 1}}));
        let _ = std::fs::remove_dir_all(&path);
    }

    struct Key;
    impl TxSigner for Key {
        fn address(&self) -> String {
            format!("0x{}", "ab".repeat(32))
        }
        fn sign_digest(&self, _: &[u8]) -> anyhow::Result<Vec<u8>> {
            Ok(vec![0; 97])
        }
    }

    #[test]
    fn manifests_bind_the_body_and_the_signer_service_checks_them() {
        let path = dir("manifest");
        let syncer = Syncer {
            store: Arc::new(Mutex::new(Store::open(&path, 4, "thing", None).unwrap())),
            config: serde_json::from_value(json!({"url": "http://127.0.0.1:1/sync", "shadows": [""]})).unwrap(),
            binding: crate::streams::Binding { chain_identifier: "chain".into(), registry: "0x5".into(), device_id: "0xd".into() },
            signer: Arc::new(Key),
        };
        let mut body = Map::new();
        body.insert("expected_version".into(), json!(3));
        let (body, manifest, _) = syncer.request("put", "cfg", body, 42).unwrap();
        let parsed: Manifest = serde_json::from_slice(&manifest).unwrap();
        assert_eq!(parsed.body_sha256, hex::encode(Sha256::digest(&body)));
        assert_eq!((parsed.shadow.as_str(), parsed.op.as_str(), parsed.created_ms), ("cfg", "put", 42));
        let signer = Key.address();
        check_manifest(&manifest, "chain", "0x5", &signer).unwrap();
        assert!(check_manifest(&manifest, "other", "0x5", &signer).is_err());
        assert!(check_manifest(&manifest, "chain", "0x6", &signer).is_err());
        assert!(check_manifest(&manifest, "chain", "0x5", "0x1").is_err());
        let mut spaced = manifest.clone();
        spaced.push(b' ');
        assert!(check_manifest(&spaced, "chain", "0x5", &signer).is_err());
        let mut bad = parsed.clone();
        bad.op = "delete-all".into();
        assert!(check_manifest(&serde_json::to_vec(&bad).unwrap(), "chain", "0x5", &signer).is_err());
        let _ = std::fs::remove_dir_all(&path);
    }

    #[test]
    fn sync_configuration_is_validated() {
        let valid = json!({"url": "https://shadows.example/v1", "shadows": ["", "cfg"]});
        let config: SyncConfig = serde_json::from_value(valid.clone()).unwrap();
        config.validate(4).unwrap();
        assert_eq!((config.desired_conflict, config.reported_conflict), (Side::Cloud, Side::Device));
        for (key, value) in [("url", json!("ftp://x")), ("url", json!("https://u:p@x/")), ("shadows", json!([])),
                             ("shadows", json!(["a", "a"])), ("shadows", json!(["bad name"])), ("interval_seconds", json!(0)),
                             ("shadows", json!(["a", "b", "c", "d", "e"]))] {
            let mut bad = valid.clone();
            bad[key] = value;
            assert!(serde_json::from_value::<SyncConfig>(bad).unwrap().validate(4).is_err(), "{key}");
        }
        assert!(serde_json::from_value::<SyncConfig>(json!({"url": "https://x", "shadows": [""], "desired_conflict": "both"})).is_err());
    }
}
