//! Test-only gRPC front end for the Python chain simulator (scripts/test_ota.py).
//!
//! The daemon speaks Sui gRPC v2; the simulator keeps its scenario state and
//! assertions in Python. This shim implements the handful of v2 RPCs the
//! daemon uses by calling the simulator's JSON methods:
//!
//! - GetServiceInfo: chain identifier (as a genesis digest) and height;
//! - GetObject: objects, packages (as real BCS with a type origin table) and
//!   dynamic fields, matched by their derived object IDs;
//! - ListEvents: simulator events as checkpoints 1..n, two per page, with
//!   opaque resume cursors;
//! - GetTransaction: checkpoint of a simulator event ("event-<n>");
//! - SimulateTransaction: checks the call through the simulator, then
//!   returns a real resolved transaction;
//! - ExecuteTransaction: verifies the user signature with Mysten's
//!   sui-crypto (independently of the daemon's signer) before the simulator
//!   decides the execution status.
//!
//! Usage: grpc_sim_shim SIMULATOR_URL  (prints "listening PORT")
use std::{str::FromStr, sync::Arc};

use serde_json::{json, Value};
use sui_crypto::SuiVerifier;
use sui_rpc::proto::sui::rpc::v2 as proto;
use sui_sdk_types::{Address, Digest, Object, ObjectData, TypeTag};
use tonic::{Request, Response, Status};

const PAGE: usize = 2;

#[derive(Clone)]
struct Shim {
    simulator: Arc<String>,
    http: reqwest::Client,
}

impl Shim {
    async fn call(&self, method: &str, params: Value) -> Result<Value, Status> {
        let reply: Value = self
            .http
            .post(self.simulator.as_str())
            .json(&json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
            .send()
            .await
            .map_err(|e| Status::unavailable(e.to_string()))?
            .json()
            .await
            .map_err(|e| Status::unavailable(e.to_string()))?;
        if let Some(error) = reply.get("error") {
            // The simulator reports an unavailable service (and failed
            // assertions) as errors; the daemon must treat both as failures.
            return Err(Status::unavailable(error["message"].as_str().unwrap_or("error").to_owned()));
        }
        Ok(reply["result"].clone())
    }

    /// Dynamic fields the simulator knows, as (parent, key type, key BCS hex),
    /// matched by their derived object IDs.
    async fn dynamic_field(&self, id: Address) -> Result<Option<proto::Object>, Status> {
        let fields = self.call("shim_fields", json!([])).await?;
        for field in fields.as_array().into_iter().flatten() {
            let (Some(parent), Some(key), Some(key_hex)) =
                (field[0].as_str(), field[1].as_str(), field[2].as_str())
            else {
                return Err(Status::internal("malformed simulator field"));
            };
            let parent_id = Address::from_str(parent).map_err(|e| Status::internal(e.to_string()))?;
            let tag = TypeTag::from_str(key).map_err(|e| Status::internal(e.to_string()))?;
            let key_bcs = hex::decode(key_hex).map_err(|e| Status::internal(e.to_string()))?;
            if parent_id.derive_dynamic_child_id(&tag, &key_bcs) != id {
                continue;
            }
            let field = self
                .call("suix_getDynamicFieldObject", json!([parent, {"type": key, "value": key_hex}]))
                .await?;
            let value = &field["data"]["content"]["fields"]["value"];
            if value.is_null() {
                // A known field that does not exist: absent, as a node reports it.
                return Err(Status::not_found(format!("dynamic field {id} not found")));
            }
            let mut object = proto::Object::default();
            object.object_type = Some(format!("0x2::dynamic_field::Field<{key}, unknown>"));
            object.json = Some(Box::new(to_proto(&json!({"value": value}))));
            return Ok(Some(object));
        }
        Ok(None)
    }
}

fn to_proto(value: &Value) -> prost_types::Value {
    use prost_types::value::Kind;
    prost_types::Value {
        kind: Some(match value {
            Value::Null => Kind::NullValue(0),
            Value::Bool(b) => Kind::BoolValue(*b),
            Value::Number(n) => Kind::NumberValue(n.as_f64().unwrap_or_default()),
            Value::String(s) => Kind::StringValue(s.clone()),
            Value::Array(items) => Kind::ListValue(prost_types::ListValue {
                values: items.iter().map(to_proto).collect(),
            }),
            Value::Object(map) => Kind::StructValue(prost_types::Struct {
                fields: map.iter().map(|(k, v)| (k.clone(), to_proto(v))).collect(),
            }),
        }),
    }
}

fn number(value: &Value) -> u64 {
    value
        .as_str()
        .and_then(|s| s.parse().ok())
        .or_else(|| value.as_u64())
        .unwrap_or_default()
}

/// The simulator spells its scenario object IDs without leading zeros
/// ("0xca"); transactions carry canonical 64-hex IDs.
fn short_id(id: &str) -> String {
    let digits = id.trim_start_matches("0x").trim_start_matches('0');
    format!("0x{}", if digits.is_empty() { "0" } else { digits })
}

fn cursor(next: usize) -> prost::bytes::Bytes {
    (next as u64).to_le_bytes().to_vec().into()
}

#[tonic::async_trait]
impl proto::ledger_service_server::LedgerService for Shim {
    async fn get_service_info(
        &self,
        _: Request<proto::GetServiceInfoRequest>,
    ) -> Result<Response<proto::GetServiceInfoResponse>, Status> {
        let chain = self.call("sui_getChainIdentifier", json!([])).await?;
        let prefix = hex::decode(chain.as_str().unwrap_or_default())
            .map_err(|e| Status::internal(e.to_string()))?;
        let mut genesis = [0u8; 32];
        genesis[..prefix.len()].copy_from_slice(&prefix);
        let mut info = proto::GetServiceInfoResponse::default();
        info.chain_id = Some(Digest::new(genesis).to_string());
        info.checkpoint_height = Some(number(&self.call("shim_height", json!([])).await?));
        Ok(Response::new(info))
    }

    async fn get_object(
        &self,
        request: Request<proto::GetObjectRequest>,
    ) -> Result<Response<proto::GetObjectResponse>, Status> {
        let request = request.into_inner();
        let id = request.object_id.unwrap_or_default();
        let address = Address::from_str(&id).map_err(|e| Status::invalid_argument(e.to_string()))?;
        let wants_bcs = request
            .read_mask
            .as_ref()
            .is_some_and(|mask| mask.paths.iter().any(|p| p == "bcs"));
        let mut response = proto::GetObjectResponse::default();
        if wants_bcs {
            let package = self.call("sui_getObject", json!([id, {"showBcs": true}])).await?;
            let data = &package["data"];
            let origins = data["bcs"]["typeOriginTable"]
                .as_array()
                .ok_or_else(|| Status::not_found("not a package"))?
                .iter()
                .map(|o| {
                    Ok(sui_sdk_types::TypeOrigin {
                        module_name: o["module_name"].as_str().unwrap_or("").parse().map_err(|_| Status::internal("module"))?,
                        struct_name: o["datatype_name"].as_str().unwrap_or("").parse().map_err(|_| Status::internal("type"))?,
                        package: Address::from_str(o["package"].as_str().unwrap_or("")).map_err(|e| Status::internal(e.to_string()))?,
                    })
                })
                .collect::<Result<Vec<_>, Status>>()?;
            let package = sui_sdk_types::MovePackage {
                id: address,
                version: number(&data["version"]),
                modules: Default::default(),
                type_origin_table: origins,
                linkage_table: Default::default(),
            };
            let object = Object::new(
                ObjectData::Package(package),
                sui_sdk_types::Owner::Immutable,
                Digest::ZERO,
                0,
            );
            let mut bcs = proto::Bcs::default();
            bcs.value = Some(bcs::to_bytes(&object).map_err(|e| Status::internal(e.to_string()))?.into());
            let mut object = proto::Object::default();
            object.bcs = Some(bcs);
            response.object = Some(object);
            return Ok(Response::new(response));
        }
        if let Some(field) = self.dynamic_field(address).await? {
            response.object = Some(field);
            return Ok(Response::new(response));
        }
        let result = self
            .call("sui_getObject", json!([id, {"showContent": true, "showOwner": true}]))
            .await?;
        if result.get("error").is_some() {
            return Err(Status::not_found(format!("object {id} not found")));
        }
        let data = &result["data"];
        let mut owner = proto::Owner::default();
        if let Some(address) = data["owner"]["AddressOwner"].as_str() {
            owner.kind = Some(proto::owner::OwnerKind::Address as i32);
            owner.address = Some(address.to_owned());
        } else if let Some(parent) = data["owner"]["ObjectOwner"].as_str() {
            owner.kind = Some(proto::owner::OwnerKind::Object as i32);
            owner.address = Some(parent.to_owned());
        } else if let Some(shared) = data["owner"].get("Shared") {
            owner.kind = Some(proto::owner::OwnerKind::Shared as i32);
            owner.version = Some(number(&shared["initial_shared_version"]));
        } else {
            owner.kind = Some(proto::owner::OwnerKind::Immutable as i32);
        }
        let mut object = proto::Object::default();
        object.object_id = Some(id);
        object.owner = Some(owner);
        object.object_type = data["content"]["type"].as_str().map(str::to_owned);
        object.json = Some(Box::new(to_proto(&data["content"]["fields"])));
        response.object = Some(object);
        Ok(Response::new(response))
    }

    async fn get_transaction(
        &self,
        request: Request<proto::GetTransactionRequest>,
    ) -> Result<Response<proto::GetTransactionResponse>, Status> {
        let digest = request.into_inner().digest.unwrap_or_default();
        let index: u64 = digest
            .strip_prefix("event-")
            .and_then(|n| n.parse().ok())
            .ok_or_else(|| Status::not_found(format!("transaction {digest}")))?;
        let mut transaction = proto::ExecutedTransaction::default();
        transaction.checkpoint = Some(index + 1);
        let mut response = proto::GetTransactionResponse::default();
        response.transaction = Some(transaction);
        Ok(Response::new(response))
    }

    async fn list_events(
        &self,
        request: Request<proto::ListEventsRequest>,
    ) -> Result<Response<tonic::codegen::BoxStream<proto::ListEventsResponse>>, Status> {
        let request = request.into_inner();
        let event_type = request
            .filter
            .as_ref()
            .and_then(|f| f.terms.first())
            .and_then(|t| t.literals.first())
            .and_then(|l| match &l.predicate {
                Some(proto::event_literal::Predicate::EventType(t)) => t.event_type.clone(),
                _ => None,
            })
            .ok_or_else(|| Status::invalid_argument("expected an event type filter"))?;
        let events = self.call("shim_events", json!([event_type])).await?;
        let events = events.as_array().cloned().unwrap_or_default();
        // Event i is in checkpoint i + 1; a cursor is the next index to read.
        let start = match request.options.as_ref().and_then(|o| o.after.as_ref()) {
            Some(after) => u64::from_le_bytes(after.as_ref().try_into().map_err(|_| Status::invalid_argument("cursor"))?) as usize,
            None => request.start_checkpoint.map(|c| c.saturating_sub(1) as usize).unwrap_or(0),
        };
        let end = (start + PAGE).min(events.len().max(start));
        let mut frames = Vec::new();
        for (index, parsed) in events.iter().enumerate().take(end).skip(start) {
            let mut event = proto::Event::default();
            event.event_type = Some(event_type.clone());
            event.json = Some(Box::new(to_proto(parsed)));
            event.checkpoint = Some(index as u64 + 1);
            // One event per simulated transaction "event-<index>".
            event.transaction_digest = Some(format!("event-{index}"));
            event.event_index = Some(0);
            let mut watermark = proto::Watermark::default();
            watermark.cursor = Some(cursor(index + 1));
            let mut frame = proto::ListEventsResponse::default();
            frame.event = Some(event);
            frame.watermark = Some(watermark);
            frames.push(Ok(frame));
        }
        let mut watermark = proto::Watermark::default();
        watermark.cursor = Some(cursor(end));
        let mut done = proto::QueryEnd::default();
        done.reason = Some(if end < events.len() {
            proto::QueryEndReason::ItemLimit
        } else {
            proto::QueryEndReason::LedgerTip
        } as i32);
        let mut frame = proto::ListEventsResponse::default();
        frame.watermark = Some(watermark);
        frame.end = Some(done);
        frames.push(Ok(frame));
        Ok(Response::new(Box::pin(futures_util::stream::iter(frames))))
    }
}

#[tonic::async_trait]
impl proto::transaction_execution_service_server::TransactionExecutionService for Shim {
    async fn simulate_transaction(
        &self,
        request: Request<proto::SimulateTransactionRequest>,
    ) -> Result<Response<proto::SimulateTransactionResponse>, Status> {
        let transaction = request.into_inner().transaction.unwrap_or_default();
        let sender = Address::from_str(transaction.sender.as_deref().unwrap_or(""))
            .map_err(|e| Status::invalid_argument(e.to_string()))?;
        let ptb = transaction.kind().programmable_transaction().clone();
        let [command] = ptb.commands.as_slice() else {
            return Err(Status::invalid_argument("expected one command"));
        };
        let call = match &command.command {
            Some(proto::command::Command::MoveCall(call)) => call,
            _ => return Err(Status::invalid_argument("expected a Move call")),
        };
        let mut arguments = Vec::new();
        for argument in &call.arguments {
            let input = &ptb.inputs[argument.input.unwrap_or_default() as usize];
            let pure: Option<&prost::bytes::Bytes> = input.pure.as_ref();
            let object: Option<&String> = input.object_id.as_ref();
            arguments.push(match (pure, object) {
                (Some(pure), _) => Value::String(pure.first().copied().unwrap_or_default().to_string()),
                (None, Some(id)) => Value::String(short_id(id)),
                _ => return Err(Status::invalid_argument("unsupported input")),
            });
        }
        let function = call.function.clone().unwrap_or_default();
        let package = call.package.clone().unwrap_or_default();
        // The simulator checks the call (and may report itself unavailable).
        self.call(
            "unsafe_moveCall",
            json!([sender.to_string(), package, call.module, function, [], arguments, null, "10000000", null]),
        )
        .await?;
        let mut builder = sui_transaction_builder::TransactionBuilder::new();
        let mut inputs = Vec::new();
        let objects = &arguments[..arguments.len() - 1];
        for (index, id) in objects.iter().enumerate() {
            let id = Address::from_str(id.as_str().unwrap_or("")).map_err(|e| Status::internal(e.to_string()))?;
            inputs.push(builder.object(match index {
                // deployment (&mut), registry (&), cap (owned), group (&)
                0 => sui_transaction_builder::ObjectInput::shared(id, 1, true),
                2 => sui_transaction_builder::ObjectInput::owned(id, 1, Digest::ZERO),
                _ => sui_transaction_builder::ObjectInput::shared(id, 1, false),
            }));
        }
        let status: u8 = arguments.last().and_then(Value::as_str).and_then(|s| s.parse().ok()).unwrap_or_default();
        inputs.push(builder.pure(&status));
        builder.move_call(
            sui_transaction_builder::Function::new(
                Address::from_str(&package).map_err(|e| Status::internal(e.to_string()))?,
                call.module.clone().unwrap_or_default().parse().map_err(|_| Status::internal("module"))?,
                function.parse().map_err(|_| Status::internal("function"))?,
            ),
            inputs,
        );
        builder.set_sender(sender);
        builder.set_gas_budget(10_000_000);
        builder.set_gas_price(1000);
        builder.add_gas_objects([sui_transaction_builder::ObjectInput::owned(
            Address::from_str("0x5ca1ab1e").unwrap(),
            1,
            Digest::ZERO,
        )]);
        let resolved = builder.try_build().map_err(|e| Status::internal(e.to_string()))?;
        let mut bcs = proto::Bcs::default();
        bcs.value = Some(bcs::to_bytes(&resolved).map_err(|e| Status::internal(e.to_string()))?.into());
        let mut tx = proto::Transaction::default();
        tx.bcs = Some(bcs);
        let mut status = proto::ExecutionStatus::default();
        status.success = Some(true);
        let mut effects = proto::TransactionEffects::default();
        effects.status = Some(status);
        let mut executed = proto::ExecutedTransaction::default();
        executed.transaction = Some(tx);
        executed.effects = Some(effects);
        let mut response = proto::SimulateTransactionResponse::default();
        response.transaction = Some(executed);
        Ok(Response::new(response))
    }

    async fn execute_transaction(
        &self,
        request: Request<proto::ExecuteTransactionRequest>,
    ) -> Result<Response<proto::ExecuteTransactionResponse>, Status> {
        let request = request.into_inner();
        let bytes = request
            .transaction
            .and_then(|t| t.bcs)
            .and_then(|b| b.value)
            .ok_or_else(|| Status::invalid_argument("missing transaction bytes"))?;
        let transaction: sui_sdk_types::Transaction =
            bcs::from_bytes(&bytes).map_err(|e| Status::invalid_argument(e.to_string()))?;
        let signature = request
            .signatures
            .first()
            .and_then(|s| s.bcs.as_ref())
            .and_then(|b| b.value.as_ref())
            .ok_or_else(|| Status::invalid_argument("missing signature"))?;
        let signature = sui_sdk_types::UserSignature::from_bytes(signature)
            .map_err(|e| Status::invalid_argument(e.to_string()))?;
        sui_crypto::UserSignatureVerifier::new()
            .verify_transaction(&transaction, &signature)
            .map_err(|e| Status::permission_denied(format!("invalid signature: {e}")))?;
        let result = self
            .call(
                "sui_executeTransactionBlock",
                json!([hex::encode(&bytes), [format!("verified:{}", transaction.sender)]]),
            )
            .await?;
        let mut status = proto::ExecutionStatus::default();
        status.success = Some(result["effects"]["status"]["status"] == "success");
        if status.success != Some(true) {
            let mut error = proto::ExecutionError::default();
            error.description = result["effects"]["status"]["error"].as_str().map(str::to_owned);
            status.error = Some(error);
        }
        let mut effects = proto::TransactionEffects::default();
        effects.status = Some(status);
        let mut executed = proto::ExecutedTransaction::default();
        executed.digest = result["digest"].as_str().map(str::to_owned);
        executed.effects = Some(effects);
        let mut response = proto::ExecuteTransactionResponse::default();
        response.transaction = Some(executed);
        Ok(Response::new(response))
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let simulator = std::env::args()
        .nth(1)
        .ok_or_else(|| anyhow::anyhow!("usage: grpc_sim_shim SIMULATOR_URL"))?;
    let shim = Shim {
        simulator: Arc::new(simulator),
        http: reqwest::Client::new(),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    println!("listening {}", listener.local_addr()?.port());
    use std::io::Write;
    std::io::stdout().flush()?;
    tonic::transport::Server::builder()
        .add_service(proto::ledger_service_server::LedgerServiceServer::new(shim.clone()))
        .add_service(
            proto::transaction_execution_service_server::TransactionExecutionServiceServer::new(shim),
        )
        .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
        .await?;
    Ok(())
}
