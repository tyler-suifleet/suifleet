use std::{path::Path, time::Duration};

use ed25519_dalek::SigningKey;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{debug, error, info, warn};

use crate::{config::SuiConfig, identity};

// ── Types ─────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
pub struct DeploymentCreatedEvent {
    pub deployment_id: String,
    pub walrus_blob_id: String,
    pub sha256_hash: String,
    pub target_groups: Vec<String>,
}

// ── Client ────────────────────────────────────────────────────────────────────

#[derive(Clone)]
pub struct SuiClient {
    config: SuiConfig,
    signing_key: SigningKey,
    http: reqwest::Client,
}

impl SuiClient {
    pub fn address(&self) -> String {
        identity::address(&self.signing_key)
    }

    pub fn origin(&self) -> &str {
        if self.config.original_package_id.is_empty() {
            &self.config.package_id
        } else {
            &self.config.original_package_id
        }
    }

    pub async fn rpc(&self, method: &str, params: Value) -> anyhow::Result<Value> {
        let response = self
            .http
            .post(&self.config.rpc_url)
            .json(&json!({"jsonrpc":"2.0", "id":1, "method":method, "params":params}))
            .send()
            .await?
            .error_for_status()?
            .json::<Value>()
            .await?;
        anyhow::ensure!(
            response.get("error").is_none(),
            "{method}: {}",
            response["error"]
        );
        response
            .get("result")
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("{method}: missing result"))
    }

    pub async fn deployments(&self, cursor: &Option<Value>) -> anyhow::Result<Value> {
        self.rpc("suix_queryEvents", json!([
            {"MoveEventType":format!("{}::deployment_manager::DeploymentCreated", self.origin())},
            cursor, 50, false
        ])).await
    }

    pub async fn latest_cursor(&self) -> anyhow::Result<Option<Value>> {
        let response = self.rpc("suix_queryEvents", json!([
            {"MoveEventType":format!("{}::deployment_manager::DeploymentCreated", self.origin())},
            null, 1, true
        ])).await?;
        let data = response["data"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("invalid event page"))?;
        Ok(data.first().map(|event| event["id"].clone()))
    }

    pub async fn optional_object(&self, id: &str) -> anyhow::Result<Option<Value>> {
        let response = self
            .rpc(
                "sui_getObject",
                json!([id, {"showContent":true,"showOwner":true}]),
            )
            .await?;
        if let Some(error) = response.get("error") {
            if matches!(error["code"].as_str(), Some("deleted" | "notExists")) {
                return Ok(None);
            }
            anyhow::bail!("object {id}: {error}");
        }
        response
            .get("data")
            .cloned()
            .map(Some)
            .ok_or_else(|| anyhow::anyhow!("object {id} has no data"))
    }

    pub async fn object(&self, id: &str) -> anyhow::Result<Value> {
        self.optional_object(id)
            .await?
            .ok_or_else(|| anyhow::anyhow!("object {id} is deleted or absent"))
    }

    pub async fn validate_cap(&self, cap_id: &str, device_id: &str) -> anyhow::Result<()> {
        let cap = self.object(cap_id).await?;
        anyhow::ensure!(
            cap["content"]["type"].as_str()
                == Some(&format!("{}::device_cap::DeviceCap", self.origin())),
            "wrong DeviceCap type"
        );
        anyhow::ensure!(
            same_id(
                cap["owner"]["AddressOwner"].as_str().unwrap_or(""),
                &self.address()
            ),
            "DeviceCap is not owned by device key"
        );
        anyhow::ensure!(
            same_id(
                cap["content"]["fields"]["device_id"].as_str().unwrap_or(""),
                device_id
            ),
            "DeviceCap belongs to another device"
        );
        let binding = self
            .rpc(
                "suix_getDynamicFieldObject",
                json!([cap_id,
            {"type":format!("{}::device_cap::DeviceAddressKey", self.origin()),"value":{}}]),
            )
            .await?;
        anyhow::ensure!(
            same_id(
                binding["data"]["content"]["fields"]["value"]
                    .as_str()
                    .unwrap_or(""),
                &self.address()
            ),
            "DeviceCap has no matching signer binding; reissue it with the upgraded contracts"
        );
        Ok(())
    }

    pub fn load(config: SuiConfig, keypair_path: &Path) -> anyhow::Result<Self> {
        let raw = std::fs::read_to_string(keypair_path)?;
        let bytes = hex::decode(raw.trim())?;
        let key_bytes: [u8; 32] = bytes.try_into().map_err(|_| anyhow::anyhow!("keypair must be 32 hex bytes"))?;
        let signing_key = SigningKey::from_bytes(&key_bytes);

        Ok(Self {
            config,
            signing_key,
            http: reqwest::Client::builder().timeout(Duration::from_secs(30)).build()?,
        })
    }

    /// Subscribe to DeploymentCreated events via WebSocket and yield events
    /// targeting any of the given group IDs. Reconnects automatically on disconnect.
    pub async fn subscribe_deployments(
        &self,
        group_ids: &[String],
        tx: tokio::sync::mpsc::Sender<DeploymentCreatedEvent>,
    ) {
        let event_type = format!(
            "{}::deployment_manager::DeploymentCreated",
            self.config.package_id
        );

        loop {
            info!("Connecting to SUI WebSocket at {}", self.config.ws_url);
            match connect_async(&self.config.ws_url).await {
                Err(e) => {
                    error!("WebSocket connect failed: {e}. Retrying in 10s…");
                    tokio::time::sleep(Duration::from_secs(10)).await;
                    continue;
                }
                Ok((mut ws, _)) => {
                    // Subscribe to move events
                    let sub_msg = json!({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "suix_subscribeEvent",
                        "params": [{
                            "MoveEventType": event_type
                        }]
                    });
                    if let Err(e) = ws.send(Message::Text(sub_msg.to_string())).await {
                        error!("Subscribe send failed: {e}");
                        continue;
                    }

                    while let Some(msg) = ws.next().await {
                        match msg {
                            Ok(Message::Text(text)) => {
                                debug!("WS message: {text}");
                                if let Ok(event) = parse_deployment_event(&text, &event_type) {
                                    let relevant = event.target_groups.iter().any(|g| group_ids.contains(g));
                                    if relevant {
                                        info!("Deployment {} targets one of our groups", event.deployment_id);
                                        let _ = tx.send(event).await;
                                    }
                                }
                            }
                            Ok(Message::Close(_)) | Err(_) => {
                                warn!("WebSocket closed. Reconnecting in 5s…");
                                tokio::time::sleep(Duration::from_secs(5)).await;
                                break;
                            }
                            _ => {}
                        }
                    }
                }
            }
        }
    }

    /// Submit a report_status transaction to the SUI chain.
    pub async fn report_status(
        &self,
        package_id: &str,
        _registry_id: &str,
        deployment_id: &str,
        device_cap_id: &str,
        status: u8,
    ) -> anyhow::Result<String> {
        // Build a programmable transaction block via the JSON-RPC unsafe_moveCall
        // endpoint. In production this would use PTBs; here we use the simpler
        // moveCall RPC for clarity.
        let sender = self.sui_address();
        info!("Reporting status {} for deployment {}", status, deployment_id);

        let res = self
            .http
            .post(&self.config.rpc_url)
            .json(&json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "unsafe_moveCall",
                "params": [
                    sender,
                    package_id,
                    "deployment_manager",
                    "report_status",
                    [],
                    [deployment_id, device_cap_id, status.to_string(), "0x6"],
                    null,
                    "10000000",
                    null
                ]
            }))
            .send()
            .await?
            .json::<Value>()
            .await?;

        let tx_bytes = res["result"]["txBytes"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("no txBytes in response: {res}"))?;

        let digest = self.sign_and_execute(tx_bytes).await?;
        info!("Status reported. Digest: {digest}");
        Ok(digest)
    }

    // ── Internals ─────────────────────────────────────────────────────────────

    fn sui_address(&self) -> String {
        crate::identity::address(&self.signing_key)
    }

    async fn sign_and_execute(&self, tx_bytes_b64: &str) -> anyhow::Result<String> {
        use base64::{engine::general_purpose::STANDARD, Engine};
        let tx_bytes = STANDARD.decode(tx_bytes_b64)?;

        let full_sig = crate::identity::signature(&self.signing_key, &tx_bytes);
        let sig_b64 = STANDARD.encode(&full_sig);

        let res = self
            .http
            .post(&self.config.rpc_url)
            .json(&json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sui_executeTransactionBlock",
                "params": [
                    tx_bytes_b64,
                    [sig_b64],
                    {"showEffects": true},
                    "WaitForLocalExecution"
                ]
            }))
            .send()
            .await?
            .json::<Value>()
            .await?;

        res["result"]["digest"]
            .as_str()
            .map(|s| s.to_string())
            .ok_or_else(|| anyhow::anyhow!("no digest in execute response: {res}"))
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn parse_deployment_event(text: &str, expected_type: &str) -> anyhow::Result<DeploymentCreatedEvent> {
    let v: Value = serde_json::from_str(text)?;
    let params = v.get("params").ok_or(anyhow::anyhow!("no params"))?;
    let result = params.get("result").ok_or(anyhow::anyhow!("no result"))?;
    let ev_type = result["type"].as_str().unwrap_or("");
    anyhow::ensure!(ev_type == expected_type, "wrong event type: {ev_type}");
    Ok(serde_json::from_value(result["parsedJson"].clone())?)
}

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
