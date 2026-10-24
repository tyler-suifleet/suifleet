//! Test-only gRPC front end for the Python chain simulator (scripts/test_ota.py).
//!
//! The daemon speaks Sui gRPC v2; the simulator keeps its scenario state and
//! assertions in Python. This shim implements the handful of v2 RPCs the
//! daemon uses by calling the simulator's JSON methods:
//!
//! - GetServiceInfo: chain identifier (as a genesis digest) and height;
//! - GetObject: objects, packages (as real BCS with a type origin table),
//!   Move objects the simulator supplies as BCS contents, and dynamic
//!   fields, matched by their derived object IDs;
//! - ListEvents: simulator events as checkpoints 1..n, two per page, with
//!   opaque resume cursors;
//! - GetTransaction: checkpoint of a simulator event ("event-<n>");
//! - SimulateTransaction: checks the call through the simulator, then
//!   returns a real resolved transaction;
//! - ExecuteTransaction: verifies the user signature with Mysten's
//!   sui-crypto (independently of the daemon's signer) before the simulator
//!   decides the execution status;
//! - GetBalance: the device's SUI balance from the simulator (SF-083).
//!
//! Usage: grpc_sim_shim SIMULATOR_URL  (prints "listening PORT")
use std::{str::FromStr, sync::Arc};

use serde_json::{json, Value};
use base64::{engine::general_purpose::STANDARD, Engine};
use prost::Message;
use sui_crypto::SuiVerifier;
use sui_rpc::proto::sui::rpc::v2 as proto;
use sui_sdk_types::{Address, Digest, MoveStruct, Object, ObjectData, StructTag, TypeTag};
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
            let message = error["message"].as_str().unwrap_or("error").to_owned();
            return Err(match error["code"].as_i64() {
                Some(429) => Status::resource_exhausted(message),
                Some(408) => Status::deadline_exceeded(message),
                _ => Status::unavailable(message),
            });
        }
        Ok(reply["result"].clone())
    }

    /// Dynamic fields the simulator knows, as (parent, key type, key BCS hex
    /// [, value type]), matched by their derived object IDs.
    async fn dynamic_field(&self, id: Address) -> Result<Option<proto::Object>, Status> {
        let fields = self.call("shim_fields", json!([])).await?;
        for field in fields.as_array().into_iter().flatten() {
            let (Some(parent), Some(key), Some(key_hex), value_type) =
                (field[0].as_str(), field[1].as_str(), field[2].as_str(), field[3].as_str().unwrap_or("unknown"))
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
            if key.ends_with("::fleet_data::CompactHeadKey") {
                let head = hex::decode(value["bcs_hex"].as_str().ok_or_else(|| Status::internal("missing compact head bytes"))?)
                    .map_err(|e| Status::internal(e.to_string()))?;
                let mut contents = id.inner().to_vec(); contents.extend(&key_bcs); contents.extend(head);
                let origin = key.split("::").next().unwrap_or_default();
                let tag = format!("0x2::dynamic_field::Field<{key}, {origin}::fleet_data::CompactHead>");
                let structure = MoveStruct::new(StructTag::from_str(&tag).map_err(|e| Status::internal(e.to_string()))?, false, 1, contents)
                    .ok_or_else(|| Status::internal("compact field has no UID"))?;
                let full = Object::new(ObjectData::Struct(structure), sui_sdk_types::Owner::Object(parent_id), Digest::ZERO, 0);
                let mut bcs = proto::Bcs::default();
                bcs.value = Some(bcs::to_bytes(&full).map_err(|e| Status::internal(e.to_string()))?.into());
                object.object_id = Some(id.to_string());
                object.object_type = Some(tag);
                object.bcs = Some(bcs);
                object.json = Some(Box::new(to_proto(&json!({"value": value}))));
                return Ok(Some(object));
            }
            object.object_type = Some(format!("0x2::dynamic_field::Field<{key}, {value_type}>"));
            object.json = Some(Box::new(to_proto(&json!({"value": value}))));
            return Ok(Some(object));
        }
        Ok(None)
    }
}

impl Shim {
    async fn versioned_object(
        &self,
        request: Request<proto::GetObjectRequest>,
    ) -> Result<Response<proto::GetObjectResponse>, Status> {
        let id = request.get_ref().object_id.clone().unwrap_or_default();
        let mut response = self.read_object(request).await?.into_inner();
        if let Some(object) = response.object.as_mut() {
            // Simulated versions change with contents and chain reset. Real
            // nodes provide monotonically increasing ledger object versions.
            if object.version.is_none() {
                use sha2::{Digest as _, Sha256};
                let salt = self.call("shim_version_salt", json!([])).await?;
                let contents = if object.bcs.is_some() {
                    let mut plain = proto::GetObjectRequest::default();
                    plain.object_id = Some(id.clone());
                    plain.read_mask = Some(prost_types::FieldMask {
                        paths: vec!["owner".into(), "object_type".into(), "json".into()],
                    });
                    self.read_object(Request::new(plain))
                        .await?
                        .into_inner()
                        .object
                        .ok_or_else(|| Status::internal("version object missing"))?
                } else {
                    object.clone()
                };
                let mut hash = Sha256::new();
                hash.update(contents.encode_to_vec());
                hash.update(salt.to_string());
                object.version = Some(u64::from_le_bytes(hash.finalize()[..8].try_into().unwrap()));
            }
            object.object_id = Some(id);
        }
        Ok(Response::new(response))
    }
    async fn read_object(
        &self,
        request: Request<proto::GetObjectRequest>,
    ) -> Result<Response<proto::GetObjectResponse>, Status> {
        let request = request.into_inner();
        let id = request.object_id.unwrap_or_default();
        let address = Address::from_str(&id).map_err(|e| Status::invalid_argument(e.to_string()))?;
        if short_id(&id)=="0x5ca1ab1e" && request.read_mask.as_ref().is_some_and(|m|m.paths.iter().any(|p|p=="version")) {
            let mut object=proto::Object::default();object.object_id=Some(id);
            object.version=Some(number(&self.call("shim_fleet_gas_version",json!([])).await?));
            let mut response=proto::GetObjectResponse::default();response.object=Some(object);return Ok(Response::new(response));
        }
        let wants_bcs = request
            .read_mask
            .as_ref()
            .is_some_and(|mask| mask.paths.iter().any(|p| p == "bcs"));
        let mut response = proto::GetObjectResponse::default();
        if wants_bcs {
            if let Some(field) = self.dynamic_field(address).await? {
                if field.bcs.is_some() { response.object = Some(field); return Ok(Response::new(response)); }
            }
            let package = self.call("sui_getObject", json!([id, {"showBcs": true}])).await?;
            let data = &package["data"];
            if data["bcs"]["dataType"] == "moveObject" {
                let tag = StructTag::from_str(data["bcs"]["type"].as_str().unwrap_or(""))
                    .map_err(|e| Status::internal(e.to_string()))?;
                let contents = hex::decode(data["bcs"]["bcsBytes"].as_str().unwrap_or(""))
                    .map_err(|e| Status::internal(e.to_string()))?;
                let value = MoveStruct::new(tag, false, number(&data["version"]), contents)
                    .ok_or_else(|| Status::internal("Move object contents lack an ID"))?;
                let owner = if let Some(shared) = data["owner"].get("Shared") {
                    sui_sdk_types::Owner::Shared(number(&shared["initial_shared_version"]))
                } else if data["owner"] == "Immutable" {
                    sui_sdk_types::Owner::Immutable
                } else {
                    let owner = data["owner"]["AddressOwner"].as_str().unwrap_or("");
                    sui_sdk_types::Owner::Address(
                        Address::from_str(owner).map_err(|e| Status::internal(e.to_string()))?,
                    )
                };
                let object = Object::new(ObjectData::Struct(value), owner, Digest::ZERO, 0);
                let mut bcs = proto::Bcs::default();
                bcs.value = Some(bcs::to_bytes(&object).map_err(|e| Status::internal(e.to_string()))?.into());
                let mut object = proto::Object::default();
                object.bcs = Some(bcs);
                response.object = Some(object);
                return Ok(Response::new(response));
            }
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
        } else if data["owner"] == "Immutable" {
            owner.kind = Some(proto::owner::OwnerKind::Immutable as i32);
        }
        // Missing/unknown ownership stays unspecified, never proof of freezing.
        let mut object = proto::Object::default();
        object.object_id = Some(id);
        object.owner = Some(owner);
        object.object_type = data["content"]["type"].as_str().map(str::to_owned);
        object.json = Some(Box::new(to_proto(&data["content"]["fields"])));
        response.object = Some(object);
        Ok(Response::new(response))
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
        self.call("shim_read", json!(["GetServiceInfo", 50]))
            .await?;
        let chain = self.call("sui_getChainIdentifier", json!([])).await?;
        let prefix = hex::decode(chain.as_str().unwrap_or_default())
            .map_err(|e| Status::internal(e.to_string()))?;
        let mut genesis = [0u8; 32];
        genesis[..prefix.len()].copy_from_slice(&prefix);
        let mut info = proto::GetServiceInfoResponse::default();
        info.chain_id = Some(Digest::new(genesis).to_string());
        let height = self.call("shim_height", json!([])).await?;
        info.checkpoint_height = Some(number(if height.is_object() {
            &height["height"]
        } else {
            &height
        }));
        info.lowest_available_checkpoint = Some(if height.is_object() {
            number(&height["lowest"])
        } else {
            0
        });
        Ok(Response::new(info))
    }

    async fn get_object(
        &self,
        request: Request<proto::GetObjectRequest>,
    ) -> Result<Response<proto::GetObjectResponse>, Status> {
        let result = self.versioned_object(request).await;
        let bytes = result
            .as_ref()
            .map(|r| r.get_ref().encoded_len())
            .unwrap_or(0);
        self.call("shim_read", json!(["GetObject", bytes])).await?;
        result
    }

    async fn batch_get_objects(
        &self,
        request: Request<proto::BatchGetObjectsRequest>,
    ) -> Result<Response<proto::BatchGetObjectsResponse>, Status> {
        let request = request.into_inner();
        if request.read_mask.as_ref().map(|m| m.paths.as_slice())
            != Some(&["version".to_owned()][..])
        {
            return Err(Status::invalid_argument(
                "watch batch must use version-only mask",
            ));
        }
        let mut response = proto::BatchGetObjectsResponse::default();
        for mut item in request.requests {
            item.read_mask = Some(prost_types::FieldMask {
                paths: vec!["owner".into(), "object_type".into(), "json".into()],
            });
            let result = match self.versioned_object(Request::new(item)).await {
                Ok(r) => {
                    let mut object = proto::Object::default();
                    object.version = r.into_inner().object.and_then(|o| o.version);
                    proto::get_object_result::Result::Object(object)
                }
                Err(e) => {
                    let mut error = sui_rpc::proto::google::rpc::Status::default();
                    error.code = e.code() as i32;
                    error.message = e.message().to_owned();
                    proto::get_object_result::Result::Error(error)
                }
            };
            let mut item = proto::GetObjectResult::default();
            item.result = Some(result);
            response.objects.push(item);
        }
        self.call(
            "shim_read",
            json!(["BatchGetObjects", response.encoded_len()]),
        )
        .await?;
        Ok(Response::new(response))
    }

    async fn get_transaction(
        &self,
        request: Request<proto::GetTransactionRequest>,
    ) -> Result<Response<proto::GetTransactionResponse>, Status> {
        let digest = request.into_inner().digest.unwrap_or_default();
        if !digest.starts_with("event-") {
            let result=self.call("shim_fleet_transaction",json!([digest.clone()])).await?;
            if result.is_null(){return Err(Status::not_found("not executed"));}
            let mut status=proto::ExecutionStatus::default();status.success=Some(result["effects"]["status"]["status"]=="success");
            let mut effects=proto::TransactionEffects::default();effects.status=Some(status);
            let mut tx=proto::ExecutedTransaction::default();tx.digest=Some(digest);tx.effects=Some(effects);tx.checkpoint=Some(0);
            let mut response=proto::GetTransactionResponse::default();response.transaction=Some(tx);return Ok(Response::new(response));
        }
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
        let combined = request.filter.as_ref().is_some_and(|f| f.terms.len() == 2);
        let events = if combined {
            self.call("shim_discovery_events", json!([])).await?
        } else {
            self.call("shim_events", json!([event_type])).await?
        };
        let events = events.as_array().cloned().unwrap_or_default();
        self.call(
            "shim_read",
            json!(["ListEvents", serde_json::to_vec(&events).unwrap().len()]),
        )
        .await?;
        // Event i is in checkpoint i + 1; a cursor is the next index to read.
        let start = match request.options.as_ref().and_then(|o| o.after.as_ref()) {
            Some(after) => u64::from_le_bytes(
                after
                    .as_ref()
                    .try_into()
                    .map_err(|_| Status::invalid_argument("cursor"))?,
            ) as usize,
            None => request
                .start_checkpoint
                .map(|c| c.saturating_sub(1) as usize)
                .unwrap_or(0),
        };
        let bound = request
            .end_checkpoint
            .map(|c| c.saturating_sub(1) as usize)
            .unwrap_or(events.len());
        let available = events.len().min(bound);
        let end = (start + PAGE).min(available.max(start));
        let mut frames = Vec::new();
        for (index, parsed) in events.iter().enumerate().take(end).skip(start) {
            let mut event = proto::Event::default();
            event.event_type = Some(if combined {
                parsed["event_type"].as_str().unwrap().to_owned()
            } else {
                event_type.clone()
            });
            event.json = Some(Box::new(to_proto(if combined {
                &parsed["json"]
            } else {
                parsed
            })));
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
        done.reason = Some(if end < available {
            proto::QueryEndReason::ItemLimit
        } else if request.end_checkpoint.is_some() {
            proto::QueryEndReason::CheckpointBound
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
        let mut pures = Vec::new();
        for argument in &call.arguments {
            let input = &ptb.inputs[argument.input.unwrap_or_default() as usize];
            let pure: Option<&prost::bytes::Bytes> = input.pure.as_ref();
            let object: Option<&String> = input.object_id.as_ref();
            arguments.push(match (pure, object) {
                // A u8 status, or a u64 amount (fleet_budget::draw_gas).
                (Some(pure), _) if matches!(call.module.as_deref(), Some("fleet_data" | "fleet_storage")) => {
                    pures.push(pure.to_vec());
                    json!({"bcs": STANDARD.encode(pure)})
                }
                (Some(pure), _) => {
                    pures.push(pure.to_vec());
                    let mut bytes = [0u8; 8];
                    bytes[..pure.len().min(8)].copy_from_slice(&pure[..pure.len().min(8)]);
                    Value::String(u64::from_le_bytes(bytes).to_string())
                }
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
        let mut pures = pures.into_iter();
        for (index, argument) in call.arguments.iter().enumerate() {
            let input = &ptb.inputs[argument.input.unwrap_or_default() as usize];
            if input.pure.is_some() {
                inputs.push(builder.pure_bytes(pures.next().unwrap_or_default()));
                continue;
            }
            let id = Address::from_str(arguments[index].as_str().unwrap_or("")).map_err(|e| Status::internal(e.to_string()))?;
            if matches!(call.module.as_deref(), Some("fleet_data" | "fleet_storage")) {
                let owned = match function.as_str() { "report_health" | "anchor" | "anchor_compact" => index==5, "register_blob" => index==4, _=>false };
                let mutable = match function.as_str() { "report_health" | "anchor" | "anchor_compact" => index==1, "register_blob" => index==0 || index==5, "certify_blob" => index==0, _=>false };
                inputs.push(builder.object(if owned { sui_transaction_builder::ObjectInput::owned(id,1,Digest::ZERO) }
                    else { sui_transaction_builder::ObjectInput::shared(id,1,mutable) }));
                continue;
            }
            inputs.push(builder.object(match (function == "draw_gas", index) {
                // draw_gas: budget (&), account (&mut), registry (&), cap
                // (owned), Clock (&), as a node resolves them.
                (true, 1) => sui_transaction_builder::ObjectInput::shared(id, 1, true),
                (true, 3) => sui_transaction_builder::ObjectInput::owned(id, 1, Digest::ZERO),
                (true, _) => sui_transaction_builder::ObjectInput::shared(id, 1, false),
                // Status reports: deployment (&mut), registry (&), cap
                // (owned), group (&).
                (false, 0) => sui_transaction_builder::ObjectInput::shared(id, 1, true),
                (false, 2) => sui_transaction_builder::ObjectInput::owned(id, 1, Digest::ZERO),
                (false, _) => sui_transaction_builder::ObjectInput::shared(id, 1, false),
            }));
        }
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
        let gas_version=if matches!(call.module.as_deref(),Some("fleet_data" | "fleet_storage")){number(&self.call("shim_fleet_gas_version",json!([])).await?)}else{1};
        builder.add_gas_objects([sui_transaction_builder::ObjectInput::owned(
            Address::from_str("0x5ca1ab1e").unwrap(),
            gas_version,
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
        let custom = match &transaction.kind {
            sui_sdk_types::TransactionKind::ProgrammableTransaction(ptb) => match ptb.commands.as_slice() {
                [sui_sdk_types::Command::MoveCall(call)] if matches!(call.module.as_str(),"fleet_data" | "fleet_storage") => {
                    let args: Vec<_> = call.arguments.iter().map(|arg| {
                        let sui_sdk_types::Argument::Input(index) = arg else { return Value::Null };
                        match &ptb.inputs[*index as usize] {
                            sui_sdk_types::Input::Pure(b) => json!({"bcs":STANDARD.encode(b)}),
                            sui_sdk_types::Input::Shared(o) => json!(short_id(&o.object_id().to_string())),
                            sui_sdk_types::Input::ImmutableOrOwned(o) => json!(short_id(&o.object_id().to_string())),
                            _ => Value::Null,
                        }
                    }).collect();
                    Some(json!([call.module.to_string(),call.function.to_string(),args,transaction.sender.to_string(),transaction.digest().to_string(),transaction.gas_payment.objects[0].version()]))
                }
                _=>None,
            }, _=>None,
        };
        let result = if let Some(args) = custom { self.call("shim_fleet_execute",args).await? } else {
            self.call("sui_executeTransactionBlock", json!([hex::encode(&bytes), [format!("verified:{}", transaction.sender)]])).await?
        };
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

#[tonic::async_trait]
impl proto::state_service_server::StateService for Shim {
    async fn get_balance(
        &self,
        request: Request<proto::GetBalanceRequest>,
    ) -> Result<Response<proto::GetBalanceResponse>, Status> {
        let request = request.into_inner();
        let owner = request.owner.unwrap_or_default();
        let value = number(&self.call("shim_balance", json!([owner, request.coin_type])).await?);
        let mut balance = proto::Balance::default();
        balance.coin_type = request.coin_type;
        balance.balance = Some(value);
        let mut response = proto::GetBalanceResponse::default();
        response.balance = Some(balance);
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
        .add_service(proto::state_service_server::StateServiceServer::new(shim.clone()))
        .add_service(
            proto::transaction_execution_service_server::TransactionExecutionServiceServer::new(shim),
        )
        .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
        .await?;
    Ok(())
}
