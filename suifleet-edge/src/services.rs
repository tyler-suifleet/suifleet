//! Deployment-authenticated service configuration. The journal is the commit
//! record; service metadata is a recoverable projection, never a preflight input
//! that is allowed to mutate storage.
use crate::{bridge, config::{Config, PubsubConfig, StreamsConfig}, identity::TxSigner, ipc, pubsub, streams};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{collections::{BTreeMap, BTreeSet}, sync::{Arc, Mutex}};

/// Local-only allowlist. Recipe fields never deserialize this authority.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CredentialAuthorization {
    pub kind: CredentialKind,
    pub path: std::path::PathBuf,
    pub destination: String,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CredentialKind { StreamBearer, MqttPassword }

fn authorize_credential(base: &Config, kind: CredentialKind, path: &std::path::Path, destination: &str) -> anyhow::Result<()> {
    // Compare before any filesystem access: untrusted recipes cannot probe files.
    anyhow::ensure!(base.service_credentials.iter().any(|entry|
        entry.kind == kind && entry.path.as_os_str() == path.as_os_str() && entry.destination == destination),
        "fleet service credential is not authorized for this destination");
    anyhow::ensure!(path.is_absolute() && path.components().all(|c| matches!(c,
        std::path::Component::RootDir | std::path::Component::Normal(_))), "credential path must be absolute and normalized");
    anyhow::ensure!(std::fs::canonicalize(path)?.as_os_str() == path.as_os_str(), "credential path must not contain symlinks");
    let metadata = std::fs::symlink_metadata(path)?;
    use std::os::unix::fs::MetadataExt;
    anyhow::ensure!(metadata.is_file() && metadata.nlink() == 1
        && (metadata.uid() == 0 || metadata.uid() == unsafe { libc::geteuid() })
        && metadata.mode() & 0o077 == 0, "credential must be an operator-owned private regular file");
    Ok(())
}

fn authorize_credentials(base: &Config, services: &Services) -> anyhow::Result<()> {
    if let Some(bridge) = services.pubsub.as_ref().and_then(|p| p.bridge.as_ref()) {
        if let Some(path) = &bridge.password_file {
            let broker = bridge.broker.as_deref().ok_or_else(|| anyhow::anyhow!("a bridge password_file needs a broker"))?;
            authorize_credential(base, CredentialKind::MqttPassword, path, broker)?;
        }
    }
    for export in services.streams.iter().flat_map(|s| &s.definitions).filter_map(|d| d.export.as_ref()) {
        if let Some(path) = &export.bearer_token_file {
            authorize_credential(base, CredentialKind::StreamBearer, path, &export.url)?;
        }
    }
    Ok(())
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Grants {
    #[serde(default)] pub operations: Vec<String>,
    #[serde(default)] pub publish: Vec<String>,
    #[serde(default)] pub subscribe: Vec<String>,
    #[serde(default)] pub pubsub_policies: Vec<pubsub::Policy>,
    #[serde(default)] pub streams: Vec<String>,
    #[serde(default)] pub shadow_policies: Vec<crate::shadows::Policy>,
}
impl Grants {
    fn acl(&self, uid: u32) -> ipc::Acl {
        ipc::Acl { uid, operations: self.operations.clone(), publish: self.publish.clone(), subscribe: self.subscribe.clone(),
            pubsub_policies: self.pubsub_policies.clone(), streams: self.streams.clone(), shadow_policies: self.shadow_policies.clone() }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Services {
    pub version: u32,
    #[serde(default)] pub grants: BTreeMap<String, Grants>,
    #[serde(default, skip_serializing_if = "Option::is_none")] pub pubsub: Option<PubsubConfig>,
    #[serde(default, skip_serializing_if = "Option::is_none")] pub streams: Option<StreamsConfig>,
}
impl<'de> Deserialize<'de> for Services {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        struct Wire {
            version: u32,
            #[serde(default)] grants: BTreeMap<String, Grants>,
            #[serde(default)] pubsub: Option<PubsubConfig>,
            #[serde(default)] streams: Option<StreamsConfig>,
        }
        let value = serde_json::Value::deserialize(deserializer)?;
        let schema: serde_json::Value = serde_json::from_str(include_str!("../../interfaces/recipe.schema.json")).map_err(serde::de::Error::custom)?;
        validate_schema(&schema["properties"]["services"], &value).map_err(serde::de::Error::custom)?;
        let wire: Wire = serde_json::from_value(value).map_err(serde::de::Error::custom)?;
        Ok(Self { version: wire.version, grants: wire.grants, pubsub: wire.pubsub, streams: wire.streams })
    }
}

/// The same reviewed schema used by the upload client, including object-only
/// sections, unknown fields and numeric bounds (Serde alone accepts tuples).
fn validate_schema(schema: &serde_json::Value, value: &serde_json::Value) -> anyhow::Result<()> {
    use serde_json::Value;
    let valid_type = match schema["type"].as_str() {
        Some("object") => value.is_object(), Some("array") => value.is_array(),
        Some("string") => value.is_string(), Some("boolean") => value.is_boolean(),
        Some("integer") => value.as_u64().is_some(), _ => false,
    };
    anyhow::ensure!(valid_type, "invalid services field type");
    if let Some(constant) = schema.get("const") { anyhow::ensure!(constant == value, "unsupported services version"); }
    if let Some(values) = schema["enum"].as_array() { anyhow::ensure!(values.contains(value), "unsupported services value"); }
    if let Some(n) = value.as_u64() {
        anyhow::ensure!(schema["minimum"].as_u64().is_none_or(|min| n >= min)
            && schema["maximum"].as_u64().is_none_or(|max| n <= max), "services field out of bounds");
    }
    if let Value::Array(values) = value {
        anyhow::ensure!(schema["maxItems"].as_u64().is_none_or(|n| values.len() as u64 <= n), "too many services entries");
        for value in values { validate_schema(&schema["items"], value)?; }
    }
    if let Value::Object(values) = value {
        for field in schema["required"].as_array().into_iter().flatten() {
            anyhow::ensure!(values.contains_key(field.as_str().unwrap()), "missing services field");
        }
        for (name, value) in values {
            if let Some(field) = schema["properties"].get(name) { validate_schema(field, value)?; }
            else {
                anyhow::ensure!(schema["additionalProperties"] != false, "unknown services field");
                if schema["additionalProperties"].is_object() { validate_schema(&schema["additionalProperties"], value)?; }
            }
        }
    }
    Ok(())
}

impl Services {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(self.version == 1, "unsupported services version");
        anyhow::ensure!(self.grants.len() <= 64, "too many service principals");
        // The reviewed recipe schema admits no fleet IotCore mappings (SF-061).
        if let Some(p) = &self.pubsub { p.validate(false)?; }
        if let Some(s) = &self.streams { s.validate()?; }
        // Validate ACLs using synthetic distinct UIDs; actual UIDs are always
        // provisioned locally and validated again in effective().
        let daemon = unsafe { libc::geteuid() };
        let mut uid = daemon.saturating_add(1000).max(1000);
        let components = self.grants.iter().map(|(name, grant)| {
            uid += 1;
            (name.clone(), grant.acl(uid))
        }).collect();
        ipc::Settings { socket: "/run/suifleet/ipc.sock".into(), administrators: vec![], components }.validate()
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Generation {
    pub deployment: String,
    pub services: Services,
    #[serde(default)] pub previous: Option<Box<Generation>>,
}

fn local_grants(acl: &ipc::Acl) -> bool {
    !acl.operations.is_empty() || !acl.publish.is_empty() || !acl.subscribe.is_empty()
        || !acl.pubsub_policies.is_empty() || !acl.streams.is_empty() || !acl.shadow_policies.is_empty()
}

/// Device-local sections override complete fleet sections; UID-only entries
/// provision identity without overriding fleet grants. Fleet sections from
/// independent roots cannot silently overwrite one another.
pub fn effective(base: &Config, generations: &BTreeMap<String, Generation>) -> anyhow::Result<Config> {
    let mut cfg = base.clone();
    cfg.services = None;
    let mut pubsub = None;
    let mut streams: Option<StreamsConfig> = None;
    let mut grants = BTreeMap::new();
    for generation in generations.values() {
        generation.services.validate()?;
        authorize_credentials(base, &generation.services)?;
        if let Some(p) = &generation.services.pubsub {
            anyhow::ensure!(pubsub.is_none(), "pubsub configuration owned by another deployment component");
            pubsub = Some(p.clone());
        }
        if let Some(s) = &generation.services.streams {
            anyhow::ensure!(streams.is_none(), "streams configuration owned by another deployment component");
            streams = Some(s.clone());
        }
        for (name, grant) in &generation.services.grants {
            anyhow::ensure!(grants.insert(name.clone(), grant).is_none(), "service grants owned by another deployment component");
        }
    }
    cfg.pubsub = base.pubsub.clone().or(pubsub);
    cfg.streams = base.streams.clone().or(streams);
    if let Some(ipc) = &mut cfg.ipc {
        for (name, grant) in grants {
            let local = ipc.components.get(&name).ok_or_else(|| anyhow::anyhow!("component {name} has no provisioned IPC UID"))?;
            if !local_grants(local) { ipc.components.insert(name, grant.acl(local.uid)); }
        }
        ipc.validate()?;
    } else {
        anyhow::ensure!(grants.is_empty(), "service grants require a provisioned IPC socket and UIDs");
    }
    if let Some(p) = &cfg.pubsub { p.validate(cfg.aws_iot.is_some())?; }
    if let Some(s) = &cfg.streams { s.validate()?; }
    // Fleet IotCore mappings count against the local connection's AWS limits.
    crate::config::validate_aws_iot(&cfg)?;
    Ok(cfg)
}

pub type StreamStore = Arc<Mutex<streams::Store>>;
pub type Broker = Arc<Mutex<pubsub::Broker>>;
pub struct Runtime {
    base: Config,
    pub effective: Config,
    pub streams: Option<StreamStore>,
    pub broker: Option<Broker>,
    pub shadow_store: Option<Arc<Mutex<crate::shadows::Store>>>,
    pub client_devices: Option<Arc<crate::clientdevices::Authority>>,
    bridge: Option<tokio::task::JoinHandle<()>>,
    bridge_stats: Option<Arc<Mutex<bridge::Stats>>>,
    /// AWS IoT Device Shadow sync (SF-061): the synchronizer's client, taken
    /// at startup, and the connection side every bridge task attaches to.
    pub aws_shadow_client: Option<crate::iotcore::ShadowClient>,
    aws_shadows: Option<crate::iotcore::ShadowEndpoint>,
    exporters: Vec<tokio::task::JoinHandle<()>>,
    signer: Arc<dyn TxSigner>,
    binding: streams::Binding,
}
impl std::fmt::Debug for Runtime {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("ServiceRuntime") }
}
impl Drop for Runtime {
    fn drop(&mut self) { self.stop_tasks(); }
}
impl Runtime {
    pub fn open(base: &Config, generations: &BTreeMap<String, Generation>, signer: Arc<dyn TxSigner>, binding: streams::Binding) -> anyhow::Result<Self> {
        let cfg = effective(base, generations)?;
        if let Some(s) = &cfg.streams { streams::preflight_disk(&base.runtime.state_dir.join("streams"), s.quota(), &s.definitions)?; }
        let streams = cfg.streams.as_ref().map(|s| streams::Store::start(&base.runtime.state_dir.join("streams"), s.quota(), &s.definitions, crate::telemetry::now_ms())
            .map(|store| Arc::new(Mutex::new(store)))).transpose()?;
        let broker = cfg.pubsub.as_ref().map(|p| pubsub::Broker::open(&base.runtime.state_dir.join("pubsub"), p.limits())
            .map(|b| Arc::new(Mutex::new(b)))).transpose()?;
        let (aws_shadow_client, aws_shadows) = match (&base.aws_iot, base.shadows.as_ref().and_then(|s| s.aws_iot.as_ref())) {
            (Some(iot), Some(sync)) => {
                let (client, endpoint) = crate::iotcore::shadow_channel(&iot.thing_name, &sync.shadows);
                (Some(client), Some(endpoint))
            }
            _ => (None, None),
        };
        let mut runtime = Self { base: base.clone(), effective: cfg, streams, broker, shadow_store: None, client_devices: None, bridge: None, bridge_stats: None,
            aws_shadow_client, aws_shadows, exporters: vec![], signer, binding };
        runtime.resume()?;
        Ok(runtime)
    }
    pub fn prepare(&self, generations: &BTreeMap<String, Generation>) -> anyhow::Result<Config> {
        self.prepare_transition(generations,false)
    }
    fn retiring(&self, next: &Config) -> Vec<String> {
        let wanted: BTreeSet<_> = next.streams.iter().flat_map(|s| s.definitions.iter().map(|d| d.name.clone())).collect();
        self.effective.streams.iter().flat_map(|s| s.definitions.iter().map(|d| d.name.clone())).filter(|n| !wanted.contains(n)).collect()
    }
    fn prepare_transition(&self, generations: &BTreeMap<String, Generation>, recovery: bool) -> anyhow::Result<Config> {
        let cfg = effective(&self.base, generations)?;
        if let Some(settings) = &cfg.streams {
            if let Some(store) = &self.streams { store.lock().unwrap().preflight_transition(settings.quota(), &settings.definitions, &self.retiring(&cfg), recovery)?; }
            // A disabled service may still have persisted streams. Enabling it
            // must inspect metadata without opening/truncating segment files.
            else { streams::preflight_disk_transition(&self.base.runtime.state_dir.join("streams"), settings.quota(), &settings.definitions, recovery)?; }
        }
        Ok(cfg)
    }
    fn stop_tasks(&mut self) {
        if let Some(task) = self.bridge.take() { task.abort(); }
        self.bridge_stats = None;
        for task in self.exporters.drain(..) { task.abort(); }
    }
    fn start_tasks(&mut self) -> anyhow::Result<()> {
        if let Some(authority) = &self.client_devices {
            authority.set_bridge_client_id(self.effective.pubsub.as_ref()
                .and_then(|p| p.bridge.as_ref()).map(|b| b.client_id.as_str()));
        }
        if let (Some(settings), Some(broker)) = (&self.effective.pubsub, &self.broker) {
            // The bridge also carries the local [aws_iot] connection (SF-061),
            // with or without mappings.
            if settings.bridge.is_some() || self.base.aws_iot.is_some() {
                let config = settings.bridge.clone().unwrap_or(bridge::BridgeConfig {
                    broker: None, client_id: "suifleet-bridge".into(), username: None, password_file: None, keep_alive_seconds: 30, mappings: Vec::new(),
                });
                let mut task = bridge::Bridge::new(config, broker.clone(), self.base.runtime.state_dir.join("pubsub/bridge.json"))?;
                if let Some(iot) = &self.base.aws_iot {
                    task = task.with_iot_core(bridge::IotLink { config: iot.clone(), shadows: self.aws_shadows.clone() });
                }
                self.bridge_stats = task.local_stats();
                self.bridge = Some(tokio::spawn(task.run()));
            }
        }
        if let (Some(settings), Some(store)) = (&self.effective.streams, &self.streams) {
            for def in &settings.definitions {
                if let Some(config) = &def.export {
                    self.exporters.push(tokio::spawn(streams::Exporter { store: store.clone(), stream: def.name.clone(), config: config.clone(),
                        binding: self.binding.clone(), signer: self.signer.clone() }.run()));
                }
            }
        }
        Ok(())
    }
    pub fn activate(&mut self, generations: &BTreeMap<String, Generation>) -> anyhow::Result<()> {
        self.activate_transition(generations,false)
    }
    fn activate_transition(&mut self, generations: &BTreeMap<String, Generation>, recovery: bool) -> anyhow::Result<()> {
        let next = self.prepare_transition(generations,recovery)?;
        let retiring = self.retiring(&next);
        // The caller owns the poll loop, so no IPC operation can race this.
        self.stop_tasks();
        if let Some(settings) = &next.streams {
            if let Some(store) = &self.streams { store.lock().unwrap().reconfigure_transition(settings.quota(), &settings.definitions, &retiring, recovery)?; }
            else { self.streams = Some(Arc::new(Mutex::new(streams::Store::stage_transition(&self.base.runtime.state_dir.join("streams"), settings.quota(), &settings.definitions, crate::telemetry::now_ms(), recovery)?))); }
        } else if let Some(store) = &self.streams {
            // Revoke exports without deleting stream records.
            let mut store = store.lock().unwrap();
            let quota = store.quota();
            store.reconfigure_transition(quota, &[], &retiring, recovery)?;
        }
        if let Some(settings) = &next.pubsub {
            if self.broker.is_none() { self.broker = Some(Arc::new(Mutex::new(pubsub::Broker::open(&self.base.runtime.state_dir.join("pubsub"), settings.limits())?))); }
        }
        self.effective = next;
        Ok(())
    }
    pub fn resume(&mut self) -> anyhow::Result<()> {
        if self.effective.streams.is_some() {
            if let Some(store) = &self.streams { store.lock().unwrap().promote(crate::telemetry::now_ms())?; }
        }
        if let Some(store) = &self.shadow_store { store.lock().unwrap().set_broker(self.effective.pubsub.as_ref().and(self.broker.clone())); }
        if let Some(broker) = &self.broker {
            let mut broker = broker.lock().unwrap();
            // Background shadow sync also publishes here. Its limits must
            // change only after the journal commit, never during staging.
            if let Some(settings) = &self.effective.pubsub { broker.set_limits(settings.limits()); }
            for subscription in broker.stats(None) {
                let principal = subscription["principal"].as_str().unwrap();
                let filter = subscription["filter"].as_str().unwrap();
                let keep = if principal.starts_with(bridge::BRIDGE_PRINCIPAL) {
                    self.effective.pubsub.as_ref().and_then(|p| p.bridge.as_ref()).is_some()
                } else if principal.starts_with("@operator/") { true }
                else {
                    self.effective.ipc.as_ref().and_then(|p| p.components.get(principal)).is_some_and(|acl|
                        acl.operations.iter().any(|op| op == "subscribe") && pubsub::authorized(&acl.pubsub_policies,"SubscribeToTopic",filter))
                };
                if !keep { broker.unsubscribe(principal,filter)?; }
            }
        }
        self.start_tasks()?;
        crate::configuration::write_private(&self.base.runtime.state_dir.join("services-effective.json"), &self.summary())
    }
    pub fn abort(&mut self, generations: &BTreeMap<String, Generation>) -> anyhow::Result<()> {
        let intent: Activation = serde_json::from_slice(&std::fs::read(self.base.runtime.state_dir.join("services-activation.json"))?)?;
        if let Some(store) = &self.streams { store.lock().unwrap().discard_uncommitted(&intent.created)?; }
        recover_activation(&self.base, generations)?;
        let staged = self.streams.as_ref().is_some_and(|s| s.lock().unwrap().is_staged());
        if staged { self.streams = None; }
        else if let Some(store) = &self.streams { store.lock().unwrap().restore_metadata()?; }
        self.activate(generations)?;
        self.resume()
    }
    /// What telemetry summarizes (SF-085): only the services in effect.
    pub fn health_sources(&self) -> crate::service_health::Sources {
        crate::service_health::Sources {
            broker: self.effective.pubsub.as_ref().and(self.broker.clone()),
            bridge: self.bridge_stats.clone(),
            streams: self.effective.streams.as_ref().and(self.streams.clone()),
            shadows: self.shadow_store.clone(),
        }
    }
    pub fn summary(&self) -> serde_json::Value {
        // Do not expose endpoints, paths, credentials, or recipe commands.
        serde_json::json!({"version":1,"grants":self.effective.ipc.as_ref().map(|p| p.components.iter().map(|(n,a)| (n, serde_json::json!({
            "operations":a.operations,"publish":a.publish,"subscribe":a.subscribe,"pubsub_policies":a.pubsub_policies,"streams":a.streams,"shadow_policies":a.shadow_policies
        }))).collect::<BTreeMap<_,_>>()),
        "pubsub":self.effective.pubsub.as_ref().map(|p| serde_json::json!({"limits":p.limits(),"bridge_mappings":p.bridge.as_ref().map(|b| &b.mappings)})),
        "streams":self.effective.streams.as_ref().map(|s| serde_json::json!({"max_streams":s.max_streams,"max_total_bytes":s.max_total_bytes,"definitions":s.definitions.iter().map(|d| serde_json::json!({"name":d.name,"max_size":d.max_size,"persistence":d.persistence,"strategy_on_full":d.strategy_on_full,"time_to_live_seconds":d.time_to_live_seconds,"discard_exported":d.discard_exported,"export_enabled":d.export.is_some(),"external_export":d.external_export})).collect::<Vec<_>>()}))})
    }
}

/// Only the authenticated root recipe can declare a generation; grants are
/// confined to components in the resolved plan, including reused dependencies.
pub fn candidate(current: &BTreeMap<String, Generation>, root: &str, deployment: &str, services: Option<&Services>, components: &BTreeSet<String>) -> anyhow::Result<BTreeMap<String, Generation>> {
    let mut next = current.clone();
    if let Some(services) = services {
        services.validate()?;
        anyhow::ensure!(services.grants.keys().all(|n| components.contains(n)), "grant outside deployment components");
        let previous = next.remove(root).map(|mut p| { p.previous = None; Box::new(p) });
        next.insert(root.into(), Generation { deployment: deployment.into(), services: services.clone(), previous });
    } else if let Some(mut previous) = next.remove(root) {
        previous.previous = None;
        next.insert(root.into(), Generation { deployment: deployment.into(), services: Services {
            version: 1, grants: BTreeMap::new(), pubsub: None, streams: None }, previous: Some(Box::new(previous)) });
    }
    anyhow::ensure!(next.len() <= 64, "too many service generations");
    Ok(next)
}

#[derive(Serialize, Deserialize)]
struct Activation {
    target: String,
    metadata: BTreeMap<String, serde_json::Value>,
    created: Vec<String>,
}
fn fingerprint(generations: &BTreeMap<String, Generation>) -> anyhow::Result<String> {
    Ok(hex::encode(Sha256::digest(&serde_json::to_vec(generations)?)))
}
/// Stop and join tasks before changing definitions: an aborted export must not
/// complete against a successor's destinations or retire data during the swap.
pub async fn quiesce(runtime: &Arc<Mutex<Runtime>>) {
    let tasks = {
        let mut runtime = runtime.lock().unwrap();
        let mut tasks = std::mem::take(&mut runtime.exporters);
        tasks.extend(runtime.bridge.take());
        runtime.bridge_stats = None;
        for task in &tasks { task.abort(); }
        tasks
    };
    for task in tasks { let _ = task.await; }
}

pub fn begin_activation(base: &Config, generations: &BTreeMap<String, Generation>, next: &Config) -> anyhow::Result<()> {
    let directory = base.runtime.state_dir.join("streams");
    let mut metadata = BTreeMap::new();
    if directory.exists() {
        for entry in std::fs::read_dir(&directory)? {
            let path = entry?.path();
            if path.join("meta.json").exists() {
                metadata.insert(path.file_name().unwrap().to_string_lossy().into_owned(), serde_json::from_slice(&std::fs::read(path.join("meta.json"))?)?);
            }
        }
    }
    let created = next.streams.iter().flat_map(|s| &s.definitions)
        .map(|d| streams::dir_name(&d.name))
        .filter(|name| !metadata.contains_key(name)).collect();
    crate::configuration::write_private(&base.runtime.state_dir.join("services-activation.json"), &Activation { target: fingerprint(generations)?, metadata, created })
}

/// The journal replacement is the transaction decision. Before it, restore the
/// exact old metadata and remove only newly created, not-yet-exposed streams.
/// After it, the new definitions are recovered by Runtime::open.
pub fn recover_activation(base: &Config, generations: &BTreeMap<String, Generation>) -> anyhow::Result<()> {
    let path = base.runtime.state_dir.join("services-activation.json");
    if !path.exists() { return Ok(()); }
    let intent: Activation = serde_json::from_slice(&std::fs::read(&path)?)?;
    if intent.target != fingerprint(generations)? {
        let dir = base.runtime.state_dir.join("streams");
        for name in &intent.created {
            anyhow::ensure!(name.len() == 64 && name.bytes().all(|b| b.is_ascii_hexdigit()), "invalid stream activation path");
            if dir.join(name).exists() { std::fs::remove_dir_all(dir.join(name))?; }
        }
        for (name, metadata) in &intent.metadata {
            anyhow::ensure!(name.len() == 64 && name.bytes().all(|b| b.is_ascii_hexdigit()), "invalid stream activation path");
            crate::configuration::write_private(&dir.join(name).join("meta.json"), metadata)?;
        }
        if dir.exists() { std::fs::File::open(dir)?.sync_all()?; }
    }
    std::fs::remove_file(&path)?;
    std::fs::File::open(&base.runtime.state_dir)?.sync_all()?;
    Ok(())
}

/// Revoke removed components and restore the previous generation when the
/// existing supervisor has rolled its root component back.
pub fn reconcile(state: &mut crate::state::State, recovered: &BTreeMap<String, String>) -> bool {
    let before = serde_json::to_vec(&state.services).unwrap();
    state.services.retain(|root, generation| {
        let Some(component) = state.components.get(root) else { return false; };
        if component.confirmed && component.deployment_id != generation.deployment && recovered.get(root) == Some(&component.deployment_id) {
            match generation.previous.take() {
                Some(previous) if previous.deployment == component.deployment_id => *generation = *previous,
                _ => return false,
            }
        }
        generation.services.grants.retain(|name, _| state.components.contains_key(name));
        true
    });
    before != serde_json::to_vec(&state.services).unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn fixture() -> Config {
        toml::from_str("[sui]\nmanifest_path='deployment.json'\n[device]\nkeypair_path='k'\ndevice_object_id='0x1'\ndevice_cap_id='0x2'\n[apply]\ncommand='true'\n[ipc]\nsocket='/run/test.sock'\n[ipc.components.app]\nuid=63001\n").unwrap()
    }
    fn services() -> Services {
        serde_json::from_value(json!({"version":1,"grants":{"app":{"operations":["publish"],"pubsub_policies":[{"operations":["aws.greengrass#PublishToTopic"],"resources":["sensors/*"]}]}},"pubsub":{}})).unwrap()
    }
    #[tokio::test]
    async fn failed_directory_sync_preserves_applied_decision_when_services_are_unchanged() {
        let mut base = fixture();
        let dir = std::env::temp_dir().join(format!("sf081-fsync-{}",std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        base.runtime.state_dir = dir.clone();
        let mut journal = crate::state::Journal::open(&dir,"local".into()).unwrap();
        journal.state.deployments.insert("d1".into(),crate::state::Outcome::default());
        journal.save().unwrap();
        let signer = Arc::new(crate::identity::Ed25519File(ed25519_dalek::SigningKey::from_bytes(&[7;32])));
        let runtime = Arc::new(Mutex::new(Runtime::open(&base,&BTreeMap::new(),signer,streams::Binding {
            chain_identifier:"local".into(),registry:"0x1".into(),device_id:"0x2".into() }).unwrap()));
        base.services = Some(runtime.clone());
        crate::state::FAIL_DIRECTORY_SYNC.with(|fault| fault.set(true));
        let error = commit(&base,&mut journal,BTreeMap::new(),Some("d1")).await.unwrap_err();
        assert!(error.downcast_ref::<RecoveryRequired>().is_some(),"{error:#}");
        let disk: crate::state::State = serde_json::from_slice(&std::fs::read(dir.join("state.json")).unwrap()).unwrap();
        assert_eq!(disk.deployments["d1"].status,Some(crate::protocol::STATUS_APPLIED));
        assert_eq!(journal.state.deployments["d1"].status,Some(crate::protocol::STATUS_APPLIED));
        recover_activation(&base,&disk.services).unwrap();
        drop(journal); drop(base); drop(runtime);
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[tokio::test]
    async fn staging_does_not_change_limits_seen_by_background_publishers() {
        let mut base = fixture();
        let dir = std::env::temp_dir().join(format!("sf081-limits-{}",std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        base.runtime.state_dir = dir.clone();
        let mut services = services();
        let grant = services.grants.get_mut("app").unwrap();
        grant.operations = vec!["subscribe".into(),"publish".into(),"receive".into()];
        grant.pubsub_policies = vec![pubsub::Policy { operations: vec!["*".into()],resources: vec!["*".into()] }];
        services.pubsub.as_mut().unwrap().max_messages_per_subscription = 3;
        let current = candidate(&BTreeMap::new(),"app","d1",Some(&services),&BTreeSet::from(["app".into()])).unwrap();
        let signer = Arc::new(crate::identity::Ed25519File(ed25519_dalek::SigningKey::from_bytes(&[7;32])));
        let mut runtime = Runtime::open(&base,&current,signer,streams::Binding { chain_identifier:"local".into(),registry:"0x1".into(),device_id:"0x2".into() }).unwrap();
        let broker = runtime.broker.clone().unwrap();
        broker.lock().unwrap().subscribe("app","topic",1).unwrap();
        broker.lock().unwrap().publish("background","topic",&json!(1),None).unwrap();
        services.pubsub.as_mut().unwrap().max_messages_per_subscription = 1;
        let next = candidate(&current,"app","d2",Some(&services),&BTreeSet::from(["app".into()])).unwrap();
        runtime.activate(&next).unwrap();
        broker.lock().unwrap().publish("background","topic",&json!(2),None).unwrap();
        runtime.resume().unwrap();
        assert!(broker.lock().unwrap().publish("background","topic",&json!(3),None).is_err());
        assert_eq!(broker.lock().unwrap().stats(Some("app"))[0]["queued"],2);
        drop(runtime); drop(broker);
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn activation_crash_uses_journal_commit_decision_and_preserves_records() {
        let mut base = fixture();
        let dir = std::env::temp_dir().join(format!("sf081-crash-{}",std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        base.runtime.state_dir = dir.clone();
        let old = BTreeMap::new();
        let mut service = services();
        service.streams = Some(serde_json::from_value(json!({"definitions":[{"name":"new","max_size":1024,"strategy_on_full":"RejectNewData"}]})).unwrap());
        let next = candidate(&old,"app","d1",Some(&service),&BTreeSet::from(["app".into()])).unwrap();
        let next_cfg = effective(&base,&next).unwrap();
        let existing = dir.join("streams").join(streams::dir_name("existing"));
        std::fs::create_dir_all(&existing).unwrap();
        crate::configuration::write_private(&existing.join("meta.json"),&json!({"old":true})).unwrap();
        std::fs::write(existing.join("records.seg"), b"unchanged records").unwrap();
        begin_activation(&base,&next,&next_cfg).unwrap();
        crate::configuration::write_private(&existing.join("meta.json"),&json!({"new":true})).unwrap();
        let created = dir.join("streams").join(streams::dir_name("new"));
        std::fs::create_dir_all(&created).unwrap();
        std::fs::write(created.join("meta.json"),b"{}").unwrap();
        recover_activation(&base,&old).unwrap();
        assert!(!created.exists());
        assert_eq!(serde_json::from_slice::<serde_json::Value>(&std::fs::read(existing.join("meta.json")).unwrap()).unwrap(),json!({"old":true}));
        assert_eq!(std::fs::read(existing.join("records.seg")).unwrap(),b"unchanged records");
        begin_activation(&base,&next,&next_cfg).unwrap();
        crate::configuration::write_private(&existing.join("meta.json"),&json!({"committed":true})).unwrap();
        recover_activation(&base,&next).unwrap();
        assert_eq!(serde_json::from_slice::<serde_json::Value>(&std::fs::read(existing.join("meta.json")).unwrap()).unwrap(),json!({"committed":true}));
        assert_eq!(serde_json::to_value(serde_json::from_value::<Services>(serde_json::to_value(service).unwrap()).unwrap()).unwrap()["version"],1);
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn schema_rejects_tuples_nulls_and_unknown_nested_fields() {
        for value in [json!([1,{}]),json!({"version":1,"pubsub":null}),json!({"version":1,"grants":{"app":[]}}),
            json!({"version":1,"pubsub":{"secret":"not supported"}}),json!({"version":1,"streams":{"max_streams":0}})] {
            assert!(serde_json::from_value::<Services>(value.clone()).is_err(),"{value}");
        }
    }
    #[test]
    fn versions_scope_and_uid_provisioning_fail_closed() {
        let mut service = services();
        service.version = 2;
        assert!(service.validate().is_err());
        service.version = 1;
        assert!(candidate(&BTreeMap::new(), "app", "d1", Some(&service), &BTreeSet::new()).is_err());
        let generations = candidate(&BTreeMap::new(), "app", "d1", Some(&service), &BTreeSet::from(["app".into()])).unwrap();
        let mut base = fixture();
        base.ipc.as_mut().unwrap().components.clear();
        assert!(effective(&base, &generations).is_err());
        assert!(serde_json::from_value::<Services>(json!({"version":1,"unknown":true})).is_err());
        assert!(serde_json::from_value::<Services>(json!({"version":1,"grants":{"app":{"uid":123}}})).is_err());
    }
    #[test]
    fn fleet_credentials_require_local_destination_bound_authority() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let mut base = fixture();
        base.apply.allow_recipe_commands = false;
        let dir = std::env::temp_dir().join(format!("sf081-credential-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let dir = std::fs::canonicalize(dir).unwrap();
        let token = dir.join("export.token");
        std::fs::write(&token, "operator-token").unwrap();
        std::fs::set_permissions(&token, std::fs::Permissions::from_mode(0o600)).unwrap();
        let destination = "https://ingest.example/streams";
        let fleet = |path: &std::path::Path, url: &str, mqtt: bool| -> BTreeMap<String, Generation> {
            let value = if mqtt { json!({"version":1,"pubsub":{"bridge":{
                "broker":url,"password_file":path,"mappings":[{"source":"Pubsub","target":"LocalMqtt","topic":"sensors/#"}]}}}) }
            else { json!({"version":1,"streams":{"definitions":[{"name":"a","max_size":1024,
                "strategy_on_full":"RejectNewData","export":{"url":url,"bearer_token_file":path}}]}}) };
            candidate(&BTreeMap::new(), "app", "d1", Some(&serde_json::from_value(value).unwrap()), &BTreeSet::from(["app".into()])).unwrap()
        };
        for path in ["/etc/suifleet/device.key", "/etc/suifleet/pin", "/etc/suifleet/config.toml",
            "/var/lib/suifleet/components/app/overlay.json", "relative", "../device.key"] {
            assert!(effective(&base, &fleet(path.as_ref(), destination, false)).is_err());
            assert!(effective(&base, &fleet(path.as_ref(), "127.0.0.1:1883", true)).is_err());
        }
        assert!(effective(&base, &fleet(&token, destination, false)).is_err());
        base.service_credentials.push(CredentialAuthorization { kind: CredentialKind::StreamBearer,
            path: token.clone(), destination: destination.into() });
        assert!(effective(&base, &fleet(&token, destination, false)).is_ok());
        assert!(effective(&base, &fleet(&token, "https://attacker.example/collect", false)).is_err());
        assert!(effective(&base, &fleet(&token, "127.0.0.1:1883", true)).is_err());
        base.service_credentials.push(CredentialAuthorization { kind: CredentialKind::MqttPassword,
            path: token.clone(), destination: "127.0.0.1:1883".into() });
        assert!(effective(&base, &fleet(&token, "127.0.0.1:1883", true)).is_ok());
        assert!(effective(&base, &fleet(&token, "attacker.example:1883", true)).is_err());
        std::fs::set_permissions(&token, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(effective(&base, &fleet(&token, destination, false)).is_err());
        std::fs::remove_file(&token).unwrap();
        let secret = dir.join("device.key");
        std::fs::write(&secret, "secret").unwrap();
        symlink(&secret, &token).unwrap();
        assert!(effective(&base, &fleet(&token, destination, false)).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn fleet_grants_local_override_revocation_and_conflict() {
        let mut base = fixture();
        let generation = candidate(&BTreeMap::new(), "app", "d1", Some(&services()), &BTreeSet::from(["app".into()])).unwrap();
        let cfg = effective(&base, &generation).unwrap();
        assert_eq!(cfg.ipc.unwrap().components["app"].operations, ["publish"]);
        assert!(cfg.pubsub.is_some());
        base.ipc.as_mut().unwrap().components.get_mut("app").unwrap().operations = vec!["status".into()];
        base.pubsub = Some(serde_json::from_value(json!({"max_messages_per_subscription":12})).unwrap());
        let cfg = effective(&base, &generation).unwrap();
        assert_eq!(cfg.ipc.unwrap().components["app"].operations, ["status"]);
        assert_eq!(cfg.pubsub.unwrap().max_messages_per_subscription,12);
        let removed = candidate(&generation,"app","d2",None,&BTreeSet::from(["app".into()])).unwrap();
        assert!(effective(&fixture(),&removed).unwrap().ipc.unwrap().components["app"].operations.is_empty());
        let mut conflict = generation.clone();
        conflict.insert("other".into(),generation["app"].clone());
        assert!(effective(&fixture(), &conflict).is_err());
    }
    #[test]
    fn quota_and_bridge_loops_rejected_without_runtime() {
        assert!(serde_json::from_value::<Services>(json!({"version":1,"streams":{"max_streams":1,"max_total_bytes":1024,
            "definitions":[{"name":"a","max_size":2048,"strategy_on_full":"RejectNewData"}]}})).unwrap().validate().is_err());
        assert!(serde_json::from_value::<Services>(json!({"version":1,"pubsub":{"bridge":{"broker":"127.0.0.1:1883","mappings":[
            {"source":"Pubsub","target":"LocalMqtt","topic":"#"},{"source":"LocalMqtt","target":"Pubsub","topic":"#"}]}}})).unwrap().validate().is_err());
    }
    #[test]
    fn fleet_bridges_cannot_reach_the_local_aws_iot_connection() {
        // [aws_iot] is device-local (SF-061): the reviewed recipe schema
        // refuses fleet IotCore mappings and a fleet bridge without a broker.
        let bridge = |bridge: serde_json::Value| serde_json::from_value::<Services>(json!({"version":1,"pubsub":{"bridge":bridge}}));
        let error = bridge(json!({"broker":"127.0.0.1:1883","mappings":[{"source":"Pubsub","target":"IotCore","topic":"dt/#"}]})).unwrap_err().to_string();
        assert!(error.contains("unsupported services value"), "{error}");
        assert!(bridge(json!({"mappings":[{"source":"Pubsub","target":"LocalMqtt","topic":"dt/#"}]})).is_err());
        let local = bridge(json!({"broker":"127.0.0.1:1883","mappings":[{"source":"Pubsub","target":"LocalMqtt","topic":"dt/#"}]})).unwrap();
        local.validate().unwrap();
        // A fleet LocalMqtt bridge is still accepted.
        let generation = candidate(&BTreeMap::new(),"app","d1",Some(&local),&BTreeSet::from(["app".into()])).unwrap();
        assert!(effective(&fixture(), &generation).unwrap().pubsub.unwrap().bridge.is_some());
    }
    #[test]
    fn root_rollback_and_removal_restore_or_revoke_grants() {
        let mut state = crate::state::State::default();
        let components = BTreeSet::from(["app".into()]);
        let first = candidate(&state.services,"app","d1",Some(&services()),&components).unwrap();
        state.services = candidate(&first,"app","d2",None,&components).unwrap();
        let component: crate::state::Component = serde_json::from_value(json!({"version":"1","deployment_id":"d1","recipe_sha256":"a","artifact_sha256":"b","dependencies":{},"confirmed":true})).unwrap();
        state.components.insert("app".into(),component);
        assert!(reconcile(&mut state, &BTreeMap::from([("app".into(),"d1".into())])));
        assert_eq!(state.services["app"].deployment,"d1");
        state.components.clear();
        assert!(reconcile(&mut state, &BTreeMap::from([("app".into(),"d1".into())])));
        assert!(state.services.is_empty());
    }
}

/// Durability or rollback needs a clean daemon start. Never relabel a possibly
/// committed deployment Failed, replay installation or serve partial undo.
#[derive(Debug)]
pub struct RecoveryRequired(pub String);
impl std::fmt::Display for RecoveryRequired {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { write!(f,"service generation needs recovery on restart: {}", self.0) }
}
impl std::error::Error for RecoveryRequired {}

/// Transaction used both by deployment completion and supervisor rollback.
pub async fn commit(cfg: &Config, journal: &mut crate::state::Journal, next: BTreeMap<String, Generation>, deployment: Option<&str>) -> anyhow::Result<()> {
    let runtime = cfg.services.as_ref().ok_or_else(|| anyhow::anyhow!("service runtime missing"))?;
    let settings = runtime.lock().unwrap().prepare_transition(&next,deployment.is_none())?;
    let previous = journal.state.services.clone();
    if !next.is_empty() || !previous.is_empty() { journal.require_service_reader()?; }
    quiesce(runtime).await;
    if let Err(error) = begin_activation(cfg, &next, &settings) {
        runtime.lock().unwrap().resume().map_err(|e| RecoveryRequired(e.to_string()))?;
        return Err(error);
    }
    let activated = runtime.lock().unwrap().activate_transition(&next,deployment.is_none());
    if let Err(error) = activated {
        runtime.lock().unwrap().abort(&previous).map_err(|undo| RecoveryRequired(format!("activation failed: {error:#}; rollback failed: {undo:#}")))?;
        return Err(error.context("service activation"));
    }
    journal.state.services = next;
    if let Some(id) = deployment {
        journal.state.deployments.entry(id.into()).or_default().status = Some(crate::protocol::STATUS_APPLIED);
    }
    if let Err(error) = journal.save() {
        // A rename followed by a failed directory fsync has an uncertain
        // durability result. Read the decision before attempting any undo.
        let disk = std::fs::read(cfg.runtime.state_dir.join("state.json"))
            .ok().and_then(|b| serde_json::from_slice::<crate::state::State>(&b).ok());
        match disk {
            Some(state) if fingerprint(&state.services)? == fingerprint(&previous)? && deployment.is_none_or(|id| state.deployments.get(id).and_then(|o| o.status) != Some(crate::protocol::STATUS_APPLIED)) => {
                journal.state.services = previous;
                if let Some(id) = deployment { journal.state.deployments.entry(id.into()).or_default().status = None; }
                runtime.lock().unwrap().abort(&journal.state.services).map_err(|e| RecoveryRequired(e.to_string()))?;
                return Err(error);
            }
            _ => return Err(RecoveryRequired(error.to_string()).into()),
        }
    }
    recover_activation(cfg, &journal.state.services).map_err(|e| RecoveryRequired(e.to_string()))?;
    runtime.lock().unwrap().resume().map_err(|e| RecoveryRequired(e.to_string()))?;
    Ok(())
}

/// A supervisor recovery receipt can precede the service journal replacement.
/// Settle it before opening streams or starting exporters: loading the obsolete
/// generation first could retire data that the recovered generation retains.
pub fn startup_recovery(cfg: &Config, journal: &mut crate::state::Journal, recovered: &BTreeMap<String,String>) -> anyhow::Result<()> {
    recover_activation(cfg,&journal.state.services)?;
    if recovered.is_empty() && journal.state.services.is_empty() { return Ok(()); }
    for (name,deployment) in recovered {
        if let Some(previous) = journal.state.last_known_good.get(name).filter(|c| &c.deployment_id == deployment).cloned() {
            journal.state.current.insert(name.clone(),deployment.clone());
            journal.state.components.insert(name.clone(),previous);
        } else {
            anyhow::ensure!(journal.state.components.get(name).is_some_and(|c| c.confirmed && &c.deployment_id == deployment),
                "application recovery receipt has no retained component identity");
        }
    }
    let previous = journal.state.services.clone();
    if !reconcile(&mut journal.state,recovered) { journal.save()?; return Ok(()); }
    let next = std::mem::replace(&mut journal.state.services,previous.clone());
    let old = effective(cfg,&previous)?;
    let target = effective(cfg,&next)?;
    let wanted: BTreeSet<_> = target.streams.iter().flat_map(|s| s.definitions.iter().map(|d| d.name.clone())).collect();
    let retiring: Vec<_> = old.streams.iter().flat_map(|s| s.definitions.iter().map(|d| d.name.clone())).filter(|n| !wanted.contains(n)).collect();
    journal.require_service_reader()?;
    begin_activation(cfg,&next,&target)?;
    let staged = (|| {
        let settings = target.streams.as_ref().or(old.streams.as_ref());
        settings.map(|s| streams::Store::stage_projection(&cfg.runtime.state_dir.join("streams"),s.quota(),
            target.streams.as_ref().map_or(&[][..],|s| s.definitions.as_slice()),crate::telemetry::now_ms(),true,&retiring)).transpose()
    })();
    let staged = match staged {
        Ok(staged) => staged,
        Err(error) => { recover_activation(cfg,&previous)?; return Err(error); }
    };
    journal.state.services = next;
    // On uncertain durability, leave the intent and read-only staged handle
    // for the next start to resolve from the actual on-disk journal decision.
    journal.save()?;
    recover_activation(cfg,&journal.state.services)?;
    drop(staged);
    Ok(())
}
