//! Publication-side chain reader (SF-006) for scripts/deployment_manifest.py
//! and scripts/publish.py. Never part of the device binary.
//!
//! Reads over the same Sui gRPC v2 API and SDK as the daemon, and prints
//! one JSON document:
//!
//!   chain_facts RPC_URL [OBJECT_ID...]
//!
//!   {"chain_identifier": "4c78adac",
//!    "objects": {"0x..": {"kind": "package", "version": 3,
//!                         "type_origins": [{"module", "datatype", "package"}]},
//!                "0x..": {"kind": "object", "type": "0x2::package::UpgradeCap",
//!                         "version": 7, "owner": {"address": "0x.."},
//!                         "upgrade_cap": {"package", "version", "policy"}},
//!                "0x..": {"kind": "absent"}}}
//!
//! Packages and objects are decoded from their BCS, so the type origin
//! table and UpgradeCap fields are authoritative rather than a rendering.
use serde_json::{json, Map, Value};
use std::{str::FromStr, time::Duration};
use sui_rpc::field::{FieldMask, FieldMaskUtil};
use sui_rpc::proto::sui::rpc::v2 as proto;
use sui_sdk_types::{Address, Digest, Object, ObjectData, Owner};

fn owner(owner: &Owner) -> Value {
    match owner {
        Owner::Address(address) => json!({"address": address.to_string()}),
        Owner::Object(parent) => json!({"object": parent.to_string()}),
        Owner::Shared(version) => json!({"shared": version}),
        Owner::Immutable => json!({"immutable": true}),
        Owner::ConsensusAddress { owner, .. } => json!({"consensus_address": owner.to_string()}),
        _ => json!({"other": true}),
    }
}

fn describe(object: &Object) -> anyhow::Result<Value> {
    Ok(match object.data() {
        ObjectData::Package(package) => json!({
            "kind": "package",
            "version": package.version,
            "type_origins": package.type_origin_table.iter().map(|origin| json!({
                "module": origin.module_name.as_str(),
                "datatype": origin.struct_name.as_str(),
                "package": origin.package.to_string(),
            })).collect::<Vec<_>>(),
        }),
        ObjectData::Struct(value) => {
            let tag = value.object_type();
            let mut fields = json!({
                "kind": "object",
                "type": tag.to_string(),
                "version": value.version(),
                "owner": owner(object.owner()),
            });
            if tag.address() == &Address::TWO
                && tag.module().as_str() == "package"
                && tag.name().as_str() == "UpgradeCap"
                && tag.type_params().is_empty()
            {
                // struct UpgradeCap { id: UID, package: ID, version: u64, policy: u8 }
                let (_, package, version, policy): (Address, Address, u64, u8) =
                    bcs::from_bytes(value.contents())
                        .map_err(|e| anyhow::anyhow!("undecodable UpgradeCap: {e}"))?;
                fields["upgrade_cap"] =
                    json!({"package": package.to_string(), "version": version, "policy": policy});
            }
            fields
        }
    })
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (url, ids) = args
        .split_first()
        .ok_or_else(|| anyhow::anyhow!("usage: chain_facts RPC_URL [OBJECT_ID...]"))?;
    let rpc = sui_rpc::Client::new(url.as_str())
        .map_err(|e| anyhow::anyhow!("invalid gRPC endpoint {url}: {e}"))?
        .with_response_headers_timeout(Duration::from_secs(30));
    let info = rpc
        .clone()
        .ledger_client()
        .get_service_info(proto::GetServiceInfoRequest::default())
        .await
        .map_err(|e| anyhow::anyhow!("GetServiceInfo: {}: {}", e.code(), e.message()))?
        .into_inner();
    // The manifest's 8-hex chain identifier: the first four bytes of the
    // genesis checkpoint digest, which gRPC reports in base58 as chain_id.
    let genesis = info.chain_id.unwrap_or_default();
    let digest = Digest::from_str(&genesis)
        .map_err(|e| anyhow::anyhow!("invalid chain_id {genesis:?}: {e}"))?;
    let mut objects = Map::new();
    for id in ids {
        let address =
            Address::from_str(id).map_err(|e| anyhow::anyhow!("invalid object ID {id}: {e}"))?;
        let mut request = proto::GetObjectRequest::default();
        request.object_id = Some(address.to_string());
        request.read_mask = Some(FieldMask::from_paths(["bcs"]));
        let fact = match rpc.clone().ledger_client().get_object(request).await {
            Ok(response) => {
                let bytes = response
                    .into_inner()
                    .object
                    .and_then(|o| o.bcs)
                    .and_then(|b| b.value)
                    .ok_or_else(|| anyhow::anyhow!("object {id} has no contents"))?;
                let object: Object = bcs::from_bytes(&bytes)
                    .map_err(|e| anyhow::anyhow!("undecodable object {id}: {e}"))?;
                describe(&object)?
            }
            // Deleted, wrapped or never created.
            Err(e) if e.code() == tonic::Code::NotFound => json!({"kind": "absent"}),
            Err(e) => anyhow::bail!("GetObject {id}: {}: {}", e.code(), e.message()),
        };
        objects.insert(address.to_string(), fact);
    }
    println!(
        "{}",
        json!({"chain_identifier": hex::encode(&digest.inner()[..4]), "objects": objects})
    );
    Ok(())
}
