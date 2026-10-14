use crate::{config::SuiConfig, identity};
use ed25519_dalek::SigningKey;
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::Path;

#[derive(Debug, Clone, Deserialize)]
pub struct DeploymentCreatedEvent {
    pub deployment_id: String,
    pub component_name: String,
    pub version: String,
    pub recipe_blob_id: String,
    pub artifact_sha256: String,
    pub target_groups: Vec<String>,
    pub target_devices: Vec<String>,
    pub creator: String,
}

#[derive(Clone)]
pub struct SuiClient {
    config: SuiConfig,
    signing_key: SigningKey,
    http: reqwest::Client,
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

impl SuiClient {
    pub fn load(config: SuiConfig, path: &Path) -> anyhow::Result<Self> {
        Ok(Self {
            config,
            signing_key: identity::load(path)?,
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()?,
        })
    }
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
    /// A matching ID alone is not authority to update a device. Only its current
    /// operator may deploy; shared groups must belong to that operator too.
    pub async fn authorized_target(
        &self,
        event: &DeploymentCreatedEvent,
        device: &str,
    ) -> anyhow::Result<bool> {
        let object = self.object(device).await?;
        anyhow::ensure!(
            object["content"]["type"].as_str()
                == Some(&format!("{}::device_registry::Device", self.origin())),
            "wrong Device type"
        );
        let owner = object["owner"]["AddressOwner"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("Device has no operator owner"))?;
        if !same_id(owner, &event.creator) {
            return Ok(false);
        }
        anyhow::ensure!(
            same_id(
                object["content"]["fields"]["device_address"]
                    .as_str()
                    .unwrap_or(""),
                &self.address()
            ),
            "device key does not match registered address"
        );
        if event.target_devices.iter().any(|id| same_id(id, device)) {
            return Ok(true);
        }
        for id in &event.target_groups {
            let Some(group) = self.optional_object(id).await? else {
                continue;
            };
            let fields = &group["content"]["fields"];
            if group["content"]["type"].as_str()
                == Some(&format!("{}::device_group::DeviceGroup", self.origin()))
                && same_id(fields["admin"].as_str().unwrap_or(""), owner)
                && fields["device_ids"].as_array().is_some_and(|ids| {
                    ids.iter()
                        .any(|id| same_id(id.as_str().unwrap_or(""), device))
                })
            {
                return Ok(true);
            }
        }
        Ok(false)
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
    pub async fn report_status(
        &self,
        deployment: &str,
        cap: &str,
        status: u8,
    ) -> anyhow::Result<String> {
        let record = self.object(deployment).await?;
        let cap_object = self.object(cap).await?;
        let device_id = cap_object["content"]["fields"]["device_id"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("missing device_id"))?;
        let direct = self.rpc("suix_getDynamicFieldObject", json!([deployment,
            {"type":format!("{}::deployment_manager::TargetDevicesKey", self.origin()),"value":{}}])).await?;
        let targets = &direct["data"]["content"]["fields"]["value"];
        let mut function = "report_status";
        let mut arguments = vec![deployment.to_string(), cap.to_string()];
        if !targets.as_array().is_some_and(|ids| {
            ids.iter()
                .any(|id| same_id(id.as_str().unwrap_or(""), device_id))
        }) {
            let groups = self.rpc("suix_getDynamicFieldObject", json!([deployment,
                {"type":format!("{}::deployment_manager::TargetGroupsKey", self.origin()),"value":{}}])).await?;
            let ids = groups["data"]["content"]["fields"]["value"]
                .as_array()
                .ok_or_else(|| anyhow::anyhow!("missing target groups"))?;
            let mut found = None;
            for id in ids {
                let id = id
                    .as_str()
                    .ok_or_else(|| anyhow::anyhow!("invalid group ID"))?;
                let Some(group) = self.optional_object(id).await? else {
                    continue;
                };
                let fields = &group["content"]["fields"];
                if same_id(
                    fields["admin"].as_str().unwrap_or(""),
                    record["content"]["fields"]["creator"]
                        .as_str()
                        .unwrap_or(""),
                ) && fields["device_ids"].as_array().is_some_and(|ids| {
                    ids.iter()
                        .any(|id| same_id(id.as_str().unwrap_or(""), device_id))
                }) {
                    found = Some(id.to_string());
                    break;
                }
            }
            arguments.push(found.ok_or_else(|| anyhow::anyhow!("device is no longer targeted"))?);
            function = "report_status_for_group";
        }
        arguments.push(status.to_string());
        let built = self
            .rpc(
                "unsafe_moveCall",
                json!([
                    self.address(),
                    self.config.package_id,
                    "deployment_manager",
                    function,
                    [],
                    arguments,
                    null,
                    "10000000",
                    null
                ]),
            )
            .await?;
        let tx = built["txBytes"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("missing txBytes"))?;
        use base64::{engine::general_purpose::STANDARD, Engine};
        let sig = STANDARD.encode(identity::signature(
            &self.signing_key,
            &STANDARD.decode(tx)?,
        ));
        let executed = self
            .rpc(
                "sui_executeTransactionBlock",
                json!([
                    tx, [sig], {"showEffects":true}, "WaitForLocalExecution"
                ]),
            )
            .await?;
        execution_digest(&executed)
    }
}

fn execution_digest(response: &Value) -> anyhow::Result<String> {
    anyhow::ensure!(
        response["effects"]["status"]["status"] == "success",
        "transaction failed: {}",
        response["effects"]["status"]
    );
    response["digest"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| anyhow::anyhow!("missing transaction digest"))
}

#[cfg(test)]
mod tests {
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
        assert!(execution_digest(
            &json!({"digest":"abc", "effects":{"status":{"status":"failure","error":"abort"}}})
        )
        .is_err());
        assert_eq!(
            execution_digest(&json!({"digest":"abc", "effects":{"status":{"status":"success"}}}))
                .unwrap(),
            "abc"
        );
    }
    #[test]
    fn id_normalization_rejects_invalid_identifiers() {
        assert!(same_id("0x1", "0x01"));
        assert!(!same_id("", ""));
        assert!(!same_id("0xzz", "0xzz"));
    }
}
