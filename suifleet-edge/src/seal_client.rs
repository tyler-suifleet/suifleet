//! Seal key retrieval for confidential delivery (SF-022).
//!
//! - Key servers come from configuration by object ID only. Their URL and
//!   public key are read from their on-chain objects (the configured Seal
//!   package's `key_server::KeyServer`), the transport must be HTTPS (plain
//!   HTTP only to loopback), and each proves possession of its master key
//!   (`/v1/service`) before it is asked for keys.
//! - A session certificate is signed by the device's own enrolled identity
//!   as a Sui personal message; requests are signed by an ephemeral session
//!   key that never leaves its holder (the signing service, with
//!   `kind = "service"`). No administrator key or signature is involved.
//! - The approval transaction calls `seal_policy::seal_approve_device` or
//!   `seal_approve_group_device` for this device; key servers evaluate it
//!   against their view of the chain, which may lag.
//! - Returned keys are ElGamal-encrypted to a fresh per-request key, checked
//!   against the server's public key, and kept only in memory for at most
//!   `key_cache_seconds`.
use crate::{
    config::{SealConfig, SealMode},
    identity::TxSigner,
    seal::{self, Kind, Object},
    sui_client::{same_id, same_type, SuiClient},
};
use ark_bls12_381::{G1Affine, G2Affine};
use base64::{engine::general_purpose::STANDARD, Engine};
use ed25519_dalek::{Signer, SigningKey};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    ops::Range,
    str::FromStr,
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use sui_sdk_types::{Address, Argument, Command, Identifier, Input, MoveCall, ProgrammableTransaction, SharedInput, TypeTag};
use tokio::sync::OnceCell;
use tracing::{info, warn};

/// Seal's limit on session lifetimes.
pub const MAX_SESSION_MINUTES: u16 = 30;
/// Approval calls this daemon (and the signing service) will authorize.
const MAX_APPROVALS: usize = 8;
const MAX_RESPONSE_BYTES: usize = 256 * 1024;
const POLICY_MODULE: &str = "seal_policy";
/// Key servers older than this lack the request format implemented here.
const MIN_SERVER_VERSION: (u64, u64, u64) = (0, 4, 1);

// ── Session certificates ─────────────────────────────────────────────────────

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

/// `YYYY-MM-DD HH:MM:SS UTC` for Unix milliseconds (Seal's message format).
pub fn utc(ms: u64) -> String {
    let seconds = ms / 1000;
    let (days, rest) = (seconds / 86_400, seconds % 86_400);
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02} {:02}:{:02}:{:02} UTC", rest / 3600, rest % 3600 / 60, rest % 60)
}

/// The personal message a session certificate signs.
pub fn session_message(package: &Address, ttl_min: u16, creation_ms: u64, session_key: &[u8; 32]) -> String {
    format!(
        "Accessing keys of package {} for {ttl_min} mins from {}, session key {}",
        package,
        utc(creation_ms),
        STANDARD.encode(session_key)
    )
}

/// What the session key signs for one request: BCS of three byte vectors.
pub fn request_message(ptb: &[u8], enc_key: &[u8], enc_verification_key: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for part in [ptb, enc_key, enc_verification_key] {
        bcs::serialize_into(&mut out, part).expect("byte vector");
    }
    out
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Certificate {
    pub user: String,
    pub session_key: [u8; 32],
    pub creation_ms: u64,
    pub ttl_min: u16,
    /// Serialized Sui `UserSignature` over the session message.
    pub signature: Vec<u8>,
}

impl Certificate {
    pub fn to_json(&self) -> Value {
        json!({
            "user": self.user,
            "session_vk": STANDARD.encode(self.session_key),
            "creation_time": self.creation_ms,
            "ttl_min": self.ttl_min,
            "signature": STANDARD.encode(&self.signature),
        })
    }
    pub fn from_json(value: &Value) -> anyhow::Result<Self> {
        let field = |name: &str| value.get(name).ok_or_else(|| anyhow::anyhow!("certificate lacks {name}"));
        Ok(Self {
            user: field("user")?.as_str().unwrap_or_default().to_owned(),
            session_key: STANDARD
                .decode(field("session_vk")?.as_str().unwrap_or_default())?
                .try_into()
                .map_err(|_| anyhow::anyhow!("invalid session key"))?,
            creation_ms: field("creation_time")?.as_u64().ok_or_else(|| anyhow::anyhow!("invalid creation time"))?,
            ttl_min: field("ttl_min")?.as_u64().and_then(|v| u16::try_from(v).ok()).ok_or_else(|| anyhow::anyhow!("invalid ttl"))?,
            signature: STANDARD.decode(field("signature")?.as_str().unwrap_or_default())?,
        })
    }
}

struct Session {
    key: SigningKey,
    certificate: Certificate,
}

/// Session settings shared by the daemon and the signing service.
#[derive(Debug, Clone, Copy)]
pub struct SessionPolicy {
    /// The Seal namespace: the fleet package's original (first-version) ID.
    pub package: Address,
    pub ttl_min: u16,
    /// Certificates are dated this far back: key servers refuse any creation
    /// time ahead of their clock.
    pub clock_skew_ms: u64,
}

/// Holds an ephemeral session key, certified by the device's own identity
/// and replaced before it expires. Never written to disk.
pub struct Sessions {
    signer: Arc<dyn TxSigner>,
    policy: SessionPolicy,
    current: Mutex<Option<Session>>,
}

impl Sessions {
    pub fn new(signer: Arc<dyn TxSigner>, policy: SessionPolicy) -> anyhow::Result<Self> {
        anyhow::ensure!((1..=MAX_SESSION_MINUTES).contains(&policy.ttl_min), "Seal sessions last 1 to {MAX_SESSION_MINUTES} minutes");
        anyhow::ensure!(policy.clock_skew_ms < u64::from(policy.ttl_min) * 30_000, "clock skew allowance must be under half the session lifetime");
        Ok(Self { signer, policy, current: Mutex::new(None) })
    }

    fn fresh(&self, now: u64) -> anyhow::Result<Session> {
        use rand::RngCore;
        let mut seed = zeroize::Zeroizing::new([0u8; 32]);
        rand::rngs::OsRng.fill_bytes(seed.as_mut());
        let key = SigningKey::from_bytes(&seed);
        let session_key = key.verifying_key().to_bytes();
        let creation_ms = now.saturating_sub(self.policy.clock_skew_ms);
        let message = session_message(&self.policy.package, self.policy.ttl_min, creation_ms, &session_key);
        let signature = self.signer.sign_personal_message(message.as_bytes())?;
        let certificate = Certificate { user: self.signer.address(), session_key, creation_ms, ttl_min: self.policy.ttl_min, signature };
        info!(expires_ms = creation_ms + u64::from(self.policy.ttl_min) * 60_000, "New Seal session certified by the device identity");
        Ok(Session { key, certificate })
    }

    /// Sign one key request with a session valid for at least a minute more.
    pub fn authorize(&self, ptb: &[u8], enc_key: &[u8], enc_verification_key: &[u8]) -> anyhow::Result<(Certificate, Vec<u8>)> {
        let now = now_ms();
        let mut current = self.current.lock().unwrap();
        let valid = current.as_ref().is_some_and(|s| {
            let expires = s.certificate.creation_ms + u64::from(s.certificate.ttl_min) * 60_000;
            now >= s.certificate.creation_ms && now + 60_000 < expires
        });
        if !valid {
            *current = Some(self.fresh(now)?);
        }
        let session = current.as_ref().unwrap();
        let signature = session.key.sign(&request_message(ptb, enc_key, enc_verification_key)).to_bytes().to_vec();
        Ok((session.certificate.clone(), signature))
    }
}

/// Who certifies sessions and signs requests for the daemon.
pub trait Authority: Send + Sync {
    fn authorize(&self, ptb: &[u8], enc_key: &[u8], enc_verification_key: &[u8]) -> anyhow::Result<(Certificate, Vec<u8>)>;
}

impl Authority for Sessions {
    fn authorize(&self, ptb: &[u8], enc_key: &[u8], enc_verification_key: &[u8]) -> anyhow::Result<(Certificate, Vec<u8>)> {
        Sessions::authorize(self, ptb, enc_key, enc_verification_key)
    }
}

/// Checks an approval transaction before any session signs it: only
/// `seal_policy::seal_approve*` calls on the fleet package, with plain
/// inputs, an identity first and shared objects read-only. Key servers
/// reject other shapes too; this keeps the session from signing anything else.
pub fn check_approval(ptb: &ProgrammableTransaction, package: &Address) -> anyhow::Result<()> {
    anyhow::ensure!(!ptb.commands.is_empty() && ptb.commands.len() <= MAX_APPROVALS, "approval needs 1 to {MAX_APPROVALS} calls");
    for command in &ptb.commands {
        let Command::MoveCall(call) = command else { anyhow::bail!("approval may contain only Move calls") };
        anyhow::ensure!(
            call.package == *package && call.module.as_str() == POLICY_MODULE && call.function.as_str().starts_with("seal_approve") && call.type_arguments.is_empty(),
            "{}::{}::{} is not a SuiFleet Seal approval",
            call.package,
            call.module,
            call.function
        );
        let mut inputs = call.arguments.iter().map(|argument| match argument {
            Argument::Input(index) => ptb.inputs.get(*index as usize).ok_or_else(|| anyhow::anyhow!("approval refers to a missing input")),
            _ => anyhow::bail!("approval arguments must be transaction inputs"),
        });
        anyhow::ensure!(matches!(inputs.next(), Some(Ok(Input::Pure(_)))), "approval must start with the identity");
        for input in inputs {
            match input? {
                Input::Pure(_) => {}
                Input::Shared(shared) => anyhow::ensure!(shared.mutability() == sui_sdk_types::Mutability::Immutable, "approval objects must be read-only"),
                _ => anyhow::bail!("approval inputs must be pure values or shared objects"),
            }
        }
    }
    Ok(())
}

// ── Key servers ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct KeyServer {
    pub object_id: [u8; 32],
    pub name: String,
    pub url: String,
    pub public_key: G2Affine,
    api_key: Option<(String, String)>,
}

/// HTTPS, or plain HTTP to this host only.
fn transport(url: &str) -> anyhow::Result<()> {
    let parsed = reqwest::Url::parse(url).map_err(|e| anyhow::anyhow!("invalid key server URL {url:?}: {e}"))?;
    let host = parsed.host_str().unwrap_or_default();
    let loopback = host == "localhost"
        || host.trim_start_matches('[').trim_end_matches(']').parse::<std::net::IpAddr>().is_ok_and(|ip| ip.is_loopback());
    anyhow::ensure!(
        (parsed.scheme() == "https" || (parsed.scheme() == "http" && loopback)) && parsed.username().is_empty() && parsed.password().is_none(),
        "key server {url} must use HTTPS (plain HTTP only to loopback)"
    );
    Ok(())
}

struct Reader<'a>(&'a [u8]);
impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> anyhow::Result<&'a [u8]> {
        anyhow::ensure!(n <= self.0.len(), "truncated key server object");
        let (head, tail) = self.0.split_at(n);
        self.0 = tail;
        Ok(head)
    }
    fn length(&mut self) -> anyhow::Result<usize> {
        let mut value = 0usize;
        for shift in (0..35).step_by(7) {
            let byte = self.take(1)?[0];
            value |= usize::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return Ok(value);
            }
        }
        anyhow::bail!("invalid length")
    }
    fn bytes(&mut self) -> anyhow::Result<&'a [u8]> {
        let n = self.length()?;
        self.take(n)
    }
    fn string(&mut self) -> anyhow::Result<String> {
        Ok(String::from_utf8(self.bytes()?.to_vec())?)
    }
    fn u64(&mut self) -> anyhow::Result<u64> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
}

/// (name, URL, public key) from the BCS of a `Field<u64, KeyServerV1|V2>`.
fn key_server_version(contents: &[u8], version: u64) -> anyhow::Result<(String, String, Vec<u8>)> {
    let mut r = Reader(contents);
    r.take(32)?; // field UID
    anyhow::ensure!(r.u64()? == version, "key server version field mismatch");
    let name = r.string()?;
    let (url, key_type, pk) = if version == 1 {
        let url = r.string()?;
        (url, r.take(1)?[0], r.bytes()?.to_vec())
    } else {
        let key_type = r.take(1)?[0];
        let pk = r.bytes()?.to_vec();
        anyhow::ensure!(r.length()? == 0, "committee (aggregated) key servers are not supported by this client");
        (r.string()?, key_type, pk)
    };
    anyhow::ensure!(key_type == 0, "unsupported key server key type {key_type}");
    anyhow::ensure!(r.0.is_empty(), "trailing bytes in key server object");
    Ok((name, url, pk))
}

/// A key server, identified by its on-chain object, with a verified URL and
/// public key.
pub async fn load_key_server(
    sui: &SuiClient,
    package: &str,
    config: &crate::config::KeyServerConfig,
) -> anyhow::Result<KeyServer> {
    let (object_type, _, contents) = sui.move_object(&config.object_id).await?;
    anyhow::ensure!(
        same_type(&object_type, &format!("{package}::key_server::KeyServer")),
        "{} is a {object_type}, not a key server of the configured Seal package",
        config.object_id
    );
    let mut r = Reader(&contents);
    r.take(32)?;
    let (first, last) = (r.u64()?, r.u64()?);
    let version = [2u64, 1].into_iter().find(|v| (first..=last).contains(v)).ok_or_else(|| anyhow::anyhow!("key server {} supports versions {first}..={last} only", config.object_id))?;
    let parent = Address::from_str(&config.object_id)?;
    let field = parent.derive_dynamic_child_id(&TypeTag::U64, &version.to_le_bytes());
    let (_, _, field) = sui.move_object(&field.to_string()).await?;
    let (name, url, pk) = key_server_version(&field, version)?;
    transport(&url)?;
    let api_key = match (&config.api_key_name, &config.api_key_file) {
        (Some(name), Some(path)) => Some((name.clone(), crate::config::read_secret(path)?)),
        (None, None) => None,
        _ => anyhow::bail!("set both api_key_name and api_key_file for key server {}", config.object_id),
    };
    Ok(KeyServer {
        object_id: parent.into_inner(),
        name,
        url: url.trim_end_matches('/').to_owned(),
        public_key: seal::g2(&pk)?,
        api_key,
    })
}

fn request_id() -> String {
    let bytes: [u8; 16] = rand::random();
    let h = hex::encode(bytes);
    format!("{}-{}-4{}-a{}-{}", &h[0..8], &h[8..12], &h[13..16], &h[17..20], &h[20..32])
}

fn server_version(response: &reqwest::Response) -> anyhow::Result<()> {
    let version = response
        .headers()
        .get("X-KeyServer-Version")
        .and_then(|v| v.to_str().ok())
        .ok_or_else(|| anyhow::anyhow!("key server reported no version"))?;
    let parts: Vec<u64> = version.split('.').map(str::parse).collect::<Result<_, _>>().map_err(|_| anyhow::anyhow!("invalid key server version {version:?}"))?;
    anyhow::ensure!(parts.len() == 3 && (parts[0], parts[1], parts[2]) >= MIN_SERVER_VERSION, "key server version {version} is unsupported");
    Ok(())
}

async fn body(response: reqwest::Response) -> anyhow::Result<Vec<u8>> {
    use futures_util::StreamExt;
    let mut stream = response.bytes_stream();
    let mut out = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        anyhow::ensure!(out.len() + chunk.len() <= MAX_RESPONSE_BYTES, "key server response too large");
        out.extend_from_slice(&chunk);
    }
    Ok(out)
}

impl KeyServer {
    fn request(&self, http: &reqwest::Client, method: reqwest::Method, path: &str) -> reqwest::RequestBuilder {
        let mut request = http
            .request(method, format!("{}{path}", self.url))
            .header("Request-Id", request_id())
            .header("Client-Sdk-Type", "suifleet-edge")
            .header("Client-Sdk-Version", env!("CARGO_PKG_VERSION"));
        if let Some((name, value)) = &self.api_key {
            request = request.header(name.as_str(), value.as_str());
        }
        request
    }

    async fn response(request: reqwest::RequestBuilder) -> anyhow::Result<Value> {
        let response = request.send().await.map_err(|e| anyhow::anyhow!("unreachable: {e}"))?;
        let status = response.status();
        let version = server_version(&response);
        let bytes = body(response).await?;
        if !status.is_success() {
            let text = String::from_utf8_lossy(&bytes[..bytes.len().min(512)]).into_owned();
            anyhow::bail!("{status}: {text}");
        }
        version?;
        Ok(serde_json::from_slice(&bytes)?)
    }

    /// The server proves it holds the master key behind its on-chain public key.
    pub async fn verify(&self, http: &reqwest::Client) -> anyhow::Result<()> {
        let id = format!("0x{}", hex::encode(self.object_id));
        let value = Self::response(self.request(http, reqwest::Method::GET, &format!("/v1/service?service_id={id}"))).await?;
        anyhow::ensure!(value["service_id"].as_str().is_some_and(|s| same_id(s, &id)), "key server answered for another service");
        let pop = STANDARD.decode(value["pop"].as_str().unwrap_or_default())?;
        anyhow::ensure!(seal::verify_proof_of_possession(&pop, &self.public_key, &self.object_id), "key server failed its proof of possession");
        Ok(())
    }

    /// User secret keys for every identity of the approval, checked against
    /// this server's public key.
    async fn fetch(&self, http: &reqwest::Client, request: &Value, enc_secret: &ark_bls12_381::Fr) -> anyhow::Result<Vec<(Vec<u8>, G1Affine)>> {
        let value = Self::response(self.request(http, reqwest::Method::POST, "/v1/fetch_key").json(request)).await?;
        let mut keys = Vec::new();
        for entry in value["decryption_keys"].as_array().ok_or_else(|| anyhow::anyhow!("malformed key response"))? {
            let id: Vec<u8> = serde_json::from_value(entry["id"].clone())?;
            let parts = entry["encrypted_key"].as_array().filter(|p| p.len() == 2).ok_or_else(|| anyhow::anyhow!("malformed encrypted key"))?;
            let c0 = seal::g1(&STANDARD.decode(parts[0].as_str().unwrap_or_default())?)?;
            let c1 = seal::g1(&STANDARD.decode(parts[1].as_str().unwrap_or_default())?)?;
            let key = seal::elgamal_decrypt(enc_secret, &c0, &c1);
            anyhow::ensure!(seal::verify_user_secret_key(&key, &id, &self.public_key), "key server returned an invalid key");
            keys.push((id, key));
        }
        Ok(keys)
    }
}

// ── Confidential blobs ───────────────────────────────────────────────────────

/// The daemon's view of an access policy.
struct Policy {
    key_epoch: u64,
    devices: Vec<[u8; 32]>,
    groups: Vec<[u8; 32]>,
    initial_version: u64,
}

pub struct Seal {
    config: SealConfig,
    sui: SuiClient,
    authority: Arc<dyn Authority>,
    device_id: Address,
    http: reqwest::Client,
    /// Every configured key server's on-chain identity (URL, public key).
    servers: OnceCell<Vec<KeyServer>>,
    /// Key servers that passed their proof of possession in this process.
    verified: Mutex<std::collections::HashSet<[u8; 32]>>,
    keys: Mutex<HashMap<(Vec<u8>, [u8; 32]), (G1Affine, Instant)>>,
}

impl Seal {
    pub fn new(config: SealConfig, sui: SuiClient, authority: Arc<dyn Authority>, device_id: &str) -> anyhow::Result<Self> {
        config.validate()?;
        anyhow::ensure!(
            crate::protocol::SEAL_POLICY_TYPES.iter().all(|name| sui.manifest().type_origins.contains_key(*name)),
            "[seal] needs a deployment manifest whose package includes seal_policy"
        );
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(config.request_timeout_seconds))
            .timeout(Duration::from_secs(config.request_timeout_seconds))
            .build()?;
        Ok(Self {
            config,
            sui,
            authority,
            device_id: Address::from_str(device_id)?,
            http,
            servers: OnceCell::new(),
            verified: Mutex::new(std::collections::HashSet::new()),
            keys: Mutex::new(HashMap::new()),
        })
    }

    pub fn required(&self) -> bool {
        self.config.mode == SealMode::Required
    }

    pub fn max_plaintext_bytes(&self) -> u64 {
        self.config.max_plaintext_bytes
    }

    /// The configured key servers' on-chain identities, read once per
    /// process. Reachability is not needed here: public keys come from chain.
    async fn servers(&self) -> anyhow::Result<&[KeyServer]> {
        let servers = self
            .servers
            .get_or_try_init(|| async {
                let mut servers = Vec::new();
                for config in &self.config.key_servers {
                    servers.push(load_key_server(&self.sui, &self.config.key_server_package, config).await?);
                }
                anyhow::Ok(servers)
            })
            .await?;
        Ok(servers)
    }

    /// Fetch from one key server, first checking its proof of possession
    /// once per process. An unreachable server fails only its own share.
    async fn fetch_from(&self, server: &KeyServer, request: &Value, enc_secret: &ark_bls12_381::Fr) -> anyhow::Result<Vec<(Vec<u8>, G1Affine)>> {
        if self.config.verify_key_servers && !self.verified.lock().unwrap().contains(&server.object_id) {
            server.verify(&self.http).await.map_err(|e| anyhow::anyhow!("proof of possession: {e:#}"))?;
            self.verified.lock().unwrap().insert(server.object_id);
            info!(name = server.name, url = server.url, "Verified Seal key server");
        }
        server.fetch(&self.http, request, enc_secret).await
    }

    async fn policy(&self, id: &[u8; 32]) -> anyhow::Result<Policy> {
        let id = format!("0x{}", hex::encode(id));
        let (object_type, owner, contents) = self.sui.move_object(&id).await?;
        anyhow::ensure!(
            same_type(&object_type, &self.sui.move_type("seal_policy::AccessPolicy")),
            "{id} is not a SuiFleet access policy"
        );
        let crate::sui_client::Owner::Shared { initial_version } = owner else { anyhow::bail!("access policy {id} is not shared") };
        let value: crate::protocol::seal_policy::AccessPolicy = bcs::from_bytes(&contents)?;
        Ok(Policy { key_epoch: value.key_epoch, devices: value.devices, groups: value.groups, initial_version })
    }

    /// The approval for this device: directly granted, or through the first
    /// granted group that currently contains it.
    async fn approval(&self, object: &Object, policy_id: [u8; 32]) -> anyhow::Result<ProgrammableTransaction> {
        let policy = self.policy(&policy_id).await?;
        let epoch = object.key_epoch().unwrap_or_default();
        anyhow::ensure!(
            epoch == policy.key_epoch,
            "encrypted under key epoch {epoch}, but the access policy is at epoch {} (rotated): re-encrypt and redeploy",
            policy.key_epoch
        );
        let device = self.device_id.into_inner();
        let mut group = None;
        if !policy.devices.contains(&device) {
            for id in &policy.groups {
                let id = format!("0x{}", hex::encode(id));
                let Ok((object_type, crate::sui_client::Owner::Shared { initial_version }, contents)) = self.sui.move_object(&id).await else { continue };
                if !same_type(&object_type, &self.sui.move_type("device_group::DeviceGroup")) {
                    continue;
                }
                let value: crate::protocol::device_group::DeviceGroup = bcs::from_bytes(&contents)?;
                if value.device_ids.contains(&device) {
                    group = Some((Address::from_str(&id)?, initial_version));
                    break;
                }
            }
            anyhow::ensure!(group.is_some(), "this device is outside the access policy's scope (no device or group grant)");
        }
        let manifest = self.sui.manifest();
        let registry = &manifest.shared_objects.registry;
        let mut inputs = vec![
            Input::Pure(bcs::to_bytes(&object.id)?),
            Input::Shared(SharedInput::new(Address::new(policy_id), policy.initial_version, false)),
            Input::Shared(SharedInput::new(Address::from_str(&registry.id)?, registry.initial_shared_version.parse()?, false)),
        ];
        let function = if let Some((group, version)) = group {
            inputs.push(Input::Shared(SharedInput::new(group, version, false)));
            "seal_approve_group_device"
        } else {
            "seal_approve_device"
        };
        inputs.push(Input::Pure(bcs::to_bytes(&self.device_id)?));
        let call = MoveCall {
            package: Address::from_str(&manifest.call_package_id)?,
            module: Identifier::from_static(POLICY_MODULE),
            function: Identifier::from_str(function)?,
            type_arguments: vec![],
            arguments: (0..inputs.len() as u16).map(Argument::Input).collect(),
        };
        Ok(ProgrammableTransaction { inputs, commands: vec![Command::MoveCall(call)] })
    }

    fn cached(&self, full_id: &[u8], servers: &[KeyServer]) -> HashMap<[u8; 32], G1Affine> {
        let ttl = Duration::from_secs(self.config.key_cache_seconds);
        let mut keys = self.keys.lock().unwrap();
        keys.retain(|_, (_, at)| at.elapsed() < ttl);
        servers.iter().filter_map(|s| keys.get(&(full_id.to_vec(), s.object_id)).map(|(k, _)| (s.object_id, *k))).collect()
    }

    /// Ask every needed key server concurrently; enough weight must answer.
    async fn fetch_keys(&self, object: &Object, servers: &[KeyServer], ptb: &ProgrammableTransaction) -> anyhow::Result<HashMap<[u8; 32], G1Affine>> {
        let full_id = object.full_id();
        let mut keys = self.cached(&full_id, servers);
        // A server's weight is how many shares the ciphertext gives it.
        let weight = |keys: &HashMap<[u8; 32], G1Affine>| object.services.iter().filter(|(id, _)| keys.contains_key(id)).count();
        if weight(&keys) >= usize::from(object.threshold) {
            return Ok(keys);
        }
        let ptb_bytes = bcs::to_bytes(ptb)?;
        let enc_secret = seal::random_scalar();
        let (enc_key, enc_verification_key) = seal::elgamal_keys(&enc_secret);
        let authority = self.authority.clone();
        let (certificate, signature) = {
            let (p, k, v) = (ptb_bytes.clone(), enc_key, enc_verification_key);
            tokio::task::spawn_blocking(move || authority.authorize(&p, &k, &v)).await??
        };
        let request = json!({
            "ptb": STANDARD.encode(&ptb_bytes),
            "enc_key": STANDARD.encode(enc_key),
            "enc_verification_key": STANDARD.encode(enc_verification_key),
            "request_signature": STANDARD.encode(&signature),
            "certificate": certificate.to_json(),
        });
        let pending: Vec<_> = servers.iter().filter(|s| !keys.contains_key(&s.object_id)).collect();
        let results = futures_util::future::join_all(pending.iter().map(|s| self.fetch_from(s, &request, &enc_secret))).await;
        let mut errors = Vec::new();
        for (server, result) in pending.iter().zip(results) {
            match result.and_then(|found| {
                found.into_iter().find(|(id, _)| *id == full_id).map(|(_, key)| key).ok_or_else(|| anyhow::anyhow!("no key for this identity"))
            }) {
                Ok(key) => {
                    keys.insert(server.object_id, key);
                    if self.config.key_cache_seconds > 0 {
                        self.keys.lock().unwrap().insert((full_id.clone(), server.object_id), (key, Instant::now()));
                    }
                }
                Err(error) => {
                    warn!(server = server.name, "Seal key request refused or failed: {error:#}");
                    errors.push(format!("{}: {error:#}", server.name));
                }
            }
        }
        anyhow::ensure!(
            weight(&keys) >= usize::from(object.threshold),
            "Seal keys unavailable: {} of {} required key servers answered ({})",
            weight(&keys),
            object.threshold,
            errors.join("; ")
        );
        Ok(keys)
    }

    /// Decrypt a confidential blob held in `buffer` in place; returns the
    /// plaintext's range. The caller still checks the plaintext digest.
    pub async fn decrypt(&self, buffer: &mut [u8], kind: Kind, plaintext_sha256: &str) -> anyhow::Result<Range<usize>> {
        let object = seal::parse(buffer)?;
        let namespace = Address::from_str(self.sui.origin())?;
        anyhow::ensure!(
            object.package_id == namespace.into_inner(),
            "encrypted for another package or network (0x{}), not this fleet's 0x{}",
            hex::encode(object.package_id),
            hex::encode(namespace.into_inner())
        );
        anyhow::ensure!(object.aad == seal::aad(kind, plaintext_sha256), "ciphertext is bound to another {} digest or kind", kind.as_str());
        let policy_id = object.policy_id().ok_or_else(|| anyhow::anyhow!("identity is not a SuiFleet access-policy identity"))?;
        let configured = self.servers().await?;
        let mut servers = Vec::new();
        for (id, _) in &object.services {
            let server = configured.iter().find(|s| s.object_id == *id).ok_or_else(|| {
                anyhow::anyhow!("encrypted to key server 0x{}, which is not configured and verified here", hex::encode(id))
            })?;
            if !servers.iter().any(|s: &KeyServer| s.object_id == *id) {
                servers.push(server.clone());
            }
        }
        anyhow::ensure!(object.threshold >= self.config.min_threshold, "threshold {} is below the configured minimum {}", object.threshold, self.config.min_threshold);
        let ptb = self.approval(&object, policy_id).await?;
        check_approval(&ptb, &Address::from_str(&self.sui.manifest().call_package_id)?)?;
        let keys = self.fetch_keys(&object, &servers, &ptb).await?;
        let public: HashMap<_, _> = servers.iter().map(|s| (s.object_id, s.public_key)).collect();
        let key = seal::dem_key(&object, &keys, &public)?;
        seal::decrypt_in_place(&object, &key, buffer)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::seal::tests::{unhex, vectors};

    #[test]
    fn personal_message_and_session_signatures_match_the_sdk() {
        let v = vectors();
        let p = &v["personal_message"];
        let key = SigningKey::from_bytes(&unhex(&p["seed"]).try_into().unwrap());
        assert_eq!(crate::identity::address(&key), p["address"].as_str().unwrap());
        let signature = crate::identity::personal_message_signature(&key, &unhex(&p["message"]));
        assert_eq!(STANDARD.encode(signature), p["signature"].as_str().unwrap());
        // A transaction signature over the same bytes is a different intent.
        assert_ne!(crate::identity::signature(&key, &unhex(&p["message"])), crate::identity::personal_message_signature(&key, &unhex(&p["message"])));
        let s = &v["session"];
        let session = SigningKey::from_bytes(&unhex(&s["seed"]).try_into().unwrap());
        let package = Address::from_str(s["package_id"].as_str().unwrap()).unwrap();
        let message = session_message(&package, s["ttl_min"].as_u64().unwrap() as u16, s["creation_time_ms"].as_u64().unwrap(), &session.verifying_key().to_bytes());
        assert_eq!(message, s["message"].as_str().unwrap());
        let request = request_message(&unhex(&s["ptb"]), &unhex(&s["enc_key"]), &unhex(&s["enc_verification_key"]));
        assert_eq!(session.sign(&request).to_bytes().to_vec(), unhex(&s["request_signature"]));
    }

    #[test]
    fn utc_formatting() {
        assert_eq!(utc(0), "1970-01-01 00:00:00 UTC");
        assert_eq!(utc(1_600_000_000_000), "2020-09-13 12:26:40 UTC");
        assert_eq!(utc(951_782_400_000), "2000-02-29 00:00:00 UTC");
        assert_eq!(utc(1_609_545_600_000 + 1_000 * (23 * 3600 + 59 * 60 + 59)), "2021-01-02 23:59:59 UTC");
    }

    struct Key(SigningKey);
    impl TxSigner for Key {
        fn address(&self) -> String {
            crate::identity::address(&self.0)
        }
        fn sign(&self, tx: &[u8]) -> anyhow::Result<Vec<u8>> {
            Ok(crate::identity::signature(&self.0, tx))
        }
        fn sign_personal_message(&self, message: &[u8]) -> anyhow::Result<Vec<u8>> {
            Ok(crate::identity::personal_message_signature(&self.0, message))
        }
    }

    #[test]
    fn sessions_are_certified_by_the_device_and_renewed() {
        use ed25519_dalek::Verifier;
        let device = SigningKey::from_bytes(&[7; 32]);
        let package = Address::from_str("0x5").unwrap();
        let policy = SessionPolicy { package, ttl_min: 10, clock_skew_ms: 60_000 };
        let sessions = Sessions::new(Arc::new(Key(device.clone())), policy).unwrap();
        let (certificate, signature) = sessions.authorize(b"ptb", &[1; 48], &[2; 96]).unwrap();
        assert_eq!(certificate.user, crate::identity::address(&device));
        assert!(certificate.creation_ms + 60_000 <= now_ms() + 1_000, "dated back for clock skew");
        // The certificate is the device's personal-message signature.
        let message = session_message(&package, 10, certificate.creation_ms, &certificate.session_key);
        assert_eq!(certificate.signature, crate::identity::personal_message_signature(&device, message.as_bytes()));
        // The request is signed by the certified session key, not the device.
        let session = ed25519_dalek::VerifyingKey::from_bytes(&certificate.session_key).unwrap();
        let signature = ed25519_dalek::Signature::from_slice(&signature).unwrap();
        session.verify(&request_message(b"ptb", &[1; 48], &[2; 96]), &signature).unwrap();
        assert_ne!(certificate.session_key, device.verifying_key().to_bytes());
        // Reused while valid; replaced once it nears expiry.
        assert_eq!(sessions.authorize(b"x", &[], &[]).unwrap().0, certificate);
        sessions.current.lock().unwrap().as_mut().unwrap().certificate.creation_ms -= 9 * 60_000;
        assert_ne!(sessions.authorize(b"x", &[], &[]).unwrap().0.session_key, certificate.session_key);
        assert!(Sessions::new(Arc::new(Key(device.clone())), SessionPolicy { ttl_min: 31, ..policy }).is_err());
        assert!(Sessions::new(Arc::new(Key(device)), SessionPolicy { ttl_min: 2, clock_skew_ms: 60_000, ..policy }).is_err());
        let json = certificate.to_json();
        assert_eq!(Certificate::from_json(&json).unwrap(), certificate);
    }

    fn approval(package: &str, module: &str, function: &str, inputs: Vec<Input>) -> ProgrammableTransaction {
        let call = MoveCall {
            package: Address::from_str(package).unwrap(),
            module: Identifier::from_str(module).unwrap(),
            function: Identifier::from_str(function).unwrap(),
            type_arguments: vec![],
            arguments: (0..inputs.len() as u16).map(Argument::Input).collect(),
        };
        ProgrammableTransaction { inputs, commands: vec![Command::MoveCall(call)] }
    }

    #[test]
    fn sessions_sign_only_fleet_approvals() {
        let package = Address::from_str("0x3").unwrap();
        let shared = |mutable| Input::Shared(SharedInput::new(Address::from_str("0x9").unwrap(), 1, mutable));
        let id = Input::Pure(bcs::to_bytes(&vec![1u8; 56]).unwrap());
        check_approval(&approval("0x3", "seal_policy", "seal_approve_device", vec![id.clone(), shared(false), shared(false), Input::Pure(vec![0; 32])]), &package).unwrap();
        for (label, bad) in [
            ("other package", approval("0x4", "seal_policy", "seal_approve_device", vec![id.clone(), shared(false)])),
            ("other module", approval("0x3", "roles", "seal_approve_device", vec![id.clone(), shared(false)])),
            ("non-approval", approval("0x3", "seal_policy", "grant_account", vec![id.clone(), shared(false)])),
            ("mutable object", approval("0x3", "seal_policy", "seal_approve_device", vec![id.clone(), shared(true)])),
            ("object first", approval("0x3", "seal_policy", "seal_approve_device", vec![shared(false), id.clone()])),
            ("owned object", approval("0x3", "seal_policy", "seal_approve_device", vec![id.clone(),
                Input::ImmutableOrOwned(sui_sdk_types::ObjectReference::new(Address::from_str("0x9").unwrap(), 1, sui_sdk_types::Digest::ZERO))])),
            ("no commands", ProgrammableTransaction { inputs: vec![id.clone()], commands: vec![] }),
        ] {
            assert!(check_approval(&bad, &package).is_err(), "{label} was accepted");
        }
        let mut many = approval("0x3", "seal_policy", "seal_approve_device", vec![id.clone()]);
        many.commands = vec![many.commands[0].clone(); MAX_APPROVALS + 1];
        assert!(check_approval(&many, &package).is_err());
    }

    #[test]
    fn key_server_objects_and_transport() {
        let mut v1 = vec![0u8; 32];
        v1.extend(1u64.to_le_bytes());
        for part in [b"mysten".as_slice(), b"https://seal.example"] {
            v1.extend(bcs::to_bytes(&part.to_vec()).unwrap());
        }
        v1.push(0);
        v1.extend(bcs::to_bytes(&vec![9u8; 96]).unwrap());
        let (name, url, pk) = key_server_version(&v1, 1).unwrap();
        assert_eq!((name.as_str(), url.as_str(), pk.len()), ("mysten", "https://seal.example", 96));
        assert!(key_server_version(&v1, 2).is_err());
        let mut v2 = vec![0u8; 32];
        v2.extend(2u64.to_le_bytes());
        v2.extend(bcs::to_bytes(&b"node".to_vec()).unwrap());
        v2.push(0);
        v2.extend(bcs::to_bytes(&vec![9u8; 96]).unwrap());
        let mut independent = v2.clone();
        independent.push(0);
        independent.extend(bcs::to_bytes(&b"https://node.example".to_vec()).unwrap());
        assert_eq!(key_server_version(&independent, 2).unwrap().1, "https://node.example");
        let mut committee = v2;
        committee.push(1);
        assert!(key_server_version(&committee, 2).unwrap_err().to_string().contains("committee"));
        transport("https://seal.example").unwrap();
        transport("http://127.0.0.1:2024").unwrap();
        transport("http://localhost:2024").unwrap();
        assert!(transport("http://seal.example").is_err(), "plain HTTP off-host");
        assert!(transport("https://user:pass@seal.example").is_err());
        assert!(transport("ftp://seal.example").is_err());
    }
}
