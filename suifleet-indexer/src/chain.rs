//! Sui gRPC v2 access for the indexer (the daemon's SDK and conventions):
//! chain identity, package type origins, objects and dynamic fields, typed
//! event pages with watermark cursors, and transaction build/execute.
use base64::{engine::general_purpose::STANDARD, Engine};
use futures_util::StreamExt;
use serde_json::{json, Value};
use std::{collections::BTreeMap, str::FromStr, time::Duration};
use sui_rpc::field::{FieldMask, FieldMaskUtil};
use sui_rpc::proto::sui::rpc::v2 as proto;
use sui_sdk_types::{Address, Digest, StructTag, Transaction, TypeTag};

pub const PAGE_SIZE: u32 = 100;

#[derive(Clone)]
pub struct Chain {
    rpc: sui_rpc::Client,
}

/// An object as the indexer needs it.
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
    Shared(u64),
    Other,
}

pub struct ServiceInfo {
    pub chain_identifier: String,
    pub checkpoint_height: Option<u64>,
    pub lowest_available_checkpoint: Option<u64>,
}

/// One event of a page: its JSON fields and position.
pub struct Event {
    pub fields: Value,
    pub checkpoint: Option<u64>,
}

pub struct EventPage {
    pub events: Vec<Event>,
    /// Cursor after everything this page scanned (store it to resume).
    pub end: Value,
    pub has_more: bool,
}

pub fn status(error: tonic::Status) -> anyhow::Error {
    anyhow::anyhow!("{}: {}", error.code(), error.message())
}

fn mask(paths: &[&str]) -> FieldMask {
    FieldMask::from_paths(paths.iter().copied())
}

pub fn same_id(a: &str, b: &str) -> bool {
    normalize(a).is_some() && normalize(a) == normalize(b)
}

/// 0x-prefixed, 64 lowercase hex digits; None if not an ID.
pub fn normalize(id: &str) -> Option<String> {
    let hex = id.strip_prefix("0x")?;
    if hex.is_empty() || hex.len() > 64 || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    Some(format!("0x{:0>64}", hex.to_ascii_lowercase()))
}

pub fn same_type(a: &str, b: &str) -> bool {
    matches!((a.split_once("::"), b.split_once("::")), (Some((aa, at)), Some((ba, bt))) if same_id(aa, ba) && at == bt)
}

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

/// A Move integer as the node renders it: u64 and wider as strings, smaller
/// ones (e.g. a u8 status) as JSON numbers, which may arrive as 1.0.
pub fn number(value: &Value) -> Option<u64> {
    value
        .as_str()
        .and_then(|s| s.parse().ok())
        .or_else(|| value.as_u64())
        .or_else(|| {
            value
                .as_f64()
                .filter(|f| f.fract() == 0.0 && *f >= 0.0 && *f < 9.0e15)
                .map(|f| f as u64)
        })
}

pub fn id_list(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|ids| {
            ids.iter()
                .filter_map(|id| id.as_str().and_then(normalize))
                .collect()
        })
        .unwrap_or_default()
}

fn watermark_cursor(watermark: &proto::Watermark) -> anyhow::Result<Value> {
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

/// Where a stream cursor resumes and the checkpoint it requires the node to hold.
fn start_of(cursor: &Option<Value>) -> anyhow::Result<(Option<Vec<u8>>, u64)> {
    let Some(value) = cursor else {
        return Ok((None, 0));
    };
    let checkpoint = value.get("checkpoint").and_then(Value::as_u64);
    match value.get("after").and_then(Value::as_str) {
        Some(after) => Ok((Some(STANDARD.decode(after)?), checkpoint.unwrap_or(0))),
        None => Ok((
            None,
            checkpoint.ok_or_else(|| anyhow::anyhow!("unrecognized cursor {value}"))?,
        )),
    }
}

/// A stream resuming at `checkpoint` needs the node to still hold it.
fn ensure_retained(event_type: &str, checkpoint: u64, lowest: Option<u64>) -> anyhow::Result<()> {
    if let Some(lowest) = lowest {
        anyhow::ensure!(
            checkpoint >= lowest,
            "event history before checkpoint {lowest} is pruned on this node, but the {event_type} stream \
             resumes at checkpoint {checkpoint}; use an archival node or rebuild from objects"
        );
    }
    Ok(())
}

impl Chain {
    pub fn new(url: &str) -> anyhow::Result<Self> {
        let rpc = sui_rpc::Client::new(url)
            .map_err(|e| anyhow::anyhow!("invalid gRPC endpoint {url}: {e}"))?
            .with_response_headers_timeout(Duration::from_secs(30));
        Ok(Self { rpc })
    }

    pub async fn service_info(&self) -> anyhow::Result<ServiceInfo> {
        let info = self
            .rpc
            .clone()
            .ledger_client()
            .get_service_info(proto::GetServiceInfoRequest::default())
            .await
            .map_err(status)?
            .into_inner();
        let genesis = info.chain_id.unwrap_or_default();
        let digest = Digest::from_str(&genesis)
            .map_err(|e| anyhow::anyhow!("invalid chain_id {genesis:?}: {e}"))?;
        Ok(ServiceInfo {
            chain_identifier: hex::encode(&digest.inner()[..4]),
            checkpoint_height: info.checkpoint_height,
            lowest_available_checkpoint: info.lowest_available_checkpoint,
        })
    }

    /// The package's version and authoritative type origin table
    /// ("module::Type" → introducing package), decoded from its BCS.
    pub async fn package(&self, id: &str) -> anyhow::Result<(u64, BTreeMap<String, String>)> {
        let mut request = proto::GetObjectRequest::default();
        request.object_id = Some(id.to_owned());
        request.read_mask = Some(mask(&["bcs"]));
        let bytes = self
            .rpc
            .clone()
            .ledger_client()
            .get_object(request)
            .await
            .map_err(status)?
            .into_inner()
            .object
            .and_then(|o| o.bcs)
            .and_then(|b| b.value)
            .ok_or_else(|| anyhow::anyhow!("package {id} has no contents"))?;
        let object: sui_sdk_types::Object = bcs::from_bytes(&bytes)
            .map_err(|e| anyhow::anyhow!("undecodable package {id}: {e}"))?;
        let sui_sdk_types::ObjectData::Package(package) = object.data() else {
            anyhow::bail!("{id} is not a package");
        };
        let mut origins = BTreeMap::new();
        for origin in &package.type_origin_table {
            let name = format!("{}::{}", origin.module_name, origin.struct_name);
            anyhow::ensure!(
                origins
                    .insert(name.clone(), origin.package.to_string())
                    .is_none(),
                "duplicate type origin {name}"
            );
        }
        Ok((package.version, origins))
    }

    /// None when deleted, wrapped or never created.
    pub async fn object(&self, id: &str) -> anyhow::Result<Option<Object>> {
        let mut request = proto::GetObjectRequest::default();
        request.object_id = Some(id.to_owned());
        request.read_mask = Some(mask(&["owner", "object_type", "json"]));
        let object = match self.rpc.clone().ledger_client().get_object(request).await {
            Ok(response) => response.into_inner().object,
            Err(e) if e.code() == tonic::Code::NotFound => return Ok(None),
            Err(e) => return Err(status(e).context(format!("object {id}"))),
        }
        .ok_or_else(|| anyhow::anyhow!("object {id} has no data"))?;
        let owner = match object.owner.as_ref() {
            Some(o) if o.kind == Some(proto::owner::OwnerKind::Address as i32) => {
                Owner::Address(o.address.clone().unwrap_or_default())
            }
            Some(o) if o.kind == Some(proto::owner::OwnerKind::Shared as i32) => {
                Owner::Shared(o.version.unwrap_or_default())
            }
            _ => Owner::Other,
        };
        Ok(Some(Object {
            object_type: object.object_type.unwrap_or_default(),
            owner,
            fields: object.json.as_deref().map(to_json).unwrap_or(Value::Null),
        }))
    }

    /// The value of a dynamic field keyed by an empty key struct of type
    /// `key_type` (full "0x…::module::Type"), read through its derived ID.
    pub async fn dynamic_field(
        &self,
        parent: &str,
        key_type: &str,
    ) -> anyhow::Result<Option<Value>> {
        let key = TypeTag::Struct(Box::new(
            StructTag::from_str(key_type)
                .map_err(|e| anyhow::anyhow!("invalid key type {key_type}: {e}"))?,
        ));
        let parent = Address::from_str(parent)
            .map_err(|e| anyhow::anyhow!("invalid object ID {parent}: {e}"))?;
        let id = parent.derive_dynamic_child_id(&key, &[0]);
        Ok(self
            .object(&id.to_string())
            .await?
            .map(|field| field.fields["value"].clone()))
    }

    /// One page of `event_type` events after `cursor` (None = genesis).
    /// Fails when the node no longer holds the resume checkpoint: pruned
    /// history is an operator decision, never skipped silently.
    pub async fn events(
        &self,
        event_type: &str,
        cursor: &Option<Value>,
    ) -> anyhow::Result<EventPage> {
        let (after, checkpoint) = start_of(cursor)?;
        ensure_retained(
            event_type,
            checkpoint,
            self.service_info().await?.lowest_available_checkpoint,
        )?;
        let mut request = proto::ListEventsRequest::default();
        request.read_mask = Some(mask(&["event_type", "json", "checkpoint"]));
        let mut literal = proto::EventLiteral::default();
        let mut by_type = proto::EventTypeFilter::default();
        by_type.event_type = Some(event_type.to_owned());
        literal.predicate = Some(proto::event_literal::Predicate::EventType(by_type));
        let mut term = proto::EventTerm::default();
        term.literals = vec![literal];
        let mut filter = proto::EventFilter::default();
        filter.terms = vec![term];
        request.filter = Some(filter);
        let mut options = proto::QueryOptions::default();
        options.limit = Some(PAGE_SIZE);
        match after {
            Some(bytes) => options.after = Some(bytes.into()),
            None => request.start_checkpoint = Some(checkpoint),
        }
        request.options = Some(options);
        let mut stream = self
            .rpc
            .clone()
            .ledger_client()
            .list_events(request)
            .await
            .map_err(status)?
            .into_inner();
        let mut events = Vec::new();
        while let Some(frame) = stream.next().await {
            let frame = frame.map_err(status)?;
            let end = watermark_cursor(
                frame
                    .watermark
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("event stream frame without a watermark"))?,
            )?;
            if let Some(event) = frame.event {
                anyhow::ensure!(
                    same_type(event.event_type.as_deref().unwrap_or(""), event_type),
                    "node returned an event outside the requested type"
                );
                events.push(Event {
                    fields: event.json.as_deref().map(to_json).unwrap_or(Value::Null),
                    checkpoint: event.checkpoint,
                });
            }
            if let Some(reason) = frame.end.as_ref().and_then(|e| e.reason) {
                let has_more = matches!(
                    proto::QueryEndReason::try_from(reason),
                    Ok(proto::QueryEndReason::ItemLimit | proto::QueryEndReason::ScanLimit)
                );
                return Ok(EventPage {
                    events,
                    end,
                    has_more,
                });
            }
        }
        anyhow::bail!("event stream ended without completion")
    }

    /// Sign-ready transaction for `builder`, resolved (gas, versions, budget)
    /// by the node's simulation for `sender`.
    pub async fn build(
        &self,
        mut builder: sui_transaction_builder::TransactionBuilder,
        sender: Address,
    ) -> anyhow::Result<Transaction> {
        builder.set_sender(sender);
        builder
            .build(&mut self.rpc.clone())
            .await
            .map_err(|e| anyhow::anyhow!("transaction simulation failed: {e}"))
    }

    /// Execute a signed transaction; success is read from its effects.
    pub async fn execute(
        &self,
        transaction: &Transaction,
        signature: &sui_sdk_types::UserSignature,
    ) -> anyhow::Result<String> {
        let mut request = proto::ExecuteTransactionRequest::default();
        let mut tx = proto::Transaction::default();
        let mut bcs_tx = proto::Bcs::default();
        bcs_tx.value = Some(bcs::to_bytes(transaction)?.into());
        tx.bcs = Some(bcs_tx);
        request.transaction = Some(tx);
        let mut user = proto::UserSignature::default();
        let mut bcs_sig = proto::Bcs::default();
        bcs_sig.value = Some(signature.to_bytes().into());
        user.bcs = Some(bcs_sig);
        request.signatures = vec![user];
        request.read_mask = Some(mask(&["digest", "effects.status"]));
        let executed = self
            .rpc
            .clone()
            .execution_client()
            .execute_transaction(request)
            .await
            .map_err(status)?
            .into_inner()
            .transaction
            .ok_or_else(|| anyhow::anyhow!("execution returned no transaction"))?;
        let digest = executed.digest.clone().unwrap_or_default();
        let success = executed
            .effects
            .as_ref()
            .and_then(|e| e.status.as_ref())
            .and_then(|s| s.success);
        anyhow::ensure!(
            success == Some(true),
            "transaction {digest} failed: {:?}",
            executed
                .effects
                .as_ref()
                .and_then(|e| e.status.as_ref())
                .and_then(|s| s.error.as_ref())
        );
        Ok(digest)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integers_in_node_json() {
        assert_eq!(number(&json!("18446744073709551615")), Some(u64::MAX));
        assert_eq!(number(&json!(2)), Some(2));
        assert_eq!(number(&json!(1.0)), Some(1));
        assert_eq!(number(&json!(1.5)), None);
        assert_eq!(number(&json!(-1.0)), None);
        assert_eq!(number(&json!("x")), None);
    }

    #[test]
    fn ids_normalize_and_compare() {
        assert_eq!(normalize("0xAB").unwrap(), format!("0x{:0>64}", "ab"));
        assert!(same_id("0x1", "0x0001"));
        assert!(
            normalize("0x").is_none() && normalize("ab").is_none() && normalize("0xzz").is_none()
        );
        assert!(same_type(
            "0x2::package::UpgradeCap",
            "0x0002::package::UpgradeCap"
        ));
        assert!(!same_type("0x2::package::UpgradeCap", "0x2::coin::Coin"));
    }

    #[test]
    fn cursors_resume_after_watermarks_or_at_checkpoints() {
        assert_eq!(start_of(&None).unwrap(), (None, 0));
        let after = json!({"after": STANDARD.encode([1, 2]), "checkpoint": 7});
        assert_eq!(start_of(&Some(after)).unwrap(), (Some(vec![1, 2]), 7));
        assert_eq!(
            start_of(&Some(json!({"checkpoint": 9}))).unwrap(),
            (None, 9)
        );
        assert!(start_of(&Some(json!({"txDigest": "x"}))).is_err());
    }

    #[test]
    fn pruned_history_is_an_error_not_a_skip() {
        assert!(ensure_retained("T", 5, None).is_ok());
        assert!(ensure_retained("T", 5, Some(5)).is_ok());
        let error = ensure_retained("T", 4, Some(5)).unwrap_err().to_string();
        assert!(
            error.contains("pruned") && error.contains("checkpoint 4"),
            "{error}"
        );
    }
}
