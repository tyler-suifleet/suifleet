//! Sui access over the supported gRPC v2 API (sui.rpc.v2), using Mysten's
//! official Rust SDK. JSON-RPC is no longer used.
//!
//! - Reads: GetServiceInfo, GetPackage, GetObject (dynamic fields by their
//!   derived object ID), GetTransaction (legacy cursor migration).
//! - Discovery: LedgerService.ListEvents filtered by the DeploymentCreated
//!   type, resumed from opaque watermark cursors stored in the journal.
//! - Writes: the transaction is built from the node's simulation (gas,
//!   object versions, budget), checked against the exact intended call,
//!   signed locally by `identity` and executed; success is read from the
//!   execution effects, never inferred from a digest.
use crate::{config::SuiConfig, identity, protocol};
use base64::{engine::general_purpose::STANDARD, Engine};
use futures_util::StreamExt;
use serde_json::{json, Value};
use std::{str::FromStr, time::Duration};
use sui_rpc::field::{FieldMask, FieldMaskUtil};
use sui_rpc::proto::sui::rpc::v2 as proto;
use sui_sdk_types::{Address, Argument, Command, Digest, Input, StructTag, Transaction, TypeTag};

/// Largest gas budget the device will sign for one status report or gas
/// draw (0.05 SUI).
const MAX_GAS_BUDGET: u64 = 50_000_000;
/// The shared Clock object.
const CLOCK: &str = "0x6";
/// Events requested per ListEvents call.
const PAGE_SIZE: u32 = 50;
const DEPLOYMENT_CREATED: &str = "deployment_manager::DeploymentCreated";
const DEVICE_ADDED_TO_GROUP: &str = "device_group::DeviceAddedToGroup";

pub use crate::protocol::DeploymentCreatedEvent;

/// An on-chain object as the daemon needs it.
#[derive(Debug, Clone)]
pub struct Object {
    pub object_type: String,
    pub owner: Owner,
    /// Move fields rendered as JSON by the node.
    pub fields: Value,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Owner {
    Address(String),
    Object(String),
    Shared { initial_version: u64 },
    Immutable,
    Other,
}

/// One page of DeploymentCreated events. Each event carries the cursor to
/// resume *after* it; `end` is the cursor after the whole scanned range.
pub struct EventPage {
    pub events: Vec<(DeploymentCreatedEvent, Value)>,
    pub end: Value,
    pub has_more: bool,
}

/// One page of DeviceAddedToGroup events as (group ID, device ID).
pub struct MembershipPage {
    pub events: Vec<(String, String)>,
    pub end: Value,
    pub has_more: bool,
}

/// Where the next ListEvents call starts, as stored in the journal.
#[derive(Debug, PartialEq)]
enum Cursor {
    /// Opaque gRPC watermark: resume strictly after it. The checkpoint it is
    /// in (when the node reports one) lets pruned history be detected.
    After(Vec<u8>, Option<u64>),
    /// Start of a checkpoint range (inclusive): enrollment baseline or a
    /// migrated JSON-RPC cursor. Events already journaled are deduplicated.
    Checkpoint(u64),
    /// Cursor written by the JSON-RPC daemon: {"txDigest", "eventSeq"}, the
    /// last processed event. Migration resumes strictly after that event.
    Legacy(String, u64),
    Genesis,
}

impl Cursor {
    fn parse(value: &Option<Value>) -> anyhow::Result<Self> {
        let Some(value) = value else {
            return Ok(Self::Genesis);
        };
        if let Some(after) = value.get("after").and_then(Value::as_str) {
            let checkpoint = value.get("checkpoint").and_then(Value::as_u64);
            return Ok(Self::After(STANDARD.decode(after)?, checkpoint));
        }
        if let Some(checkpoint) = value.get("checkpoint").and_then(Value::as_u64) {
            return Ok(Self::Checkpoint(checkpoint));
        }
        if let Some(digest) = value.get("txDigest").and_then(Value::as_str) {
            let sequence = value
                .get("eventSeq")
                .and_then(|v| v.as_str().and_then(|s| s.parse().ok()).or_else(|| v.as_u64()))
                .ok_or_else(|| anyhow::anyhow!("legacy event cursor without a valid eventSeq: {value}"))?;
            return Ok(Self::Legacy(digest.to_owned(), sequence));
        }
        anyhow::bail!("unrecognized event cursor in journal: {value}")
    }
}

enum Start {
    After(Vec<u8>),
    Checkpoint(u64),
}

pub struct DiscoveryPage {
    pub deployments: Vec<(DeploymentCreatedEvent, Value)>,
    pub groups: Vec<String>,
    pub end: Value,
    pub has_more: bool,
}

struct RawPage {
    events: Vec<(proto::Event, Value)>,
    end: Value,
    has_more: bool,
}

/// Whether `event` is the legacy cursor's event; then its resume cursor.
/// Passing the legacy checkpoint without meeting it is an error.
fn legacy_boundary(
    event: &proto::Event,
    digest: &str,
    sequence: u64,
    checkpoint: u64,
    watermark: &Value,
) -> anyhow::Result<Option<Value>> {
    let at = event.checkpoint.unwrap_or_default();
    anyhow::ensure!(
        at <= checkpoint,
        "legacy event cursor {digest}#{sequence} was not found in checkpoint {checkpoint}; \
         refusing to guess where to resume"
    );
    let same = event.transaction_digest.as_deref() == Some(digest)
        && event.event_index.map(u64::from) == Some(sequence);
    Ok(same.then(|| watermark.clone()))
}

fn after(watermark: &proto::Watermark) -> anyhow::Result<Value> {
    let cursor = watermark
        .cursor
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("event stream frame without a resume cursor"))?;
    let mut value = json!({ "after": STANDARD.encode(cursor) });
    if let Some(checkpoint) = watermark.checkpoint {
        value["checkpoint"] = json!(checkpoint);
    }
    Ok(value)
}

/// Resuming from `position` needs the node to still hold that checkpoint's
/// events. Pruned history is an operator decision (an archival node, or a
/// deliberate re-enrollment), never something to skip silently — unless the
/// device can resynchronize from its inbox and group heads (SF-125), which
/// the caller decides on `HistoryPruned`.
fn ensure_retained(position: Option<u64>, lowest: Option<u64>) -> anyhow::Result<()> {
    if let (Some(position), Some(lowest)) = (position, lowest) {
        if position < lowest {
            return Err(anyhow::Error::new(HistoryPruned { position, lowest }));
        }
    }
    Ok(())
}

/// The checkpoint a cursor resumes in, when the cursor itself records it.
/// Genesis resumes before checkpoint 1; a JSON-RPC cursor needs a lookup.
fn boundary_checkpoint(cursor: &Cursor) -> Option<u64> {
    match cursor {
        Cursor::Genesis => Some(0),
        Cursor::Checkpoint(checkpoint) | Cursor::After(_, Some(checkpoint)) => Some(*checkpoint),
        Cursor::After(_, None) | Cursor::Legacy(..) => None,
    }
}

fn timestamp_ms(at: Option<prost_types::Timestamp>) -> anyhow::Result<u64> {
    let at = at.ok_or_else(|| anyhow::anyhow!("node reported no checkpoint time"))?;
    let ms = at.seconds.saturating_mul(1000).saturating_add(i64::from(at.nanos) / 1_000_000);
    u64::try_from(ms).map_err(|_| anyhow::anyhow!("node reported a negative checkpoint time"))
}

/// The node no longer holds the events this device resumes from.
#[derive(Debug)]
pub struct HistoryPruned {
    pub position: u64,
    pub lowest: u64,
}
impl std::fmt::Display for HistoryPruned {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "event history before checkpoint {} is pruned on this node, but this device resumes \
             at checkpoint {}; point rpc_url at a node that retains it (nothing is skipped)",
            self.lowest, self.position
        )
    }
}
impl std::error::Error for HistoryPruned {}

/// What has been announced to a device inbox or group (fleet_inbox::Head).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Head {
    pub seq: u64,
    pub next_start_ms: u64,
    /// Component name → latest announced deployment.
    pub components: std::collections::BTreeMap<String, String>,
}

/// A device's inbox: its head and the groups it was added to.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Inbox {
    pub head: Head,
    pub groups: Vec<String>,
}

/// A device inbox's address: `sui::derived_object` under the registry,
/// keyed `DerivedObjectKey<InboxKey { device_id }>`.
fn inbox_id(registry: &str, inbox_key: &str, device: &str) -> anyhow::Result<String> {
    let inner = StructTag::from_str(inbox_key).map_err(|e| anyhow::anyhow!("invalid inbox key type: {e}"))?;
    let key = TypeTag::Struct(Box::new(StructTag::new(
        Address::TWO,
        sui_sdk_types::Identifier::from_static("derived_object"),
        sui_sdk_types::Identifier::from_static("DerivedObjectKey"),
        vec![TypeTag::Struct(Box::new(inner))],
    )));
    let registry = Address::from_str(registry).map_err(|e| anyhow::anyhow!("invalid registry ID: {e}"))?;
    let device = Address::from_str(device).map_err(|e| anyhow::anyhow!("invalid device ID {device}: {e}"))?;
    Ok(registry.derive_dynamic_child_id(&key, device.inner()).to_string())
}

fn parse_head(value: &Value) -> anyhow::Result<Head> {
    let value = value.get("fields").unwrap_or(value);
    let seq = number(&value["seq"]).ok_or_else(|| anyhow::anyhow!("head without a sequence: {value}"))?;
    let next_start_ms = number(&value["next_start_ms"])
        .ok_or_else(|| anyhow::anyhow!("head without a next start: {value}"))?;
    let components = value["components"].get("fields").unwrap_or(&value["components"]);
    let entries = components["contents"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("undecodable head components: {value}"))?;
    let mut head = Head { seq, next_start_ms, components: Default::default() };
    for entry in entries {
        let entry = entry.get("fields").unwrap_or(entry);
        match (entry["key"].as_str(), entry["value"].as_str()) {
            (Some(name), Some(deployment)) => {
                head.components.insert(name.to_owned(), deployment.to_owned());
            }
            _ => anyhow::bail!("undecodable head component: {entry}"),
        }
    }
    Ok(head)
}

#[derive(Clone)]
pub struct SuiClient {
    config: SuiConfig,
    signer: std::sync::Arc<dyn identity::TxSigner>,
    rpc: Vec<sui_rpc::Client>,
    verified: std::sync::Arc<Vec<std::sync::atomic::AtomicBool>>,
    mutations: std::sync::Arc<tokio::sync::Mutex<()>>,
    pub funding: std::sync::Arc<tokio::sync::Mutex<crate::funding::Funding>>,
    pub receipt_retries: std::sync::Arc<std::sync::Mutex<std::collections::BTreeMap<String, std::time::Instant>>>,
    stats: std::sync::Arc<std::sync::Mutex<(u64, u64)>>,
    poll: std::sync::Arc<std::sync::Mutex<PollWatch>>,
    packages: std::sync::Arc<
        tokio::sync::Mutex<std::collections::BTreeMap<String, sui_sdk_types::MovePackage>>,
    >,
}

/// Only successful full polls authorize the idle fast path. Object values are
/// never cached: execution/admission and reconcile double reads stay fresh.
#[derive(Default)]
struct PollWatch {
    versions: std::collections::BTreeMap<String, Option<u64>>,
    spellings: std::collections::BTreeMap<String, String>,
    roots: std::collections::BTreeMap<String, String>,
    observed: std::collections::BTreeSet<String>,
    sleeping_roots: std::collections::BTreeSet<String>,
    valid: bool,
    full_at: Option<std::time::Instant>,
    discovery_at: Option<std::time::Instant>,
    /// Whether the last version check found the event discovery bound due.
    discovery_due: bool,
    generation: u64,
    event_limit: Option<u64>,
    registry_metadata: Option<Object>,
    package_transactions: std::collections::BTreeMap<String, String>,
    membership_birth: Option<u64>,
}

/// Objects created by a successful enrollment (SF-021).
#[derive(Debug, Clone)]
pub struct Enrolled {
    pub digest: String,
    pub device_object_id: String,
    pub device_cap_id: String,
    pub device_address: String,
    /// Sent to the policy's operator.
    pub owner_cap_id: String,
}

/// Largest gas budget a claim key sponsors for one enrollment (0.05 SUI).
const MAX_ENROLL_BUDGET: u64 = 50_000_000;

pub fn same_id(a: &str, b: &str) -> bool {
    fn normalized(s: &str) -> Option<Vec<u8>> {
        let s = s.strip_prefix("0x")?;
        if s.is_empty() || s.len() > 64 {
            return None;
        }
        hex::decode(format!("{:0>64}", s)).ok()
    }
    matches!((normalized(a), normalized(b)), (Some(a), Some(b)) if a == b)
}

pub fn same_type(a: &str, b: &str) -> bool {
    matches!((a.split_once("::"), b.split_once("::")), (Some((aa, at)), Some((ba, bt))) if same_id(aa, ba) && at == bt)
}

/// The 8-hex chain identifier used in manifests (and by JSON-RPC's
/// sui_getChainIdentifier): the first four bytes of the genesis checkpoint
/// digest, which gRPC reports in base58 as `chain_id`.
pub fn chain_identifier(genesis_digest: &str) -> anyhow::Result<String> {
    let digest = Digest::from_str(genesis_digest)
        .map_err(|e| anyhow::anyhow!("invalid chain_id {genesis_digest:?}: {e}"))?;
    Ok(hex::encode(&digest.inner()[..4]))
}

fn retryable(error: &tonic::Status) -> bool {
    matches!(
        error.code(),
        tonic::Code::Unavailable | tonic::Code::ResourceExhausted | tonic::Code::DeadlineExceeded
    ) || (error.code() == tonic::Code::Unknown && error.message().contains("429"))
}

// The pinned builder flattens tonic errors into Error::Input. Match only
// its exact simulation-error prefix and tonic code, never a Move failure or
// arbitrary message containing a retry word.
fn builder_status(error: sui_transaction_builder::Error) -> tonic::Status {
    if let sui_transaction_builder::Error::Input(message) = &error {
        for code in [
            tonic::Code::Unavailable,
            tonic::Code::ResourceExhausted,
            tonic::Code::DeadlineExceeded,
        ] {
            if message.starts_with(&format!("error simulating transaction: code: '{}'", code)) {
                return tonic::Status::new(code, "transaction simulation transport failure");
            }
        }
    }
    tonic::Status::failed_precondition(format!("transaction simulation failed: {error}"))
}

fn status(error: tonic::Status) -> anyhow::Error {
    anyhow::anyhow!("{}: {}", error.code(), error.message())
}

fn mask(paths: &[&str]) -> FieldMask {
    FieldMask::from_paths(paths.iter().copied())
}

/// google.protobuf.Value -> serde_json::Value.
fn to_json(value: &prost_types::Value) -> Value {
    use prost_types::value::Kind;
    match &value.kind {
        None | Some(Kind::NullValue(_)) => Value::Null,
        Some(Kind::BoolValue(b)) => Value::Bool(*b),
        Some(Kind::NumberValue(n)) => json!(n),
        Some(Kind::StringValue(s)) => Value::String(s.clone()),
        Some(Kind::ListValue(list)) => Value::Array(list.values.iter().map(to_json).collect()),
        Some(Kind::StructValue(map)) => Value::Object(
            map.fields
                .iter()
                .map(|(k, v)| (k.clone(), to_json(v)))
                .collect(),
        ),
    }
}

/// A Move integer as the node renders it (string, or exact safe JSON number).
pub fn json_u64(value: &Value) -> Option<u64> { number(value) }

fn number(value: &Value) -> Option<u64> {
    value
        .as_str()
        .and_then(|s| s.parse().ok())
        .or_else(|| value.as_u64())
        // Protobuf JSON numbers arrive as doubles (not integer JSON tokens).
        // Accept only exact nonnegative safe integers; full u64s use strings.
        .or_else(|| value.as_f64().filter(|n| n.is_finite() && *n >= 0.0 &&
            *n <= 9_007_199_254_740_991.0 && n.fract() == 0.0).map(|n| n as u64))
}

fn byte_vector(value: &Value) -> anyhow::Result<Vec<u8>> {
    if let Some(encoded) = value.as_str() {
        return STANDARD.decode(encoded).map_err(Into::into);
    }
    value.as_array().ok_or_else(|| anyhow::anyhow!("invalid byte vector"))?
        .iter().map(|v| number(v).and_then(|n| u8::try_from(n).ok())
            .ok_or_else(|| anyhow::anyhow!("invalid byte vector element"))).collect()
}

fn id_list(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|ids| ids.iter().filter_map(|id| id.as_str().map(str::to_owned)).collect())
        .unwrap_or_default()
}

impl SuiClient {
    pub fn load(config: SuiConfig, signer: std::sync::Arc<dyn identity::TxSigner>) -> anyhow::Result<Self> {
        let manifest = config
            .manifest
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("deployment manifest required"))?;
        manifest.validate()?;
        let urls = if config.rpc_urls.is_empty() {
            vec![manifest.rpc_url.clone()]
        } else {
            config.rpc_urls.clone()
        };
        let rpc = urls
            .iter()
            .map(|url| {
                sui_rpc::Client::new(url.as_str())
                    .map(|c| c.with_response_headers_timeout(Duration::from_secs(30)))
                    .map_err(|_| anyhow::anyhow!("invalid gRPC endpoint"))
            })
            .collect::<anyhow::Result<Vec<_>>>()?;
        Ok(Self {
            config,
            signer,
            verified: std::sync::Arc::new(
                (0..rpc.len())
                    .map(|_| std::sync::atomic::AtomicBool::new(false))
                    .collect(),
            ),
            rpc,
            mutations: Default::default(),
            funding: Default::default(),
            receipt_retries: Default::default(),
            stats: Default::default(),
            poll: Default::default(),
            packages: Default::default(),
        })
    }
    /// Validate candidates before any reads/signing. A wrong chain is a hard
    /// error, even when another candidate would have matched.
    async fn check_rpc(&self, mut rpc: sui_rpc::Client) -> Result<sui_rpc::Client, tonic::Status> {
        self.stats.lock().unwrap().0 += 1;
        let response = tokio::time::timeout(
            Duration::from_secs(30),
            rpc.ledger_client()
                .get_service_info(proto::GetServiceInfoRequest::default()),
        )
        .await
        .map_err(|_| tonic::Status::deadline_exceeded("RPC identity timeout"))??;
        self.stats.lock().unwrap().1 += prost::Message::encoded_len(response.get_ref()) as u64;
        let info = response.into_inner();
        let chain = chain_identifier(info.chain_id.as_deref().unwrap_or_default())
            .map_err(|_| tonic::Status::failed_precondition("invalid chain identity"))?;
        if chain != self.manifest().chain_identifier {
            return Err(tonic::Status::failed_precondition(
                "chain identifier mismatch",
            ));
        }
        Ok(rpc)
    }
    async fn rpc_call<T, F, Fut>(&self, call: F) -> Result<T, tonic::Status>
    where
        F: FnMut(sui_rpc::Client) -> Fut,
        Fut: std::future::Future<Output = Result<T, tonic::Status>>,
    {
        self.rpc_call_inner(true, call).await
    }
    /// Count every explicit read attempt and protobuf response, including
    /// retries; transaction execution/building uses the separate rpc_call path.
    async fn read_rpc<T, F, Fut>(&self, mut call: F) -> Result<tonic::Response<T>, tonic::Status>
    where
        T: prost::Message,
        F: FnMut(sui_rpc::Client) -> Fut,
        Fut: std::future::Future<Output = Result<tonic::Response<T>, tonic::Status>>,
    {
        self.rpc_call(|rpc| {
            self.stats.lock().unwrap().0 += 1;
            let future = call(rpc);
            async move {
                let response = future.await?;
                self.stats.lock().unwrap().1 +=
                    prost::Message::encoded_len(response.get_ref()) as u64;
                Ok(response)
            }
        })
        .await
    }
    async fn rpc_call_inner<T, F, Fut>(&self, verify: bool, mut call: F) -> Result<T, tonic::Status>
    where
        F: FnMut(sui_rpc::Client) -> Fut,
        Fut: std::future::Future<Output = Result<T, tonic::Status>>,
    {
        let mut last = tonic::Status::unavailable("no RPC endpoints");
        for (index, rpc) in self.rpc.iter().enumerate() {
            let result = async {
                let rpc =
                    if verify && !self.verified[index].load(std::sync::atomic::Ordering::Acquire) {
                        let rpc = self.check_rpc(rpc.clone()).await?;
                        self.verified[index].store(true, std::sync::atomic::Ordering::Release);
                        rpc
                    } else {
                        rpc.clone()
                    };
                tokio::time::timeout(Duration::from_secs(30), call(rpc))
                    .await
                    .map_err(|_| tonic::Status::deadline_exceeded("RPC request timeout"))?
            }
            .await;
            match result {
                Ok(value) => {
                    self.verified[index].store(true, std::sync::atomic::Ordering::Release);
                    return Ok(value);
                }
                Err(error) if retryable(&error) => last = error,
                Err(error) => return Err(error),
            }
        }
        Err(last)
    }
    // One identity probe per candidate per validation cycle. Service-info
    // reads themselves always check identity, including enrollment/cursor reads.
    async fn service_info(
        &self,
    ) -> Result<tonic::Response<proto::GetServiceInfoResponse>, tonic::Status> {
        self.rpc_call_inner(false, |mut rpc| async move {
            self.stats.lock().unwrap().0 += 1;
            let response = rpc
                .ledger_client()
                .get_service_info(proto::GetServiceInfoRequest::default())
                .await?;
            self.stats.lock().unwrap().1 += prost::Message::encoded_len(response.get_ref()) as u64;
            let chain =
                chain_identifier(response.get_ref().chain_id.as_deref().unwrap_or_default())
                    .map_err(|_| tonic::Status::failed_precondition("invalid chain identity"))?;
            if chain != self.manifest().chain_identifier {
                return Err(tonic::Status::failed_precondition(
                    "chain identifier mismatch",
                ));
            }
            Ok(response)
        })
        .await
    }
    async fn build_rpc(
        &self,
        factory: impl Fn() -> anyhow::Result<sui_transaction_builder::TransactionBuilder>,
    ) -> anyhow::Result<Transaction> {
        self.rpc_call(|mut rpc| {
            let builder = factory();
            async move {
                let builder =
                    builder.map_err(|e| tonic::Status::invalid_argument(e.to_string()))?;
                builder.build(&mut rpc).await.map_err(builder_status)
            }
        })
        .await
        .map_err(status)
    }
    pub fn address(&self) -> String {
        self.signer.address()
    }
    pub fn manifest(&self) -> &crate::config::DeploymentManifest {
        self.config.manifest.as_ref().expect("validated manifest")
    }
    pub fn move_type(&self, name: &str) -> String {
        self.manifest().move_type(name)
    }
    pub fn origin(&self) -> &str {
        &self.manifest().type_origins["device_registry::Device"]
    }

    fn watch_id(id: &str) -> String {
        Address::from_str(id)
            .map(|a| a.to_string())
            .unwrap_or_else(|_| id.to_owned())
    }

    pub fn watch_child(&self, id: &str, parent: &str) {
        self.poll
            .lock()
            .unwrap()
            .roots
            .insert(Self::watch_id(id), Self::watch_id(parent));
    }

    fn observe(&self, id: &str, version: Option<u64>) {
        let spelling = id.to_owned();
        let id = Self::watch_id(id);
        if same_id(&id, CLOCK) {
            return;
        }
        let mut watch = self.poll.lock().unwrap();
        // Keep the first version seen during a slow pass. A later read must
        // not hide a concurrent change from the following poll.
        watch.spellings.insert(id.clone(), spelling);
        if watch.observed.insert(id.clone()) {
            watch.versions.insert(id, version);
        }
    }

    pub fn read_stats(&self) -> (u64, u64) {
        *self.stats.lock().unwrap()
    }

    pub fn watch_generation(&self) -> u64 {
        self.poll.lock().unwrap().generation
    }

    /// One version-only request. Per-object errors other than NotFound, missing
    /// versions, and incomplete batches fail closed; no successful idle poll.
    pub async fn poll_changed(
        &self,
        roots: &[String],
        sleeping_roots: &[String],
        full_seconds: u64,
        discovery_seconds: u64,
    ) -> anyhow::Result<bool> {
        let (ids, spellings, valid, full_due, discovery_due) = {
            let mut w = self.poll.lock().unwrap();
            w.sleeping_roots = sleeping_roots.iter().map(|id| Self::watch_id(id)).collect();
            let roots: std::collections::BTreeSet<_> =
                roots.iter().map(|id| Self::watch_id(id)).collect();
            let children = w.roots.clone();
            w.versions.retain(|id, _| {
                roots.contains(id)
                    || children
                        .get(id)
                        .is_some_and(|parent| roots.contains(parent))
            });
            w.roots.retain(|_, parent| roots.contains(parent));
            (
                w.versions.clone(),
                w.spellings.clone(),
                w.valid,
                w.full_at
                    .is_none_or(|at| at.elapsed() >= Duration::from_secs(full_seconds)),
                w.discovery_at
                    .is_none_or(|at| at.elapsed() >= Duration::from_secs(discovery_seconds)),
            )
        };
        self.poll.lock().unwrap().discovery_due = discovery_due;
        if !valid || ids.is_empty() {
            return Ok(true);
        }
        let mut request = proto::BatchGetObjectsRequest::default();
        request.read_mask = Some(mask(&["version"]));
        request.requests = ids
            .keys()
            .map(|id| {
                let mut r = proto::GetObjectRequest::default();
                r.object_id = Some(spellings.get(id).unwrap_or(id).clone());
                r.read_mask = Some(mask(&["version"]));
                r
            })
            .collect();
        let response = self
            .read_rpc(|mut rpc| {
                let request = request.clone();
                async move { rpc.ledger_client().batch_get_objects(request).await }
            })
            .await
            .map_err(status)?
            .into_inner();
        anyhow::ensure!(
            response.objects.len() == ids.len(),
            "incomplete watch batch"
        );
        let mut versions_changed = false;
        for ((id, previous), result) in ids.iter().zip(response.objects) {
            let version = match result.result {
                Some(proto::get_object_result::Result::Object(object)) => Some(
                    object
                        .version
                        .ok_or_else(|| anyhow::anyhow!("watch object {id} has no version"))?,
                ),
                Some(proto::get_object_result::Result::Error(error)) if error.code == 5 => None,
                other => anyhow::bail!("watch read failed for {id}: {other:?}"),
            };
            versions_changed |= &version != previous;
            if same_id(id, &self.manifest().shared_objects.registry.id) {
                let mut w = self.poll.lock().unwrap();
                if version.is_none() {
                    w.registry_metadata = None;
                }
                // Registry contents may grow; its type and initial shared
                // version cannot change. Keep its existence/version sentinel
                // current without downloading the registry again.
                w.versions.insert(id.clone(), version);
            }
        }
        let changed = versions_changed || full_due || discovery_due;
        if versions_changed || full_due {
            let mut w = self.poll.lock().unwrap();
            w.generation = w.generation.wrapping_add(1);
        }
        tracing::info!(objects = ids.len(), changed, "Chain poll version batch");
        Ok(changed)
    }

    pub fn set_event_limit(&self, checkpoint: Option<u64>) {
        self.poll.lock().unwrap().event_limit = checkpoint;
    }

    /// Whether this poll's event discovery is due by its bound; announced
    /// work (SF-125) does not need an event scan.
    pub fn discovery_due(&self) -> bool {
        self.poll.lock().unwrap().discovery_due
    }

    pub fn begin_full_poll(&self) {
        let mut w = self.poll.lock().unwrap();
        w.valid = false;
        w.observed.clear();
    }
    /// `scanned`: this poll ran event discovery, which restarts its bound.
    pub fn finish_full_poll(&self, scanned: bool) {
        let mut w = self.poll.lock().unwrap();
        let observed = w.observed.clone();
        let sleeping = w.sleeping_roots.clone();
        let children = w.roots.clone();
        let registry = Self::watch_id(&self.manifest().shared_objects.registry.id);
        // Stop watching fields that successful reconciliation no longer uses
        // (e.g. DesiredKey after membership removal). Sleeping fleet producers
        // retain their validated snapshots until their next due attempt.
        w.versions.retain(|id, _| {
            observed.contains(id)
                || id == &registry
                || sleeping.contains(id)
                || children
                    .get(id)
                    .is_some_and(|parent| sleeping.contains(parent))
        });
        let retained: std::collections::BTreeSet<_> = w.versions.keys().cloned().collect();
        w.spellings.retain(|id, _| retained.contains(id));
        w.roots.retain(|id, _| retained.contains(id));
        w.valid = true;
        w.full_at = Some(std::time::Instant::now());
        if scanned {
            w.discovery_at = w.full_at;
        }
    }

    pub async fn validate_deployment(&self) -> anyhow::Result<()> {
        for verified in self.verified.iter() {
            verified.store(false, std::sync::atomic::Ordering::Release);
        }
        let manifest = self.manifest();
        let info = self.service_info().await.map_err(status)?.into_inner();
        anyhow::ensure!(
            chain_identifier(info.chain_id.as_deref().unwrap_or_default())?
                == manifest.chain_identifier,
            "chain identifier mismatch; network may have reset"
        );
        let package = self.package(&manifest.call_package_id).await?;
        anyhow::ensure!(
            package.version.to_string() == manifest.package_version,
            "package metadata mismatch"
        );
        for name in crate::config::REQUIRED_TYPES.iter().chain(manifest.optional_types()) {
            let (module, datatype) = name.split_once("::").unwrap();
            let entries: Vec<_> = package
                .type_origin_table
                .iter()
                .filter(|o| o.module_name.as_str() == module && o.struct_name.as_str() == datatype)
                .collect();
            anyhow::ensure!(
                entries.len() == 1
                    && same_id(&entries[0].package.to_string(), &manifest.type_origins[*name]),
                "type origin mismatch: {name} (package reports {:?})",
                entries.iter().map(|o| o.package.to_string()).collect::<Vec<_>>()
            );
        }
        let cap = self.object(&manifest.upgrade_cap_id).await?;
        anyhow::ensure!(
            same_type(&cap.object_type, "0x2::package::UpgradeCap")
                && same_id(
                    cap.fields["package"].as_str().unwrap_or(""),
                    &manifest.call_package_id
                )
                && number(&cap.fields["version"]).map(|v| v.to_string()).as_deref()
                    == Some(manifest.package_version.as_str()),
            "UpgradeCap does not identify latest call package/version"
        );
        let registry = &manifest.shared_objects.registry;
        let cached = self.poll.lock().unwrap().registry_metadata.clone();
        let object = match cached {
            Some(object) => object,
            None => self.object(&registry.id).await?,
        };
        anyhow::ensure!(
            same_type(
                &object.object_type,
                &self.move_type("device_registry::DeviceRegistry")
            ) && matches!(object.owner, Owner::Shared { initial_version }
                if initial_version.to_string() == registry.initial_shared_version),
            "registry type/shared version mismatch"
        );
        self.poll.lock().unwrap().registry_metadata = Some(object);
        Ok(())
    }

    /// A package's on-chain contents, decoded from its BCS: the type origin
    /// table is authoritative, not a rendering by the node.
    async fn package(&self, id: &str) -> anyhow::Result<sui_sdk_types::MovePackage> {
        let mut packages = self.packages.lock().await;
        if let Some(package) = packages.get(id) {
            return Ok(package.clone());
        }
        let mut request = proto::GetObjectRequest::default();
        request.object_id = Some(id.to_owned());
        request.read_mask = Some(mask(&["bcs"]));
        let bytes = self
            .read_rpc(|mut rpc| {
                let request = request.clone();
                async move { rpc.ledger_client().get_object(request).await }
            })
            .await
            .map_err(status)?
            .into_inner()
            .object
            .and_then(|o| o.bcs)
            .and_then(|b| b.value)
            .ok_or_else(|| anyhow::anyhow!("package {id} has no contents"))?;
        let object: sui_sdk_types::Object = bcs::from_bytes(&bytes)
            .map_err(|e| anyhow::anyhow!("undecodable package {id}: {e}"))?;
        match object.data() {
            sui_sdk_types::ObjectData::Package(package) => {
                self.poll
                    .lock()
                    .unwrap()
                    .package_transactions
                    .insert(id.to_owned(), object.previous_transaction().to_string());
                packages.insert(id.to_owned(), package.clone());
                Ok(package.clone())
            }
            _ => anyhow::bail!("{id} is not a package"),
        }
    }

    /// A Move object's type, owner and BCS contents, decoded from the node's
    /// object BCS rather than its JSON rendering.
    pub async fn move_object(&self, id: &str) -> anyhow::Result<(String, Owner, Vec<u8>)> {
        let (object_type, owner, _, contents) = self.versioned_move_object(id).await?;
        Ok((object_type, owner, contents))
    }

    /// `move_object` with the object's version (its last mutation).
    pub async fn versioned_move_object(&self, id: &str) -> anyhow::Result<(String, Owner, u64, Vec<u8>)> {
        let mut request = proto::GetObjectRequest::default();
        request.object_id = Some(id.to_owned());
        request.read_mask = Some(mask(&["bcs", "version"]));
        let response = self
            .read_rpc(|mut rpc| {
                let request = request.clone();
                async move { rpc.ledger_client().get_object(request).await }
            })
            .await
            .map_err(|e| status(e).context(format!("object {id}")))?
            .into_inner();
        let object = response
            .object
            .ok_or_else(|| anyhow::anyhow!("object {id} has no data"))?;
        self.observe(
            id,
            Some(
                object
                    .version
                    .ok_or_else(|| anyhow::anyhow!("object {id} has no version"))?,
            ),
        );
        let bytes = object
            .bcs
            .and_then(|b| b.value)
            .ok_or_else(|| anyhow::anyhow!("object {id} has no contents"))?;
        let object: sui_sdk_types::Object = bcs::from_bytes(&bytes)
            .map_err(|e| anyhow::anyhow!("undecodable object {id}: {e}"))?;
        let owner = match object.owner() {
            sui_sdk_types::Owner::Address(address) => Owner::Address(address.to_string()),
            sui_sdk_types::Owner::Object(address) => Owner::Object(address.to_string()),
            sui_sdk_types::Owner::Shared(version) => Owner::Shared {
                initial_version: *version,
            },
            sui_sdk_types::Owner::Immutable => Owner::Immutable,
            _ => Owner::Other,
        };
        match object.data() {
            sui_sdk_types::ObjectData::Struct(value) => Ok((
                value.object_type().to_string(),
                owner,
                object.version(),
                value.contents().to_vec(),
            )),
            _ => anyhow::bail!("{id} is not a Move object"),
        }
    }

    pub async fn optional_object(&self, id: &str) -> anyhow::Result<Option<Object>> {
        let mut request = proto::GetObjectRequest::default();
        request.object_id = Some(id.to_owned());
        request.read_mask = Some(mask(&["owner", "object_type", "json", "version"]));
        let object = match self
            .read_rpc(|mut rpc| {
                let request = request.clone();
                async move { rpc.ledger_client().get_object(request).await }
            })
            .await
        {
            Ok(response) => response.into_inner().object,
            // Deleted, wrapped or never created.
            Err(e) if e.code() == tonic::Code::NotFound => {
                self.observe(id, None);
                return Ok(None);
            }
            Err(e) => {
                return Err(status(e).context(format!("object {id}")));
            }
        }
        .ok_or_else(|| anyhow::anyhow!("object {id} has no data"))?;
        self.observe(
            id,
            Some(
                object
                    .version
                    .ok_or_else(|| anyhow::anyhow!("object {id} has no version"))?,
            ),
        );
        let owner = match object.owner.as_ref() {
            Some(o) if o.kind == Some(proto::owner::OwnerKind::Address as i32) => {
                Owner::Address(o.address.clone().unwrap_or_default())
            }
            Some(o) if o.kind == Some(proto::owner::OwnerKind::Object as i32) => {
                Owner::Object(o.address.clone().unwrap_or_default())
            }
            Some(o) if o.kind == Some(proto::owner::OwnerKind::Shared as i32) => Owner::Shared {
                initial_version: o.version.unwrap_or_default(),
            },
            Some(o) if o.kind == Some(proto::owner::OwnerKind::Immutable as i32) => Owner::Immutable,
            _ => Owner::Other,
        };
        Ok(Some(Object {
            object_type: object.object_type.unwrap_or_default(),
            owner,
            fields: object.json.as_deref().map(to_json).unwrap_or(Value::Null),
        }))
    }
    pub async fn object(&self, id: &str) -> anyhow::Result<Object> {
        self.optional_object(id)
            .await?
            .ok_or_else(|| anyhow::anyhow!("object {id} is deleted or absent"))
    }

    /// The value of a dynamic field keyed by one of our empty key structs
    /// (`name` like "deployment_manager::RecipeDigestKey"), read through its
    /// derived object ID. Empty Move structs serialize as one `false` byte
    /// (the implicit dummy_field).
    pub async fn dynamic_field(&self, parent: &str, name: &str) -> anyhow::Result<Option<Value>> {
        self.keyed_field(parent, name, &[0]).await
    }

    /// The fleet's SF-130 settings record (`fleet_settings::Settings` in a
    /// dynamic field of its `FleetBudget`), or None when the deployment has
    /// no settings types or the owner has not created one. The derived ID
    /// binds the parent and key type; the value type is checked as well.
    pub async fn fleet_settings(&self, budget_id: &str) -> anyhow::Result<Option<Value>> {
        let manifest = self.manifest();
        if !protocol::SETTINGS_TYPES.iter().all(|name| manifest.type_origins.contains_key(*name)) {
            return Ok(None);
        }
        let key = self.move_type(protocol::fleet_settings::SettingsKey::MOVE_TYPE);
        let value = self.move_type(protocol::fleet_settings::Settings::MOVE_TYPE);
        let parent = Address::from_str(budget_id)
            .map_err(|e| anyhow::anyhow!("invalid budget ID {budget_id}: {e}"))?;
        let id = parent.derive_dynamic_child_id(&TypeTag::Struct(Box::new(StructTag::from_str(&key)?)), &[0]);
        self.watch_child(&id.to_string(), budget_id);
        let Some(field) = self.optional_object(&id.to_string()).await? else { return Ok(None) };
        let expected = StructTag::from_str(&format!("0x2::dynamic_field::Field<{key}, {value}>"))?;
        anyhow::ensure!(StructTag::from_str(&field.object_type).ok() == Some(expected), "unexpected fleet settings type {}", field.object_type);
        Ok(Some(field.fields["value"].clone()))
    }

    /// A dynamic field whose key struct has fields, given as their BCS.
    async fn keyed_field(&self, parent: &str, name: &str, key_bcs: &[u8]) -> anyhow::Result<Option<Value>> {
        let key = TypeTag::Struct(Box::new(
            StructTag::from_str(&self.move_type(name))
                .map_err(|e| anyhow::anyhow!("invalid key type {name}: {e}"))?,
        ));
        let parent = Address::from_str(parent)
            .map_err(|e| anyhow::anyhow!("invalid object ID {parent}: {e}"))?;
        let id = parent.derive_dynamic_child_id(&key, key_bcs);
        self.watch_child(&id.to_string(), &parent.to_string());
        Ok(self
            .optional_object(&id.to_string())
            .await?
            .map(|field| field.fields["value"].clone()))
    }

    /// Enrollment baseline: deployments created before now are never applied.
    pub async fn latest_cursor(&self) -> anyhow::Result<Option<Value>> {
        let info = self.service_info().await.map_err(status)?.into_inner();
        let tip = info
            .checkpoint_height
            .ok_or_else(|| anyhow::anyhow!("node reported no checkpoint height"))?;
        Ok(Some(json!({ "checkpoint": tip + 1 })))
    }

    /// The next page of DeploymentCreated events after `cursor`.
    pub async fn deployments(&self, cursor: &Option<Value>) -> anyhow::Result<EventPage> {
        let (start, position) = match Cursor::parse(cursor)? {
            Cursor::After(bytes, checkpoint) => (Start::After(bytes), checkpoint),
            Cursor::Checkpoint(checkpoint) => (Start::Checkpoint(checkpoint), Some(checkpoint)),
            Cursor::Legacy(digest, sequence) => {
                // Only translate the position; the next page starts after it.
                let end = self.seek_legacy(&digest, sequence).await?;
                return Ok(EventPage { events: Vec::new(), end, has_more: true });
            }
            Cursor::Genesis => (Start::Checkpoint(0), Some(0)),
        };
        self.ensure_retained(position).await?;
        let page = self.event_page(start, DEPLOYMENT_CREATED).await?;
        let mut events = Vec::new();
        for (event, watermark) in page.events {
            let fields = event.json.as_deref().map(to_json).unwrap_or(Value::Null);
            let parsed: DeploymentCreatedEvent = serde_json::from_value(fields)
                .map_err(|e| anyhow::anyhow!("undecodable DeploymentCreated event: {e}"))?;
            events.push((parsed, watermark));
        }
        Ok(EventPage { events, end: page.end, has_more: page.has_more })
    }

    /// One DNF query, with a local creator/target prefilter before any object
    /// reads. Authority is still re-read by authorized_target and reconcile.
    pub async fn discovery(
        &self,
        cursor: &Option<Value>,
        device: &str,
        operator: &str,
    ) -> anyhow::Result<DiscoveryPage> {
        let (start, position) = match Cursor::parse(cursor)? {
            Cursor::After(bytes, checkpoint) => (Start::After(bytes), checkpoint),
            Cursor::Checkpoint(checkpoint) => (Start::Checkpoint(checkpoint), Some(checkpoint)),
            _ => anyhow::bail!("combined discovery requires a migrated cursor"),
        };
        self.ensure_retained(position).await?;
        let page = self.event_page(start, "*").await?;
        let mut deployments = Vec::new();
        let mut groups = Vec::new();
        for (event, watermark) in page.events {
            let fields = event.json.as_deref().map(to_json).unwrap_or(Value::Null);
            if same_type(
                event.event_type.as_deref().unwrap_or(""),
                &self.move_type(DEPLOYMENT_CREATED),
            ) {
                let parsed: DeploymentCreatedEvent = serde_json::from_value(fields)?;
                if same_id(&parsed.creator, operator)
                    && (parsed.target_devices.iter().any(|id| same_id(id, device))
                        || !parsed.target_groups.is_empty())
                {
                    deployments.push((parsed, watermark));
                }
            } else {
                let group = fields["group_id"]
                    .as_str()
                    .ok_or_else(|| anyhow::anyhow!("invalid membership group"))?;
                let member = fields["device_id"]
                    .as_str()
                    .ok_or_else(|| anyhow::anyhow!("invalid membership device"))?;
                if same_id(member, device) {
                    groups.push(group.to_owned());
                }
            }
        }
        Ok(DiscoveryPage {
            deployments,
            groups,
            end: page.end,
            has_more: page.has_more,
        })
    }

    async fn ensure_retained(&self, position: Option<u64>) -> anyhow::Result<()> {
        if position.is_none() {
            return Ok(());
        }
        let info = self.service_info().await.map_err(status)?.into_inner();
        ensure_retained(position, info.lowest_available_checkpoint)
    }

    /// The exact resume cursor after a JSON-RPC-era cursor's event. Starting at
    /// its checkpoint, walk the filtered stream in ledger order (transaction,
    /// then event index) to that very event: earlier events in the checkpoint,
    /// even in the same transaction, must not be redelivered, since the journal
    /// only remembers deployments that were acted on. Fails rather than guess.
    async fn seek_legacy(&self, digest: &str, sequence: u64) -> anyhow::Result<Value> {
        let checkpoint = self.transaction_checkpoint(digest).await?;
        self.ensure_retained(Some(checkpoint)).await?;
        let mut start = Start::Checkpoint(checkpoint);
        loop {
            let page = self.event_page(start, DEPLOYMENT_CREATED).await?;
            for (event, watermark) in page.events {
                if let Some(found) = legacy_boundary(&event, digest, sequence, checkpoint, &watermark)? {
                    return Ok(found);
                }
            }
            anyhow::ensure!(
                page.has_more,
                "legacy event cursor {digest}#{sequence} was not found in checkpoint {checkpoint}; \
                 refusing to guess where to resume"
            );
            start = match Cursor::parse(&Some(page.end))? {
                Cursor::After(bytes, _) => Start::After(bytes),
                _ => anyhow::bail!("event stream returned an unusable resume cursor"),
            };
        }
    }

    /// One ListEvents page of `event_type` events (e.g.
    /// "deployment_manager::DeploymentCreated"), each with the cursor to
    /// resume after it.
    async fn event_page(&self, start: Start, event_type: &str) -> anyhow::Result<RawPage> {
        let mut request = proto::ListEventsRequest::default();
        request.read_mask = Some(mask(&[
            "event_type",
            "json",
            "checkpoint",
            "transaction_digest",
            "event_index",
        ]));
        let types = if event_type == "*" {
            vec![DEPLOYMENT_CREATED, DEVICE_ADDED_TO_GROUP]
        } else {
            vec![event_type]
        };
        let mut filter = proto::EventFilter::default();
        filter.terms = types
            .iter()
            .map(|name| {
                let mut literal = proto::EventLiteral::default();
                let mut by_type = proto::EventTypeFilter::default();
                by_type.event_type = Some(self.move_type(name));
                literal.predicate = Some(proto::event_literal::Predicate::EventType(by_type));
                let mut term = proto::EventTerm::default();
                term.literals = vec![literal];
                term
            })
            .collect();
        request.end_checkpoint = self.poll.lock().unwrap().event_limit;
        request.filter = Some(filter);
        let mut options = proto::QueryOptions::default();
        options.limit = Some(PAGE_SIZE);
        match start {
            Start::After(bytes) => options.after = Some(bytes.into()),
            Start::Checkpoint(checkpoint) => request.start_checkpoint = Some(checkpoint),
        }
        request.options = Some(options);
        let expected: Vec<_> = types.iter().map(|name| self.move_type(name)).collect();
        let event_limit = self.poll.lock().unwrap().event_limit;
        // Retry the entire page, discarding partial frames. The journal advances
        // only after a complete page, so interrupted streams cannot skip events.
        self.rpc_call(|mut rpc| {
            let request = request.clone();
            let expected = expected.clone();
            async move {
                self.stats.lock().unwrap().0 += 1;
                let mut stream = rpc.ledger_client().list_events(request).await?.into_inner();
                let mut events = Vec::new();
                while let Some(frame) = stream.next().await {
                    let frame = frame?;
                    self.stats.lock().unwrap().1 += prost::Message::encoded_len(&frame) as u64;
                    let watermark = frame.watermark.as_ref().ok_or_else(|| {
                        tonic::Status::failed_precondition("event frame without watermark")
                    })?;
                    let watermark = after(watermark)
                        .map_err(|e| tonic::Status::failed_precondition(e.to_string()))?;
                    if let Some(event) = frame.event {
                        if !expected.iter().any(|kind| same_type(event.event_type.as_deref().unwrap_or(""), kind)) {
                            return Err(tonic::Status::failed_precondition(
                                "event outside requested type",
                            ));
                        }
                        events.push((event, watermark.clone()));
                    }
                    if let Some(reason) = frame.end.as_ref().and_then(|e| e.reason) {
                        if !matches!(proto::QueryEndReason::try_from(reason), Ok(
                            proto::QueryEndReason::ItemLimit | proto::QueryEndReason::ScanLimit |
                            proto::QueryEndReason::CheckpointBound | proto::QueryEndReason::CursorBound |
                            proto::QueryEndReason::LedgerTip)) {
                            return Err(tonic::Status::failed_precondition("unknown event query completion reason"));
                        }
                        let has_more = matches!(
                            proto::QueryEndReason::try_from(reason),
                            Ok(proto::QueryEndReason::ItemLimit | proto::QueryEndReason::ScanLimit)
                        );
                        if !has_more && event_limit.is_some() && reason != proto::QueryEndReason::CheckpointBound as i32 {
                            return Err(tonic::Status::failed_precondition("event index has not reached the cursor-merge checkpoint fence; retry without skipping history"));
                        }
                        return Ok(RawPage {
                            events,
                            end: watermark,
                            has_more,
                        });
                    }
                }
                Err(tonic::Status::unavailable(
                    "event stream ended without completion",
                ))
            }
        })
        .await
        .map_err(|e| {
            // Public nodes reject ranges below their retained history.
            if e.code() == tonic::Code::OutOfRange {
                anyhow::Error::new(HistoryPruned { position: 0, lowest: 0 }).context(e.message().to_owned())
            } else {
                status(e)
            }
        })
    }

    async fn transaction_checkpoint(&self, digest: &str) -> anyhow::Result<u64> {
        let mut request = proto::GetTransactionRequest::default();
        request.digest = Some(digest.to_owned());
        request.read_mask = Some(mask(&["checkpoint"]));
        self.read_rpc(|mut rpc| {
            let request = request.clone();
            async move { rpc.ledger_client().get_transaction(request).await }
        })
        .await
        .map_err(status)?
        .into_inner()
        .transaction
        .and_then(|t| t.checkpoint)
        .ok_or_else(|| {
            anyhow::anyhow!(
                "cannot locate legacy event cursor {digest}; the node may have pruned it"
            )
        })
    }

    /// Whether this deployment has device inboxes and group heads (SF-125).
    pub fn has_inbox(&self) -> bool {
        crate::protocol::INBOX_TYPES
            .iter()
            .all(|name| self.manifest().type_origins.contains_key(*name))
    }

    /// The device's inbox address: derived from the registry and the device
    /// ID by `sui::derived_object` (key DerivedObjectKey<InboxKey>).
    pub fn inbox_id(&self, device: &str) -> anyhow::Result<String> {
        inbox_id(&self.manifest().shared_objects.registry.id, &self.move_type("fleet_inbox::InboxKey"), device)
    }

    /// The device's inbox, or None until its operator creates it.
    pub async fn inbox(&self, device: &str) -> anyhow::Result<Option<Inbox>> {
        let id = self.inbox_id(device)?;
        let Some(object) = self.optional_object(&id).await? else {
            return Ok(None);
        };
        anyhow::ensure!(
            same_type(&object.object_type, &self.move_type("fleet_inbox::DeviceInbox"))
                && matches!(object.owner, Owner::Shared { .. })
                && same_id(object.fields["device_id"].as_str().unwrap_or(""), device),
            "{id} is not this device's shared inbox"
        );
        Ok(Some(Inbox {
            head: parse_head(&object.fields["head"])?,
            groups: id_list(&object.fields["groups"]),
        }))
    }

    /// The group's head; an empty head before its first announcement.
    pub async fn group_head(&self, group: &str) -> anyhow::Result<Head> {
        match self.dynamic_field(group, "fleet_inbox::HeadKey").await? {
            None => Ok(Head::default()),
            Some(value) => parse_head(&value),
        }
    }

    /// Chain time (UTC ms) of the node's latest checkpoint.
    pub async fn chain_time_ms(&self) -> anyhow::Result<u64> {
        let info = self.service_info().await.map_err(status)?.into_inner();
        timestamp_ms(info.timestamp)
    }

    /// Chain time (UTC ms) from which events after `cursor` can have been
    /// created: the time of the checkpoint before the cursor's. Records
    /// created earlier were all within reach of the scan that wrote it.
    pub async fn cursor_time_ms(&self, cursor: &Option<Value>) -> anyhow::Result<u64> {
        let parsed = Cursor::parse(cursor)?;
        let checkpoint = match (boundary_checkpoint(&parsed), &parsed) {
            (Some(checkpoint), _) => checkpoint,
            (None, Cursor::Legacy(digest, _)) => self.transaction_checkpoint(digest).await?,
            _ => anyhow::bail!("the event cursor records no checkpoint"),
        };
        if checkpoint == 0 {
            return Ok(0);
        }
        let mut request = proto::GetCheckpointRequest::default();
        request.checkpoint_id = Some(proto::get_checkpoint_request::CheckpointId::SequenceNumber(checkpoint - 1));
        request.read_mask = Some(mask(&["summary.timestamp"]));
        let response = self
            .rpc_call(|mut rpc| {
                let request = request.clone();
                async move { rpc.ledger_client().get_checkpoint(request).await }
            })
            .await
            .map_err(|e| anyhow::anyhow!("cannot read checkpoint {}: {}", checkpoint - 1, e.message()))?
            .into_inner();
        timestamp_ms(response.checkpoint.and_then(|c| c.summary).and_then(|s| s.timestamp))
    }

    /// A missing membership cursor covers the type's complete history. On a
    /// pruned chain, its immutable origin package's publication is a proven
    /// lower bound: this event type could not exist before that checkpoint.
    /// Never replace an existing cursor or skip a pruned part of that history.
    async fn membership_start(&self) -> anyhow::Result<u64> {
        let info = self.service_info().await.map_err(status)?.into_inner();
        if info.lowest_available_checkpoint.unwrap_or(0) == 0 {
            return Ok(0);
        }
        let cached = self.poll.lock().unwrap().membership_birth;
        let checkpoint = match cached {
            Some(checkpoint) => checkpoint,
            None => {
                let origin = &self.manifest().type_origins[DEVICE_ADDED_TO_GROUP];
                self.package(origin).await?;
                let digest = self
                    .poll
                    .lock()
                    .unwrap()
                    .package_transactions
                    .get(origin)
                    .cloned()
                    .ok_or_else(|| anyhow::anyhow!("membership origin publication unavailable"))?;
                let checkpoint = self.transaction_checkpoint(&digest).await?;
                self.poll.lock().unwrap().membership_birth = Some(checkpoint);
                checkpoint
            }
        };
        ensure_retained(Some(checkpoint), info.lowest_available_checkpoint)?;
        Ok(checkpoint)
    }

    /// The next page of DeviceAddedToGroup events after `cursor`. A missing
    /// cursor starts no later than the type's publication; pruned history fails.
    pub async fn memberships(&self, cursor: &Option<Value>) -> anyhow::Result<MembershipPage> {
        let (start, position) = match Cursor::parse(cursor)? {
            Cursor::After(bytes, checkpoint) => (Start::After(bytes), checkpoint),
            Cursor::Checkpoint(checkpoint) => (Start::Checkpoint(checkpoint), Some(checkpoint)),
            Cursor::Genesis => (Start::Checkpoint(self.membership_start().await?), None),
            Cursor::Legacy(..) => anyhow::bail!("membership cursor cannot be a JSON-RPC cursor"),
        };
        self.ensure_retained(position).await?;
        let page = self.event_page(start, DEVICE_ADDED_TO_GROUP).await?;
        let mut events = Vec::new();
        for (event, _) in page.events {
            let fields = event.json.as_deref().map(to_json).unwrap_or(Value::Null);
            match (fields["group_id"].as_str(), fields["device_id"].as_str()) {
                (Some(group), Some(device)) => events.push((group.to_owned(), device.to_owned())),
                _ => anyhow::bail!("undecodable DeviceAddedToGroup event: {fields}"),
            }
        }
        Ok(MembershipPage { events, end: page.end, has_more: page.has_more })
    }

    /// The device's operator. An activated Device is held by the registry; its
    /// operator is whoever owns the DeviceOwnerCap that its identity names.
    /// Also checks that the Device is registered to this daemon's key.
    pub async fn operator(&self, device: &str) -> anyhow::Result<String> {
        let object = self.object(device).await?;
        anyhow::ensure!(
            same_type(&object.object_type, &self.move_type("device_registry::Device")),
            "wrong Device type"
        );
        let identity = self
            .identity(device)
            .await?
            .ok_or_else(|| anyhow::anyhow!("device has no registry identity"))?;
        self.watch_child(identity["owner_cap"].as_str().unwrap_or(""), device);
        let owner_cap = self
            .object(identity["owner_cap"].as_str().unwrap_or(""))
            .await?;
        anyhow::ensure!(
            same_type(&owner_cap.object_type, &self.move_type("device_registry::DeviceOwnerCap"))
                && same_id(owner_cap.fields["device_id"].as_str().unwrap_or(""), device),
            "identity names a wrong DeviceOwnerCap"
        );
        let owner = match &owner_cap.owner {
            Owner::Address(owner) => owner.clone(),
            Owner::Object(parent_id) => {
                // A managed owner cap is a dynamic object field, not an owned
                // wallet credential. Verify every link before attributing work.
                let origin = self
                    .manifest()
                    .type_origins
                    .get("customer::DeviceKey")
                    .ok_or_else(|| anyhow::anyhow!("managed fleet type origins are missing"))?;
                let parent = self.object(parent_id).await?;
                let expected = format!("0x2::dynamic_field::Field<0x2::dynamic_object_field::Wrapper<{origin}::customer::DeviceKey>,0x2::object::ID>");
                anyhow::ensure!(
                    StructTag::from_str(&parent.object_type).ok() == Some(StructTag::from_str(&expected)?),
                    "wrong managed custody field type"
                );
                let Owner::Object(fleet_id) = &parent.owner else {
                    anyhow::bail!("custody field has no fleet owner");
                };
                anyhow::ensure!(
                    same_id(
                        parent.fields["value"].as_str().unwrap_or(""),
                        identity["owner_cap"].as_str().unwrap_or("")
                    ) && same_id(
                        parent.fields["name"]["name"]["device_id"]
                            .as_str()
                            .unwrap_or(""),
                        device
                    ),
                    "managed custody binding mismatch"
                );
                let fleet = self.object(fleet_id).await?;
                anyhow::ensure!(
                    same_type(&fleet.object_type, &self.move_type("customer::Fleet"))
                    && json_u64(&fleet.fields["version"]) == Some(1)
                        && matches!(fleet.owner, Owner::Shared { .. })
                        && fleet.fields["devices"].as_array().is_some_and(|ids| ids
                            .iter()
                            .any(|id| same_id(id.as_str().unwrap_or(""), device))),
                    "managed fleet inventory mismatch"
                );
                let customer = self
                    .object(fleet.fields["customer_id"].as_str().unwrap_or(""))
                    .await?;
                anyhow::ensure!(
                    same_type(&customer.object_type, &self.move_type("customer::Customer"))
                    && json_u64(&customer.fields["version"]) == Some(1)
                        && matches!(customer.owner, Owner::Shared { .. })
                        && same_id(
                            customer.fields["registry_id"].as_str().unwrap_or(""),
                            &self.manifest().shared_objects.registry.id
                        )
                        && customer.fields["fleets"].as_array().is_some_and(|ids| ids
                            .iter()
                            .any(|id| same_id(id.as_str().unwrap_or(""), fleet_id))),
                    "managed customer binding mismatch"
                );
                fleet_id.clone()
            }
            _ => anyhow::bail!("DeviceOwnerCap has no operator owner"),
        };
        anyhow::ensure!(
            same_id(object.fields["device_address"].as_str().unwrap_or(""), &self.address()),
            "device key does not match registered address"
        );
        Ok(owner)
    }

    /// The group's current desired state, if this device must follow it:
    /// None when the group is gone, belongs to another operator, or no longer
    /// contains the device. A group without desired state is revision 0.
    pub async fn group_desired(
        &self,
        group: &str,
        operator: &str,
        device: &str,
    ) -> anyhow::Result<Option<crate::reconcile::GroupDesired>> {
        if !self.group_contains(group, operator, device).await? {
            return Ok(None);
        }
        let mut desired = crate::reconcile::GroupDesired {
            group: group.to_owned(),
            revision: 0,
            components: Default::default(),
        };
        if let Some(state) = self.dynamic_field(group, "desired_state::DesiredKey").await? {
            desired.revision = number(&state["revision"])
                .ok_or_else(|| anyhow::anyhow!("desired state of {group} has no revision"))?;
            let entries = state["components"]["contents"]
                .as_array()
                .ok_or_else(|| anyhow::anyhow!("undecodable desired state of {group}: {state}"))?;
            for entry in entries {
                match (entry["key"].as_str(), entry["value"].as_str()) {
                    (Some(name), Some(deployment)) => {
                        desired.components.insert(name.to_owned(), deployment.to_owned());
                    }
                    _ => anyhow::bail!("undecodable desired state of {group}: {state}"),
                }
            }
        }
        Ok(Some(desired))
    }

    /// A DeploymentRecord as its creation event would describe it, or None
    /// when it was deleted. Fails on any other type.
    pub async fn deployment(
        &self,
        id: &str,
    ) -> anyhow::Result<Option<(DeploymentCreatedEvent, Object)>> {
        let Some(record) = self.optional_object(id).await? else {
            return Ok(None);
        };
        anyhow::ensure!(
            same_type(
                &record.object_type,
                &self.move_type(protocol::deployment_manager::DeploymentRecord::MOVE_TYPE)
            ),
            "{id} is not a DeploymentRecord"
        );
        let field = |name: &str| record.fields[name].as_str().unwrap_or_default().to_owned();
        let groups = self.dynamic_field(id, "deployment_manager::TargetGroupsKey").await?;
        let devices = self.dynamic_field(id, "deployment_manager::TargetDevicesKey").await?;
        let event = DeploymentCreatedEvent {
            deployment_id: id.to_owned(),
            component_name: field("component_name"),
            version: field("version"),
            recipe_blob_id: field("recipe_blob_id"),
            artifact_sha256: field("artifact_sha256"),
            target_groups: groups.as_ref().map(id_list).unwrap_or_default(),
            target_devices: devices.as_ref().map(id_list).unwrap_or_default(),
            creator: field("creator"),
        };
        Ok(Some((event, record)))
    }

    /// Whether desired state references this record; reconciliation then
    /// applies it, never the one-shot creation-event path.
    pub async fn deployment_control(&self, deployment: &str) -> anyhow::Result<crate::control::Control> {
        match self.dynamic_field(deployment, "deployment_manager::ControlKey").await? {
            None => Ok(crate::control::Control::default()),
            Some(value) => Ok(serde_json::from_value(
                value.get("fields").cloned().unwrap_or(value)
            )?),
        }
    }

    /// Schema 3 requires a grant in the introduction-origin rollout field.
    /// Every call re-reads chain state, including after artifact downloads.
    pub async fn rollout_admission(&self, deployment: &str, device: &str) -> anyhow::Result<Option<&'static str>> {
        let record = self.object(deployment).await?;
        if number(&record.fields["schema_version"]) != Some(3) { return Ok(None); }
        // Ownership or signer trust can change while artifacts are fetched.
        // Refresh both at the same final admission boundary as the grant.
        let identity = self.identity(device).await?;
        check_identity(identity.as_ref(), &self.address())?;
        let operator = self.operator(device).await?;
        if !same_id(&operator, record.fields["creator"].as_str().unwrap_or("")) {
            return Ok(Some("rollout operator changed"));
        }
        let r = self.dynamic_field(deployment, "deployment_manager::RolloutKey").await?
            .ok_or_else(|| anyhow::anyhow!("schema-3 deployment has no rollout policy"))?;
        let r = r.get("fields").unwrap_or(&r);
        let state = number(&r["state"]).ok_or_else(|| anyhow::anyhow!("invalid rollout state"))?;
        anyhow::ensure!(state <= 3, "unsupported rollout state");
        if state == 1 { return Ok(Some("rollout paused")); }
        if state == 2 { return Ok(Some("rollout aborted")); }
        if state == 3 { return Ok(Some("rollout complete")); }
        let members = id_list(&r["members"]);
        let admitted = r["admitted_at"].as_array().ok_or_else(|| anyhow::anyhow!("invalid rollout grants"))?;
        // gRPC renders Move vector<u8> as base64; simulator/older JSON
        // renderings may use numeric arrays. Validate either representation.
        let outcomes = byte_vector(&r["outcomes"])?;
        let skipped = r["skipped"].as_array().ok_or_else(|| anyhow::anyhow!("invalid rollout exclusions"))?;
        anyhow::ensure!(!members.is_empty() && members.len() <= 512 &&
            [admitted.len(), outcomes.len(), skipped.len()].iter().all(|n| *n == members.len()),
            "invalid rollout vector lengths");
        let Some(index) = members.iter().position(|id| same_id(id, device)) else {
            return Ok(Some("outside rollout cohort snapshot"));
        };
        let grant = number(&admitted[index]).ok_or_else(|| anyhow::anyhow!("invalid rollout grant"))?;
        let outcome = outcomes[index];
        anyhow::ensure!(outcome <= 2, "invalid rollout outcome");
        let skip = skipped[index].as_bool().ok_or_else(|| anyhow::anyhow!("invalid rollout exclusion"))?;
        Ok(if grant != 0 && outcome == 0 && !skip { None } else { Some("waiting for rollout eligibility") })
    }

    pub async fn is_managed(&self, deployment: &str) -> anyhow::Result<bool> {
        Ok(self
            .dynamic_field(deployment, "deployment_manager::ManagedKey")
            .await?
            .is_some())
    }

    /// A matching ID alone is not authority to update a device. Only its current
    /// operator may deploy; shared groups must belong to that operator too.
    pub async fn authorized_target(
        &self,
        event: &DeploymentCreatedEvent,
        device: &str,
    ) -> anyhow::Result<bool> {
        let owner = &self.operator(device).await?;
        if !same_id(owner, &event.creator) {
            return Ok(false);
        }
        if event.target_devices.iter().any(|id| same_id(id, device)) {
            return Ok(true);
        }
        for id in &event.target_groups {
            if self.group_contains(id, owner, device).await? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    async fn group_contains(&self, group: &str, admin: &str, device: &str) -> anyhow::Result<bool> {
        let Some(object) = self.optional_object(group).await? else {
            return Ok(false);
        };
        if !same_type(&object.object_type, &self.move_type("device_group::DeviceGroup"))
            || !same_id(object.fields["admin"].as_str().unwrap_or(""), admin)
        {
            return Ok(false);
        }
        if id_list(&object.fields["device_ids"]).iter().any(|id| same_id(id, device)) {
            return Ok(true);
        }
        // Indexed members (SF-125) are MemberKey fields, not vector entries.
        if !self.has_inbox() {
            return Ok(false);
        }
        let member = Address::from_str(device)
            .map_err(|e| anyhow::anyhow!("invalid device ID {device}: {e}"))?;
        Ok(self
            .keyed_field(group, "device_group::MemberKey", member.inner())
            .await?
            .is_some_and(|value| value.as_bool() == Some(true)))
    }

    pub async fn validate_cap(&self, cap_id: &str, device_id: &str) -> anyhow::Result<()> {
        let cap = self.object(cap_id).await?;
        anyhow::ensure!(
            same_type(&cap.object_type, &self.move_type("device_cap::DeviceCap")),
            "wrong DeviceCap type"
        );
        anyhow::ensure!(
            matches!(&cap.owner, Owner::Address(owner) if same_id(owner, &self.address())),
            "DeviceCap is not owned by device key"
        );
        anyhow::ensure!(
            same_id(cap.fields["device_id"].as_str().unwrap_or(""), device_id),
            "DeviceCap belongs to another device"
        );
        let binding = self
            .dynamic_field(cap_id, "device_cap::DeviceAddressKey")
            .await?;
        anyhow::ensure!(
            binding
                .as_ref()
                .and_then(Value::as_str)
                .is_some_and(|signer| same_id(signer, &self.address())),
            "DeviceCap has no matching signer binding; reissue it with the upgraded contracts"
        );
        // The registry's identity decides whether this key is still trusted
        // (revocation, rotation, deregistration); reports would abort anyway.
        let identity = self.identity(device_id).await?;
        check_identity(identity.as_ref(), &self.address())
    }

    /// The device's registry identity, if it was ever activated.
    async fn identity(&self, device_id: &str) -> anyhow::Result<Option<Value>> {
        let device = Address::from_str(device_id)
            .map_err(|e| anyhow::anyhow!("invalid device ID {device_id}: {e}"))?;
        self.keyed_field(
            &self.manifest().shared_objects.registry.id,
            "device_registry::IdentityKey",
            device.inner(),
        )
        .await
    }

    pub async fn report_status(
        &self,
        deployment: &str,
        cap: &str,
        status: u8,
    ) -> anyhow::Result<String> {
        let _mutation = self.mutations.lock().await;
        let record = self.object(deployment).await?;
        let cap_object = self.object(cap).await?;
        let device_id = cap_object.fields["device_id"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("missing device_id"))?
            .to_owned();
        let direct = self
            .dynamic_field(deployment, "deployment_manager::TargetDevicesKey")
            .await?
            .unwrap_or(Value::Null);
        let registry = self.manifest().shared_objects.registry.id.clone();
        let mut objects = vec![deployment.to_owned(), registry, cap.to_owned()];
        let mut function = protocol::deployment_manager::REPORT_VERIFIED_STATUS;
        if !id_list(&direct).iter().any(|id| same_id(id, &device_id)) {
            let groups = self
                .dynamic_field(deployment, "deployment_manager::TargetGroupsKey")
                .await?
                .ok_or_else(|| anyhow::anyhow!("missing target groups"))?;
            let creator = record.fields["creator"].as_str().unwrap_or("").to_owned();
            let mut found = None;
            for id in id_list(&groups) {
                if self.group_contains(&id, &creator, &device_id).await? {
                    found = Some(id);
                    break;
                }
            }
            objects.push(found.ok_or_else(|| anyhow::anyhow!("device is no longer targeted"))?);
            function = protocol::deployment_manager::REPORT_VERIFIED_STATUS_FOR_GROUP;
        }
        let mut args: Vec<Arg> = objects.into_iter().map(Arg::Object).collect();
        let mut module = protocol::deployment_manager::MODULE;
        if self
            .manifest()
            .type_origins
            .contains_key("deployment_manager::CustomerFleetKey")
        {
            if let Some(value) = self
                .dynamic_field(deployment, "deployment_manager::CustomerFleetKey")
                .await?
            {
                let fleet_id = value
                    .as_str()
                    .ok_or_else(|| anyhow::anyhow!("invalid managed deployment binding"))?;
                anyhow::ensure!(
                    same_id(&self.operator(&device_id).await?, fleet_id),
                    "device no longer belongs to the deployment fleet"
                );
                let fleet = self.object(fleet_id).await?;
                let customer = fleet.fields["customer_id"]
                    .as_str()
                    .ok_or_else(|| anyhow::anyhow!("managed fleet has no customer"))?;
                args.splice(
                    0..0,
                    [
                        Arg::SharedRead(customer.into()),
                        Arg::SharedRead(fleet_id.into()),
                    ],
                );
                module = "customer";
                function = if function == protocol::deployment_manager::REPORT_VERIFIED_STATUS {
                    "report_status"
                } else {
                    "report_group_status"
                };
            }
        }
        args.push(Arg::Pure(vec![status]));
        let call = Call {
            module,
            function,
            args,
        };
        let transaction = self.build(&call).await?;
        self.execute(&transaction).await
    }

    /// Grant-bound post-apply signal, independent of periodic fleet-data cadence.
    pub async fn report_rollout_health(&self, deployment: &str, cap: &str, applied: bool, inventory: &crate::telemetry::Inventory, receipt: Option<&str>) -> anyhow::Result<()> {
        // A deleted historical record has no gate left to satisfy. A read
        // outage still propagates and retains the pending journal marker.
        let Some(record) = self.optional_object(deployment).await? else { return Ok(()); };
        if record.fields["schema_version"].as_str() != Some("3") && record.fields["schema_version"].as_u64() != Some(3) { return Ok(()); }
        anyhow::ensure!(protocol::ROLLOUT_HEALTH_TYPES.iter().all(|name| self.manifest().type_origins.contains_key(*name)), "rollout health requires an upgraded package and regenerated manifest");
        // Simulation can lag effects until the receipt reaches a checkpoint.
        // This is a bounded read barrier, never another status submission.
        if let Some(digest) = receipt { self.health_receipt_checkpoint(digest).await?; }
        let component = record.fields["component_name"].as_str().ok_or_else(|| anyhow::anyhow!("missing rollout component name"))?;
        let version = record.fields["version"].as_str().ok_or_else(|| anyhow::anyhow!("missing rollout component version"))?;
        let healthy = applied && crate::telemetry::rollout_healthy(inventory, deployment, component, version);
        let _mutation = self.mutations.lock().await;
        let call = Call { module: protocol::deployment_manager::MODULE, function: protocol::deployment_manager::REPORT_ROLLOUT_HEALTH, args: vec![
            Arg::Object(deployment.into()), Arg::Object(self.manifest().shared_objects.registry.id.clone()),
            Arg::Object(cap.into()), Arg::Pure(bcs::to_bytes(&healthy)?), Arg::SharedRead(CLOCK.into()),
        ] };
        let transaction = self.build(&call).await?;
        self.execute(&transaction).await?;
        Ok(())
    }

    async fn health_receipt_checkpoint(&self, digest: &str) -> anyhow::Result<()> {
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                let mut request = proto::GetTransactionRequest::default();
                request.digest = Some(digest.to_owned());
                request.read_mask = Some(mask(&["digest", "effects.status", "checkpoint"]));
                match self.rpc_call(|mut rpc| {
                    let request = request.clone();
                    async move { rpc.ledger_client().get_transaction(request).await }
                }).await {
                    Ok(response) => {
                        let tx = response.into_inner().transaction
                            .ok_or_else(|| anyhow::anyhow!("receipt read returned no transaction"))?;
                        anyhow::ensure!(execution_digest(&tx)? == digest, "receipt digest mismatch");
                        if tx.checkpoint.is_some() { return Ok(()); }
                    }
                    Err(error) if error.code() == tonic::Code::NotFound => (),
                    Err(error) => return Err(status(error)),
                }
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
        }).await.map_err(|_| anyhow::anyhow!("receipt checkpoint pending; health remains queued"))?
    }

    /// This device's SUI balance (MIST).
    pub async fn gas_balance(&self) -> anyhow::Result<u64> {
        let mut request = proto::GetBalanceRequest::default();
        request.owner = Some(self.address());
        request.coin_type = Some("0x2::sui::SUI".into());
        let balance = self
            .read_rpc(|mut rpc| {
                let request = request.clone();
                async move { rpc.state_client().get_balance(request).await }
            })
            .await
            .map_err(status)?
            .into_inner()
            .balance
            .ok_or_else(|| anyhow::anyhow!("node returned no balance"))?;
        Ok(balance.balance.unwrap_or_default())
    }

    /// The fleet budget (SF-083) and this device's current account in it
    /// (its shared `GasAccount` object ID and state), if enrolled.
    pub async fn budget(
        &self,
        budget_id: &str,
        device_id: &str,
    ) -> anyhow::Result<(crate::funding::Budget, Option<(String, crate::funding::Account)>)> {
        let object = self.object(budget_id).await?;
        anyhow::ensure!(
            same_type(&object.object_type, &self.move_type("fleet_budget::FleetBudget")),
            "{budget_id} is not a FleetBudget of this package"
        );
        anyhow::ensure!(matches!(object.owner, Owner::Shared { .. }), "the fleet budget is not shared");
        let budget = crate::funding::Budget::from_json(&object.fields)?;
        anyhow::ensure!(
            same_id(&budget.registry_id, &self.manifest().shared_objects.registry.id),
            "the fleet budget belongs to another registry"
        );
        let device = Address::from_str(device_id).map_err(|e| anyhow::anyhow!("invalid device ID {device_id}: {e}"))?;
        let Some(account_id) = self.keyed_field(budget_id, "fleet_budget::DeviceKey", device.inner()).await? else {
            return Ok((budget, None));
        };
        let account_id = account_id
            .as_str()
            .filter(|id| same_id(id, id))
            .ok_or_else(|| {
                anyhow::anyhow!("the budget's device index is not an object ID: {account_id}")
            })?
            .to_owned();
        self.watch_child(&account_id, budget_id);
        let object = self.object(&account_id).await?;
        anyhow::ensure!(
            same_type(&object.object_type, &self.move_type("fleet_budget::GasAccount")),
            "{account_id} is not a GasAccount of this package"
        );
        anyhow::ensure!(matches!(object.owner, Owner::Shared { .. }), "the gas account is not shared");
        let account = crate::funding::Account::from_json(&object.fields)?;
        anyhow::ensure!(
            same_id(&account.budget_id, budget_id) && same_id(&account.device_id, device_id),
            "the gas account belongs to another budget or device"
        );
        Ok((budget, Some((account_id, account))))
    }

    /// Draw `amount` MIST from this device's account to its address:
    /// exactly one `fleet_budget::draw_gas` call, paid by the device. The
    /// budget must be an immutable input: draws never write it.
    pub async fn draw_gas(&self, budget_id: &str, account_id: &str, cap: &str, amount: u64) -> anyhow::Result<String> {
        let _mutation = self.mutations.lock().await;
        let registry = self.manifest().shared_objects.registry.id.clone();
        let call = Call {
            module: protocol::fleet_budget::MODULE,
            function: protocol::fleet_budget::DRAW_GAS,
            args: vec![
                Arg::SharedRead(budget_id.to_owned()),
                Arg::Object(account_id.to_owned()),
                Arg::Object(registry),
                Arg::Object(cap.to_owned()),
                Arg::Pure(bcs::to_bytes(&amount)?),
                Arg::Object(CLOCK.to_owned()),
            ],
        };
        let transaction = self.build(&call).await?;
        self.execute(&transaction).await
    }

    /// One exact fleet-data health call, verified after simulation before signing.
    pub async fn report_health(&self, config: &crate::fleet_data::Config, cap: &str,
        seq: u64, message: Vec<u8>, signature: Vec<u8>) -> anyhow::Result<String> {
        let _mutation = self.mutations.lock().await;
        let call = Call { module: "fleet_data", function: "report_health", args: vec![
            Arg::SharedRead(config.fleet_id.clone()), Arg::Object(config.writer_id.clone()),
            Arg::SharedRead(config.budget_id.clone()), Arg::SharedRead(config.account_id.clone()),
            Arg::SharedRead(self.manifest().shared_objects.registry.id.clone()), Arg::Object(cap.into()),
            Arg::Pure(bcs::to_bytes(&seq)?), Arg::Pure(bcs::to_bytes(&message)?),
            Arg::Pure(bcs::to_bytes(&signature)?), Arg::SharedRead(CLOCK.into()),
        ] };
        let transaction = self.build_package(&config.package_id, &call).await?;
        self.execute(&transaction).await
    }

    /// Persist the resolved transaction before submission. Recovery resubmits
    /// exactly the same digest; it cannot buy the same archive twice.
    async fn durable_call(&self, package: &str, call: &Call, path: &std::path::Path) -> anyhow::Result<String> {
        use std::{io::Write, os::unix::fs::OpenOptionsExt};
        let _mutation = self.mutations.lock().await;
        // Resolve a persisted transaction's effects before doing anything with
        // a new gas version. Missing/pruned history is never permission to pay
        // for the archive again.
        if let Ok(bytes) = std::fs::read(path) {
            let old: Transaction = bcs::from_bytes(&bytes)?;
            check_transaction(
                &old,
                call,
                Address::from_str(package)?,
                Address::from_str(&self.address())?,
            )?;
            let digest = old.digest().to_string();
            let mut request = proto::GetTransactionRequest::default();
            request.digest = Some(digest.clone());
            request.read_mask = Some(mask(&["digest", "effects.status"]));
            match self
                .read_rpc(|mut rpc| {
                    let request = request.clone();
                    async move { rpc.ledger_client().get_transaction(request).await }
                })
                .await
            {
                Ok(response) => {
                    let executed = response
                        .into_inner()
                        .transaction
                        .ok_or_else(|| anyhow::anyhow!("missing durable transaction effects"))?;
                    anyhow::ensure!(
                        executed.digest.as_deref() == Some(digest.as_str()),
                        "durable transaction digest mismatch"
                    );
                    match execution_digest(&executed) {
                        Err(error) if error.downcast_ref::<FailedExecution>().is_some() => {
                            std::fs::remove_file(path)?;
                            std::fs::File::open(path.parent().unwrap())?.sync_all()?;
                        }
                        result => return result,
                    }
                }
                Err(error) if error.code() == tonic::Code::NotFound => {
                    let checkpoint: u64 =
                        std::fs::read_to_string(path.with_extension("checkpoint"))?.parse()?;
                    let info = self.service_info().await?.into_inner();
                    anyhow::ensure!(
                        info.lowest_available_checkpoint
                            .is_some_and(|lowest| lowest <= checkpoint),
                        "durable transaction history unavailable; owner reconciliation required"
                    );
                    let mut stale = false;
                    for coin in &old.gas_payment.objects {
                        let mut request = proto::GetObjectRequest::default();
                        request.object_id = Some(coin.object_id().to_string());
                        request.read_mask = Some(mask(&["version"]));
                        let version = self
                            .read_rpc(|mut rpc| {
                                let request = request.clone();
                                async move { rpc.ledger_client().get_object(request).await }
                            })
                            .await?
                            .into_inner()
                            .object
                            .and_then(|o| o.version)
                            .ok_or_else(|| anyhow::anyhow!("gas object version unavailable"))?;
                        anyhow::ensure!(
                            version >= coin.version(),
                            "fullnode is behind the persisted transaction"
                        );
                        stale |= version > coin.version();
                    }
                    if stale {
                        // The old transaction could have committed between the
                        // first effects read and the gas read. Once its gas is
                        // stale it cannot execute anew: resolve effects again,
                        // then recheck retention before concluding it is absent.
                        let mut request = proto::GetTransactionRequest::default();
                        request.digest = Some(digest.clone());
                        request.read_mask = Some(mask(&["digest", "effects.status"]));
                        match self
                            .read_rpc(|mut rpc| {
                                let request = request.clone();
                                async move { rpc.ledger_client().get_transaction(request).await }
                            })
                            .await
                        {
                            Ok(response) => {
                                let executed =
                                    response.into_inner().transaction.ok_or_else(|| {
                                        anyhow::anyhow!(
                                            "missing durable effects after gas reconciliation"
                                        )
                                    })?;
                                anyhow::ensure!(
                                    executed.digest.as_deref() == Some(digest.as_str()),
                                    "durable transaction digest mismatch"
                                );
                                match execution_digest(&executed) {
                                    Err(error)
                                        if error.downcast_ref::<FailedExecution>().is_some() =>
                                    {
                                        ()
                                    }
                                    result => return result,
                                }
                            }
                            Err(error) if error.code() == tonic::Code::NotFound => {
                                let info = self.service_info().await?.into_inner();
                                anyhow::ensure!(info.lowest_available_checkpoint.is_some_and(|lowest| lowest <= checkpoint), "durable transaction history became unavailable; owner reconciliation required");
                            }
                            Err(error) => return Err(error.into()),
                        }
                        std::fs::remove_file(path)?; std::fs::File::open(path.parent().unwrap())?.sync_all()?;
                    }
                }
                Err(error)=>return Err(error.into()),
            }
        }
        let transaction = match std::fs::read(path) {
            Ok(bytes) => bcs::from_bytes(&bytes)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                let info = self.service_info().await?.into_inner();
                let checkpoint = info
                    .checkpoint_height
                    .ok_or_else(|| anyhow::anyhow!("durable creation checkpoint unavailable"))?;
                let transaction = self.build_package(package, call).await?;
                let marker = path.with_extension("checkpoint");
                let temporary = marker.with_extension("tmp");
                let mut f = std::fs::OpenOptions::new()
                    .write(true)
                    .create(true)
                    .truncate(true)
                    .mode(0o600)
                    .open(&temporary)?;
                f.write_all(checkpoint.to_string().as_bytes())?;
                f.sync_all()?;
                std::fs::rename(temporary, marker)?;
                std::fs::File::open(path.parent().unwrap())?.sync_all()?;
                let temporary = path.with_extension("tmp");
                let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&temporary)?;
                f.write_all(&bcs::to_bytes(&transaction)?)?; f.sync_all()?;
                std::fs::rename(temporary,path)?; std::fs::File::open(path.parent().unwrap())?.sync_all()?;
                transaction
            }
            Err(e) => return Err(e.into()),
        };
        check_transaction(&transaction,call,Address::from_str(package)?,Address::from_str(&self.address())?)?;
        match self.execute(&transaction).await {
            Err(error) if error.downcast_ref::<FailedExecution>().is_some() => {
                // Effects prove no registration/certification/anchor committed.
                std::fs::remove_file(path)?; std::fs::File::open(path.parent().unwrap())?.sync_all()?;
                Err(error)
            }
            result => result,
        }
    }
    pub async fn register_archive(&self, data: &crate::fleet_data::Config, storage: &crate::fleet_archive::Config,
        cap: &str, blob_id: &str, root_hash: &str, size: u64, path: &std::path::Path) -> anyhow::Result<String> {
        let mut args = vec![
            Arg::Object(data.storage_id.clone()), Arg::SharedRead(data.budget_id.clone()), Arg::SharedRead(data.account_id.clone()),
            Arg::SharedRead(self.manifest().shared_objects.registry.id.clone()), Arg::Object(cap.into()), Arg::Object(storage.system_id.clone()),
            Arg::Pure(decimal_u256(blob_id)?), Arg::Pure(decimal_u256(root_hash)?), Arg::Pure(bcs::to_bytes(&size)?),
            Arg::Pure(vec![1]), Arg::SharedRead(CLOCK.into()),
        ];
        let function = if let Some(managed) = &data.managed {
            args.splice(
                0..0,
                [
                    Arg::Object(managed.customer_id.clone()),
                    Arg::SharedRead(managed.fleet_id.clone()),
                ],
            );
            "register_managed_blob"
        } else {
            "register_blob"
        };
        let call = Call {
            module: "fleet_storage",
            function,
            args,
        };
        self.durable_call(&storage.storage_package, &call, path)
            .await
    }
    pub async fn certify_archive(&self, data: &crate::fleet_data::Config, storage: &crate::fleet_archive::Config,
        blob: &str, cert: &crate::fleet_archive::Certificate, path: &std::path::Path) -> anyhow::Result<String> {
        let call = Call { module: "fleet_storage", function: "certify_blob", args: vec![
            Arg::Object(data.storage_id.clone()), Arg::SharedRead(storage.system_id.clone()),
            Arg::Pure(bcs::to_bytes(&Address::from_str(blob)?)?), Arg::Pure(bcs::to_bytes(&STANDARD.decode(&cert.signature)?)?),
            Arg::Pure(bcs::to_bytes(&STANDARD.decode(&cert.bitmap)?)?), Arg::Pure(bcs::to_bytes(&STANDARD.decode(&cert.message)?)?),
        ] };
        self.durable_call(&storage.storage_package,&call,path).await
    }
    pub async fn anchor_archive(&self, data: &crate::fleet_data::Config, cap: &str, blob: &str,
        seq: u64, kind: u8, sha256: Vec<u8>, path: &std::path::Path) -> anyhow::Result<String> {
        let call = Call { module: "fleet_data", function: "anchor", args: vec![
            Arg::SharedRead(data.fleet_id.clone()), Arg::Object(data.writer_id.clone()), Arg::SharedRead(data.budget_id.clone()),
            Arg::SharedRead(data.account_id.clone()), Arg::SharedRead(self.manifest().shared_objects.registry.id.clone()), Arg::Object(cap.into()),
            Arg::SharedRead(data.storage_id.clone()), Arg::Pure(bcs::to_bytes(&Address::from_str(blob)?)?),
            Arg::Pure(bcs::to_bytes(&seq)?), Arg::Pure(vec![kind]), Arg::Pure(bcs::to_bytes(&sha256)?), Arg::SharedRead(CLOCK.into()),
        ] };
        self.durable_call(&data.package_id,&call,path).await
    }
    pub async fn anchor_opaque_items(&self, data: &crate::fleet_data::Config, cap: &str, blob: &str,
        seq: u64, kind: u8, sha256: Vec<u8>, items: &[crate::opaque::Index], path: &std::path::Path) -> anyhow::Result<String> {
        for item in items { item.validate()?; }
        let encoded=items.iter().map(bcs::to_bytes).collect::<Result<Vec<_>,_>>()?;
        let call = Call { module: "fleet_data", function: "anchor_items", args: vec![
            Arg::SharedRead(data.fleet_id.clone()), Arg::Object(data.writer_id.clone()), Arg::SharedRead(data.budget_id.clone()),
            Arg::SharedRead(data.account_id.clone()), Arg::SharedRead(self.manifest().shared_objects.registry.id.clone()), Arg::Object(cap.into()),
            Arg::SharedRead(data.storage_id.clone()), Arg::Pure(bcs::to_bytes(&Address::from_str(blob)?)?),
            Arg::Pure(bcs::to_bytes(&seq)?), Arg::Pure(vec![kind]), Arg::Pure(bcs::to_bytes(&sha256)?),
            Arg::Pure(bcs::to_bytes(&encoded)?), Arg::SharedRead(CLOCK.into()),
        ] };
        self.durable_call(&data.package_id,&call,path).await
    }
    pub async fn verify_opaque_items(&self, data: &crate::fleet_data::Config, seq: u64, anchor_index: u64,
        items: &[crate::opaque::Index]) -> anyhow::Result<()> {
        let package=self.package(&data.package_id).await?;
        let origin=package.type_origin_table.iter().find(|o|o.module_name.as_str()=="fleet_data" && o.struct_name.as_str()=="ItemKey")
            .ok_or_else(||anyhow::anyhow!("opaque index origin missing"))?.package;
        let value_origin=package.type_origin_table.iter().find(|o|o.module_name.as_str()=="fleet_data" && o.struct_name.as_str()=="DataItem")
            .ok_or_else(||anyhow::anyhow!("opaque value origin missing"))?.package;
        let key: TypeTag=format!("{origin}::fleet_data::ItemKey").parse()?;
        #[derive(serde::Deserialize)] struct Field { id: Address, name: (u64,u64), value: (u64,crate::opaque::Index) }
        for (n,item) in items.iter().enumerate() {
            let id=Address::from_str(&data.writer_id)?.derive_dynamic_child_id(&key,&bcs::to_bytes(&(seq,n as u64))?);
            let (kind,owner,bytes)=self.move_object(&id.to_string()).await?;
            let field: Field=bcs::from_bytes(&bytes)?;
            anyhow::ensure!(same_type(&kind,&format!("0x2::dynamic_field::Field<{origin}::fleet_data::ItemKey, {value_origin}::fleet_data::DataItem>"))
                && matches!(owner,Owner::Object(p) if same_id(&p,&data.writer_id))
                && field.id==id && field.name==(seq,n as u64) && field.value.0==anchor_index && field.value.1==*item,
                "opaque index not independently acknowledged");
        }
        Ok(())
    }
    /// Resolve the new key's actual type origin; never use DeviceData's older
    /// introduction package for a type added by an upgrade.
    pub async fn compact_archive_head(&self, data: &crate::fleet_data::Config) -> anyhow::Result<Vec<u8>> {
        let package = self.package(&data.package_id).await?;
        let origins: Vec<_> = package.type_origin_table.iter().filter(|o|
            o.module_name.as_str() == "fleet_data" && o.struct_name.as_str() == "CompactHeadKey").collect();
        anyhow::ensure!(origins.len() == 1, "compact history requires an upgraded fleet-data package");
        let origin = origins[0].package;
        let value_origin: Vec<_> = package.type_origin_table.iter().filter(|o|
            o.module_name.as_str() == "fleet_data" && o.struct_name.as_str() == "CompactHead").collect();
        anyhow::ensure!(value_origin.len() == 1 && value_origin[0].package == origin, "compact history type origin mismatch");
        let key: TypeTag = format!("{}::fleet_data::CompactHeadKey",origin).parse()?;
        let parent = Address::from_str(&data.writer_id)?;
        let id = parent.derive_dynamic_child_id(&key, &[0]);
        self.watch_child(&id.to_string(), &data.writer_id);
        let Some(_) = self.optional_object(&id.to_string()).await? else { return Ok(vec![]); };
        let (kind, owner, bytes) = self.move_object(&id.to_string()).await?;
        anyhow::ensure!(matches!(owner, Owner::Object(ref parent_id) if same_id(parent_id, &data.writer_id)), "compact root is not held by this writer");
        let expected = format!("0x2::dynamic_field::Field<{}::fleet_data::CompactHeadKey,{}::fleet_data::CompactHead>",origin,origin);
        anyhow::ensure!(kind.parse::<TypeTag>()? == expected.parse::<TypeTag>()? && bytes.len() > 33
            && bytes[..32] == *id.inner() && bytes[32] == 0, "unexpected compact history field");
        let head = &bytes[33..];
        crate::fleet_data::CompactHead::parse(head)?;
        Ok(head.to_vec())
    }
    pub async fn verify_compact_archive_anchor(&self, data: &crate::fleet_data::Config,
        seq: u64, digest: &[u8]) -> anyhow::Result<()> {
        let bytes = self.compact_archive_head(data).await?;
        let head = crate::fleet_data::CompactHead::parse(&bytes)?;
        anyhow::ensure!(head.anchor.seq == seq && head.anchor.ciphertext_sha256 == digest,
            "pending compact archive is not acknowledged by chain head");
        Ok(())
    }
    pub async fn anchor_archive_compact(&self, data: &crate::fleet_data::Config, cap: &str, blob: &str,
        seq: u64, kind: u8, sha256: Vec<u8>, previous_hash: Vec<u8>, path: &std::path::Path) -> anyhow::Result<String> {
        let call = Call { module: "fleet_data", function: "anchor_compact", args: vec![
            Arg::SharedRead(data.fleet_id.clone()), Arg::Object(data.writer_id.clone()), Arg::SharedRead(data.budget_id.clone()),
            Arg::SharedRead(data.account_id.clone()), Arg::SharedRead(self.manifest().shared_objects.registry.id.clone()), Arg::Object(cap.into()),
            Arg::SharedRead(data.storage_id.clone()), Arg::Pure(bcs::to_bytes(&Address::from_str(blob)?)?),
            Arg::Pure(bcs::to_bytes(&seq)?), Arg::Pure(vec![kind]), Arg::Pure(bcs::to_bytes(&sha256)?),
            Arg::Pure(bcs::to_bytes(&previous_hash)?), Arg::SharedRead(CLOCK.into()),
        ] };
        self.durable_call(&data.package_id,&call,path).await
    }
    pub async fn requested_diagnostics(&self, data: &crate::fleet_data::Config) -> anyhow::Result<u64> {
        // DiagnosticKey was added after the original data objects. Resolve
        // its own introduction package, never reuse FleetData's origin.
        let package = self.package(&data.package_id).await?;
        let entries: Vec<_> = package
            .type_origin_table
            .iter()
            .filter(|o| {
                o.module_name.as_str() == "fleet_data" && o.struct_name.as_str() == "DiagnosticKey"
            })
            .collect();
        anyhow::ensure!(
            entries.len() == 1,
            "call package has no unique DiagnosticKey origin"
        );
        let key: TypeTag = format!("{}::fleet_data::DiagnosticKey", entries[0].package).parse()?;
        let id = Address::from_str(&data.writer_id)?.derive_dynamic_child_id(&key, &[0]);
        self.watch_child(&id.to_string(), &data.writer_id);
        let Some(field) = self.optional_object(&id.to_string()).await? else {
            return Ok(0);
        };
        number(&field.fields["value"]["seq"])
            .ok_or_else(|| anyhow::anyhow!("invalid diagnostic request"))
    }
    pub async fn verify_archive_anchor(&self, data: &crate::fleet_data::Config, index: u64, seq: u64, sha256: &[u8]) -> anyhow::Result<()> {
        let key: TypeTag = format!("{}::fleet_data::AnchorKey",data.type_origin).parse()?;
        let id = Address::from_str(&data.writer_id)?.derive_dynamic_child_id(&key,&bcs::to_bytes(&index)?);
        let field = self.object(&id.to_string()).await?;
        let anchor = &field.fields["value"];
        anyhow::ensure!(number(&anchor["seq"]) == Some(seq) && byte_vector(&anchor["ciphertext_sha256"])? == sha256, "pending archive is not acknowledged by chain anchor");
        Ok(())
    }

    /// Ask the node to resolve object versions, gas and budget for `call`,
    /// then refuse anything other than exactly that call paid by this device.
    async fn build(&self, call: &Call) -> anyhow::Result<Transaction> {
        self.build_package(&self.config.package_id, call).await
    }

    async fn build_package(&self, package_id: &str, call: &Call) -> anyhow::Result<Transaction> {
        let package = Address::from_str(package_id)
            .map_err(|e| anyhow::anyhow!("invalid package ID: {e}"))?;
        let sender = Address::from_str(&self.address())?;
        let transaction = self
            .build_rpc(|| {
                let mut builder = sui_transaction_builder::TransactionBuilder::new();
                let mut arguments = Vec::new();
                for arg in &call.args {
                    arguments.push(match arg {
                        Arg::Object(id) | Arg::SharedRead(id) => {
                            let id = Address::from_str(id)
                                .map_err(|e| anyhow::anyhow!("invalid object ID {id}: {e}"))?;
                            builder.object(sui_transaction_builder::ObjectInput::new(id))
                        }
                        Arg::Pure(bytes) => builder.pure_bytes(bytes.clone()),
                    });
                }
                builder.move_call(
                    sui_transaction_builder::Function::new(
                        package,
                        call.module.parse()?,
                        call.function.parse()?,
                    ),
                    arguments,
                );
                builder.set_sender(sender);
                Ok(builder)
            })
            .await?;
        check_transaction(&transaction, call, package, sender)?;
        Ok(transaction)
    }

    /// Enroll this device's key under an enrollment policy (SF-021): one
    /// transaction sent by the device and sponsored by the claim key, which
    /// pays its gas. Checked against the exact intended call before either
    /// key signs; success and the created objects come from the effects.
    pub async fn enroll(
        &self,
        policy: &str,
        name: &str,
        arch: &str,
        claim: &dyn identity::TxSigner,
        managed: Option<&crate::config::ManagedEnrollment>,
    ) -> anyhow::Result<Enrolled> {
        let package = Address::from_str(&self.config.package_id)?;
        let sender = Address::from_str(&self.address())?;
        let sponsor = Address::from_str(&claim.address())?;
        anyhow::ensure!(
            sender != sponsor,
            "the claim key must differ from the device key"
        );
        let registry = Address::from_str(&self.manifest().shared_objects.registry.id)?;
        let mut args = vec![
            Arg::Object(policy.into()),
            Arg::Object(registry.to_string()),
            Arg::Pure(bcs::to_bytes(&name.to_owned())?),
            Arg::Pure(bcs::to_bytes(&arch.to_owned())?),
            Arg::SharedRead(CLOCK.into()),
        ];
        let (module, function) = if let Some(binding) = managed {
            args.splice(
                0..0,
                [
                    Arg::Object(binding.customer_id.clone()),
                    Arg::Object(binding.fleet_id.clone()),
                    Arg::Object(binding.budget_id.clone()),
                ],
            );
            ("customer", "enroll_claim")
        } else {
            (protocol::enrollment::MODULE, protocol::enrollment::ENROLL)
        };
        let call = Call {
            module,
            function,
            args,
        };
        let transaction = self
            .build_rpc(|| {
                let mut builder = sui_transaction_builder::TransactionBuilder::new();
                let mut arguments = Vec::new();
                for arg in &call.args {
                    arguments.push(match arg {
                        Arg::Object(id) | Arg::SharedRead(id) => builder.object(
                            sui_transaction_builder::ObjectInput::new(Address::from_str(id)?),
                        ),
                        Arg::Pure(bytes) => builder.pure_bytes(bytes.clone()),
                    });
                }
                builder.move_call(
                    sui_transaction_builder::Function::new(
                        package,
                        module.parse()?,
                        function.parse()?,
                    ),
                    arguments,
                );
                builder.set_sender(sender);
                builder.set_sponsor(sponsor);
                Ok(builder)
            })
            .await?;
        check_call_transaction(&transaction, &call, package, sender, sponsor)?;
        anyhow::ensure!(
            transaction.gas_payment.budget <= MAX_ENROLL_BUDGET,
            "enrollment gas exceeds ceiling"
        );

        let bytes = bcs::to_bytes(&transaction)?;
        let signature = |value: Vec<u8>| {
            let mut out = proto::UserSignature::default();
            let mut bcs = proto::Bcs::default();
            bcs.value = Some(value.into());
            out.bcs = Some(bcs);
            out
        };
        let mut request = proto::ExecuteTransactionRequest::default();
        let mut tx = proto::Transaction::default();
        let mut tx_bcs = proto::Bcs::default();
        tx_bcs.value = Some(bytes.clone().into());
        tx.bcs = Some(tx_bcs);
        request.transaction = Some(tx);
        request.signatures = vec![
            signature(self.signer.sign(&bytes)?),
            signature(claim.sign(&bytes)?),
        ];
        request.read_mask = Some(mask(&["digest", "effects.status", "events"]));
        let executed = self
            .rpc_call(|mut rpc| {
                let request = request.clone();
                async move { rpc.execution_client().execute_transaction(request).await }
            })
            .await
            .map_err(status)?
            .into_inner()
            .transaction
            .ok_or_else(|| anyhow::anyhow!("execution returned no transaction"))?;
        let digest = execution_digest(&executed)?;
        let expected = self.move_type(protocol::enrollment::DeviceEnrolled::MOVE_TYPE);
        let event = executed
            .events
            .iter()
            .flat_map(|events| events.events.iter())
            .find(|event| same_type(event.event_type.as_deref().unwrap_or(""), &expected))
            .and_then(|event| event.json.as_deref().map(to_json))
            .ok_or_else(|| anyhow::anyhow!("enrollment {digest} emitted no DeviceEnrolled event"))?;
        let field = |name: &str| event[name].as_str().map(str::to_owned).ok_or_else(|| anyhow::anyhow!("DeviceEnrolled without {name}"));
        let enrolled = Enrolled { digest, device_object_id: field("device_id")?, device_cap_id: field("device_cap")?, device_address: field("device_address")?, owner_cap_id: field("owner_cap")? };
        anyhow::ensure!(same_id(&enrolled.device_address, &self.address()), "enrolled another key");
        Ok(enrolled)
    }

    async fn execute(&self, transaction: &Transaction) -> anyhow::Result<String> {
        let bytes = bcs::to_bytes(transaction)?;
        let mut request = proto::ExecuteTransactionRequest::default();
        let mut tx = proto::Transaction::default();
        let mut bcs = proto::Bcs::default();
        bcs.value = Some(bytes.clone().into());
        tx.bcs = Some(bcs);
        request.transaction = Some(tx);
        let mut signature = proto::UserSignature::default();
        let mut bcs = proto::Bcs::default();
        bcs.value = Some(self.signer.sign(&bytes)?.into());
        signature.bcs = Some(bcs);
        request.signatures = vec![signature];
        request.read_mask = Some(mask(&["digest", "effects.status"]));
        let executed = self
            .rpc_call(|mut rpc| {
                let request = request.clone();
                async move { rpc.execution_client().execute_transaction(request).await }
            })
            .await
            .map_err(status)?
            .into_inner()
            .transaction
            .ok_or_else(|| anyhow::anyhow!("execution returned no transaction"))?;
        execution_digest(&executed)
    }
}

/// Canonical little-endian BCS u256 without a platform bigint dependency.
fn decimal_u256(text: &str) -> anyhow::Result<Vec<u8>> {
    anyhow::ensure!(!text.is_empty() && text.len() <= 78 && text.bytes().all(|b| b.is_ascii_digit()) && (text == "0" || !text.starts_with('0')), "invalid u256");
    let mut out = vec![0u8;32];
    for digit in text.bytes() { let mut carry = u16::from(digit-b'0'); for byte in &mut out {
        carry += u16::from(*byte)*10; *byte = carry as u8; carry >>= 8;
    } anyhow::ensure!(carry == 0,"u256 overflow"); }
    Ok(out)
}

/// Whether the registry still trusts `address` for this device.
fn check_identity(identity: Option<&Value>, address: &str) -> anyhow::Result<()> {
    let identity = identity.ok_or_else(|| {
        anyhow::anyhow!("device has no registry identity; its owner must call device_registry::activate_device")
    })?;
    anyhow::ensure!(
        identity["revoked"] != Value::Bool(true),
        "device identity is revoked or deregistered; its owner must reinstate it before it reports again"
    );
    anyhow::ensure!(
        same_id(identity["signer"].as_str().unwrap_or(""), address),
        "device key was rotated away from this key; install the new key and its DeviceCap"
    );
    Ok(())
}

/// The only transactions this daemon signs: one Move call with exactly these
/// arguments (a status report, or a gas draw from the fleet budget).
struct Call {
    module: &'static str,
    function: &'static str,
    args: Vec<Arg>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Arg {
    Object(String),
    /// A shared object the call only reads; it must be an immutable input.
    SharedRead(String),
    /// BCS of a pure value.
    Pure(Vec<u8>),
}

/// The node resolved gas and versions; it must not have changed anything
/// else. A compromised RPC endpoint could otherwise obtain a signature for an
/// arbitrary transaction.
fn check_transaction(
    transaction: &Transaction,
    call: &Call,
    package: Address,
    sender: Address,
) -> anyhow::Result<()> {
    check_call_transaction(transaction, call, package, sender, sender)
}
fn check_call_transaction(
    transaction: &Transaction,
    call: &Call,
    package: Address,
    sender: Address,
    gas_owner: Address,
) -> anyhow::Result<()> {
    anyhow::ensure!(
        transaction.sender == sender,
        "simulated transaction has another sender"
    );
    anyhow::ensure!(
        transaction.gas_payment.owner == gas_owner,
        "simulated transaction is paid by another address"
    );
    anyhow::ensure!(
        transaction.gas_payment.budget <= MAX_GAS_BUDGET,
        "simulated gas budget {} exceeds the {MAX_GAS_BUDGET} MIST limit",
        transaction.gas_payment.budget
    );
    let sui_sdk_types::TransactionKind::ProgrammableTransaction(ptb) = &transaction.kind else {
        anyhow::bail!("simulated transaction is not a programmable transaction");
    };
    let [Command::MoveCall(move_call)] = ptb.commands.as_slice() else {
        anyhow::bail!("simulated transaction must contain exactly the one status call");
    };
    anyhow::ensure!(
        move_call.package == package
            && move_call.module.as_str() == call.module
            && move_call.function.as_str() == call.function
            && move_call.type_arguments.is_empty()
            && move_call.arguments.len() == call.args.len(),
        "simulated transaction calls something else"
    );
    anyhow::ensure!(
        ptb.inputs.len() <= move_call.arguments.len(),
        "simulated transaction has extra inputs"
    );
    // Equal pure BCS values can share a builder input. Require every input
    // to be referenced and still compare each argument's exact bytes below.
    for index in 0..ptb.inputs.len() {
        anyhow::ensure!(move_call.arguments.iter().any(|a| matches!(a, Argument::Input(i) if *i as usize == index)),
            "simulated transaction has unused inputs");
    }
    for (index, argument) in move_call.arguments.iter().enumerate() {
        let Argument::Input(input) = argument else {
            anyhow::bail!("simulated call argument is not a transaction input");
        };
        let input = ptb
            .inputs
            .get(*input as usize)
            .ok_or_else(|| anyhow::anyhow!("simulated call refers to a missing input"))?;
        match (&call.args[index], input) {
            (Arg::Object(id), Input::ImmutableOrOwned(reference)) => anyhow::ensure!(
                same_id(&reference.object_id().to_string(), id),
                "simulated call swapped object argument {index}"
            ),
            (Arg::Object(id), Input::Shared(shared)) => anyhow::ensure!(
                same_id(&shared.object_id().to_string(), id),
                "simulated call swapped object argument {index}"
            ),
            (Arg::SharedRead(id), Input::Shared(shared)) => anyhow::ensure!(
                same_id(&shared.object_id().to_string(), id) && !shared.mutability().is_mutable(),
                "simulated call swapped or writes shared argument {index}"
            ),
            (Arg::Pure(expected), Input::Pure(value)) => anyhow::ensure!(
                value.as_slice() == expected.as_slice(),
                "simulated call changed pure argument {index}"
            ),
            _ => anyhow::bail!("simulated call argument {index} has an unexpected kind"),
        }
    }
    Ok(())
}

#[derive(Debug)]
struct FailedExecution;
impl std::fmt::Display for FailedExecution { fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("chain execution failed; no fleet mutation committed") } }
impl std::error::Error for FailedExecution {}

fn execution_digest(executed: &proto::ExecutedTransaction) -> anyhow::Result<String> {
    let status = executed.effects.as_ref().and_then(|e| e.status.as_ref());
    if status.and_then(|s| s.success) == Some(false) { return Err(FailedExecution.into()); }
    anyhow::ensure!(
        status.and_then(|s| s.success) == Some(true),
        "transaction failed: {}",
        status
            .and_then(|s| s.error.as_ref())
            .and_then(|e| e.description.clone())
            .unwrap_or_else(|| "no successful execution status".into())
    );
    executed
        .digest
        .clone()
        .ok_or_else(|| anyhow::anyhow!("missing transaction digest"))
}

#[cfg(test)]
mod tests {
    #[test]
    fn fallback_only_retries_transport_failures() {
        for code in [
            tonic::Code::Unavailable,
            tonic::Code::ResourceExhausted,
            tonic::Code::DeadlineExceeded,
        ] {
            assert!(retryable(&tonic::Status::new(code, "injected")));
            let flattened = sui_transaction_builder::Error::Input(format!(
                "error simulating transaction: {}",
                tonic::Status::new(code, "injected")
            ));
            assert_eq!(builder_status(flattened).code(), code);
        }
        assert!(retryable(&tonic::Status::unknown("HTTP status 429")));
        for code in [
            tonic::Code::FailedPrecondition,
            tonic::Code::PermissionDenied,
            tonic::Code::InvalidArgument,
            tonic::Code::NotFound,
        ] {
            assert!(!retryable(&tonic::Status::new(code, "unavailable 429")));
        }
        assert!(!retryable(&builder_status(
            sui_transaction_builder::Error::Input("unavailable 429".into())
        )));
    }
    #[test]
    fn protobuf_byte_vectors_are_base64_or_exact_bytes() {
        assert_eq!(super::byte_vector(&serde_json::json!("AAEC")).unwrap(), vec![0,1,2]);
        assert_eq!(super::byte_vector(&serde_json::json!([0.0,1.0,2.0])).unwrap(), vec![0,1,2]);
        for value in [serde_json::json!([256]), serde_json::json!([0.5]), serde_json::json!("!")] {
            assert!(super::byte_vector(&value).is_err());
        }
    }
    #[test]
    fn protobuf_numbers_require_exact_safe_integers() {
        use serde_json::json;
        assert_eq!(super::number(&json!(0.0)), Some(0));
        assert_eq!(super::number(&json!(255.0)), Some(255));
        assert_eq!(super::number(&json!("18446744073709551615")), Some(u64::MAX));
        for value in [json!(-1.0), json!(0.5), json!(9_007_199_254_740_992.0), json!("invalid")] {
            assert_eq!(super::number(&value), None);
        }
    }
    use super::*;

    #[test]
    fn current_contract_event_decodes() {
        let event: DeploymentCreatedEvent = serde_json::from_value(json!({
            "deployment_id":"0x1", "component_name":"app", "version":"1.0.0", "recipe_blob_id":"recipe",
            "artifact_sha256":"a".repeat(64), "target_groups":[], "target_devices":["0x2"], "creator":"0x3"
        })).unwrap();
        assert_eq!(event.recipe_blob_id, "recipe");
        assert!(event.target_devices.iter().any(|id| same_id(id, "0x02")));
    }

    #[test]
    fn digest_is_not_proof_of_success() {
        let mut executed = proto::ExecutedTransaction::default();
        executed.digest = Some("abc".into());
        let mut effects = proto::TransactionEffects::default();
        let mut status = proto::ExecutionStatus::default();
        status.success = Some(false);
        effects.status = Some(status.clone());
        executed.effects = Some(effects.clone());
        assert!(execution_digest(&executed).is_err());
        executed.effects = None;
        assert!(execution_digest(&executed).is_err(), "missing effects are not success");
        status.success = Some(true);
        effects.status = Some(status);
        executed.effects = Some(effects);
        assert_eq!(execution_digest(&executed).unwrap(), "abc");
    }

    #[test]
    fn id_normalization_rejects_invalid_identifiers() {
        assert!(same_id("0x1", "0x01"));
        assert!(!same_id("", ""));
        assert!(!same_id("0xzz", "0xzz"));
    }

    #[test]
    fn chain_identifier_matches_known_networks() {
        // Public genesis checkpoint digests (gRPC chain_id) and their
        // JSON-RPC/manifest chain identifiers.
        assert_eq!(
            chain_identifier("4btiuiMPvEENsttpZC7CZ53DruC3MAgfznDbASZ7DR6S").unwrap(),
            "35834a8a"
        );
        assert_eq!(
            chain_identifier("69WiPg3DAQiwdxfncX6wYQ2siKwAe6L9BZthQea3JNMD").unwrap(),
            "4c78adac"
        );
        assert!(chain_identifier("not-a-digest").is_err());
    }

    #[test]
    fn cursors_parse_from_every_journal_format() {
        assert_eq!(Cursor::parse(&None).unwrap(), Cursor::Genesis);
        assert_eq!(
            Cursor::parse(&Some(json!({"checkpoint": 7}))).unwrap(),
            Cursor::Checkpoint(7)
        );
        let mut watermark = proto::Watermark::default();
        watermark.cursor = Some(vec![1, 2, 3].into());
        assert_eq!(
            Cursor::parse(&Some(after(&watermark).unwrap())).unwrap(),
            Cursor::After(vec![1, 2, 3], None)
        );
        watermark.checkpoint = Some(9);
        assert_eq!(
            Cursor::parse(&Some(after(&watermark).unwrap())).unwrap(),
            Cursor::After(vec![1, 2, 3], Some(9))
        );
        assert!(after(&proto::Watermark::default()).is_err());
        assert_eq!(
            Cursor::parse(&Some(json!({"txDigest": "D1", "eventSeq": "3"}))).unwrap(),
            Cursor::Legacy("D1".into(), 3)
        );
        assert!(Cursor::parse(&Some(json!({"unknown": 1}))).is_err());
    }

    #[test]
    fn legacy_boundary_is_the_exact_event() {
        let event = |checkpoint: u64, digest: &str, index: u32| {
            let mut event = proto::Event::default();
            event.checkpoint = Some(checkpoint);
            event.transaction_digest = Some(digest.into());
            event.event_index = Some(index);
            event
        };
        let mark = json!({"after": "AA=="});
        // Earlier transaction, or an earlier event of the same transaction: keep seeking.
        assert_eq!(legacy_boundary(&event(7, "A", 0), "B", 1, 7, &mark).unwrap(), None);
        assert_eq!(legacy_boundary(&event(7, "B", 0), "B", 1, 7, &mark).unwrap(), None);
        // The event itself: resume strictly after it.
        assert_eq!(legacy_boundary(&event(7, "B", 1), "B", 1, 7, &mark).unwrap(), Some(mark.clone()));
        // Past its checkpoint without meeting it: refuse.
        assert!(legacy_boundary(&event(8, "C", 0), "B", 1, 7, &mark).is_err());
        assert!(Cursor::parse(&Some(json!({"txDigest": "D1"}))).is_err(), "eventSeq is required");
    }

    #[test]
    fn inbox_addresses_match_the_sdk_derivation() {
        // The same vector as @mysten/sui deriveObjectID and deployment_manifest.inbox_id.
        assert_eq!(
            inbox_id("0x1", "0x125::fleet_inbox::InboxKey", "0xde").unwrap(),
            "0x0085d65fcfe36c94b1bbe99a04811031070b86693e13140f71a8f27ddfb8dfac"
        );
        assert_ne!(inbox_id("0x1", "0x125::fleet_inbox::InboxKey", "0xdf").unwrap(),
            inbox_id("0x1", "0x125::fleet_inbox::InboxKey", "0xde").unwrap());
    }

    #[test]
    fn heads_decode_from_either_rendering() {
        let head = parse_head(&json!({"seq": "3", "latest": "0xb", "next_start_ms": "90",
            "components": {"contents": [{"key": "app", "value": "0xb"}, {"key": "db", "value": "0xd"}]}}))
            .unwrap();
        assert_eq!((head.seq, head.next_start_ms, head.components.len()), (3, 90, 2));
        assert_eq!(head.components["app"], "0xb");
        let nested = parse_head(&json!({"fields": {"seq": 1, "next_start_ms": 0,
            "components": {"fields": {"contents": [{"fields": {"key": "app", "value": "0xa"}}]}}}})).unwrap();
        assert_eq!(nested.components["app"], "0xa");
        assert!(parse_head(&json!({"seq": "1", "next_start_ms": "0", "components": {"contents": [{"key": "app"}]}})).is_err());
        assert!(parse_head(&json!({"next_start_ms": "0", "components": {"contents": []}})).is_err());
    }

    #[test]
    fn cursor_boundaries_come_from_the_cursor_or_a_lookup() {
        let parse = |value: Value| Cursor::parse(&Some(value)).unwrap();
        assert_eq!(boundary_checkpoint(&Cursor::parse(&None).unwrap()), Some(0));
        assert_eq!(boundary_checkpoint(&parse(json!({"checkpoint": 42}))), Some(42));
        assert_eq!(boundary_checkpoint(&parse(json!({"after": "AQID", "checkpoint": 7}))), Some(7));
        // Neither records where it is: a JSON-RPC cursor needs its transaction.
        assert_eq!(boundary_checkpoint(&parse(json!({"after": "AQID"}))), None);
        assert_eq!(boundary_checkpoint(&parse(json!({"txDigest": "d", "eventSeq": "0"}))), None);
        let at = |seconds, nanos| Some(prost_types::Timestamp { seconds, nanos });
        assert_eq!(timestamp_ms(at(1_700_000_000, 250_000_000)).unwrap(), 1_700_000_000_250);
        assert!(timestamp_ms(at(-1, 0)).is_err() && timestamp_ms(None).is_err());
    }

    #[test]
    fn pruned_history_stops_instead_of_skipping() {
        ensure_retained(Some(100), Some(100)).unwrap();
        ensure_retained(Some(100), None).unwrap();
        ensure_retained(None, Some(100)).unwrap();
        let error = ensure_retained(Some(99), Some(100)).unwrap_err();
        // Typed, so a device with an inbox can resynchronize from objects.
        assert!(error.downcast_ref::<HistoryPruned>().is_some());
        let error = error.to_string();
        assert!(error.contains("pruned") && error.contains("nothing is skipped"), "{error}");
        // From genesis on a pruned node is pruned too.
        assert!(ensure_retained(Some(0), Some(1)).is_err());
    }

    #[test]
    fn only_an_active_identity_for_this_key_is_trusted() {
        let key = "0xa0";
        let active = json!({"signer": "0x00a0", "owner_cap": "0xc1", "revoked": false});
        check_identity(Some(&active), key).unwrap();
        let error = check_identity(None, key).unwrap_err().to_string();
        assert!(error.contains("activate_device"), "{error}");
        let revoked = json!({"signer": "0xa0", "owner_cap": "0xc1", "revoked": true});
        assert!(check_identity(Some(&revoked), key).unwrap_err().to_string().contains("revoked"));
        let rotated = json!({"signer": "0xb1", "owner_cap": "0xc1", "revoked": false});
        assert!(check_identity(Some(&rotated), key).unwrap_err().to_string().contains("rotated"));
    }

    fn status_call() -> (Transaction, Call, Address, Address) {
        let package = Address::from_str("0x3").unwrap();
        let sender = Address::from_str("0xa0").unwrap();
        let mut builder = sui_transaction_builder::TransactionBuilder::new();
        let record = builder.object(sui_transaction_builder::ObjectInput::shared(
            Address::from_str("0x10").unwrap(),
            1,
            true,
        ));
        let registry = builder.object(sui_transaction_builder::ObjectInput::shared(
            Address::from_str("0x13").unwrap(),
            1,
            false,
        ));
        let cap = builder.object(sui_transaction_builder::ObjectInput::owned(
            Address::from_str("0x11").unwrap(),
            2,
            Digest::ZERO,
        ));
        let value = builder.pure(&1u8);
        builder.move_call(
            sui_transaction_builder::Function::new(
                package,
                "deployment_manager".parse().unwrap(),
                "report_verified_status".parse().unwrap(),
            ),
            vec![record, registry, cap, value],
        );
        builder.set_sender(sender);
        builder.set_gas_budget(10_000_000);
        builder.set_gas_price(1000);
        builder.add_gas_objects([sui_transaction_builder::ObjectInput::owned(
            Address::from_str("0x12").unwrap(),
            3,
            Digest::ZERO,
        )]);
        let call = Call {
            module: protocol::deployment_manager::MODULE,
            function: protocol::deployment_manager::REPORT_VERIFIED_STATUS,
            args: report_args(&["0x10", "0x13", "0x11"], 1),
        };
        (builder.try_build().unwrap(), call, package, sender)
    }

    fn report_args(objects: &[&str], status: u8) -> Vec<Arg> {
        let mut args: Vec<Arg> = objects.iter().map(|id| Arg::Object((*id).into())).collect();
        args.push(Arg::Pure(vec![status]));
        args
    }

    #[test]
    fn managed_claim_verifies_exact_objects_pure_values_and_sponsor_before_signing() {
        use sui_transaction_builder::{Function, ObjectInput, TransactionBuilder};
        let a = |value: &str| Address::from_str(value).unwrap();
        let package = a("0x3");
        let sender = a("0xa0");
        let sponsor = a("0xcc");
        let mut b = TransactionBuilder::new();
        let mut arguments = Vec::new();
        for id in ["0xc0", "0xf0", "0xb0", "0xe0", "0x13"] {
            arguments.push(b.object(ObjectInput::shared(a(id), 1, true)));
        }
        arguments.push(b.pure(&"Pi".to_string()));
        arguments.push(b.pure(&"x86_64".to_string()));
        arguments.push(b.object(ObjectInput::shared(a("0x6"), 1, false)));
        b.move_call(
            Function::new(
                package,
                "customer".parse().unwrap(),
                "enroll_claim".parse().unwrap(),
            ),
            arguments,
        );
        b.set_sender(sender);
        b.set_sponsor(sponsor);
        b.set_gas_budget(10_000_000);
        b.set_gas_price(1000);
        b.add_gas_objects([ObjectInput::owned(a("0x12"), 1, Digest::ZERO)]);
        let tx = b.try_build().unwrap();
        let mut args: Vec<_> = ["0xc0", "0xf0", "0xb0", "0xe0", "0x13"]
            .into_iter()
            .map(|id| Arg::Object(id.into()))
            .collect();
        args.extend([
            Arg::Pure(bcs::to_bytes(&"Pi".to_string()).unwrap()),
            Arg::Pure(bcs::to_bytes(&"x86_64".to_string()).unwrap()),
            Arg::SharedRead("0x6".into()),
        ]);
        let call = Call {
            module: "customer",
            function: "enroll_claim",
            args,
        };
        check_call_transaction(&tx, &call, package, sender, sponsor).unwrap();
        for index in 0..5 {
            let mut wrong = tx.clone();
            let sui_sdk_types::TransactionKind::ProgrammableTransaction(ptb) = &mut wrong.kind
            else {
                panic!()
            };
            ptb.inputs[index] = Input::Shared(sui_sdk_types::SharedInput::new(a("0xbad"), 1, true));
            assert!(check_call_transaction(&wrong, &call, package, sender, sponsor).is_err());
        }
        let mut wrong = tx.clone();
        wrong.gas_payment.owner = sender;
        assert!(check_call_transaction(&wrong, &call, package, sender, sponsor).is_err());
        let mut wrong = tx.clone();
        let sui_sdk_types::TransactionKind::ProgrammableTransaction(ptb) = &mut wrong.kind else {
            panic!()
        };
        ptb.inputs[5] = Input::Pure(bcs::to_bytes(&"Substituted".to_string()).unwrap());
        assert!(check_call_transaction(&wrong, &call, package, sender, sponsor).is_err());
        let mut wrong = tx.clone();
        let sui_sdk_types::TransactionKind::ProgrammableTransaction(ptb) = &mut wrong.kind else {
            panic!()
        };
        ptb.inputs[7] = Input::Shared(sui_sdk_types::SharedInput::new(a("0x6"), 1, true));
        assert!(check_call_transaction(&wrong, &call, package, sender, sponsor).is_err());
    }

    #[test]
    fn signs_only_the_exact_intended_call() {
        let (tx, call, package, sender) = status_call();
        check_transaction(&tx, &call, package, sender).unwrap();

        let mut other = tx.clone();
        other.sender = Address::from_str("0xbad").unwrap();
        assert!(check_transaction(&other, &call, package, sender).is_err());

        let mut other = tx.clone();
        other.gas_payment.owner = Address::from_str("0xbad").unwrap();
        assert!(check_transaction(&other, &call, package, sender).is_err());

        let mut other = tx.clone();
        other.gas_payment.budget = MAX_GAS_BUDGET + 1;
        assert!(check_transaction(&other, &call, package, sender).is_err());

        let wrong_status = Call { args: report_args(&["0x10", "0x13", "0x11"], 2), ..call };
        assert!(check_transaction(&tx, &wrong_status, package, sender).is_err());
        let call = Call { args: report_args(&["0x10", "0x13", "0x11"], 1), ..wrong_status };

        let swapped = Call { args: report_args(&["0x10", "0x13", "0x99"], 1), ..call };
        assert!(check_transaction(&tx, &swapped, package, sender).is_err());
        let call = Call { args: report_args(&["0x10", "0x13", "0x11"], 1), ..swapped };
        let other_module = Call { module: protocol::fleet_budget::MODULE, ..call };
        assert!(check_transaction(&tx, &other_module, package, sender).is_err());
        let call = Call { module: protocol::deployment_manager::MODULE, ..other_module };
        let unverified = Call { function: protocol::deployment_manager::REPORT_STATUS, ..call };
        assert!(check_transaction(&tx, &unverified, package, sender).is_err());
        let call = Call { function: protocol::deployment_manager::REPORT_VERIFIED_STATUS, ..unverified };

        assert!(check_transaction(&tx, &call, Address::from_str("0x4").unwrap(), sender).is_err());

        let mut extra = tx.clone();
        if let sui_sdk_types::TransactionKind::ProgrammableTransaction(ptb) = &mut extra.kind {
            ptb.commands.push(ptb.commands[0].clone());
        }
        assert!(check_transaction(&extra, &call, package, sender).is_err());
    }

    #[test]
    fn pure_input_deduplication_keeps_exact_call_and_unused_input_guards() {
        use sui_transaction_builder::{TransactionBuilder, Function, ObjectInput};
        let package: Address = "0x3".parse().unwrap();
        let sender: Address = "0xa0".parse().unwrap();
        let mut b = TransactionBuilder::new();
        let kind = b.pure(&0u8); let previous = b.pure(&Vec::<u8>::new());
        b.move_call(Function::new(package,"fleet_data".parse().unwrap(),"anchor_compact".parse().unwrap()),vec![kind,previous]);
        b.set_sender(sender); b.set_gas_budget(10_000_000); b.set_gas_price(1000);
        b.add_gas_objects([ObjectInput::owned("0x12".parse().unwrap(),1,Digest::ZERO)]);
        let tx=b.try_build().unwrap();
        let call=Call { module:"fleet_data",function:"anchor_compact",args:vec![Arg::Pure(vec![0]),Arg::Pure(vec![0])] };
        check_transaction(&tx,&call,package,sender).unwrap();
        let mut extra=tx.clone();
        if let sui_sdk_types::TransactionKind::ProgrammableTransaction(p)=&mut extra.kind {p.inputs.push(Input::Pure(vec![42]));}
        assert!(check_transaction(&extra,&call,package,sender).is_err());
        let changed=Call { args:vec![Arg::Pure(vec![0]),Arg::Pure(vec![1])],..call };
        assert!(check_transaction(&tx,&changed,package,sender).is_err());
    }
    #[test]
    fn signs_only_the_exact_intended_gas_draw() {
        let package = Address::from_str("0x3").unwrap();
        let sender = Address::from_str("0xa0").unwrap();
        let build = |amount: u64, budget_mutable: bool| {
            let mut builder = sui_transaction_builder::TransactionBuilder::new();
            let a = Address::from_str;
            let budget = builder.object(sui_transaction_builder::ObjectInput::shared(a("0xb0").unwrap(), 1, budget_mutable));
            let account = builder.object(sui_transaction_builder::ObjectInput::shared(a("0xac").unwrap(), 1, true));
            let registry = builder.object(sui_transaction_builder::ObjectInput::shared(a("0x13").unwrap(), 1, false));
            let cap = builder.object(sui_transaction_builder::ObjectInput::owned(a("0x11").unwrap(), 2, Digest::ZERO));
            let value = builder.pure(&amount);
            let clock = builder.object(sui_transaction_builder::ObjectInput::shared(a("0x6").unwrap(), 1, false));
            builder.move_call(
                sui_transaction_builder::Function::new(package, "fleet_budget".parse().unwrap(), "draw_gas".parse().unwrap()),
                vec![budget, account, registry, cap, value, clock],
            );
            builder.set_sender(sender);
            builder.set_gas_budget(10_000_000);
            builder.set_gas_price(1000);
            builder.add_gas_objects([sui_transaction_builder::ObjectInput::owned(a("0x12").unwrap(), 3, Digest::ZERO)]);
            builder.try_build().unwrap()
        };
        let call = |amount: u64, budget: &str, account: &str| Call {
            module: protocol::fleet_budget::MODULE,
            function: protocol::fleet_budget::DRAW_GAS,
            args: vec![
                Arg::SharedRead(budget.into()),
                Arg::Object(account.into()),
                Arg::Object("0x13".into()),
                Arg::Object("0x11".into()),
                Arg::Pure(bcs::to_bytes(&amount).unwrap()),
                Arg::Object(CLOCK.into()),
            ],
        };
        check_transaction(&build(5_000, false), &call(5_000, "0xb0", "0xac"), package, sender).unwrap();
        // A node that raises the amount or swaps the budget or account gets no signature.
        assert!(check_transaction(&build(5_001, false), &call(5_000, "0xb0", "0xac"), package, sender).is_err());
        assert!(check_transaction(&build(5_000, false), &call(5_000, "0xb1", "0xac"), package, sender).is_err());
        assert!(check_transaction(&build(5_000, false), &call(5_000, "0xb0", "0xad"), package, sender).is_err());
        // Nor one that takes the fleet budget mutably, contending with every device.
        let error = check_transaction(&build(5_000, true), &call(5_000, "0xb0", "0xac"), package, sender).unwrap_err();
        assert!(error.to_string().contains("writes shared argument 0"), "{error}");
    }
}

#[cfg(test)]
#[path = "poll_tests.rs"]
mod poll_tests;
