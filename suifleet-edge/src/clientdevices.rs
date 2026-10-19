//! LAN client devices (SF-035): identities, certificates and authorization.
//!
//! A local CA issues each enrolled client device ("thing") a TLS client
//! certificate from its CSR. The registry records every issued certificate by
//! its SHA-256 fingerprint, so a connection is identified by the exact
//! certificate it presented and is denied once that identity is unknown,
//! revoked, expired or superseded by a rotation. Authorization follows the
//! `aws.greengrass.clientdevices.Auth` 2.5.x device-group policies
//! (docs/CLIENT_DEVICES.md). The network side is `client_gateway`.
use base64::{engine::general_purpose::STANDARD, Engine as _};
use rcgen::{BasicConstraints, CertificateParams, DistinguishedName, DnType, ExtendedKeyUsagePurpose, IsCa, Issuer,
    KeyPair, KeyUsagePurpose, SanType, SerialNumber};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, io::Write, net::SocketAddr, os::unix::fs::OpenOptionsExt, path::{Path, PathBuf}, sync::{Arc, Mutex}};

/// The deviceGroups format version AWS defines.
pub const FORMAT_VERSION: &str = "2021-03-05";
/// The policy variable AWS substitutes with the connecting thing's name.
const THING_VARIABLE: &str = "${iot:Connection.Thing.ThingName}";
const OPERATIONS: [(&str, &str); 3] =
    [("mqtt:connect", "mqtt:clientId:"), ("mqtt:publish", "mqtt:topic:"), ("mqtt:subscribe", "mqtt:topicfilter:")];
const MAX_CSR: usize = 16 * 1024;
const CA_YEARS: i64 = 10;
/// Issued certificates start this long before issuance (clock skew).
const BACKDATE_SECONDS: i64 = 300;

/// `[client_devices]` (docs/CLIENT_DEVICES.md).
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    /// The AWS deviceGroups document (definitions and policies) as JSON.
    pub device_groups_file: PathBuf,
    /// The local broker LAN clients are proxied to; loopback only.
    pub broker: String,
    #[serde(default)]
    pub broker_username: Option<String>,
    #[serde(default)]
    pub broker_password_file: Option<PathBuf>,
    #[serde(default = "default_mqtt_listen")]
    pub mqtt_listen: String,
    #[serde(default = "default_https_listen")]
    pub https_listen: String,
    /// Unauthenticated LAN discovery hints over UDP; off when omitted.
    #[serde(default)]
    pub udp_discovery: Option<String>,
    #[serde(default = "default_validity")]
    pub certificate_validity_seconds: u64,
    #[serde(default = "default_validity")]
    pub server_certificate_validity_seconds: u64,
    #[serde(default = "default_grace")]
    pub rotation_grace_seconds: u64,
    /// A certificate younger than this cannot rotate (bounds registry writes).
    #[serde(default = "default_rotation_interval")]
    pub min_rotation_interval_seconds: u64,
    #[serde(default = "default_ttl")]
    pub discovery_ttl_seconds: u64,
    /// Extra DNS names for the gateway's server certificate and discovery.
    #[serde(default)]
    pub hostnames: Vec<String>,
    #[serde(default)]
    pub include_loopback: bool,
    #[serde(default)]
    pub include_link_local: bool,
    #[serde(default = "default_connections")]
    pub max_connections: usize,
    #[serde(default = "default_devices")]
    pub max_devices: usize,
}
fn default_mqtt_listen() -> String {
    "0.0.0.0:8883".into()
}
fn default_https_listen() -> String {
    "0.0.0.0:8443".into()
}
fn default_validity() -> u64 {
    7 * 86_400
}
fn default_grace() -> u64 {
    300
}
fn default_rotation_interval() -> u64 {
    60
}
fn default_ttl() -> u64 {
    3600
}
fn default_connections() -> usize {
    64
}
fn default_devices() -> usize {
    1000
}

pub const MIN_VALIDITY_SECONDS: u64 = 60;
pub const MAX_VALIDITY_SECONDS: u64 = 365 * 86_400;

impl Settings {
    pub fn validate(&self) -> anyhow::Result<Policy> {
        let listen = |what: &str, address: &str| -> anyhow::Result<SocketAddr> {
            address.parse().map_err(|_| anyhow::anyhow!("client_devices.{what} must be IP:port"))
        };
        let (mqtt, https) = (listen("mqtt_listen", &self.mqtt_listen)?, listen("https_listen", &self.https_listen)?);
        anyhow::ensure!(mqtt != https, "client_devices.mqtt_listen and https_listen must differ");
        if let Some(udp) = &self.udp_discovery {
            listen("udp_discovery", udp)?;
        }
        // LAN clients must not reach the broker except through the gateway.
        anyhow::ensure!(listen("broker", &self.broker)?.ip().is_loopback(),
            "client_devices.broker must be a loopback address: LAN clients would otherwise bypass authorization");
        anyhow::ensure!(self.broker_password_file.is_none() || self.broker_username.is_some(),
            "client_devices.broker_password_file needs broker_username");
        anyhow::ensure!(self.broker_username.as_ref().is_none_or(|u| !u.is_empty() && u.len() <= 256),
            "client_devices.broker_username must be 1..=256 bytes");
        for (what, value) in [("certificate_validity_seconds", self.certificate_validity_seconds),
                              ("server_certificate_validity_seconds", self.server_certificate_validity_seconds)] {
            anyhow::ensure!((MIN_VALIDITY_SECONDS..=MAX_VALIDITY_SECONDS).contains(&value),
                "client_devices.{what} must be {MIN_VALIDITY_SECONDS}..={MAX_VALIDITY_SECONDS}");
        }
        anyhow::ensure!(self.rotation_grace_seconds <= 86_400, "client_devices.rotation_grace_seconds must be 0..=86400");
        anyhow::ensure!(self.min_rotation_interval_seconds <= 86_400, "client_devices.min_rotation_interval_seconds must be 0..=86400");
        anyhow::ensure!((60..=86_400).contains(&self.discovery_ttl_seconds), "client_devices.discovery_ttl_seconds must be 60..=86400");
        anyhow::ensure!((1..=1024).contains(&self.max_connections), "client_devices.max_connections must be 1..=1024");
        anyhow::ensure!((1..=100_000).contains(&self.max_devices), "client_devices.max_devices must be 1..=100000");
        anyhow::ensure!(self.hostnames.len() <= 16 && self.hostnames.iter().all(|h| valid_hostname(h)),
            "client_devices.hostnames must be at most 16 DNS names");
        let raw = std::fs::read_to_string(&self.device_groups_file)
            .map_err(|e| anyhow::anyhow!("cannot read client_devices.device_groups_file: {e}"))?;
        Policy::parse(&raw)
    }

    pub fn broker_password(&self) -> anyhow::Result<Option<Vec<u8>>> {
        self.broker_password_file.as_ref().map(|path| {
            let mut bytes = std::fs::read(path)?;
            while bytes.last().is_some_and(|b| *b == b'\n' || *b == b'\r') {
                bytes.pop();
            }
            anyhow::ensure!(bytes.len() <= 65_535, "broker password too long");
            Ok(bytes)
        }).transpose()
    }
}

fn valid_hostname(name: &str) -> bool {
    !name.is_empty() && name.len() <= 253 && name.split('.').all(|label| {
        !label.is_empty() && label.len() <= 63 && !label.starts_with('-') && !label.ends_with('-')
            && label.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
    })
}

/// AWS thing names (as for shadows): 1-128 of `[a-zA-Z0-9:_-]`. The thing
/// name is also the only MQTT client ID the device may use.
pub use crate::shadows::valid_thing;

// ---------------------------------------------------------------------------
// Device groups and policies.
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct GroupsDocument {
    format_version: String,
    definitions: BTreeMap<String, DefinitionDocument>,
    policies: BTreeMap<String, BTreeMap<String, StatementDocument>>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct DefinitionDocument {
    selection_rule: String,
    policy_name: String,
}
#[derive(Deserialize, Clone, Debug)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct StatementDocument {
    #[serde(default)]
    #[allow(dead_code)]
    statement_description: Option<String>,
    operations: Vec<String>,
    resources: Vec<String>,
}

/// One `thingName:` term of a selection rule: a name with an optional `*`
/// at its start and/or end.
#[derive(Debug, Clone)]
struct Selector {
    text: String,
    any_prefix: bool,
    any_suffix: bool,
}
impl Selector {
    fn matches(&self, thing: &str) -> bool {
        match (self.any_prefix, self.any_suffix) {
            (true, true) => thing.contains(&self.text),
            (true, false) => thing.ends_with(&self.text),
            (false, true) => thing.starts_with(&self.text),
            (false, false) => thing == self.text,
        }
    }
}

#[derive(Debug, Clone)]
struct Group {
    selectors: Vec<Selector>,
    statements: Vec<StatementDocument>,
}

/// Compiled device groups: the union of every group selecting a thing applies.
#[derive(Debug, Clone)]
pub struct Policy {
    groups: Vec<Group>,
    /// SHA-256 of the groups document, to notice policy changes.
    digest: String,
}

fn parse_selection_rule(rule: &str) -> anyhow::Result<Vec<Selector>> {
    anyhow::ensure!(rule.len() <= 2048, "selection rule too long");
    rule.split(" OR ").map(|term| {
        let (key, pattern) = term.split_once(':').ok_or_else(|| anyhow::anyhow!("selection term {term:?} needs thingName:"))?;
        anyhow::ensure!(key.trim() == "thingName", "unsupported selection key {:?} (only thingName)", key.trim());
        let pattern = pattern.trim().replace("\\:", ":");
        let any_prefix = pattern.starts_with('*');
        let any_suffix = pattern.len() > 1 && pattern.ends_with('*');
        let text = pattern.trim_start_matches('*').trim_end_matches('*');
        anyhow::ensure!(!text.contains('*'), "selection pattern {pattern:?}: * only at the start or end");
        anyhow::ensure!(!text.is_empty() || any_prefix, "empty selection pattern");
        Ok(Selector { text: text.to_owned(), any_prefix, any_suffix })
    }).collect()
}

impl Policy {
    pub fn parse(raw: &str) -> anyhow::Result<Self> {
        let document: GroupsDocument = serde_json::from_str(raw).map_err(|e| anyhow::anyhow!("invalid device groups: {e}"))?;
        anyhow::ensure!(document.format_version == FORMAT_VERSION, "device groups formatVersion must be {FORMAT_VERSION}");
        anyhow::ensure!(document.definitions.len() <= 256 && document.policies.len() <= 256, "too many device groups or policies");
        for (name, statements) in &document.policies {
            anyhow::ensure!(statements.len() <= 64, "policy {name}: too many statements");
            for (id, statement) in statements {
                anyhow::ensure!(!statement.operations.is_empty() && !statement.resources.is_empty()
                    && statement.operations.len() <= 8 && statement.resources.len() <= 256,
                    "policy {name}/{id}: operations and resources must be nonempty");
                for operation in &statement.operations {
                    anyhow::ensure!(operation == "*" || OPERATIONS.iter().any(|(o, _)| o == operation),
                        "policy {name}/{id}: unknown operation {operation:?}");
                }
                for resource in &statement.resources {
                    anyhow::ensure!(resource.len() <= 1024 && (resource == "*" || OPERATIONS.iter().any(|(_, p)| resource.starts_with(p))),
                        "policy {name}/{id}: resource {resource:?} must be * or mqtt:clientId:/mqtt:topic:/mqtt:topicfilter:");
                }
            }
        }
        let groups = document.definitions.iter().map(|(name, definition)| {
            let statements = document.policies.get(&definition.policy_name)
                .ok_or_else(|| anyhow::anyhow!("device group {name}: unknown policy {:?}", definition.policy_name))?;
            Ok(Group {
                selectors: parse_selection_rule(&definition.selection_rule).map_err(|e| e.context(format!("device group {name}")))?,
                statements: statements.values().cloned().collect(),
            })
        }).collect::<anyhow::Result<_>>()?;
        Ok(Self { groups, digest: hex::encode(Sha256::digest(raw.as_bytes())) })
    }

    /// Whether the groups selecting `thing` grant `operation` on `resource`.
    /// Identity state is checked separately ([`Authority::permits`]).
    pub fn permits(&self, thing: &str, operation: &str, resource: &str) -> bool {
        let Some((_, prefix)) = OPERATIONS.iter().find(|(o, _)| *o == operation) else { return false };
        if !resource.starts_with(prefix) {
            return false;
        }
        // Hardening relative to AWS: a device connects only as itself.
        if operation == "mqtt:connect" && resource[prefix.len()..] != *thing {
            return false;
        }
        self.groups.iter().filter(|g| g.selectors.iter().any(|s| s.matches(thing))).any(|group| {
            group.statements.iter().any(|statement| {
                statement.operations.iter().any(|o| o == "*" || o == operation)
                    && statement.resources.iter().any(|r| r == "*" || crate::streams::glob(&r.replace(THING_VARIABLE, thing), resource))
            })
        })
    }
}

// ---------------------------------------------------------------------------
// The registry of enrolled things and their certificates.
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Active,
    Revoked,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Certificate {
    /// SHA-256 of the certificate's DER encoding (lowercase hex).
    pub sha256: String,
    pub serial: String,
    pub issued_ms: u64,
    pub not_after_ms: u64,
    /// Set when a rotation superseded this certificate: usable until then.
    #[serde(default)]
    pub retire_ms: Option<u64>,
}
impl Certificate {
    fn usable(&self, now_ms: u64) -> bool {
        now_ms < self.not_after_ms && self.retire_ms.is_none_or(|retire| now_ms < retire)
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Thing {
    pub state: State,
    pub enrolled_ms: u64,
    /// Lifetime of each certificate issued to it (enrollment and rotations).
    pub validity_seconds: u64,
    #[serde(default)]
    pub revoked_ms: Option<u64>,
    pub certificates: Vec<Certificate>,
    /// The policy digest under which its broker session was last opened: a
    /// persistent session (and the subscriptions in it) is only resumed under
    /// the same policy.
    #[serde(default)]
    pub session_policy: Option<String>,
}
impl Thing {
    /// Active with at least one usable certificate.
    pub fn valid(&self, now_ms: u64) -> bool {
        self.state == State::Active && self.certificates.iter().any(|c| c.usable(now_ms))
    }
}

#[derive(Serialize, Deserialize, Default, Debug)]
#[serde(deny_unknown_fields)]
struct RegistryFile {
    format: u32,
    things: BTreeMap<String, Thing>,
}

/// The registry and its certificate-fingerprint index (checked per packet).
struct Registry {
    file: RegistryFile,
    index: std::collections::HashMap<String, String>,
}
impl Registry {
    fn new(file: RegistryFile) -> Self {
        let index = file.things.iter()
            .flat_map(|(name, thing)| thing.certificates.iter().map(move |c| (c.sha256.clone(), name.clone())))
            .collect();
        Self { file, index }
    }
}

/// Why a presented certificate is not an authenticated identity.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Denial {
    Unknown,
    Revoked,
    Expired,
    Superseded,
}
impl Denial {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unknown => "unknown",
            Self::Revoked => "revoked",
            Self::Expired => "expired",
            Self::Superseded => "superseded",
        }
    }
}
impl std::fmt::Display for Denial {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A newly issued client certificate.
#[derive(Debug, Clone)]
pub struct Issued {
    pub thing: String,
    pub certificate_pem: String,
    pub record: Certificate,
}

/// The device this gateway runs on, as the discovery document names it.
#[derive(Debug, Clone, Serialize)]
pub struct Gateway {
    pub chain: String,
    pub registry: String,
    pub device_object_id: String,
    pub address: String,
}

pub const CA_BINDING_KIND: &str = "client-device-ca";
const MAX_CA_BINDING_BYTES: usize = 1024;

/// The statement, signed with the device key as a Sui personal message, that
/// this device operates the client-device CA with this fingerprint. Clients
/// that check the device on chain can trust the CA (and through mutual TLS,
/// the gateway's discovery answers) without a separate trust root.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CaBinding {
    pub schema: u32,
    pub kind: String,
    pub chain_identifier: String,
    pub registry: String,
    pub device_id: String,
    pub signer: String,
    pub ca_sha256: String,
    pub created_ms: u64,
}

/// The signing service's check (SF-053): exactly one canonical CA binding of
/// this chain and registry, naming this key.
pub fn check_ca_binding(message: &[u8], chain_identifier: &str, registry: &str, signer: &str) -> anyhow::Result<()> {
    use crate::streams::same_address;
    anyhow::ensure!(message.len() <= MAX_CA_BINDING_BYTES, "CA binding exceeds {MAX_CA_BINDING_BYTES} bytes");
    let binding: CaBinding = serde_json::from_slice(message).map_err(|e| anyhow::anyhow!("not a CA binding: {e}"))?;
    anyhow::ensure!(serde_json::to_vec(&binding)? == message, "CA binding is not in canonical form");
    anyhow::ensure!(binding.schema == 1 && binding.kind == CA_BINDING_KIND, "unsupported CA binding");
    anyhow::ensure!(binding.chain_identifier == chain_identifier, "CA binding is for another chain");
    anyhow::ensure!(same_address(&binding.registry, registry), "CA binding is not for this fleet's registry");
    anyhow::ensure!(same_address(&binding.signer, signer), "CA binding names another signer");
    anyhow::ensure!(same_address(&binding.device_id, &binding.device_id)
        && binding.ca_sha256.len() == 64 && binding.ca_sha256.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "malformed CA binding");
    Ok(())
}

#[derive(Default, Debug)]
struct Counters {
    sessions: BTreeMap<String, usize>,
    accepted: u64,
    denied: BTreeMap<String, u64>,
}

pub fn fingerprint(der: &[u8]) -> String {
    hex::encode(Sha256::digest(der))
}

fn write_private(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    let temporary = path.with_extension("tmp");
    let mut file = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&temporary)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    std::fs::rename(&temporary, path)?;
    std::fs::File::open(path.parent().unwrap_or(Path::new(".")))?.sync_all()?;
    Ok(())
}

fn at_ms(ms: u64) -> anyhow::Result<time::OffsetDateTime> {
    Ok(time::OffsetDateTime::from_unix_timestamp((ms / 1000) as i64)?)
}

fn random_serial() -> SerialNumber {
    let mut bytes: [u8; 16] = rand::random();
    bytes[0] &= 0x7f; // positive
    bytes[0] |= 0x01; // and of full length
    SerialNumber::from_slice(&bytes)
}

/// The CA, registry, policy and live-session counters, shared by the gateway
/// tasks and the operator's IPC requests.
pub struct Authority {
    dir: PathBuf,
    pub settings: Settings,
    policy: Policy,
    ca_key: KeyPair,
    ca_der: rustls::pki_types::CertificateDer<'static>,
    ca_pem: String,
    ca_sha256: String,
    gateway: Gateway,
    /// The signed [`CaBinding`] served with every discovery document.
    ca_binding: Value,
    registry: Mutex<Registry>,
    counters: Mutex<Counters>,
    /// MQTT client IDs of the broker's local clients (the SF-032 bridge): a
    /// thing with that name could take over their broker sessions.
    reserved_client_ids: Vec<String>,
}

impl Authority {
    /// Open (or on first use create) the CA and registry under `dir`.
    pub fn open(dir: &Path, settings: Settings, gateway: Gateway, signer: Arc<dyn crate::identity::TxSigner>, now_ms: u64) -> anyhow::Result<Self> {
        let policy = settings.validate()?;
        std::fs::create_dir_all(dir)?;
        std::fs::set_permissions(dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))?;
        let (key_path, cert_path) = (dir.join("ca.key"), dir.join("ca.pem"));
        let (ca_key, ca_pem) = match (key_path.exists(), cert_path.exists()) {
            (true, true) => (KeyPair::from_pem(&std::fs::read_to_string(&key_path)?)?, std::fs::read_to_string(&cert_path)?),
            (false, false) => {
                let key = KeyPair::generate()?;
                let mut params = CertificateParams::default();
                let mut name = DistinguishedName::new();
                name.push(DnType::OrganizationName, "SuiFleet");
                name.push(DnType::CommonName, "SuiFleet client-device CA");
                params.distinguished_name = name;
                params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
                params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign, KeyUsagePurpose::DigitalSignature];
                params.serial_number = Some(random_serial());
                params.not_before = at_ms(now_ms)? - time::Duration::seconds(BACKDATE_SECONDS);
                params.not_after = at_ms(now_ms)? + time::Duration::days(365 * CA_YEARS);
                let certificate = params.self_signed(&key)?;
                // The key first: a CA certificate is never present without it.
                write_private(&key_path, key.serialize_pem().as_bytes())?;
                write_private(&cert_path, certificate.pem().as_bytes())?;
                (key, certificate.pem())
            }
            _ => anyhow::bail!("client-device CA is incomplete in {}: restore ca.key and ca.pem together", dir.display()),
        };
        let ca_der = rustls::pki_types::CertificateDer::from(pem::parse(&ca_pem)?.contents().to_vec());
        // The stored certificate must belong to the stored key.
        let parsed = Issuer::from_ca_cert_der(&ca_der, &ca_key)?;
        drop(parsed);
        anyhow::ensure!(x509_public_key(&ca_der)? == ca_key.public_key_raw(), "client-device CA certificate does not match ca.key");
        let registry = match std::fs::read(dir.join("registry.json")) {
            Ok(bytes) => {
                let registry: RegistryFile = serde_json::from_slice(&bytes)
                    .map_err(|e| anyhow::anyhow!("client-device registry is unreadable ({e}); refusing to start rather than forget revocations"))?;
                anyhow::ensure!(registry.format == 1, "unsupported client-device registry format");
                registry
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => RegistryFile { format: 1, things: BTreeMap::new() },
            Err(e) => return Err(e.into()),
        };
        let ca_sha256 = fingerprint(&ca_der);
        let statement = serde_json::to_vec(&CaBinding {
            schema: 1, kind: CA_BINDING_KIND.into(), chain_identifier: gateway.chain.clone(), registry: gateway.registry.clone(),
            device_id: gateway.device_object_id.clone(), signer: gateway.address.clone(), ca_sha256: ca_sha256.clone(), created_ms: now_ms,
        })?;
        let ca_binding = json!({"message": STANDARD.encode(&statement), "signature": STANDARD.encode(signer.sign_personal_message(&statement)?),
                                "signer": gateway.address});
        Ok(Self {
            dir: dir.to_owned(), settings, policy, ca_sha256, ca_key, ca_der, ca_pem, gateway, ca_binding,
            registry: Mutex::new(Registry::new(registry)), counters: Mutex::new(Counters::default()), reserved_client_ids: Vec::new(),
        })
    }

    /// Never enroll or admit a thing named `client_id` (another broker client).
    pub fn reserve_client_id(&mut self, client_id: &str) {
        self.reserved_client_ids.push(client_id.to_owned());
    }

    pub fn ca_der(&self) -> &rustls::pki_types::CertificateDer<'static> {
        &self.ca_der
    }
    pub fn ca_sha256(&self) -> &str {
        &self.ca_sha256
    }
    pub fn gateway(&self) -> &Gateway {
        &self.gateway
    }

    fn save(&self, registry: &RegistryFile) -> anyhow::Result<()> {
        write_private(&self.dir.join("registry.json"), &serde_json::to_vec_pretty(registry)?)
    }

    /// Sign a certificate for `public_key` with this CA.
    fn sign(&self, params: &CertificateParams, public_key: &impl rcgen::PublicKeyData) -> anyhow::Result<rcgen::Certificate> {
        let issuer = Issuer::from_ca_cert_der(&self.ca_der, &self.ca_key)?;
        Ok(params.signed_by(public_key, &issuer)?)
    }

    fn issue(&self, thing: &str, csr_pem: &str, validity_seconds: u64, now_ms: u64) -> anyhow::Result<Issued> {
        anyhow::ensure!(csr_pem.len() <= MAX_CSR, "CSR too large");
        // Verifies the CSR's self-signature (proof of possession). Only its
        // public key is used: names, SANs and extensions come from the CA.
        let csr = rcgen::CertificateSigningRequestParams::from_pem(csr_pem).map_err(|e| anyhow::anyhow!("invalid CSR: {e}"))?;
        let mut params = CertificateParams::default();
        let mut name = DistinguishedName::new();
        name.push(DnType::OrganizationName, "SuiFleet client device");
        name.push(DnType::CommonName, thing);
        params.distinguished_name = name;
        params.is_ca = IsCa::ExplicitNoCa;
        params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
        params.use_authority_key_identifier_extension = true;
        let serial = random_serial();
        params.serial_number = Some(serial.clone());
        let not_after_ms = (now_ms / 1000 + validity_seconds) * 1000;
        params.not_before = at_ms(now_ms)? - time::Duration::seconds(BACKDATE_SECONDS);
        params.not_after = at_ms(not_after_ms)?;
        let certificate = self.sign(&params, &csr.public_key)?;
        Ok(Issued {
            thing: thing.to_owned(),
            certificate_pem: certificate.pem(),
            record: Certificate { sha256: fingerprint(certificate.der()), serial: hex::encode(serial.to_bytes()), issued_ms: now_ms,
                not_after_ms, retire_ms: None },
        })
    }

    /// Operator enrollment of a new thing from its CSR.
    pub fn enroll(&self, thing: &str, csr_pem: &str, validity_seconds: Option<u64>, now_ms: u64) -> anyhow::Result<Issued> {
        anyhow::ensure!(valid_thing(thing), "invalid thing name");
        anyhow::ensure!(!self.reserved_client_ids.iter().any(|id| id == thing), "thing name is a reserved broker client ID");
        let validity = validity_seconds.unwrap_or(self.settings.certificate_validity_seconds);
        anyhow::ensure!((MIN_VALIDITY_SECONDS..=self.settings.certificate_validity_seconds).contains(&validity),
            "validity_seconds must be {MIN_VALIDITY_SECONDS}..=client_devices.certificate_validity_seconds");
        let mut registry = self.registry.lock().unwrap();
        anyhow::ensure!(!registry.file.things.contains_key(thing), "thing already enrolled; remove it first to re-enroll");
        anyhow::ensure!(registry.file.things.len() < self.settings.max_devices, "client_devices.max_devices reached");
        let issued = self.issue(thing, csr_pem, validity, now_ms)?;
        let mut next = RegistryFile { format: 1, things: registry.file.things.clone() };
        next.things.insert(thing.to_owned(), Thing { state: State::Active, enrolled_ms: now_ms, validity_seconds: validity,
            revoked_ms: None, certificates: vec![issued.record.clone()], session_policy: None });
        self.save(&next)?;
        *registry = Registry::new(next);
        Ok(issued)
    }

    /// A new certificate for the thing that presented `presented` (already
    /// authenticated). The presented certificate stays usable for the
    /// rotation grace period; any older superseded one is dropped.
    pub fn rotate(&self, presented: &str, csr_pem: &str, now_ms: u64) -> anyhow::Result<Issued> {
        let thing = self.authenticate(presented, now_ms).map_err(|d| anyhow::anyhow!("not authenticated: {d}"))?;
        let mut registry = self.registry.lock().unwrap();
        let current = registry.file.things.get(&thing).ok_or_else(|| anyhow::anyhow!("not authenticated: unknown"))?;
        let presented_record = current.certificates.iter().find(|c| c.sha256 == presented && c.retire_ms.is_none())
            .ok_or_else(|| anyhow::anyhow!("only the current certificate may rotate"))?;
        anyhow::ensure!(now_ms >= presented_record.issued_ms + self.settings.min_rotation_interval_seconds * 1000,
            "certificate rotated too recently (client_devices.min_rotation_interval_seconds)");
        // Rotation keeps the lifetime the thing was enrolled with (within the current bound).
        let validity = current.validity_seconds.min(self.settings.certificate_validity_seconds);
        let issued = self.issue(&thing, csr_pem, validity, now_ms)?;
        let mut record = current.clone();
        let grace_end = now_ms + self.settings.rotation_grace_seconds * 1000;
        record.certificates.retain(|c| c.retire_ms.is_none() && c.usable(now_ms));
        for certificate in &mut record.certificates {
            certificate.retire_ms = Some(grace_end.min(certificate.not_after_ms));
        }
        record.certificates.push(issued.record.clone());
        let mut next = RegistryFile { format: 1, things: registry.file.things.clone() };
        next.things.insert(thing.clone(), record);
        self.save(&next)?;
        *registry = Registry::new(next);
        Ok(issued)
    }

    /// Whether the broker may resume `thing`'s persistent session: only if it
    /// was opened under the current policy. Records the current policy as the
    /// session's from now on (the caller then opens a clean session if not).
    pub fn resume_session(&self, thing: &str) -> anyhow::Result<bool> {
        let mut registry = self.registry.lock().unwrap();
        let record = registry.file.things.get(thing).ok_or_else(|| anyhow::anyhow!("unknown thing"))?;
        if record.session_policy.as_deref() == Some(self.policy.digest.as_str()) {
            return Ok(true);
        }
        let mut next = RegistryFile { format: 1, things: registry.file.things.clone() };
        next.things.get_mut(thing).unwrap().session_policy = Some(self.policy.digest.clone());
        self.save(&next)?;
        *registry = Registry::new(next);
        Ok(false)
    }

    pub fn revoke(&self, thing: &str, now_ms: u64) -> anyhow::Result<()> {
        let mut registry = self.registry.lock().unwrap();
        let mut next = RegistryFile { format: 1, things: registry.file.things.clone() };
        let record = next.things.get_mut(thing).ok_or_else(|| anyhow::anyhow!("unknown thing"))?;
        if record.state == State::Revoked {
            return Ok(());
        }
        record.state = State::Revoked;
        record.revoked_ms = Some(now_ms);
        self.save(&next)?;
        *registry = Registry::new(next);
        Ok(())
    }

    pub fn remove(&self, thing: &str) -> anyhow::Result<()> {
        let mut registry = self.registry.lock().unwrap();
        let mut next = RegistryFile { format: 1, things: registry.file.things.clone() };
        anyhow::ensure!(next.things.remove(thing).is_some(), "unknown thing");
        self.save(&next)?;
        *registry = Registry::new(next);
        Ok(())
    }

    /// The thing whose certificate has this fingerprint, if that certificate
    /// is currently an authenticated identity.
    pub fn authenticate(&self, sha256: &str, now_ms: u64) -> Result<String, Denial> {
        let registry = self.registry.lock().unwrap();
        let name = registry.index.get(sha256).ok_or(Denial::Unknown)?;
        let thing = registry.file.things.get(name).ok_or(Denial::Unknown)?;
        let certificate = thing.certificates.iter().find(|c| c.sha256 == sha256).ok_or(Denial::Unknown)?;
        if thing.state == State::Revoked {
            return Err(Denial::Revoked);
        }
        if now_ms >= certificate.not_after_ms {
            return Err(Denial::Expired);
        }
        if certificate.retire_ms.is_some_and(|retire| now_ms >= retire) {
            return Err(Denial::Superseded);
        }
        Ok(name.clone())
    }

    /// Identity and policy together: the certificate `sha256` is an
    /// authenticated identity whose groups grant `operation` on `resource`.
    pub fn permits(&self, sha256: &str, operation: &str, resource: &str, now_ms: u64) -> bool {
        self.authenticate(sha256, now_ms).is_ok_and(|thing| self.policy.permits(&thing, operation, resource)
            && !(operation == "mqtt:connect" && self.reserved_client_ids.contains(&thing)))
    }

    /// The registry-level check (as `client-device-cases.json` states it): an
    /// active thing with a usable certificate whose groups grant the request.
    #[cfg(test)]
    pub fn thing_permits(&self, thing: &str, operation: &str, resource: &str, now_ms: u64) -> bool {
        let valid = self.registry.lock().unwrap().file.things.get(thing).is_some_and(|t| t.valid(now_ms));
        valid && self.policy.permits(thing, operation, resource)
    }

    pub fn count_denial(&self, reason: &str) {
        *self.counters.lock().unwrap().denied.entry(reason.to_owned()).or_default() += 1;
    }

    /// Track a live session; the guard ends it.
    pub fn session(self: &Arc<Self>, thing: &str) -> SessionGuard {
        let mut counters = self.counters.lock().unwrap();
        counters.accepted += 1;
        *counters.sessions.entry(thing.to_owned()).or_default() += 1;
        SessionGuard { authority: self.clone(), thing: thing.to_owned() }
    }

    pub fn status(&self, now_ms: u64) -> Value {
        let registry = self.registry.lock().unwrap();
        let counters = self.counters.lock().unwrap();
        let things: BTreeMap<_, _> = registry.file.things.iter().map(|(name, thing)| (name.clone(), json!({
            "state": thing.state, "valid": thing.valid(now_ms), "enrolled_ms": thing.enrolled_ms, "revoked_ms": thing.revoked_ms,
            "certificates": thing.certificates, "sessions": counters.sessions.get(name).copied().unwrap_or(0),
        }))).collect();
        json!({"ca_sha256": self.ca_sha256, "gateway": self.gateway, "things": things,
               "sessions_accepted": counters.accepted, "denied": counters.denied})
    }

    /// Operator IPC: `{"action": "list" | "enroll" | "revoke" | "remove", ...}`.
    pub fn handle(&self, payload: &Value, now_ms: u64) -> anyhow::Result<Value> {
        let thing = || payload.get("thing").and_then(Value::as_str).ok_or_else(|| anyhow::anyhow!("thing required"));
        match payload.get("action").and_then(Value::as_str) {
            Some("list") => Ok(self.status(now_ms)),
            Some("enroll") => {
                let csr = payload.get("csr").and_then(Value::as_str).ok_or_else(|| anyhow::anyhow!("csr required"))?;
                let validity = match payload.get("validity_seconds") {
                    None | Some(Value::Null) => None,
                    Some(value) => Some(value.as_u64().ok_or_else(|| anyhow::anyhow!("validity_seconds must be a number"))?),
                };
                let issued = self.enroll(thing()?, csr, validity, now_ms)?;
                Ok(json!({"thing": issued.thing, "certificate": issued.certificate_pem, "ca": self.ca_pem,
                          "sha256": issued.record.sha256, "not_after_ms": issued.record.not_after_ms}))
            }
            Some("revoke") => {
                self.revoke(thing()?, now_ms)?;
                Ok(json!({"revoked": thing()?}))
            }
            Some("remove") => {
                self.remove(thing()?)?;
                Ok(json!({"removed": thing()?}))
            }
            _ => anyhow::bail!("unknown client_devices action"),
        }
    }

    /// The rotation response: the new certificate and the CA.
    pub fn issued_json(&self, issued: &Issued) -> Value {
        json!({"thing": issued.thing, "certificate": issued.certificate_pem, "ca": self.ca_pem,
               "sha256": issued.record.sha256, "not_after_ms": issued.record.not_after_ms})
    }

    /// The discovery document for an authenticated client. Its authenticity
    /// comes from the mutual-TLS channel it is served on (a server certificate
    /// of this CA); the CA binding ties that CA to the on-chain device key.
    pub fn discovery(&self, presented: &str, mqtt: &[String], https: &[String], now_ms: u64) -> anyhow::Result<Value> {
        let thing = self.authenticate(presented, now_ms).map_err(|d| anyhow::anyhow!("not authenticated: {d}"))?;
        let certificate = self.registry.lock().unwrap().file.things.get(&thing)
            .and_then(|t| t.certificates.iter().find(|c| c.sha256 == presented).cloned())
            .ok_or_else(|| anyhow::anyhow!("not authenticated: unknown"))?;
        // Rotate once two thirds of the lifetime have passed.
        let rotate_after_ms = certificate.issued_ms + (certificate.not_after_ms.saturating_sub(certificate.issued_ms)) * 2 / 3;
        let document = json!({
            "format": 1,
            "gateway": self.gateway,
            "thing": thing,
            "ca": {"pem": self.ca_pem, "sha256": self.ca_sha256},
            "mqtt": mqtt,
            "https": https,
            "certificate": {"sha256": certificate.sha256, "not_after_ms": certificate.not_after_ms,
                            "retire_ms": certificate.retire_ms, "rotate_after_ms": rotate_after_ms},
            "issued_ms": now_ms,
            "expires_ms": now_ms + self.settings.discovery_ttl_seconds * 1000,
        });
        Ok(json!({"document": document, "ca_binding": self.ca_binding}))
    }

    /// A fresh server certificate (and key, kept in memory only) for `names`.
    pub fn server_certificate(&self, names: &[SanType], now_ms: u64) -> anyhow::Result<(Vec<rustls::pki_types::CertificateDer<'static>>, rustls::pki_types::PrivateKeyDer<'static>)> {
        let key = KeyPair::generate()?;
        let mut params = CertificateParams::default();
        let mut name = DistinguishedName::new();
        name.push(DnType::OrganizationName, "SuiFleet");
        name.push(DnType::CommonName, "SuiFleet client-device gateway");
        params.distinguished_name = name;
        params.subject_alt_names = names.to_vec();
        params.is_ca = IsCa::ExplicitNoCa;
        params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        params.use_authority_key_identifier_extension = true;
        params.serial_number = Some(random_serial());
        params.not_before = at_ms(now_ms)? - time::Duration::seconds(BACKDATE_SECONDS);
        params.not_after = at_ms(now_ms + self.settings.server_certificate_validity_seconds * 1000)?;
        let certificate = self.sign(&params, &key)?;
        let private = rustls::pki_types::PrivateKeyDer::Pkcs8(key.serialize_der().into());
        Ok((vec![certificate.der().clone(), self.ca_der.clone()], private))
    }
}

/// The subject public key of a DER certificate.
fn x509_public_key(der: &[u8]) -> anyhow::Result<Vec<u8>> {
    let (_, certificate) = x509_parser::parse_x509_certificate(der).map_err(|e| anyhow::anyhow!("invalid CA certificate: {e}"))?;
    Ok(certificate.public_key().subject_public_key.data.to_vec())
}

pub struct SessionGuard {
    authority: Arc<Authority>,
    thing: String,
}
impl Drop for SessionGuard {
    fn drop(&mut self) {
        let mut counters = self.authority.counters.lock().unwrap();
        if let Some(count) = counters.sessions.get_mut(&self.thing) {
            *count -= 1;
            if *count == 0 {
                counters.sessions.remove(&self.thing);
            }
        }
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::identity::{Ed25519File, TxSigner};

    pub const GROUPS: &str = r#"{"formatVersion":"2021-03-05",
      "definitions":{"sensors":{"selectionRule":"thingName: sensor-*","policyName":"sensorPolicy"}},
      "policies":{"sensorPolicy":{
        "connect":{"operations":["mqtt:connect"],"resources":["mqtt:clientId:sensor-*"]},
        "publish":{"operations":["mqtt:publish"],"resources":["mqtt:topic:sensors/${iot:Connection.Thing.ThingName}/*", "mqtt:topic:lan/commands/*"]},
        "subscribe":{"operations":["mqtt:subscribe"],"resources":["mqtt:topicfilter:commands/+"]}}}}"#;

    pub fn settings(dir: &Path, groups: &str) -> Settings {
        let path = dir.join("groups.json");
        std::fs::write(&path, groups).unwrap();
        toml::from_str(&format!("device_groups_file = {:?}\nbroker = \"127.0.0.1:1883\"\nrotation_grace_seconds = 10\nmin_rotation_interval_seconds = 1\n", path)).unwrap()
    }

    pub fn authority(dir: &Path, groups: &str, now_ms: u64) -> Authority {
        let signer = Arc::new(Ed25519File(ed25519_dalek::SigningKey::from_bytes(&[7; 32])));
        let gateway = Gateway { chain: "c".into(), registry: "0x13".into(), device_object_id: "0x1".into(), address: signer.address() };
        Authority::open(&dir.join("cd"), settings(dir, groups), gateway, signer, now_ms).unwrap()
    }

    pub fn csr() -> (KeyPair, String) {
        let key = KeyPair::generate().unwrap();
        let request = CertificateParams::default().serialize_request(&key).unwrap();
        (key, request.pem().unwrap())
    }

    fn temp() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("suifleet-cd-{}-{}", std::process::id(), rand::random::<u64>()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn shared_client_device_authorization_cases() {
        // interfaces/compat/client-device-cases.json (SF-028), executed against
        // the daemon's registry and policy engine.
        let cases: Vec<Value> = serde_json::from_str(include_str!("../../interfaces/compat/client-device-cases.json")).unwrap();
        let mut count = 0;
        for case in cases {
            let dir = temp();
            let authority = authority(&dir, &case["config"]["deviceGroups"].to_string(), 0);
            {
                let mut guard = authority.registry.lock().unwrap();
                let mut registry = RegistryFile { format: 1, things: BTreeMap::new() };
                for (name, identity) in case["config"]["identities"].as_object().unwrap() {
                    let state = if identity["state"] == "active" { State::Active } else { State::Revoked };
                    registry.things.insert(name.clone(), Thing { state, enrolled_ms: 0, validity_seconds: 60, revoked_ms: None, session_policy: None, certificates: vec![Certificate {
                        sha256: name.clone(), serial: "1".into(), issued_ms: 0, not_after_ms: identity["expires"].as_u64().unwrap(), retire_ms: None }] });
                }
                *guard = Registry::new(registry);
            }
            for request in case["requests"].as_array().unwrap() {
                let allowed = authority.thing_permits(request["thing"].as_str().unwrap(), request["operation"].as_str().unwrap(),
                    request["resource"].as_str().unwrap(), request["now"].as_u64().unwrap());
                // Through the certificate path too (fingerprint = name here).
                let by_certificate = authority.permits(request["thing"].as_str().unwrap(), request["operation"].as_str().unwrap(),
                    request["resource"].as_str().unwrap(), request["now"].as_u64().unwrap());
                assert_eq!(allowed, request["allowed"].as_bool().unwrap(), "{request}");
                assert_eq!(by_certificate, allowed, "{request}");
                count += 1;
            }
            std::fs::remove_dir_all(dir).unwrap();
        }
        assert_eq!(count, 16);
    }

    #[test]
    fn policies_and_selection_rules() {
        let policy = Policy::parse(GROUPS).unwrap();
        assert!(policy.permits("sensor-1", "mqtt:publish", "mqtt:topic:sensors/sensor-1/temp"));
        assert!(!policy.permits("sensor-1", "mqtt:publish", "mqtt:topic:sensors/sensor-2/temp"), "policy variable is the caller");
        assert!(!policy.permits("cam-1", "mqtt:publish", "mqtt:topic:lan/commands/x"), "no group selects it");
        assert!(policy.permits("sensor-1", "mqtt:connect", "mqtt:clientId:sensor-1"));
        assert!(!policy.permits("sensor-1", "mqtt:connect", "mqtt:clientId:sensor-2"), "only its own client ID");
        assert!(!policy.permits("sensor-1", "mqtt:subscribe", "mqtt:topicfilter:commands/#"), "MQTT wildcards are literal");
        let rule = |r: &str| parse_selection_rule(r).map(|s| s.iter().map(|s| (s.text.clone(), s.any_prefix, s.any_suffix)).collect::<Vec<_>>());
        assert_eq!(rule("thingName: a\\:b OR thingName: *x*").unwrap(), [("a:b".into(), false, false), ("x".into(), true, true)]);
        assert_eq!(rule("thingName: *").unwrap(), [("".into(), true, false)]);
        for bad in ["thingName: a*b", "attribute: x", "thingName:", "nothing"] {
            assert!(rule(bad).is_err(), "{bad}");
        }
        let selector = &rule("thingName: *").unwrap();
        assert!(selector.len() == 1 && parse_selection_rule("thingName: *").unwrap()[0].matches("anything"));
        for bad in [GROUPS.replace("2021-03-05", "2020-01-01"), GROUPS.replace("\"mqtt:connect\"]", "\"mqtt:retain\"]"),
                    GROUPS.replace("mqtt:clientId:sensor-*", "clientId:sensor-*"), GROUPS.replace("\"policyName\":\"sensorPolicy\"", "\"policyName\":\"missing\""),
                    GROUPS.replace("\"selectionRule\"", "\"extra\":1,\"selectionRule\"")] {
            assert!(Policy::parse(&bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn settings_are_bounded() {
        let dir = temp();
        let base = settings(&dir, GROUPS);
        assert!(base.validate().is_ok());
        for change in [
            |s: &mut Settings| s.broker = "192.168.1.2:1883".into(),
            |s: &mut Settings| s.mqtt_listen = s.https_listen.clone(),
            |s: &mut Settings| s.certificate_validity_seconds = 10,
            |s: &mut Settings| s.rotation_grace_seconds = 90_000,
            |s: &mut Settings| s.min_rotation_interval_seconds = 90_000,
            |s: &mut Settings| s.hostnames = vec!["bad host".into()],
            |s: &mut Settings| s.broker_password_file = Some("/x".into()),
            |s: &mut Settings| s.device_groups_file = "/nonexistent".into(),
        ] {
            let mut changed = base.clone();
            change(&mut changed);
            assert!(changed.validate().is_err(), "{changed:?}");
        }
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn enrollment_rotation_revocation_and_expiry() {
        let dir = temp();
        let now = 1_800_000_000_000;
        let authority = authority(&dir, GROUPS, now);
        let (_, request) = csr();
        let issued = authority.enroll("sensor-1", &request, Some(3600), now).unwrap();
        assert!(authority.enroll("sensor-1", &request, None, now).is_err(), "enrolled twice");
        assert!(authority.enroll("bad name", &request, None, now).is_err());
        assert!(authority.enroll("sensor-2", "not a csr", None, now).is_err());
        assert!(authority.enroll("sensor-2", &request, Some(10), now).is_err(), "validity below the minimum");
        // A tampered CSR fails its self-signature check.
        let der = pem::parse(&request).unwrap();
        let mut bytes = der.contents().to_vec();
        let at = bytes.len() / 2;
        bytes[at] ^= 1;
        assert!(authority.enroll("sensor-2", &pem::encode(&pem::Pem::new("CERTIFICATE REQUEST", bytes)), None, now).is_err());

        let first = issued.record.sha256.clone();
        assert_eq!(issued.record.not_after_ms, now + 3_600_000);
        assert_eq!(authority.authenticate(&first, now), Ok("sensor-1".into()));
        assert_eq!(authority.authenticate("ff", now), Err(Denial::Unknown));
        assert_eq!(authority.authenticate(&first, now + 3_600_000), Err(Denial::Expired));
        assert!(authority.permits(&first, "mqtt:publish", "mqtt:topic:sensors/sensor-1/t", now));

        // Rotation: the old certificate is usable for the grace period only.
        let (_, request) = csr();
        let rotated = authority.rotate(&first, &request, now + 1000).unwrap();
        let second = rotated.record.sha256.clone();
        assert_eq!(rotated.record.not_after_ms, now + 1000 + 3_600_000, "rotation keeps the enrolled lifetime");
        assert_ne!(first, second);
        assert_eq!(authority.authenticate(&first, now + 5000), Ok("sensor-1".into()));
        assert_eq!(authority.authenticate(&first, now + 11_000), Err(Denial::Superseded));
        assert!(authority.rotate(&first, &request, now + 2000).is_err(), "a superseded certificate cannot rotate again");
        assert!(authority.rotate(&second, &request, now + 1500).is_err(), "rotated again within the minimum interval");
        // Rotating again drops the earlier superseded certificate immediately.
        let third = authority.rotate(&second, &request, now + 3000).unwrap().record.sha256;
        assert_eq!(authority.authenticate(&first, now + 3001), Err(Denial::Unknown));
        assert_eq!(authority.authenticate(&second, now + 3001), Ok("sensor-1".into()));

        // Revocation denies every certificate of the thing, and persists.
        authority.revoke("sensor-1", now + 4000).unwrap();
        assert_eq!(authority.authenticate(&third, now + 4001), Err(Denial::Revoked));
        assert_eq!(authority.authenticate(&second, now + 4001), Err(Denial::Revoked));
        assert!(authority.rotate(&third, &request, now + 4002).is_err());
        let ca = authority.ca_sha256().to_owned();
        drop(authority);
        let reopened = super::tests::authority(&dir, GROUPS, now);
        assert_eq!(reopened.ca_sha256(), ca, "the CA persists");
        assert_eq!(reopened.authenticate(&third, now + 4001), Err(Denial::Revoked));
        assert!(reopened.enroll("sensor-1", &request, None, now).is_err(), "revoked things are not silently re-enrolled");
        reopened.remove("sensor-1").unwrap();
        assert_eq!(reopened.authenticate(&third, now), Err(Denial::Unknown));
        reopened.enroll("sensor-1", &request, None, now).unwrap();
        // Persistent broker sessions resume only under the policy they were opened with.
        assert!(!reopened.resume_session("sensor-1").unwrap(), "first session after enrollment");
        assert!(reopened.resume_session("sensor-1").unwrap());
        drop(reopened);
        let changed = GROUPS.replace("commands/+", "commands/x");
        let signer = Arc::new(Ed25519File(ed25519_dalek::SigningKey::from_bytes(&[7; 32])));
        let gateway = Gateway { chain: "c".into(), registry: "0x13".into(), device_object_id: "0x1".into(), address: signer.address() };
        let reopened = Authority::open(&dir.join("cd"), settings(&dir, &changed), gateway, signer, now).unwrap();
        assert!(!reopened.resume_session("sensor-1").unwrap(), "the policy changed: clean session");
        assert!(reopened.resume_session("sensor-1").unwrap());
        let mut reserving = reopened;
        reserving.reserve_client_id("sensor-bridge");
        assert!(reserving.enroll("sensor-bridge", &request, None, now).is_err(), "a reserved broker client ID");
        let reopened = reserving;

        // An unreadable registry or a half-present CA stops startup.
        drop(reopened);
        std::fs::write(dir.join("cd/registry.json"), b"{").unwrap();
        let signer = Arc::new(Ed25519File(ed25519_dalek::SigningKey::from_bytes(&[7; 32])));
        let gateway = Gateway { chain: "c".into(), registry: "0x13".into(), device_object_id: "0x1".into(), address: signer.address() };
        assert!(Authority::open(&dir.join("cd"), settings(&dir, GROUPS), gateway.clone(), signer.clone(), now).is_err());
        std::fs::remove_file(dir.join("cd/registry.json")).unwrap();
        std::fs::remove_file(dir.join("cd/ca.key")).unwrap();
        assert!(Authority::open(&dir.join("cd"), settings(&dir, GROUPS), gateway, signer, now).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn issued_certificates_are_client_only_and_chain_to_the_ca() {
        let dir = temp();
        let now = crate::telemetry::now_ms();
        let authority = authority(&dir, GROUPS, now);
        // A CSR asking to be a CA with a SAN gets neither.
        let key = KeyPair::generate().unwrap();
        let mut asked = CertificateParams::new(vec!["evil.example".to_string()]).unwrap();
        asked.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        let issued = authority.enroll("sensor-1", &asked.serialize_request(&key).unwrap().pem().unwrap(), None, now).unwrap();
        let der = pem::parse(&issued.certificate_pem).unwrap().contents().to_vec();
        let (_, certificate) = x509_parser::parse_x509_certificate(&der).unwrap();
        assert!(!certificate.is_ca());
        assert!(certificate.subject_alternative_name().unwrap().is_none());
        assert_eq!(certificate.subject().iter_common_name().next().unwrap().as_str().unwrap(), "sensor-1");
        let eku = certificate.extended_key_usage().unwrap().unwrap().value;
        assert!(eku.client_auth && !eku.server_auth);
        assert_eq!(certificate.public_key().subject_public_key.data.as_ref(), key.public_key_raw());
        // rustls accepts it as a client certificate under the CA.
        let mut roots = rustls::RootCertStore::empty();
        roots.add(authority.ca_der().clone()).unwrap();
        let verifier = rustls::server::WebPkiClientVerifier::builder_with_provider(Arc::new(roots),
            Arc::new(rustls::crypto::ring::default_provider())).build().unwrap();
        let time = rustls::pki_types::UnixTime::since_unix_epoch(std::time::Duration::from_secs(now / 1000));
        verifier.verify_client_cert(&rustls::pki_types::CertificateDer::from(der), &[], time).unwrap();
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn discovery_carries_the_device_signed_ca_binding() {
        let dir = temp();
        let now = 1_800_000_000_000;
        let authority = authority(&dir, GROUPS, now);
        let (_, request) = csr();
        let issued = authority.enroll("sensor-1", &request, Some(3000), now).unwrap();
        let envelope = authority.discovery(&issued.record.sha256, &["10.0.0.2:8883".into()], &["10.0.0.2:8443".into()], now).unwrap();
        let binding = &envelope["ca_binding"];
        let bytes = STANDARD.decode(binding["message"].as_str().unwrap()).unwrap();
        let signature = sui_sdk_types::UserSignature::from_bytes(&STANDARD.decode(binding["signature"].as_str().unwrap()).unwrap()).unwrap();
        use sui_crypto::SuiVerifier;
        sui_crypto::UserSignatureVerifier::new()
            .verify_personal_message(&sui_sdk_types::PersonalMessage(bytes.clone().into()), &signature).unwrap();
        let statement: CaBinding = serde_json::from_slice(&bytes).unwrap();
        assert_eq!((statement.ca_sha256.as_str(), statement.signer.as_str()), (authority.ca_sha256(), authority.gateway().address.as_str()));
        check_ca_binding(&bytes, "c", "0x13", &authority.gateway().address).unwrap();
        // What the signing service refuses.
        let address = authority.gateway().address.clone();
        let variant = |edit: &dyn Fn(&mut CaBinding)| { let mut b = statement.clone(); edit(&mut b); serde_json::to_vec(&b).unwrap() };
        for (label, bad) in [
            ("another chain", variant(&|b| b.chain_identifier = "x".into())),
            ("another registry", variant(&|b| b.registry = "0x14".into())),
            ("another signer", variant(&|b| b.signer = "0xa1".into())),
            ("another kind", variant(&|b| b.kind = "stream-export".into())),
            ("bad fingerprint", variant(&|b| b.ca_sha256 = "AB".repeat(32))),
            ("bad device", variant(&|b| b.device_id = "nope".into())),
        ] {
            assert!(check_ca_binding(&bad, "c", "0x13", &address).is_err(), "{label}");
        }
        let mut spaced = bytes.clone();
        spaced.insert(1, b' ');
        assert!(check_ca_binding(&spaced, "c", "0x13", &address).is_err(), "non-canonical");
        let document = &envelope["document"];
        assert_eq!(document["thing"], "sensor-1");
        assert_eq!(document["ca"]["sha256"], authority.ca_sha256());
        assert_eq!(document["mqtt"][0], "10.0.0.2:8883");
        assert_eq!(document["certificate"]["rotate_after_ms"], now + 2_000_000);
        assert_eq!(binding["signer"], authority.gateway().address);
        authority.revoke("sensor-1", now).unwrap();
        assert!(authority.discovery(&issued.record.sha256, &[], &[], now).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }
}
