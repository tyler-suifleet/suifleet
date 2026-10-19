//! AWS IoT Core connection (SF-061; docs/AWS_IOT.md).
//!
//! One MQTT 3.1.1 connection over mutual TLS from the device to AWS IoT
//! Core as an AWS IoT thing. It carries the bridge's `IotCore` mappings
//! (`bridge.rs`) and AWS IoT Device Shadow synchronization (`shadows.rs`).
//!
//! - **Separate trust domain.** The thing certificate, its private key and
//!   the CA that verifies the endpoint are explicitly configured files. The
//!   Sui device key is never used here and may not be configured as the
//!   AWS key; AWS credentials never sign Sui transactions.
//! - **TLS.** Only the configured CA verifies the endpoint (no system
//!   roots). Port 443 needs ALPN `x-amzn-mqtt-ca`, set by default there.
//! - **Rotation.** The files are re-read every `credential_check_seconds`;
//!   when a complete, consistent new set appears, the connection is closed
//!   and reopened with it. An incomplete or mismatched set is ignored (and
//!   reported) while the current connection continues.
//! - **Shadows.** Requests go to `$aws/things/<thing>/shadow[/name/<n>]/
//!   {get,update,delete}` with a `clientToken`; the answer is the matching
//!   `accepted`/`rejected` message. Cloud changes (`update/documents`,
//!   `delete/accepted`) from other writers wake the synchronizer.
use crate::shadows::{self, Put};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::sync::{mpsc, oneshot, Notify};

pub const ALPN_MQTT_CA: &str = "x-amzn-mqtt-ca";
/// AWS IoT Core quotas: topic bytes, slashes in a topic or filter,
/// subscriptions per SUBSCRIBE and per connection.
pub const MAX_TOPIC_BYTES: usize = 256;
pub const MAX_SLASHES: usize = 7;
pub const SUBSCRIBE_BATCH: usize = 8;
pub const MAX_SUBSCRIPTIONS: usize = 50;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

fn default_port() -> u16 {
    8883
}
fn default_keep_alive() -> u16 {
    60
}
fn default_credential_check() -> u64 {
    30
}

/// `[aws_iot]`: the thing's connection to AWS IoT Core.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IotConfig {
    /// The account's data endpoint host, e.g. `abc123-ats.iot.us-east-1.amazonaws.com`.
    pub endpoint: String,
    /// 8883, or 443 with ALPN.
    #[serde(default = "default_port")]
    pub port: u16,
    /// ALPN protocol; `x-amzn-mqtt-ca` by default on port 443, none otherwise.
    #[serde(default)]
    pub alpn: Option<String>,
    /// The AWS IoT thing this device connects as.
    pub thing_name: String,
    /// MQTT client id; the thing name by default (the usual policy condition).
    #[serde(default)]
    pub client_id: Option<String>,
    /// CA certificates that verify the endpoint (for AWS, Amazon Root CA 1).
    pub ca_file: PathBuf,
    /// The thing certificate (PEM, leaf first, optional intermediates).
    pub certificate_file: PathBuf,
    /// Its private key (PEM, owner-only); never the Sui device key.
    pub private_key_file: PathBuf,
    #[serde(default = "default_keep_alive")]
    pub keep_alive_seconds: u16,
    /// How often the credential files are checked for rotation.
    #[serde(default = "default_credential_check")]
    pub credential_check_seconds: u64,
}

pub fn valid_host(host: &str) -> bool {
    !host.is_empty() && host.len() <= 253 && host.split('.').all(|label| {
        !label.is_empty() && label.len() <= 63 && !label.starts_with('-') && !label.ends_with('-')
            && label.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
    })
}

fn valid_client_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 128 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_:".contains(&b))
}

/// Whether AWS IoT Core accepts `topic` (or filter) as a publish/subscribe
/// topic: at most 256 bytes and 7 slashes, not counting a Basic Ingest
/// `$aws/rules/<rule>/` prefix.
pub fn within_aws_limits(topic: &str) -> bool {
    let rest = match topic.strip_prefix("$aws/rules/").and_then(|r| r.split_once('/')) {
        Some((_, rest)) => rest,
        None => topic,
    };
    rest.len() <= MAX_TOPIC_BYTES && rest.matches('/').count() <= MAX_SLASHES
}

impl IotConfig {
    pub fn client_id(&self) -> &str {
        self.client_id.as_deref().unwrap_or(&self.thing_name)
    }

    pub fn alpn(&self) -> Option<&str> {
        match &self.alpn {
            Some(alpn) => Some(alpn),
            None if self.port == 443 => Some(ALPN_MQTT_CA),
            None => None,
        }
    }

    pub fn address(&self) -> String {
        format!("{}:{}", self.endpoint, self.port)
    }

    /// Static checks; the files are checked when they are loaded.
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(valid_host(&self.endpoint), "aws_iot.endpoint must be a host name (no scheme or port)");
        anyhow::ensure!(self.port != 0, "aws_iot.port must be 1-65535");
        anyhow::ensure!(self.alpn.as_deref().is_none_or(|a| !a.is_empty() && a.len() <= 255 && a.is_ascii()), "invalid aws_iot.alpn");
        anyhow::ensure!(shadows::valid_thing(&self.thing_name), "aws_iot.thing_name must be 1-128 of [A-Za-z0-9:_-]");
        anyhow::ensure!(valid_client_id(self.client_id()), "aws_iot.client_id must be 1-128 of [A-Za-z0-9:_-]");
        anyhow::ensure!((30..=1200).contains(&self.keep_alive_seconds), "aws_iot.keep_alive_seconds must be 30..=1200 (AWS IoT Core)");
        anyhow::ensure!((5..=3600).contains(&self.credential_check_seconds), "aws_iot.credential_check_seconds must be 5..=3600");
        for (name, path) in [("ca_file", &self.ca_file), ("certificate_file", &self.certificate_file), ("private_key_file", &self.private_key_file)] {
            anyhow::ensure!(path.is_absolute(), "aws_iot.{name} must be an absolute path");
        }
        Ok(())
    }

    /// The AWS key must not be the Sui device key: they are separate trust
    /// domains, and an AWS-side compromise must not yield the chain identity.
    pub fn check_separate(&self, sui_key: &Path) -> anyhow::Result<()> {
        if sui_key.as_os_str().is_empty() {
            return Ok(());
        }
        let same_path = self.private_key_file == sui_key
            || std::fs::canonicalize(&self.private_key_file).ok().zip(std::fs::canonicalize(sui_key).ok()).is_some_and(|(a, b)| a == b);
        anyhow::ensure!(!same_path && !same_file(&self.private_key_file, sui_key),
            "aws_iot.private_key_file is the Sui device key; AWS IoT needs its own separately provisioned key");
        Ok(())
    }
}

#[cfg(unix)]
fn same_file(a: &Path, b: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    match (std::fs::metadata(a), std::fs::metadata(b)) {
        (Ok(x), Ok(y)) => x.dev() == y.dev() && x.ino() == y.ino(),
        _ => false,
    }
}
#[cfg(not(unix))]
fn same_file(_: &Path, _: &Path) -> bool {
    false
}

/// A loaded, consistent credential set.
pub struct Credentials {
    pub tls: Arc<rustls::ClientConfig>,
    /// SHA-256 of the thing certificate (DER): public, safe to report.
    pub certificate_sha256: String,
    /// SHA-256 over the three files, to notice rotation. Kept in memory only.
    digest: [u8; 32],
}

/// The files' current digest, or None when one cannot be read.
fn files_digest(config: &IotConfig) -> Option<[u8; 32]> {
    let mut hash = Sha256::new();
    for path in [&config.ca_file, &config.certificate_file, &config.private_key_file] {
        let bytes = zeroize::Zeroizing::new(std::fs::read(path).ok()?);
        hash.update((bytes.len() as u64).to_be_bytes());
        hash.update(&*bytes);
    }
    Some(hash.finalize().into())
}

impl Credentials {
    /// Read and check the CA, certificate and key. Errors name the file and
    /// problem, never key material.
    pub fn load(config: &IotConfig) -> anyhow::Result<Self> {
        use rustls::pki_types::{pem::PemObject, CertificateDer, PrivateKeyDer};
        let read = |name: &str, path: &Path| std::fs::read(path).map_err(|e| anyhow::anyhow!("cannot read aws_iot.{name} {}: {}", path.display(), e.kind()));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&config.private_key_file)
                .map_err(|e| anyhow::anyhow!("cannot read aws_iot.private_key_file {}: {}", config.private_key_file.display(), e.kind()))?
                .permissions().mode();
            anyhow::ensure!(mode & 0o077 == 0, "aws_iot.private_key_file {} must be readable by its owner only (mode 0600)", config.private_key_file.display());
        }
        let ca = read("ca_file", &config.ca_file)?;
        let chain = read("certificate_file", &config.certificate_file)?;
        let key = zeroize::Zeroizing::new(read("private_key_file", &config.private_key_file)?);
        let mut roots = rustls::RootCertStore::empty();
        for cert in CertificateDer::pem_slice_iter(&ca) {
            roots.add(cert.map_err(|_| anyhow::anyhow!("aws_iot.ca_file holds an invalid PEM certificate"))?)
                .map_err(|e| anyhow::anyhow!("aws_iot.ca_file: {e}"))?;
        }
        anyhow::ensure!(!roots.is_empty(), "aws_iot.ca_file holds no certificate");
        let chain: Vec<CertificateDer<'static>> = CertificateDer::pem_slice_iter(&chain).collect::<Result<_, _>>()
            .map_err(|_| anyhow::anyhow!("aws_iot.certificate_file holds an invalid PEM certificate"))?;
        anyhow::ensure!(!chain.is_empty(), "aws_iot.certificate_file holds no certificate");
        let key = PrivateKeyDer::from_pem_slice(&key).map_err(|_| anyhow::anyhow!("aws_iot.private_key_file holds no usable PEM private key"))?;
        let certificate_sha256 = hex::encode(Sha256::digest(chain[0].as_ref()));
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let mut tls = rustls::ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()?
            .with_root_certificates(roots)
            // Refuses a key that does not match the certificate.
            .with_client_auth_cert(chain, key)
            .map_err(|e| anyhow::anyhow!("aws_iot certificate and private key are unusable together: {e}"))?;
        tls.alpn_protocols = config.alpn().map(|a| vec![a.as_bytes().to_vec()]).unwrap_or_default();
        let digest = files_digest(config).ok_or_else(|| anyhow::anyhow!("aws_iot credential files changed while loading"))?;
        Ok(Self { tls: Arc::new(tls), certificate_sha256, digest })
    }

    /// Whether the files on disk differ from the loaded set.
    pub fn rotated(&self, config: &IotConfig) -> bool {
        files_digest(config).is_some_and(|d| d != self.digest)
    }
}

/// Open the TLS connection to the endpoint; returns the stream and the
/// negotiated ALPN protocol.
pub async fn connect(config: &IotConfig, credentials: &Credentials) -> anyhow::Result<(Box<dyn crate::mqtt::Stream>, Option<String>)> {
    let tcp = crate::mqtt::connect_tcp(&config.address()).await?;
    let name = rustls::pki_types::ServerName::try_from(config.endpoint.clone())?;
    let tls = tokio::time::timeout(Duration::from_secs(10), tokio_rustls::TlsConnector::from(credentials.tls.clone()).connect(name, tcp)).await
        .map_err(|_| anyhow::anyhow!("TLS handshake with {} timed out", config.endpoint))?
        .map_err(|e| anyhow::anyhow!("TLS handshake with {} failed: {e}", config.endpoint))?;
    let alpn = tls.get_ref().1.alpn_protocol().map(|p| String::from_utf8_lossy(p).into_owned());
    if let Some(wanted) = config.alpn() {
        anyhow::ensure!(alpn.as_deref() == Some(wanted), "the endpoint did not accept ALPN {wanted}");
    }
    Ok((Box::new(tls), alpn))
}

// ---------------------------------------------------------------------------
// AWS IoT Device Shadow over the connection.
// ---------------------------------------------------------------------------

/// The filters the connection subscribes for synchronized shadows: their
/// get, update and delete answers and change notifications.
pub fn shadow_filters(thing: &str, names: &[String]) -> Vec<String> {
    names.iter().flat_map(|name| {
        let prefix = shadows::resource(thing, name);
        ["get", "update", "delete"].map(|op| format!("{prefix}/{op}/+"))
    }).collect()
}

/// One request awaiting its answer.
pub struct ShadowRequest {
    pub topic: String,
    pub payload: Vec<u8>,
    pub token: String,
    pub reply: oneshot::Sender<anyhow::Result<(bool, Value)>>,
}

/// Synchronizer side: sends requests over the connection.
#[derive(Clone)]
pub struct ShadowClient {
    requests: mpsc::Sender<ShadowRequest>,
    thing: String,
    connected: Arc<AtomicBool>,
    next: Arc<AtomicU64>,
    /// Woken by cloud changes from other writers.
    pub changed: Arc<Notify>,
}

/// Connection side: requests waiting to be sent.
pub struct ShadowRequests(mpsc::Receiver<ShadowRequest>);

impl ShadowRequests {
    /// The next request to send, once one is waiting.
    pub async fn next(&mut self) -> Option<ShadowRequest> {
        self.0.recv().await
    }
}

/// Connection side: routes answers and notifications back.
pub struct ShadowRouter {
    pending: HashMap<String, (String, oneshot::Sender<anyhow::Result<(bool, Value)>>)>,
    thing: String,
    pub filters: Vec<String>,
    connected: Arc<AtomicBool>,
    changed: Arc<Notify>,
}

pub fn shadow_channel(thing: &str, names: &[String]) -> (ShadowClient, ShadowRouter, ShadowRequests) {
    let (tx, rx) = mpsc::channel(16);
    let connected = Arc::new(AtomicBool::new(false));
    let changed = Arc::new(Notify::new());
    (ShadowClient { requests: tx, thing: thing.into(), connected: connected.clone(), next: Arc::new(AtomicU64::new(1)), changed: changed.clone() },
     ShadowRouter { pending: HashMap::new(), thing: thing.into(), filters: shadow_filters(thing, names), connected, changed },
     ShadowRequests(rx))
}

impl ShadowClient {
    /// Publish `body` (plus a clientToken) to `<shadow>/<op>` and wait for
    /// the answer: (accepted, document).
    pub async fn call(&self, shadow: &str, op: &str, mut body: Map<String, Value>) -> anyhow::Result<(bool, Value)> {
        anyhow::ensure!(self.connected.load(Ordering::SeqCst), "AWS IoT Core is not connected");
        let token = format!("sf-{}-{}", std::process::id(), self.next.fetch_add(1, Ordering::SeqCst));
        body.insert("clientToken".into(), json!(token));
        let (reply, answer) = oneshot::channel();
        let request = ShadowRequest { topic: format!("{}/{op}", shadows::resource(&self.thing, shadow)), payload: serde_json::to_vec(&body)?, token, reply };
        self.requests.send(request).await.map_err(|_| anyhow::anyhow!("the AWS IoT Core connection stopped"))?;
        tokio::time::timeout(REQUEST_TIMEOUT, answer).await
            .map_err(|_| anyhow::anyhow!("AWS IoT shadow {op} timed out"))?
            .map_err(|_| anyhow::anyhow!("the AWS IoT Core connection dropped the request"))?
    }
}

impl ShadowRouter {
    pub fn set_connected(&self, connected: bool) {
        self.connected.store(connected, Ordering::SeqCst);
    }

    /// Remember a sent request until its answer arrives.
    pub fn sent(&mut self, request: ShadowRequest) {
        self.pending.insert(request.token, (request.topic, request.reply));
    }

    /// Fail a request that could not be sent or whose connection ended.
    pub fn fail(&mut self, token: &str, reason: &str) {
        if let Some((_, reply)) = self.pending.remove(token) {
            let _ = reply.send(Err(anyhow::anyhow!("{reason}")));
        }
    }

    pub fn fail_all(&mut self, reason: &str) {
        for (_, (_, reply)) in self.pending.drain() {
            let _ = reply.send(Err(anyhow::anyhow!("{reason}")));
        }
    }

    /// Whether `topic` belongs to this thing's shadow service.
    pub fn owns(&self, topic: &str) -> bool {
        let prefix = format!("$aws/things/{}/shadow", self.thing);
        topic.strip_prefix(&prefix).is_some_and(|rest| rest.is_empty() || rest.starts_with('/'))
    }

    /// An incoming message on a shadow topic.
    pub fn deliver(&mut self, topic: &str, payload: &[u8]) {
        let value: Value = serde_json::from_slice(payload).unwrap_or(Value::Null);
        let token = value.get("clientToken").and_then(Value::as_str);
        let answer = topic.rsplit_once('/').and_then(|(request, outcome)| match outcome {
            "accepted" => Some((request, true)),
            "rejected" => Some((request, false)),
            _ => None,
        });
        if let (Some((request, accepted)), Some(token)) = (answer, token) {
            if self.pending.get(token).is_some_and(|(sent, _)| sent == request) {
                let (_, reply) = self.pending.remove(token).unwrap();
                let _ = reply.send(Ok((accepted, value)));
                return;
            }
        }
        // Another writer changed or deleted a shadow: synchronize promptly.
        if topic.ends_with("/update/documents") || topic.ends_with("/delete/accepted") {
            self.changed.notify_one();
        }
    }
}

/// The AWS Device Shadow side of shadow synchronization.
pub struct AwsShadows(pub ShadowClient);

fn rejection(op: &str, value: &Value) -> anyhow::Error {
    anyhow::anyhow!("AWS IoT shadow {op} rejected: {} {}", value.get("code").and_then(Value::as_u64).unwrap_or(0),
        crate::telemetry::bounded(value.get("message").and_then(Value::as_str).unwrap_or(""), 200))
}

/// The update that turns `from` into `to` under AWS merge semantics: keys
/// to remove are null, objects are patched key by key, other values whole.
pub fn patch(from: &Map<String, Value>, to: &Map<String, Value>) -> Map<String, Value> {
    let mut out = Map::new();
    for key in from.keys().filter(|k| !to.contains_key(*k)) {
        out.insert(key.clone(), Value::Null);
    }
    for (key, value) in to {
        match (from.get(key), value) {
            (Some(Value::Object(a)), Value::Object(b)) => {
                let inner = patch(a, b);
                if !inner.is_empty() {
                    out.insert(key.clone(), Value::Object(inner));
                }
            }
            (Some(old), new) if shadows::json_equal(old, new) => {}
            _ => {
                out.insert(key.clone(), value.clone());
            }
        }
    }
    out
}

impl shadows::Cloud for AwsShadows {
    async fn fetch(&self, shadow: &str) -> anyhow::Result<(u64, Option<Map<String, Value>>)> {
        let (accepted, value) = self.0.call(shadow, "get", Map::new()).await?;
        if !accepted {
            // Absent (never created, or deleted): version 0 stands for absent.
            anyhow::ensure!(value.get("code").and_then(Value::as_u64) == Some(404), rejection("get", &value));
            return Ok((0, None));
        }
        let version = value.get("version").and_then(Value::as_u64).filter(|v| *v > 0)
            .ok_or_else(|| anyhow::anyhow!("AWS IoT shadow answer has no version"))?;
        let mut document = Map::new();
        for section in ["desired", "reported"] {
            match value.get("state").and_then(|s| s.get(section)) {
                None | Some(Value::Null) => {}
                Some(Value::Object(object)) => {
                    document.insert(section.into(), Value::Object(object.clone()));
                }
                Some(_) => anyhow::bail!("cloud shadow refused: {section} is not an object"),
            }
        }
        Ok((version, Some(document)))
    }

    async fn put(&self, shadow: &str, expected: u64, cloud: Option<&Map<String, Value>>, document: Option<&Map<String, Value>>) -> anyhow::Result<Put> {
        let Some(document) = document else {
            // AWS deletion carries no version: not compare-and-swap.
            let (accepted, value) = self.0.call(shadow, "delete", Map::new()).await?;
            if accepted {
                return Ok(Put::Stored(0));
            }
            anyhow::ensure!(value.get("code").and_then(Value::as_u64) == Some(404), rejection("delete", &value));
            return Ok(Put::Conflict);
        };
        let mut body = Map::new();
        body.insert("state".into(), Value::Object(patch(cloud.unwrap_or(&Map::new()), document)));
        // An absent shadow has no version to compare (creation is not compare-and-swap).
        if expected > 0 {
            body.insert("version".into(), json!(expected));
        }
        let (accepted, value) = self.0.call(shadow, "update", body).await?;
        if !accepted {
            anyhow::ensure!(value.get("code").and_then(Value::as_u64) == Some(409), rejection("update", &value));
            return Ok(Put::Conflict);
        }
        let version = value.get("version").and_then(Value::as_u64).ok_or_else(|| anyhow::anyhow!("AWS IoT shadow answer has no version"))?;
        anyhow::ensure!(version > expected, "AWS IoT did not advance the shadow version");
        Ok(Put::Stored(version))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> IotConfig {
        serde_json::from_value(json!({"endpoint": "abc-ats.iot.us-east-1.amazonaws.com", "thing_name": "dev1",
            "ca_file": "/etc/aws/ca.pem", "certificate_file": "/etc/aws/cert.pem", "private_key_file": "/etc/aws/key.pem"})).unwrap()
    }

    #[test]
    fn configuration_rules() {
        let c = config();
        c.validate().unwrap();
        assert_eq!((c.client_id(), c.alpn(), c.address().as_str()), ("dev1", None, "abc-ats.iot.us-east-1.amazonaws.com:8883"));
        let mut tls443 = config();
        tls443.port = 443;
        assert_eq!(tls443.alpn(), Some(ALPN_MQTT_CA), "port 443 needs ALPN by default");
        for (key, value) in [("endpoint", json!("https://x.example")), ("endpoint", json!("x.example:8883")), ("endpoint", json!("")),
                             ("thing_name", json!("bad name")), ("client_id", json!("")), ("client_id", json!("x".repeat(129))),
                             ("keep_alive_seconds", json!(10)), ("keep_alive_seconds", json!(1201)), ("credential_check_seconds", json!(1)),
                             ("private_key_file", json!("relative.key")), ("alpn", json!(""))] {
            let mut bad = serde_json::to_value(serde_json::from_value::<Value>(json!({"endpoint": "abc-ats.iot.us-east-1.amazonaws.com", "thing_name": "dev1",
                "ca_file": "/etc/aws/ca.pem", "certificate_file": "/etc/aws/cert.pem", "private_key_file": "/etc/aws/key.pem"})).unwrap()).unwrap();
            bad[key] = value;
            assert!(serde_json::from_value::<IotConfig>(bad).unwrap().validate().is_err(), "{key}");
        }
        assert!(serde_json::from_value::<IotConfig>(json!({"endpoint": "x", "thing_name": "t", "ca_file": "/a", "certificate_file": "/b",
            "private_key_file": "/c", "password": "p"})).is_err(), "unknown fields refused");
    }

    fn cases() -> Value {
        serde_json::from_str(include_str!("../../interfaces/compat/aws-iot-cases.json")).unwrap()
    }

    /// interfaces/compat/aws-iot-cases.json: connection settings, topic
    /// limits and shadow update patches.
    #[test]
    fn shared_aws_iot_connection_topic_and_patch_cases() {
        let cases = cases();
        for case in cases["connection"].as_array().unwrap() {
            let config = serde_json::from_value::<IotConfig>(case["config"].clone()).ok().filter(|c| c.validate().is_ok());
            let actual = match config {
                Some(c) => json!({"valid": true, "alpn": c.alpn(), "client_id": c.client_id()}),
                None => json!({"valid": false}),
            };
            assert_eq!(actual, case["expected"], "{}", case["id"]);
        }
        for case in cases["topics"].as_array().unwrap() {
            assert_eq!(within_aws_limits(case["topic"].as_str().unwrap()), case["within_limits"].as_bool().unwrap(), "{}", case["id"]);
        }
        for case in cases["shadow_patches"].as_array().unwrap() {
            let actual = Value::Object(patch(case["from"].as_object().unwrap(), case["to"].as_object().unwrap()));
            assert!(crate::shadows::json_equal(&actual, &case["patch"]), "{}: {actual}", case["id"]);
        }
    }

    /// interfaces/compat/aws-iot-cases.json: the synchronizer's requests and
    /// its reading of AWS Device Shadow answers, over the real request path.
    #[tokio::test]
    async fn shared_aws_iot_shadow_answer_cases() {
        use crate::shadows::Cloud;
        for case in cases()["shadow_answers"].as_array().unwrap() {
            let (client, mut router, mut requests) = shadow_channel("dev1", &["config".into()]);
            router.set_connected(true);
            let shadows = AwsShadows(client);
            let doc = |key: &str| case[key].as_object().cloned();
            let call = async {
                if case["call"] == "fetch" {
                    shadows.fetch("config").await.map(|(version, document)| json!({"version": version, "document": document}))
                } else {
                    shadows.put("config", case["expected_version"].as_u64().unwrap(), doc("cloud").as_ref(), doc("document").as_ref()).await
                        .map(|put| match put {
                            Put::Stored(version) => json!({"stored": version}),
                            Put::Conflict => json!({"conflict": true}),
                        })
                }
            };
            let cloud = async {
                let request = requests.next().await.unwrap();
                let mut body: Value = serde_json::from_slice(&request.payload).unwrap();
                let token = body.as_object_mut().unwrap().remove("clientToken").unwrap();
                let op = case["request"]["op"].as_str().unwrap();
                assert_eq!(request.topic, format!("$aws/things/dev1/shadow/name/config/{op}"), "{}", case["id"]);
                assert!(crate::shadows::json_equal(&body, &case["request"]["body"]), "{}: {body}", case["id"]);
                let topic = format!("{}/{}", request.topic, if case["answer"]["accepted"].as_bool().unwrap() { "accepted" } else { "rejected" });
                let mut answer = case["answer"]["body"].clone();
                answer["clientToken"] = token;
                router.sent(request);
                router.deliver(&topic, &serde_json::to_vec(&answer).unwrap());
            };
            let (result, ()) = tokio::join!(call, cloud);
            match case["expected"]["error"].as_str() {
                Some(error) => assert!(result.as_ref().is_err_and(|e| e.to_string().contains(error)), "{}: {result:?}", case["id"]),
                None => assert!(crate::shadows::json_equal(&result.unwrap(), &case["expected"]), "{}", case["id"]),
            }
        }
    }

    #[test]
    fn the_sui_device_key_is_never_the_aws_key() {
        let dir = std::env::temp_dir().join(format!("suifleet-iot-separate-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let sui = dir.join("device.key");
        std::fs::write(&sui, b"k").unwrap();
        let mut c = config();
        c.private_key_file = sui.clone();
        assert!(c.check_separate(&sui).unwrap_err().to_string().contains("Sui device key"));
        // Through a symlink and a hard link too.
        let link = dir.join("aws.key");
        std::os::unix::fs::symlink(&sui, &link).unwrap();
        c.private_key_file = link;
        assert!(c.check_separate(&sui).is_err());
        let hard = dir.join("hard.key");
        std::fs::hard_link(&sui, &hard).unwrap();
        c.private_key_file = hard;
        assert!(c.check_separate(&sui).is_err());
        c.private_key_file = dir.join("other.key");
        std::fs::write(&c.private_key_file, b"o").unwrap();
        c.check_separate(&sui).unwrap();
        c.check_separate(Path::new("")).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn credential_files_are_checked_without_revealing_them() {
        let dir = std::env::temp_dir().join(format!("suifleet-iot-creds-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let mut c = config();
        c.ca_file = dir.join("ca.pem");
        c.certificate_file = dir.join("cert.pem");
        c.private_key_file = dir.join("key.pem");
        std::fs::write(&c.ca_file, "not a certificate").unwrap();
        std::fs::write(&c.certificate_file, "").unwrap();
        std::fs::write(&c.private_key_file, "-----BEGIN PRIVATE KEY-----\nc2VjcmV0\n-----END PRIVATE KEY-----\n").unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&c.private_key_file, std::fs::Permissions::from_mode(0o644)).unwrap();
        let error = Credentials::load(&c).err().unwrap().to_string();
        assert!(error.contains("owner only"), "{error}");
        std::fs::set_permissions(&c.private_key_file, std::fs::Permissions::from_mode(0o600)).unwrap();
        let error = Credentials::load(&c).err().unwrap().to_string();
        assert!(error.contains("ca_file holds no certificate"), "{error}");
        assert!(!error.contains("c2VjcmV0"));
        std::fs::remove_file(&c.private_key_file).unwrap();
        assert!(Credentials::load(&c).err().unwrap().to_string().contains("private_key_file"));
        assert!(files_digest(&c).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn aws_topic_limits() {
        assert!(within_aws_limits("a/b/c/d/e/f/g/h"));
        assert!(!within_aws_limits("a/b/c/d/e/f/g/h/i"), "8 slashes");
        assert!(within_aws_limits(&format!("$aws/rules/r/{}", "x".repeat(256))), "the Basic Ingest prefix is not counted");
        assert!(!within_aws_limits(&"x".repeat(257)));
    }

    #[test]
    fn patches_follow_aws_merge_semantics() {
        let from = json!({"desired": {"a": 1, "b": {"x": 1, "y": 2}, "c": [1, 2], "gone": true}, "reported": {"r": 1}});
        let to = json!({"desired": {"a": 1, "b": {"x": 3}, "c": [1], "d": {"n": 1}}});
        let patch = patch(from.as_object().unwrap(), to.as_object().unwrap());
        assert_eq!(Value::Object(patch.clone()), json!({"reported": null, "desired": {"gone": null, "b": {"y": null, "x": 3}, "c": [1], "d": {"n": 1}}}));
        // Applying it with the local (AWS-equivalent) merge reaches `to`.
        let mut store = crate::shadows::Store::open(&std::env::temp_dir().join(format!("suifleet-iot-patch-{}", std::process::id())), 4, "t", None).unwrap();
        store.update("p", &json!({"state": from}), 1).unwrap();
        let after = store.update("p", &json!({"state": patch}), 2).unwrap();
        assert_eq!(after["state"]["desired"], to["desired"]);
        assert!(after["state"].get("reported").is_none());
        assert_eq!(super::patch(to.as_object().unwrap(), to.as_object().unwrap()), Map::new());
    }

    #[tokio::test]
    async fn shadow_answers_are_matched_by_client_token_and_topic() {
        let (client, mut router, mut requests) = shadow_channel("dev1", &["".into(), "cfg".into()]);
        assert_eq!(router.filters, ["$aws/things/dev1/shadow/get/+", "$aws/things/dev1/shadow/update/+", "$aws/things/dev1/shadow/delete/+",
            "$aws/things/dev1/shadow/name/cfg/get/+", "$aws/things/dev1/shadow/name/cfg/update/+", "$aws/things/dev1/shadow/name/cfg/delete/+"]);
        assert!(client.call("cfg", "get", Map::new()).await.unwrap_err().to_string().contains("not connected"));
        router.set_connected(true);
        let call = tokio::spawn({
            let client = client.clone();
            async move { client.call("cfg", "get", Map::new()).await }
        });
        let request = requests.next().await.unwrap();
        assert_eq!(request.topic, "$aws/things/dev1/shadow/name/cfg/get");
        let token = request.token.clone();
        assert_eq!(serde_json::from_slice::<Value>(&request.payload).unwrap()["clientToken"], token);
        router.sent(request);
        // Someone else's answer and an answer on another topic are not ours.
        router.deliver("$aws/things/dev1/shadow/name/cfg/get/accepted", br#"{"clientToken":"other","version":9}"#);
        router.deliver("$aws/things/dev1/shadow/get/accepted", format!(r#"{{"clientToken":"{token}","version":8}}"#).as_bytes());
        router.deliver("$aws/things/dev1/shadow/name/cfg/get/rejected", format!(r#"{{"clientToken":"{token}","code":404}}"#).as_bytes());
        let (accepted, value) = call.await.unwrap().unwrap();
        assert!(!accepted);
        assert_eq!(value["code"], 404);
        // A change from another writer wakes the synchronizer.
        let changed = client.changed.clone();
        router.deliver("$aws/things/dev1/shadow/name/cfg/update/documents", br#"{"current":{}}"#);
        tokio::time::timeout(Duration::from_secs(1), changed.notified()).await.unwrap();
        // A dropped connection fails pending requests.
        let call = tokio::spawn({
            let client = client.clone();
            async move { client.call("", "update", Map::new()).await }
        });
        let request = requests.next().await.unwrap();
        router.sent(request);
        router.fail_all("connection lost");
        assert!(call.await.unwrap().unwrap_err().to_string().contains("connection lost"));
        assert!(router.owns("$aws/things/dev1/shadow/name/x/get/accepted") && !router.owns("$aws/things/dev2/shadow/get/accepted"));
    }
}
