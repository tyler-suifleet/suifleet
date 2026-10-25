//! Direct-chain fleet producer. Reads the chain's cadence/sequence on every
//! attempt, including after uncertain execution, so restart cannot reset limits.
use std::{path::PathBuf, str::FromStr, sync::Arc, time::Duration};
use serde::{Deserialize, Serialize};
use sui_sdk_types::{Address, TypeTag};
use crate::{identity::TxSigner, sui_client::{SuiClient, Owner, same_type}, telemetry};

pub const MAX_HEALTH: usize = 16_384;
pub const MAX_ARCHIVE: usize = 4 * 1024 * 1024;
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub package_id: String,
    pub type_origin: String,
    pub fleet_id: String,
    pub budget_id: String,
    pub storage_id: String,
    pub writer_id: String,
    pub account_id: String,
    #[serde(default)]
    pub archives: Option<crate::fleet_archive::Config>,
    #[serde(default)]
    pub managed: Option<ManagedFleet>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ManagedFleet {
    pub customer_id: String,
    pub fleet_id: String,
}
impl Config {
    pub fn validate(&self) -> anyhow::Result<()> {
        for id in [
            &self.package_id,
            &self.type_origin,
            &self.fleet_id,
            &self.budget_id,
            &self.storage_id,
            &self.writer_id,
            &self.account_id,
        ] {
            Address::from_str(id)?;
        }
        if let Some(managed) = &self.managed {
            Address::from_str(&managed.customer_id)?;
            Address::from_str(&managed.fleet_id)?;
        }
        if let Some(archives) = &self.archives {
            archives.validate()?;
        }
        Ok(())
    }
}
#[derive(Debug, Deserialize)]
pub struct Fleet {
    pub id: Address, pub version: u64, pub budget_id: Address, pub storage_id: Address,
    pub registry_id: Address, pub chain_identifier: String, pub key_epoch: u64,
    pub support: Vec<Address>, pub health_interval_ms: u64, pub batch_interval_ms: u64,
    pub emergency_interval_ms: u64, pub device_count: u64,
}
#[derive(Debug, Deserialize)]
pub struct Writer {
    pub id: Address, pub version: u64, pub fleet_id: Address, pub device_id: Address,
    pub account_id: Address, pub health_seq: u64, pub health_at_ms: u64,
    pub message: Vec<u8>, pub signature: Vec<u8>, pub batch_seq: u64,
    pub batch_at_ms: Option<u64>, pub emergency_at_ms: Option<u64>, pub anchor_count: u64,
}
impl Config {
    pub async fn read(&self, sui: &SuiClient, device_id: &str) -> anyhow::Result<(Fleet, Writer)> {
        if let Some(managed) = &self.managed {
            let operator = sui.operator(device_id).await?;
            anyhow::ensure!(
                crate::sui_client::same_id(&operator, &managed.fleet_id),
                "managed operator mismatch"
            );
            let fleet = sui.object(&managed.fleet_id).await?;
            anyhow::ensure!(
                crate::sui_client::same_id(
                    fleet.fields["customer_id"].as_str().unwrap_or(""),
                    &managed.customer_id
                ) && crate::sui_client::same_id(
                    fleet.fields["budget_id"].as_str().unwrap_or(""),
                    &self.budget_id
                ),
                "managed fleet data binding mismatch"
            );
        }
        let (kind, owner, bytes) = sui.move_object(&self.fleet_id).await?;
        anyhow::ensure!(same_type(&kind, &format!("{}::fleet_data::FleetData", self.type_origin))
            && matches!(owner, Owner::Shared { .. }), "unexpected fleet-data type/ownership");
        let fleet: Fleet = bcs::from_bytes(&bytes)?;
        anyhow::ensure!(fleet.id == Address::from_str(&self.fleet_id)? && fleet.version == 1
            && fleet.budget_id == Address::from_str(&self.budget_id)?
            && fleet.storage_id == Address::from_str(&self.storage_id)?
            && fleet.registry_id == Address::from_str(&sui.manifest().shared_objects.registry.id)?
            && fleet.chain_identifier == sui.manifest().chain_identifier, "fleet-data binding/version mismatch");
        anyhow::ensure!((60_000..=86_400_000).contains(&fleet.health_interval_ms), "invalid health cadence");
        let device = Address::from_str(device_id)?;
        let key: TypeTag = format!("{}::fleet_data::DeviceKey", self.type_origin).parse()?;
        let index_id = fleet.id.derive_dynamic_child_id(&key, device.inner());
        sui.watch_child(&index_id.to_string(), &self.fleet_id);
        let index = sui.object(&index_id.to_string()).await?;
        anyhow::ensure!(
            index.fields["value"]
                .as_str()
                .is_some_and(|v| crate::sui_client::same_id(v, &self.writer_id)),
            "writer is not fleet indexed"
        );
        let (kind, owner, bytes) = sui.move_object(&self.writer_id).await?;
        anyhow::ensure!(same_type(&kind, &format!("{}::fleet_data::DeviceData", self.type_origin))
            && matches!(owner, Owner::Shared { .. }), "unexpected writer type/ownership");
        let writer: Writer = bcs::from_bytes(&bytes)?;
        anyhow::ensure!(writer.id == Address::from_str(&self.writer_id)? && writer.version == 1
            && writer.fleet_id == fleet.id && writer.device_id == device
            && writer.account_id == Address::from_str(&self.account_id)?, "writer binding/version mismatch");
        let (_, account) = sui.budget(&self.budget_id, device_id).await?;
        anyhow::ensure!(account.as_ref().is_some_and(|(id, a)| crate::sui_client::same_id(id, &self.account_id) && !a.closed && !a.suspended), "writer account is not current/active");
        Ok((fleet, writer))
    }
}

/// Archive schema shared with the browser. No arbitrary signing payloads.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Record { pub source: String, pub line: String }
/// Schema 2 (SF-084): the device's shadow documents when it archived, for the
/// fleet owner and support roles only. Shadow contents never enter public health.
pub const MAX_SHADOW_SNAPSHOT: usize = 512 * 1024;
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowDocument {
    /// `""` is the classic shadow.
    pub name: String, pub version: u64, pub updated_ms: u64,
    pub desired: Option<serde_json::Map<String, serde_json::Value>>,
    pub reported: Option<serde_json::Map<String, serde_json::Value>>,
    /// The desired keys that differ from reported (the shadow store's `delta` function).
    pub delta: serde_json::Map<String, serde_json::Value>,
}
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowSnapshot {
    /// Existing shadows by name; those beyond the size limit are only counted.
    pub documents: Vec<ShadowDocument>, pub omitted: u64,
}
impl ShadowSnapshot {
    pub fn validate(&self) -> anyhow::Result<()> {
        let safe = |n: u64| n <= 9_007_199_254_740_991;
        anyhow::ensure!(self.documents.len() <= crate::shadows::MAX_SHADOWS && safe(self.omitted)
            && serde_json::to_vec(self)?.len() <= MAX_SHADOW_SNAPSHOT, "shadow snapshot limit");
        anyhow::ensure!(self.documents.windows(2).all(|w| w[0].name < w[1].name), "shadow snapshot order");
        for d in &self.documents {
            let mut document = serde_json::Map::new();
            if let Some(v) = &d.desired { document.insert("desired".into(), v.clone().into()); }
            if let Some(v) = &d.reported { document.insert("reported".into(), v.clone().into()); }
            anyhow::ensure!(crate::shadows::valid_name(&d.name) && safe(d.version) && safe(d.updated_ms)
                && crate::shadows::check_document(&document).is_ok()
                && d.delta == crate::shadows::delta(d.desired.as_ref(), d.reported.as_ref()), "invalid shadow document");
        }
        Ok(())
    }
}
/// Exact BCS layout of the compact root; u256 is 32 little-endian bytes.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CompactAnchor {
    pub seq: u64, pub kind: u8, pub blob_object_id: Address, pub blob_id: [u8; 32],
    pub ciphertext_sha256: Vec<u8>, pub signer: Address, pub at_ms: u64,
    pub end_epoch: u32, pub key_epoch: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CompactHead {
    pub first_index: u64, pub index: u64, pub anchor: CompactAnchor, pub previous_hash: Vec<u8>,
}
impl CompactHead {
    pub fn parse(bytes: &[u8]) -> anyhow::Result<Self> {
        anyhow::ensure!(bytes.len() <= 512, "compact archive head exceeds limit");
        let head: Self = bcs::from_bytes(bytes)?;
        anyhow::ensure!(head.first_index <= head.index && head.anchor.seq > 0 && head.anchor.kind <= 2
            && head.anchor.ciphertext_sha256.len() == 32
            && matches!(head.previous_hash.len(), 0 | 32), "invalid compact archive head");
        Ok(head)
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuiltItem {
    pub identifier: String, pub size: u64, pub sha256: String,
}
impl QuiltItem {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(!self.identifier.is_empty() && self.identifier.len() <= 64
            && self.identifier != "manifest" && self.identifier.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
            && self.size > 0 && self.size <= MAX_ARCHIVE as u64
            && self.sha256.len() == 64 && self.sha256.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)), "invalid Quilt item descriptor");
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Archive {
    pub schema: u8, pub kind: String, pub chain_identifier: String, pub registry: String,
    pub fleet_id: String, pub device_id: String, pub signer: String, pub seq: u64,
    pub observed_ms: u64, pub reason: String, pub dropped_lines: u64, pub records: Vec<Record>,
    /// Present exactly in schema 2; devices without shadows keep schema 1.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub shadows: Option<ShadowSnapshot>,
    /// Schema 3: exact prior on-chain CompactHead BCS bytes (empty at start).
    /// Its hash is checked against the new head before history is trusted.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub previous_head: Option<Vec<u8>>,
    /// Schema 4: independently signed/encrypted patches in the same Quilt.
    /// Their identifiers are locators, SHA256 binds each exact ciphertext.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub items: Option<Vec<QuiltItem>>,
}
impl Archive {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(self.kind == "fleet-data" && match self.schema {
            1 => self.shadows.is_none() && self.previous_head.is_none() && self.items.is_none(),
            2 => self.shadows.is_some() && self.previous_head.is_none() && self.items.is_none(),
            3 => self.previous_head.is_some() && self.items.is_none(),
            4 => self.items.as_ref().is_some_and(|items| (2..=16).contains(&items.len())) && self.records.is_empty(),
            _ => false,
        }, "unsupported archive schema/kind");
        if let Some(items) = &self.items {
            let mut names = std::collections::HashSet::new();
            let mut bytes = 0u64;
            for item in items { item.validate()?; bytes += item.size; anyhow::ensure!(names.insert(&item.identifier), "duplicate Quilt item"); }
            anyhow::ensure!(bytes <= MAX_ARCHIVE as u64, "Quilt ciphertext quota exceeded");
        }
        if let Some(previous) = &self.previous_head {
            if !previous.is_empty() {
                let head = CompactHead::parse(previous)?;
                anyhow::ensure!(head.anchor.seq < self.seq, "archive history does not move backwards");
            }
        }
        if let Some(shadows) = &self.shadows { shadows.validate()?; }
        anyhow::ensure!(["logs", "diagnostics", "crash"].contains(&self.reason.as_str()), "invalid archive reason");
        anyhow::ensure!(self.seq > 0 && [self.seq, self.observed_ms, self.dropped_lines].iter().all(|n| *n <= 9_007_199_254_740_991), "archive integer out of range");
        anyhow::ensure!(self.records.len() <= 4096 && self.records.iter().all(|r| !r.source.is_empty() && r.source.len() <= crate::logs::MAX_SOURCE && r.line.len() <= crate::logs::MAX_LINE && !r.line.contains(['\n','\r']) && (r.source == "daemon" || (r.source.starts_with("component:") && r.source.len()>10))), "archive record limit");
        Ok(())
    }
}

pub struct Task {
    pub config: Config, pub sui: SuiClient, pub signer: Arc<dyn TxSigner>, pub cap: String,
    pub device_id: String, pub state_dir: PathBuf, pub sync: telemetry::SharedSync,
    pub services: Arc<std::sync::Mutex<crate::services::Runtime>>, pub started_ms: u64,
    pub poll_seconds: u64,
    pub overlay_dir: PathBuf,
    pub log_settings: Option<crate::config::LogsConfig>,
    pub budget: Option<crate::funding::BudgetConfig>,
}
impl Task {
    pub async fn before_write(&self) -> anyhow::Result<()> {
        if let Some(budget) = &self.budget {
            let mut funding = crate::funding::Funding::default();
            crate::funding::top_up(
                budget,
                &self.sui,
                &self.device_id,
                &self.cap,
                &mut funding,
                telemetry::now_ms(),
            )
            .await?;
            // The main poll persists this shared record in the journal and telemetry.
        }
        Ok(())
    }
    async fn beat(&self, tracker: &mut crate::service_health::Tracker, settings: Option<&crate::fleet_settings::Settings>) -> anyhow::Result<Duration> {
        self.sui.validate_deployment().await?;
        self.sui.validate_cap(&self.cap, &self.device_id).await?;
        let (fleet, writer) = self.config.read(&self.sui, &self.device_id).await?;
        // Cadence uses chain time, never the device's possibly fast clock.
        let clock = self.sui.object("0x6").await?;
        let now = clock.fields["timestamp_ms"]
            .as_str()
            .and_then(|s| s.parse::<u64>().ok())
            .or_else(|| clock.fields["timestamp_ms"].as_u64())
            .ok_or_else(|| anyhow::anyhow!("invalid chain clock"))?;
        let interval = crate::fleet_settings::health_interval_ms(fleet.health_interval_ms, settings);
        if writer.health_seq != 0
            && now < writer.health_at_ms.saturating_add(interval)
        {
            return Ok(Duration::from_millis(
                writer.health_at_ms.saturating_add(interval) - now,
            ));
        }
        let seq = writer
            .health_seq
            .checked_add(1)
            .filter(|n| *n <= 9_007_199_254_740_991)
            .ok_or_else(|| anyhow::anyhow!("health sequence exhausted"))?;
        let binding = telemetry::Binding {
            chain_identifier: fleet.chain_identifier,
            registry: fleet.registry_id.to_string(),
            device_id: self.device_id.clone(),
            interval_seconds: interval / 1000,
            poll_seconds: self.poll_seconds,
            started_ms: self.started_ms,
            boot_id: telemetry::boot_id(),
        };
        let funding = self.sui.funding.lock().await.clone();
        let mut inventory = telemetry::inventory_with_funding(&self.state_dir, self.budget.as_ref().map(|_| &funding));
        let sources = self.services.lock().unwrap().health_sources();
        inventory.services = crate::service_health::collect(&sources, tracker, self.started_ms, telemetry::now_ms());
        let snapshot = telemetry::snapshot_limit(&binding, &self.signer.address(), seq, 0,
            self.sync.lock().unwrap().clone(), inventory, telemetry::now_ms(), MAX_HEALTH);
        let message = serde_json::to_vec(&snapshot)?;
        anyhow::ensure!(
            message.len() <= MAX_HEALTH,
            "health summary exceeds chain limit"
        );
        let signature = self.signer.sign_personal_message(&message)?;
        self.before_write().await?;
        let digest = self
            .sui
            .report_health(&self.config, &self.cap, seq, message, signature)
            .await?;
        crate::fleet_archive::health_receipt(&self.state_dir, seq, &digest)?;
        Ok(Duration::from_millis(interval))
    }
    pub async fn run(self) {
        let mut tracker = crate::service_health::Tracker::default();
        let mut applied = crate::fleet_settings::Applied::new("fleet data");
        let mut next_health = tokio::time::Instant::now();
        let mut archive = crate::fleet_archive::Schedule::default();
        let mut generation = self.sui.watch_generation();
        loop {
            applied.refresh(&self.sui, &self.config.budget_id).await;
            let settings = applied.current().copied();
            let current = self.sui.watch_generation();
            if generation != current {
                generation = current;
                next_health = tokio::time::Instant::now();
                archive.wake();
            }
            if tokio::time::Instant::now() >= next_health {
                let delay = match self.beat(&mut tracker, settings.as_ref()).await {
                    Ok(delay) => delay,
                    Err(error) => {
                        tracing::warn!(%error, "Direct-chain health pending; will re-read authority/cadence before retry");
                        Duration::from_secs(self.poll_seconds.clamp(1, 60))
                    }
                };
                next_health = tokio::time::Instant::now() + delay;
            }
            if self.config.archives.is_some() {
                let result = crate::fleet_archive::tick(&self, &mut archive, settings.as_ref()).await;
                if let Err(error) = &result {
                    archive.next_due = tokio::time::Instant::now()
                        + Duration::from_secs(self.poll_seconds.clamp(1, 60));
                    tracing::warn!(%error, "Encrypted fleet archive pending; retained for retry");
                }
                if let Err(error) = crate::fleet_archive::status(&self.state_dir, result.is_err()) {
                    tracing::warn!(%error, "Archive publication status could not be persisted");
                }
            }
            let wait = Duration::from_secs(self.poll_seconds.clamp(1, 60));
            tokio::time::sleep(wait).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn quilt_manifest_binds_bounded_unique_ciphertexts_and_history() {
        let item = QuiltItem { identifier: "logs-1".into(), size: 128, sha256: "ab".repeat(32) };
        let mut archive = Archive { schema: 4, kind: "fleet-data".into(), chain_identifier: "4c78adac".into(),
            registry: "0x1".into(), fleet_id: "0x2".into(), device_id: "0x3".into(), signer: "0x4".into(), seq: 1,
            observed_ms: 1, reason: "logs".into(), dropped_lines: 0, records: vec![], shadows: None,
            previous_head: Some(vec![]), items: Some(vec![item.clone(), QuiltItem {identifier:"diagnostics-2".into(), ..item.clone()}]) };
        archive.validate().unwrap();
        let bytes = serde_json::to_vec(&archive).unwrap();
        serde_json::from_slice::<Archive>(&bytes).unwrap().validate().unwrap();
        archive.items.as_mut().unwrap()[1].identifier = item.identifier.clone();
        assert!(archive.validate().is_err());
        archive.items = Some(vec![item.clone()]); assert!(archive.validate().is_err());
        for identifier in ["manifest", "../escape", ""] {
            assert!(QuiltItem {identifier:identifier.into(), ..item.clone()}.validate().is_err());
        }
        for sha256 in ["AB".repeat(32), "aa".repeat(31)] {
            assert!(QuiltItem {sha256, ..item.clone()}.validate().is_err());
        }
        assert!(QuiltItem {size:0, ..item}.validate().is_err());
    }
    #[test]
    fn compact_head_matches_browser_bcs_and_rejects_invalid_history() {
        let vector: serde_json::Value = serde_json::from_str(include_str!("../../interfaces/compat/compact-head.json")).unwrap();
        let bytes = hex::decode(vector["bcs_hex"].as_str().unwrap()).unwrap();
        let mut head = CompactHead::parse(&bytes).unwrap();
        assert_eq!(head.anchor.seq, 2);
        assert_eq!(head.anchor.blob_id[0], 123);
        assert_eq!(bcs::to_bytes(&head).unwrap(), bytes);
        head.first_index = 2;
        assert!(CompactHead::parse(&bcs::to_bytes(&head).unwrap()).is_err());
        assert!(CompactHead::parse(&vec![0;513]).is_err());
        head.first_index = 0; head.previous_hash = vec![0;31];
        assert!(CompactHead::parse(&bcs::to_bytes(&head).unwrap()).is_err());
    }
    #[test]
    fn compact_archive_schema_keeps_legacy_shapes_and_requires_backward_link() {
        let vector: serde_json::Value = serde_json::from_str(include_str!("../../interfaces/compat/compact-head.json")).unwrap();
        let previous = hex::decode(vector["bcs_hex"].as_str().unwrap()).unwrap();
        let mut a = Archive { schema: 3, kind: "fleet-data".into(), chain_identifier: "4c78adac".into(), registry: "0x1".into(),
            fleet_id: "0x2".into(), device_id: "0x3".into(), signer: "0x4".into(), seq: 3, observed_ms: 1,
            reason: "logs".into(), dropped_lines: 0, records: vec![], shadows: None, previous_head: Some(previous), items: None };
        a.validate().unwrap();
        a.seq = 2; assert!(a.validate().is_err()); a.seq = 3;
        a.schema = 1; assert!(a.validate().is_err()); a.schema = 3;
        a.previous_head = None; assert!(a.validate().is_err());
        a.previous_head = Some(vec![]); a.validate().unwrap();
    }
    #[test]
    fn archive_shape_rejects_unsafe_integers_unknown_fields_and_oversized_records() {
        let mut a = Archive { schema: 1, kind: "fleet-data".into(), chain_identifier: "4c78adac".into(), registry: "0x1".into(),
            fleet_id: "0x2".into(), device_id: "0x3".into(), signer: "0x4".into(), seq: 1, observed_ms: 1,
            reason: "logs".into(), dropped_lines: 0, records: vec![Record { source: "daemon".into(), line: "ready".into() }], shadows: None, previous_head: None, items: None };
        a.validate().unwrap();
        a.seq = 9_007_199_254_740_992; assert!(a.validate().is_err()); a.seq = 1;
        a.records[0].line = "x".repeat(4097); assert!(a.validate().is_err()); a.records.clear();
        let mut value = serde_json::to_value(a).unwrap(); value["unexpected"] = true.into();
        assert!(serde_json::from_value::<Archive>(value).is_err());
    }
    #[test]
    fn schema_two_carries_bounded_consistent_shadow_documents() {
        use serde_json::json;
        let doc = |name: &str, desired: serde_json::Value, reported: serde_json::Value| {
            let (desired, reported) = (desired.as_object().cloned(), reported.as_object().cloned());
            ShadowDocument { name: name.into(), version: 3, updated_ms: 5, delta: crate::shadows::delta(desired.as_ref(), reported.as_ref()), desired, reported }
        };
        let snapshot = ShadowSnapshot { documents: vec![doc("", json!({"mode": "eco", "fan": {"speed": 2}}), json!({"mode": "eco", "fan": {"speed": 1}})),
            doc("cfg", json!(null), json!({"uptime": 9}))], omitted: 0 };
        assert_eq!(snapshot.documents[0].delta, json!({"fan": {"speed": 2}}).as_object().cloned().unwrap());
        let mut a = Archive { schema: 2, kind: "fleet-data".into(), chain_identifier: "4c78adac".into(), registry: "0x1".into(),
            fleet_id: "0x2".into(), device_id: "0x3".into(), signer: "0x4".into(), seq: 1, observed_ms: 1,
            reason: "logs".into(), dropped_lines: 0, records: vec![Record { source: "daemon".into(), line: "Shadow state snapshot".into() }],
            shadows: Some(snapshot.clone()), previous_head: None, items: None };
        a.validate().unwrap();
        // Round trip, and schema 1 bytes never mention shadows.
        let bytes = serde_json::to_vec(&a).unwrap();
        a = serde_json::from_slice(&bytes).unwrap();
        a.validate().unwrap();
        let mut one = serde_json::from_slice::<Archive>(&bytes).unwrap(); (one.schema, one.shadows) = (1, None);
        assert!(!String::from_utf8(serde_json::to_vec(&one).unwrap()).unwrap().contains("shadows"));
        one.validate().unwrap();
        // Schema and field must agree.
        one.schema = 2; assert!(one.validate().is_err());
        a.schema = 1; assert!(a.validate().is_err()); a.schema = 2;
        let mut value = serde_json::to_value(&a).unwrap(); value["shadows"] = serde_json::Value::Null;
        assert!(serde_json::from_value::<Archive>(value).unwrap().validate().is_err());
        let mut value = serde_json::to_value(&a).unwrap(); value["shadows"]["documents"][0]["extra"] = true.into();
        assert!(serde_json::from_value::<Archive>(value).is_err());
        let check = |edit: &dyn Fn(&mut ShadowSnapshot)| { let mut s = snapshot.clone(); edit(&mut s); s.validate() };
        assert!(check(&|_| ()).is_ok());
        assert!(check(&|s| s.documents[0].delta = serde_json::Map::new()).is_err(), "inconsistent delta");
        assert!(check(&|s| s.documents.swap(0, 1)).is_err(), "unsorted");
        assert!(check(&|s| s.documents[1].name = String::new()).is_err(), "duplicate");
        assert!(check(&|s| s.documents[1].name = "bad name".into()).is_err());
        assert!(check(&|s| s.documents[1].version = 9_007_199_254_740_992).is_err());
        assert!(check(&|s| { s.documents[1].reported.as_mut().unwrap().insert("x".into(), serde_json::Value::Null); }).is_err(), "null value");
        assert!(check(&|s| { s.documents[1].reported.as_mut().unwrap().insert("x".into(), "y".repeat(8200).into()); }).is_err(), "document size");
    }

    #[test]
    fn shared_archive_shadow_cases() {
        let cases: serde_json::Value = serde_json::from_str(include_str!("../../interfaces/compat/archive-shadow-cases.json")).unwrap();
        for case in cases["cases"].as_array().unwrap() {
            let accepted = serde_json::from_value::<ShadowSnapshot>(case["shadows"].clone()).map_err(anyhow::Error::from).and_then(|s| s.validate());
            assert_eq!(accepted.is_ok(), case["valid"].as_bool().unwrap(), "{}: {accepted:?}", case["name"]);
        }
    }

    #[test]
    fn shadow_store_snapshot_is_ordered_complete_and_bounded() {
        let dir = std::env::temp_dir().join(format!("suifleet-shadow-snapshot-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut store = crate::shadows::Store::open(&dir, 128, "thing", None).unwrap();
        assert_eq!(store.snapshot(), ShadowSnapshot::default());
        store.update("b", &serde_json::json!({"state": {"desired": {"on": true}, "reported": {"on": false}}}), 7).unwrap();
        store.update("", &serde_json::json!({"state": {"reported": {"t": 1}}}), 8).unwrap();
        store.update("gone", &serde_json::json!({"state": {"reported": {"t": 1}}}), 8).unwrap();
        store.delete("gone", &serde_json::json!({}), 9).unwrap();
        let snapshot = store.snapshot();
        snapshot.validate().unwrap();
        assert_eq!(snapshot.documents.iter().map(|d| d.name.as_str()).collect::<Vec<_>>(), ["", "b"], "deleted shadows are not listed");
        assert_eq!((snapshot.documents[1].version, snapshot.documents[1].updated_ms), (1, 7));
        assert_eq!(serde_json::Value::Object(snapshot.documents[1].delta.clone()), serde_json::json!({"on": true}));
        // Large documents: those beyond the limit are counted, never cut.
        let big = "v".repeat(8000);
        for i in 0..100 { store.update(&format!("n{i:03}"), &serde_json::json!({"state": {"reported": {"k": big}}}), 10).unwrap(); }
        let snapshot = store.snapshot();
        snapshot.validate().unwrap();
        assert!(snapshot.omitted > 0 && snapshot.documents.len() + snapshot.omitted as usize == 102);
        assert!(serde_json::to_vec(&snapshot).unwrap().len() <= MAX_SHADOW_SNAPSHOT);
        drop(store);
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn native_encryption_needs_threshold_and_authenticates_ciphertext() {
        use ark_ec::{PrimeGroup, CurveGroup};
        use std::collections::HashMap;
        let secrets = [crate::seal::random_scalar(), crate::seal::random_scalar(), crate::seal::random_scalar()];
        let servers: Vec<_> = secrets.iter().enumerate().map(|(i,s)| ([i as u8;32], (ark_bls12_381::G2Projective::generator()*s).into_affine())).collect();
        let id = vec![42;56]; let package = [9;32];
        let mut encrypted = crate::seal::encrypt(package, id, &servers, 2, b"binding".to_vec(), b"private redacted logs").unwrap();
        let object = crate::seal::parse(&encrypted).unwrap();
        let keys: HashMap<_,_> = secrets.iter().enumerate().map(|(i,s)| ([i as u8;32], (crate::seal::hash_identity(&object.full_id())*s).into_affine())).collect();
        let public: HashMap<_,_> = servers.into_iter().collect();
        let one = keys.iter().take(1).map(|(k,v)| (*k,*v)).collect();
        assert!(crate::seal::dem_key(&object, &one, &public).is_err());
        let key = crate::seal::dem_key(&object, &keys, &public).unwrap();
        let mut tampered = encrypted.clone(); tampered[object.blob.start] ^= 1;
        assert!(crate::seal::decrypt_in_place(&object, &key, &mut tampered).is_err());
        let range = crate::seal::decrypt_in_place(&object, &key, &mut encrypted).unwrap();
        assert_eq!(&encrypted[range], b"private redacted logs");
    }
}
