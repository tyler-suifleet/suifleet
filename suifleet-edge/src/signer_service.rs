//! Device signing service (SF-053): the device key lives with a separate
//! service user, never with the daemon user that runs component lifecycle
//! commands. The daemon asks for signatures over a Unix socket.
//!
//! This is not a signing oracle. Every request is decoded and checked here,
//! independently of the daemon's own verify-before-sign:
//! - status reports: exactly one `deployment_manager::report_verified_status`
//!   or `report_verified_status_for_group` call on the manifest's package, with
//!   the manifest's registry, this device's cap, status Applied or Failed, paid
//!   by the device within a gas ceiling; only from the configured caller (in
//!   production, the main process of `suifleet.service`, by the PID and start
//!   time the unit records as root when it starts, see [`is_daemon`]);
//! - enrollment: exactly `enrollment::enroll` with the manifest's registry,
//!   sponsored by another address; only from root (provisioning);
//! - telemetry (SF-019): a personal message that is exactly one canonical,
//!   bounded telemetry snapshot of the manifest's chain and registry, signed
//!   by this key; only from the daemon's main process, like status reports.
//!   A personal-message signature is never valid as a transaction. Log
//!   batches, stream-export manifests and the client-device CA binding are
//!   checked the same way (canonical, this chain, registry and key).
//!
//! - gas draws (SF-083, only with `[budget]`): exactly one
//!   `fleet_budget::draw_gas` call on the manifest's package with the
//!   configured budget as an immutable input, a shared gas account, the
//!   manifest's registry, this device's cap and the Clock, for at most
//!   `max_draw_mist`, paid by the device within the gas ceiling; only from
//!   the daemon's main process. The contract binds the account to this
//!   device and budget, and bounds draws again, so a compromised daemon is
//!   still limited to its device's float and period allowance.
//!
//! Any process may ask for the device's public address. Everything else is
//! refused. Component processes run as the daemon user and may reach the
//! socket, but they are not the unit's main process.
// Only the client side exists elsewhere: it refuses to connect.
#![cfg_attr(not(target_os = "linux"), allow(dead_code, unused_imports))]
use crate::identity::TxSigner;
use crate::protocol::{deployment_manager as dm, enrollment, fleet_budget};
use crate::seal_client::{check_approval, Authority, Certificate, SessionPolicy, Sessions};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read, Write},
    os::unix::net::{UnixListener, UnixStream},
    path::{Path, PathBuf},
    str::FromStr,
    sync::Arc,
    time::Duration,
};
use sui_sdk_types::{Address, Argument, Command, Input, Transaction};

pub const MAX_ENROLL_BUDGET: u64 = 50_000_000;
/// Room for a base64 telemetry snapshot of [`crate::telemetry::MAX_MESSAGE_BYTES`].
const MAX_REQUEST_BYTES: u64 = 8 * 1024 * 1024;
const IO_TIMEOUT: Duration = Duration::from_secs(10);

fn default_socket() -> PathBuf {
    "/run/suifleet-signer/sign.sock".into()
}
fn default_record() -> PathBuf {
    MAIN_RECORD.into()
}
fn default_budget() -> u64 {
    50_000_000
}

/// `/etc/suifleet/signer.toml`, readable only by the signer service user.
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct ServiceConfig {
    /// The deployment manifest (package and registry the reports must use).
    pub manifest_path: PathBuf,
    pub device_cap_id: String,
    /// ed25519 key file; unused when `pkcs11` is set.
    #[serde(default)]
    pub keypair_path: PathBuf,
    /// A key in a PKCS#11 token instead (SF-021).
    #[serde(default)]
    pub pkcs11: Option<crate::config::SignerConfig>,
    /// Listening socket when not socket-activated by systemd.
    #[serde(default = "default_socket")]
    pub socket: PathBuf,
    /// Production: only this systemd unit's main process may request reports.
    #[serde(default)]
    pub caller_unit: Option<String>,
    /// Where that unit records its main process (`ExecStartPost=`).
    #[serde(default = "default_record")]
    pub caller_record: PathBuf,
    /// Development only: any process of this UID (weaker: components share it).
    #[serde(default)]
    pub caller_uid: Option<u32>,
    #[serde(default = "default_budget")]
    pub max_gas_budget: u64,
    /// Seal sessions for confidential delivery (SF-022). Absent: refused.
    #[serde(default)]
    pub seal: Option<SealSessions>,
    /// Gas draws from the fleet budget (SF-083). Absent: refused.
    #[serde(default)]
    pub budget: Option<BudgetDraws>,
    /// Explicit direct-chain writer binding; absent means refused.
    #[serde(default)]
    pub fleet_data: Option<crate::fleet_data::Config>,
}

/// `[budget]` in `signer.toml`: the one budget draws may name, and the most
/// one draw may ask for (the contract's own limits apply as well).
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct BudgetDraws {
    pub budget_id: String,
    pub max_draw_mist: u64,
}

/// `[seal]` in `signer.toml`: the service certifies session keys with the
/// device key and signs key requests; the session key never leaves it.
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct SealSessions {
    #[serde(default = "default_session_minutes")]
    pub session_ttl_minutes: u16,
    #[serde(default = "default_skew")]
    pub clock_skew_seconds: u64,
}
fn default_session_minutes() -> u16 {
    10
}
fn default_skew() -> u64 {
    60
}

impl ServiceConfig {
    pub fn load(path: &Path) -> anyhow::Result<(Self, Policy)> {
        use anyhow::Context;
        let config: Self = toml::from_str(&std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?)?;
        anyhow::ensure!(
            config.caller_unit.is_some() != config.caller_uid.is_some(),
            "set exactly one of caller_unit (production) or caller_uid (development)"
        );
        anyhow::ensure!(config.pkcs11.is_some() || !config.keypair_path.as_os_str().is_empty(), "set keypair_path or [pkcs11]");
        let manifest_path = if config.manifest_path.is_absolute() {
            config.manifest_path.clone()
        } else {
            path.parent().unwrap_or(Path::new(".")).join(&config.manifest_path)
        };
        let manifest: crate::config::DeploymentManifest =
            serde_json::from_str(&std::fs::read_to_string(&manifest_path).with_context(|| format!("reading {}", manifest_path.display()))?)?;
        manifest.validate()?;
        if let Some(data) = &config.fleet_data { data.validate()?; }
        let policy = Policy {
            fleet_data: config.fleet_data.clone(),
            device: Address::ZERO,
            package: Address::from_str(&manifest.call_package_id)?,
            namespace: Address::from_str(&manifest.type_origins["device_registry::DeviceRegistry"])?,
            registry: Address::from_str(&manifest.shared_objects.registry.id)?,
            cap: Address::from_str(&config.device_cap_id)?,
            max_budget: config.max_gas_budget,
            chain_identifier: manifest.chain_identifier.clone(),
            budget: config
                .budget
                .as_ref()
                .map(|budget| {
                    anyhow::ensure!(budget.max_draw_mist > 0, "budget.max_draw_mist must be positive");
                    Ok((Address::from_str(&budget.budget_id)?, budget.max_draw_mist))
                })
                .transpose()?,
        };
        Ok((config, policy))
    }

    /// Seal sessions certified by `key` for this fleet's package, if enabled.
    pub fn sessions(&self, key: Arc<dyn TxSigner>, policy: &Policy) -> anyhow::Result<Option<Sessions>> {
        self.seal
            .as_ref()
            .map(|seal| {
                Sessions::new(key, SessionPolicy {
                    package: policy.namespace,
                    ttl_min: seal.session_ttl_minutes,
                    clock_skew_ms: seal.clock_skew_seconds * 1000,
                })
            })
            .transpose()
    }

    fn key(&self) -> anyhow::Result<Arc<dyn TxSigner>> {
        match &self.pkcs11 {
            Some(pkcs11) => crate::identity::signer_from(pkcs11),
            None => {
                use anyhow::Context;
                let key = crate::identity::load(&self.keypair_path).with_context(|| format!("reading {}", self.keypair_path.display()))?;
                Ok(Arc::new(crate::identity::Ed25519File(key)))
            }
        }
    }
}

/// What a request may be signed for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Intent {
    Report,
    Enroll,
    Draw,
    FleetData,
}

#[derive(Debug, Clone)]
pub struct Policy {
    pub device: Address,
    pub package: Address,
    /// The package's original ID: the Seal key namespace.
    pub namespace: Address,
    pub registry: Address,
    pub cap: Address,
    pub max_budget: u64,
    /// The manifest's chain; telemetry snapshots must name it.
    pub chain_identifier: String,
    /// The fleet budget gas draws may name and the most one draw may take.
    pub budget: Option<(Address, u64)>,
    pub fleet_data: Option<crate::fleet_data::Config>,
}

fn input<'a>(inputs: &'a [Input], argument: &Argument) -> anyhow::Result<&'a Input> {
    let Argument::Input(index) = argument else {
        anyhow::bail!("call argument is not a transaction input");
    };
    inputs.get(*index as usize).ok_or_else(|| anyhow::anyhow!("call refers to a missing input"))
}

fn shared(input: &Input) -> anyhow::Result<Address> {
    match input {
        Input::Shared(shared) => Ok(shared.object_id()),
        _ => anyhow::bail!("expected a shared object argument"),
    }
}

/// Decode and check a transaction against the policy; returns its intent.
pub fn check(policy: &Policy, bytes: &[u8]) -> anyhow::Result<Intent> {
    let tx: Transaction = bcs::from_bytes(bytes).map_err(|e| anyhow::anyhow!("not a Sui transaction: {e}"))?;
    anyhow::ensure!(tx.sender == policy.device, "transaction sender is not this device");
    let sui_sdk_types::TransactionKind::ProgrammableTransaction(ptb) = &tx.kind else {
        anyhow::bail!("not a programmable transaction");
    };
    let [Command::MoveCall(call)] = ptb.commands.as_slice() else {
        anyhow::bail!("transaction must contain exactly one call");
    };
    if call.module.as_str() == "fleet_storage" {
        return check_storage_transaction(policy, &tx, ptb, call);
    }
    if call.module.as_str() == "fleet_data" {
        return check_fleet_transaction(policy, &tx, ptb, call);
    }
    anyhow::ensure!(call.package == policy.package && call.type_arguments.is_empty(), "call is not on the fleet package");
    anyhow::ensure!(ptb.inputs.len() == call.arguments.len(), "transaction has extra inputs");
    let args: Vec<&Input> = call.arguments.iter().map(|a| input(&ptb.inputs, a)).collect::<anyhow::Result<_>>()?;
    match (call.module.as_str(), call.function.as_str()) {
        (dm::MODULE, function @ (dm::REPORT_VERIFIED_STATUS | dm::REPORT_VERIFIED_STATUS_FOR_GROUP)) => {
            anyhow::ensure!(tx.gas_payment.owner == policy.device, "a status report must be paid by the device");
            anyhow::ensure!(tx.gas_payment.budget <= policy.max_budget, "gas budget {} exceeds {}", tx.gas_payment.budget, policy.max_budget);
            let group = function == dm::REPORT_VERIFIED_STATUS_FOR_GROUP;
            anyhow::ensure!(args.len() == if group { 5 } else { 4 }, "unexpected argument count");
            shared(args[0])?;
            anyhow::ensure!(shared(args[1])? == policy.registry, "not this fleet's registry");
            anyhow::ensure!(
                matches!(args[2], Input::ImmutableOrOwned(reference) if *reference.object_id() == policy.cap),
                "not this device's cap"
            );
            if group {
                shared(args[3])?;
            }
            anyhow::ensure!(
                matches!(args[args.len() - 1], Input::Pure(value) if value.as_slice() == [1] || value.as_slice() == [2]),
                "status must be Applied or Failed"
            );
            Ok(Intent::Report)
        }
        (dm::MODULE, dm::REPORT_ROLLOUT_HEALTH) => {
            anyhow::ensure!(tx.gas_payment.owner == policy.device && tx.gas_payment.budget <= policy.max_budget, "health gas owner/budget");
            anyhow::ensure!(args.len() == 5, "unexpected health arguments");
            anyhow::ensure!(matches!(args[0], Input::Shared(s) if s.mutability().is_mutable()), "health deployment must be mutable");
            anyhow::ensure!(matches!(args[1], Input::Shared(s) if s.object_id() == policy.registry && !s.mutability().is_mutable()), "foreign/mutable health registry");
            anyhow::ensure!(matches!(args[2], Input::ImmutableOrOwned(r) if *r.object_id() == policy.cap), "foreign health cap");
            anyhow::ensure!(matches!(args[3], Input::Pure(v) if v.as_slice() == [0] || v.as_slice() == [1]), "health must be bool");
            anyhow::ensure!(matches!(args[4], Input::Shared(s) if s.object_id() == Address::from_str("0x6")? && !s.mutability().is_mutable()), "health clock");
            Ok(Intent::Report)
        }
        (fleet_budget::MODULE, fleet_budget::DRAW_GAS) => {
            let Some((budget, max_draw)) = policy.budget else {
                anyhow::bail!("gas draws are not enabled in signer.toml");
            };
            anyhow::ensure!(tx.gas_payment.owner == policy.device, "a gas draw must be paid by the device");
            anyhow::ensure!(tx.gas_payment.budget <= policy.max_budget, "gas budget {} exceeds {}", tx.gas_payment.budget, policy.max_budget);
            anyhow::ensure!(args.len() == 6, "unexpected argument count");
            anyhow::ensure!(shared(args[0])? == budget, "not this fleet's budget");
            // Draws only read the fleet budget; a mutable budget input would
            // contend with every other device and the owner.
            anyhow::ensure!(
                matches!(args[0], Input::Shared(input) if !input.mutability().is_mutable()),
                "a gas draw must not take the fleet budget mutably"
            );
            // The device's GasAccount; the contract binds it to the cap and budget.
            shared(args[1])?;
            anyhow::ensure!(shared(args[2])? == policy.registry, "not this fleet's registry");
            anyhow::ensure!(
                matches!(args[3], Input::ImmutableOrOwned(reference) if *reference.object_id() == policy.cap),
                "not this device's cap"
            );
            let Input::Pure(amount) = args[4] else {
                anyhow::bail!("draw amount is not a pure value");
            };
            let amount: [u8; 8] = amount.as_slice().try_into().map_err(|_| anyhow::anyhow!("draw amount is not a u64"))?;
            let amount = u64::from_le_bytes(amount);
            anyhow::ensure!(amount > 0 && amount <= max_draw, "draw of {amount} MIST exceeds {max_draw}");
            anyhow::ensure!(shared(args[5])? == Address::from_str("0x6")?, "not the Clock");
            Ok(Intent::Draw)
        }
        (enrollment::MODULE, enrollment::ENROLL) => {
            anyhow::ensure!(tx.gas_payment.owner != policy.device, "enrollment must be sponsored by the claim key");
            anyhow::ensure!(tx.gas_payment.budget <= MAX_ENROLL_BUDGET, "gas budget exceeds the enrollment ceiling");
            anyhow::ensure!(args.len() == 5, "unexpected argument count");
            shared(args[0])?;
            anyhow::ensure!(shared(args[1])? == policy.registry, "not this fleet's registry");
            Ok(Intent::Enroll)
        }
        (module, function) => anyhow::bail!("{module}::{function} is not a permitted signing intent"),
    }
}

/// Fleet-data intents have a separate configured package and fixed object set.
/// Keep the budget/account/fleet/registry read-only; only the indexed writer mutates.
fn check_fleet_transaction(policy: &Policy, tx: &Transaction,
    ptb: &sui_sdk_types::ProgrammableTransaction, call: &sui_sdk_types::MoveCall) -> anyhow::Result<Intent> {
    let cfg = policy.fleet_data.as_ref().ok_or_else(|| anyhow::anyhow!("fleet data signing is disabled"))?;
    anyhow::ensure!(call.package == Address::from_str(&cfg.package_id)? && call.type_arguments.is_empty(), "wrong fleet-data package");
    anyhow::ensure!(tx.gas_payment.owner == policy.device && tx.gas_payment.budget <= policy.max_budget, "wrong gas owner/budget");
    if call.function.as_str() == "register_stream_blob" {
        let storage = cfg.archives.as_ref().filter(|s| s.stream_retention)
            .ok_or_else(|| anyhow::anyhow!("stream retention signing disabled"))?;
        anyhow::ensure!(call.arguments.len() == 13 && ptb.inputs.len() <= 13, "unexpected stream inputs");
        for index in 0..ptb.inputs.len() {
            anyhow::ensure!(call.arguments.iter().any(|a| matches!(a, Argument::Input(i) if *i as usize == index)), "unused stream input");
        }
        let args: Vec<_> = call.arguments.iter().map(|a| input(&ptb.inputs, a)).collect::<anyhow::Result<_>>()?;
        for (i, id, mutable) in [(0, &cfg.fleet_id, false), (1, &cfg.writer_id, false),
            (2, &cfg.budget_id, false), (3, &cfg.account_id, false), (4, &policy.registry.to_string(), false),
            (6, &cfg.storage_id, true), (7, &storage.system_id, true), (12, &"0x6".to_string(), false)] {
            anyhow::ensure!(matches!(args[i], Input::Shared(s) if s.object_id() == Address::from_str(id)? && s.mutability().is_mutable() == mutable), "wrong stream object/mutability at {i}");
        }
        anyhow::ensure!(matches!(args[5], Input::ImmutableOrOwned(r) if *r.object_id() == policy.cap), "not this device cap");
        let pure = |i: usize| -> anyhow::Result<&[u8]> { match args[i] { Input::Pure(b) => Ok(b), _ => anyhow::bail!("not pure") } };
        let kind: u8 = bcs::from_bytes(pure(8)?)?;
        let size: u64 = bcs::from_bytes(pure(11)?)?;
        anyhow::ensure!(kind <= 2 && pure(9)?.len() == 32 && pure(10)?.len() == 32
            && size > 0 && size <= crate::fleet_data::MAX_ARCHIVE as u64, "stream bounds");
        return Ok(Intent::FleetData);
    }
    let compact = call.function.as_str() == "anchor_compact";
    let opaque = call.function.as_str() == "anchor_items";
    let anchor = compact || opaque || call.function.as_str() == "anchor";
    anyhow::ensure!(anchor || call.function.as_str() == "report_health", "unexpected fleet-data intent");
    let length = if compact || opaque { 13 } else if anchor { 12 } else { 10 };
    anyhow::ensure!(call.arguments.len() == length && ptb.inputs.len() <= length, "unexpected fleet-data inputs");
    // The SDK deduplicates equal pure BCS inputs (empty vector and kind=0
    // are both [0]). Every argument is still typed/bound below, and no unused
    // input may be smuggled into the transaction.
    for index in 0..ptb.inputs.len() {
        anyhow::ensure!(call.arguments.iter().any(|a| matches!(a, Argument::Input(i) if *i as usize == index)), "unused fleet-data input");
    }
    let args: Vec<_> = call.arguments.iter().map(|a| input(&ptb.inputs, a)).collect::<anyhow::Result<_>>()?;
    for (index, expected, mutable) in [(0, Address::from_str(&cfg.fleet_id)?, false),
        (1, Address::from_str(&cfg.writer_id)?, true), (2, Address::from_str(&cfg.budget_id)?, false),
        (3, Address::from_str(&cfg.account_id)?, false), (4, policy.registry, false),
        (length-1, Address::from_str("0x6")?, false)] {
        anyhow::ensure!(matches!(args[index], Input::Shared(s) if s.object_id() == expected && s.mutability().is_mutable() == mutable), "wrong fleet-data object/mutability at {index}");
    }
    anyhow::ensure!(matches!(args[5], Input::ImmutableOrOwned(r) if *r.object_id() == policy.cap), "not this device cap");
    let pure = |index: usize| -> anyhow::Result<&[u8]> { match args[index] { Input::Pure(b) => Ok(b), _ => anyhow::bail!("not pure") } };
    if anchor {
        anyhow::ensure!(cfg.archives.is_some(), "archive signing disabled");
        anyhow::ensure!(matches!(args[6], Input::Shared(s) if s.object_id() == Address::from_str(&cfg.storage_id).unwrap() && !s.mutability().is_mutable()), "foreign archive storage");
        let _: Address = bcs::from_bytes(pure(7)?)?;
        let seq: u64 = bcs::from_bytes(pure(8)?)?; let kind: u8 = bcs::from_bytes(pure(9)?)?;
        let hash: Vec<u8> = bcs::from_bytes(pure(10)?)?;
        anyhow::ensure!(seq > 0 && seq <= 9_007_199_254_740_991 && kind <= 2 && hash.len() == 32, "archive anchor bounds");
        if opaque {
            anyhow::ensure!(cfg.archives.as_ref().is_some_and(|a|a.opaque), "opaque signing disabled");
            anyhow::ensure!(pure(11)?.len() <= 8192, "opaque index transaction byte limit");
            let encoded: Vec<Vec<u8>>=bcs::from_bytes(pure(11)?)?;
            anyhow::ensure!((1..=16).contains(&encoded.len()), "opaque index item count");
            for bytes in encoded { let item: crate::opaque::Index=bcs::from_bytes(&bytes)?;item.validate()?; }
        }
        if compact {
            anyhow::ensure!(cfg.archives.as_ref().is_some_and(|a| a.compact_history), "compact archive signing disabled");
            let previous: Vec<u8> = bcs::from_bytes(pure(11)?)?;
            anyhow::ensure!(matches!(previous.len(),0 | 32), "invalid compact history predecessor");
        }
        return Ok(Intent::FleetData);
    }
    let seq: u64 = bcs::from_bytes(pure(6)?)?;
    let message: Vec<u8> = bcs::from_bytes(pure(7)?)?;
    let signature: Vec<u8> = bcs::from_bytes(pure(8)?)?;
    anyhow::ensure!(message.len() <= crate::fleet_data::MAX_HEALTH && [97, 98].contains(&signature.len()), "health payload bounds");
    check_telemetry(policy, &message)?;
    let snapshot: crate::telemetry::Snapshot = serde_json::from_slice(&message)?;
    anyhow::ensure!(seq > 0 && snapshot.seq == seq, "health sequence mismatch");
    Ok(Intent::FleetData)
}

fn check_storage_transaction(policy: &Policy, tx: &Transaction,
    ptb: &sui_sdk_types::ProgrammableTransaction, call: &sui_sdk_types::MoveCall) -> anyhow::Result<Intent> {
    let data = policy.fleet_data.as_ref().ok_or_else(|| anyhow::anyhow!("fleet data disabled"))?;
    let storage = data.archives.as_ref().ok_or_else(|| anyhow::anyhow!("archives disabled"))?;
    anyhow::ensure!(call.package == Address::from_str(&storage.storage_package)? && call.type_arguments.is_empty(), "foreign storage package");
    anyhow::ensure!(tx.gas_payment.owner == policy.device && tx.gas_payment.budget <= policy.max_budget, "wrong gas owner/budget");
    let register = call.function.as_str() == "register_blob";
    anyhow::ensure!(register || call.function.as_str() == "certify_blob", "unexpected storage intent");
    let count = if register { 11 } else { 6 };
    anyhow::ensure!(call.arguments.len() == count && ptb.inputs.len() == count, "unexpected storage inputs");
    let args: Vec<_> = call.arguments.iter().map(|a| input(&ptb.inputs,a)).collect::<anyhow::Result<_>>()?;
    let mut objects = vec![(0, Address::from_str(&data.storage_id)?,true), (if register {5} else {1},Address::from_str(&storage.system_id)?,register)];
    if register { objects.extend([(1,Address::from_str(&data.budget_id)?,false),(2,Address::from_str(&data.account_id)?,false),(3,policy.registry,false),(10,Address::from_str("0x6")?,false)]); }
    for (index,id,mutable) in objects { anyhow::ensure!(matches!(args[index],Input::Shared(s) if s.object_id()==id && s.mutability().is_mutable()==mutable), "wrong storage object/mutability at {index}"); }
    let pure = |index: usize| -> anyhow::Result<&[u8]> { match args[index] { Input::Pure(b) => Ok(b),_ => anyhow::bail!("not pure") } };
    if register {
        anyhow::ensure!(matches!(args[4],Input::ImmutableOrOwned(r) if *r.object_id()==policy.cap),"not this device cap");
        anyhow::ensure!(pure(6)?.len()==32 && pure(7)?.len()==32,"invalid blob/root hash");
        let size: u64 = bcs::from_bytes(pure(8)?)?; let encoding: u8 = bcs::from_bytes(pure(9)?)?;
        anyhow::ensure!(size>0 && size<=crate::fleet_data::MAX_ARCHIVE as u64 && encoding==1,"archive encoding/size bounds");
    } else {
        let _: Address = bcs::from_bytes(pure(2)?)?;
        let sig: Vec<u8> = bcs::from_bytes(pure(3)?)?; let bitmap: Vec<u8> = bcs::from_bytes(pure(4)?)?; let message: Vec<u8> = bcs::from_bytes(pure(5)?)?;
        anyhow::ensure!(!sig.is_empty() && sig.len()<=256 && !bitmap.is_empty() && bitmap.len()<=512 && !message.is_empty() && message.len()<=4096,"certificate bounds");
    }
    Ok(Intent::FleetData)
}

/// Check a telemetry personal message: exactly the canonical JSON of one
/// bounded snapshot, of this chain and registry, naming this key as signer.
/// Its content is otherwise the daemon's own report, like a status report's.
pub fn check_telemetry(policy: &Policy, message: &[u8]) -> anyhow::Result<()> {
    use crate::telemetry::{Snapshot, MAX_MESSAGE_BYTES};
    anyhow::ensure!(message.len() <= MAX_MESSAGE_BYTES, "telemetry snapshot exceeds {MAX_MESSAGE_BYTES} bytes");
    let snapshot: Snapshot = serde_json::from_slice(message).map_err(|e| anyhow::anyhow!("not a telemetry snapshot: {e}"))?;
    anyhow::ensure!(serde_json::to_vec(&snapshot)? == message, "telemetry snapshot is not in canonical form");
    // Schema 2 exactly when a services section (SF-085) is present.
    anyhow::ensure!(snapshot.schema == Snapshot::schema_for(snapshot.services.is_some()), "unsupported telemetry schema {}", snapshot.schema);
    // Service errors leave the device only as categories, never as texts.
    anyhow::ensure!(snapshot.services.as_ref().is_none_or(|s| s.errors_are_categories()), "telemetry service error is not a category");
    anyhow::ensure!(snapshot.chain_identifier == policy.chain_identifier, "telemetry snapshot is for another chain");
    anyhow::ensure!(Address::from_str(&snapshot.registry).ok() == Some(policy.registry), "telemetry snapshot is not for this fleet's registry");
    anyhow::ensure!(Address::from_str(&snapshot.signer).ok() == Some(policy.device), "telemetry snapshot names another signer");
    Ok(())
}

/// Check a log-batch personal message (SF-020): exactly the canonical JSON of
/// one bounded schema-1 batch, of this chain and registry, naming this key.
/// Its lines are the daemon's own redacted log text, like telemetry content.
pub fn check_logs(policy: &Policy, message: &[u8]) -> anyhow::Result<()> {
    use crate::logs::{validate, Batch, MAX_MESSAGE_BYTES};
    anyhow::ensure!(message.len() <= MAX_MESSAGE_BYTES, "log batch exceeds {MAX_MESSAGE_BYTES} bytes");
    let batch: Batch = serde_json::from_slice(message).map_err(|e| anyhow::anyhow!("not a log batch: {e}"))?;
    anyhow::ensure!(serde_json::to_vec(&batch)? == message, "log batch is not in canonical form");
    validate(&batch)?;
    anyhow::ensure!(batch.chain_identifier == policy.chain_identifier, "log batch is for another chain");
    anyhow::ensure!(Address::from_str(&batch.registry).ok() == Some(policy.registry), "log batch is not for this fleet's registry");
    anyhow::ensure!(Address::from_str(&batch.signer).ok() == Some(policy.device), "log batch names another signer");
    Ok(())
}

/// A personal message the service signs: a telemetry snapshot, a log batch,
/// a stream export manifest, a shadow sync manifest or a client-device CA
/// binding (SF-035).
pub fn check_personal(policy: &Policy, message: &[u8]) -> anyhow::Result<()> {
    let kind = serde_json::from_slice::<serde_json::Value>(message).ok().and_then(|v| v.get("kind").and_then(|k| k.as_str()).map(str::to_owned));
    match kind.as_deref() {
        Some("fleet-opaque") => {
            let cfg=policy.fleet_data.as_ref().ok_or_else(||anyhow::anyhow!("fleet data disabled"))?;
            anyhow::ensure!(cfg.archives.as_ref().is_some_and(|a|a.opaque) && message.len() <= 65536, "opaque signing disabled/oversize");
            let opaque: crate::opaque::Message=serde_json::from_slice(message)?;opaque.validate()?;
            anyhow::ensure!(serde_json::to_vec(&opaque)? == message && opaque.chain_identifier==policy.chain_identifier
                && Address::from_str(&opaque.registry)?==policy.registry && Address::from_str(&opaque.signer)?==policy.device
                && Address::from_str(&opaque.fleet_id)?==Address::from_str(&cfg.fleet_id)?, "foreign opaque binding");
            Ok(())
        }
        Some("fleet-data") => {
            let cfg = policy.fleet_data.as_ref().ok_or_else(|| anyhow::anyhow!("fleet data signing is disabled"))?;
            anyhow::ensure!(cfg.archives.is_some(), "archive signing is disabled");
            anyhow::ensure!(message.len() <= crate::fleet_data::MAX_ARCHIVE, "archive too large");
            let archive: crate::fleet_data::Archive = serde_json::from_slice(message).map_err(|_| anyhow::anyhow!("invalid archive JSON"))?;
            archive.validate()?;
            anyhow::ensure!(archive.previous_head.is_none() || cfg.archives.as_ref().is_some_and(|a| a.compact_history), "compact archive signing disabled");
            anyhow::ensure!(archive.schema != 4 || cfg.archives.as_ref().is_some_and(|a| a.quilt), "Quilt archive signing disabled");
            anyhow::ensure!(serde_json::to_vec(&archive)? == message, "noncanonical archive");
            anyhow::ensure!(archive.chain_identifier == policy.chain_identifier
                && Address::from_str(&archive.registry)? == policy.registry
                && Address::from_str(&archive.signer)? == policy.device
                && Address::from_str(&archive.fleet_id)? == Address::from_str(&cfg.fleet_id)?, "foreign archive binding");
            Ok(())
        }
        Some(crate::logs::KIND) => check_logs(policy, message),
        Some(crate::streams::MANIFEST_KIND) => crate::streams::check_manifest(message, &policy.chain_identifier, &policy.registry.to_string(), &policy.device.to_string()),
        Some(crate::shadows::MANIFEST_KIND) => crate::shadows::check_manifest(message, &policy.chain_identifier, &policy.registry.to_string(), &policy.device.to_string()),
        Some(crate::clientdevices::CA_BINDING_KIND) => crate::clientdevices::check_ca_binding(message, &policy.chain_identifier,
            &policy.registry.to_string(), &policy.device.to_string()),
        _ => check_telemetry(policy, message),
    }
}

/// Who may ask for what.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Callers {
    /// The main process of this systemd unit, as recorded by the unit itself.
    Unit { unit: String, record: PathBuf },
    Uid(u32),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Peer {
    pub pid: u32,
    pub uid: u32,
}

/// A process's start time (clock ticks after boot) and cgroup v2 path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Process {
    pub start: u64,
    pub cgroup: String,
}

/// The unit's main process: `PID START`, written by the unit's root
/// `ExecStartPost=` from systemd's `$MAINPID` (see [`record_main_process`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MainProcess {
    pub pid: u32,
    pub start: u64,
}

impl MainProcess {
    fn parse(text: &str) -> Option<Self> {
        let (pid, start) = text.trim().split_once(' ')?;
        Some(Self { pid: pid.parse().ok()?, start: start.parse().ok()? })
    }
}

/// Default record of `suifleet.service`'s main process.
pub const MAIN_RECORD: &str = "/run/suifleet-signer/daemon.main";

/// Whether `peer` is the daemon allowed to request status reports. For a
/// unit: exactly the main process systemd started for it, identified by PID
/// and start time. Parent PID and cgroup are not enough: a component that
/// kills the daemon is reparented to PID 1 inside the same cgroup until
/// systemd removes it. Neither can a component claim the role: only the
/// main process may notify systemd (NotifyAccess=main), the record is
/// written by root, and a reused PID has a different start time.
pub fn is_daemon(callers: &Callers, peer: Peer, main: Option<MainProcess>, process: impl Fn(u32) -> Option<Process>) -> bool {
    match callers {
        Callers::Unit { unit, .. } => main.is_some_and(|main| {
            main.pid == peer.pid
                && process(peer.pid).is_some_and(|p| p.start == main.start && p.cgroup.rsplit('/').next() == Some(unit.as_str()))
        }),
        Callers::Uid(uid) => peer.uid == *uid,
    }
}

fn proc_start(stat: &str) -> Option<u64> {
    // Field 22; fields after the parenthesized command name start at 3.
    stat.rsplit_once(')')?.1.split_whitespace().nth(19)?.parse().ok()
}

#[cfg(target_os = "linux")]
fn proc_process(pid: u32) -> Option<Process> {
    let start = proc_start(&std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?)?;
    let cgroup = std::fs::read_to_string(format!("/proc/{pid}/cgroup")).ok()?;
    let cgroup = cgroup.lines().find_map(|line| line.strip_prefix("0::"))?.trim().to_owned();
    Some(Process { start, cgroup })
}

/// `suifleet --record-main-process PATH`, run by the daemon unit as root
/// (`ExecStartPost=+`): records systemd's `$MAINPID` and its start time for
/// the signing service. The directory is root's, so the daemon user cannot
/// write the record. Only a main process running this same read-only base
/// binary is recorded: the daemon user (and so any component) can write the
/// launcher's daemon-update state, so the unit's main process could otherwise
/// be an executable a component chose. Any other main process (a daemon
/// update, which the service signer does not allow) leaves no record, so the
/// signing service trusts nobody; the unit itself keeps running.
#[cfg(target_os = "linux")]
pub fn record_main_process(path: &Path) -> anyhow::Result<()> {
    use anyhow::Context;
    let pid: u32 = std::env::var("MAINPID").context("MAINPID is not set (run as the unit's ExecStartPost=)")?.parse()?;
    if !record(path, pid, Path::new("/proc/self/exe"))? {
        eprintln!("suifleet: main process {pid} is not running the base binary; no main process recorded for the signing service");
    }
    Ok(())
}

/// Whether two paths name the same file (device and inode), following links.
#[cfg(target_os = "linux")]
fn same_file(a: &Path, b: &Path) -> std::io::Result<bool> {
    use std::os::unix::fs::MetadataExt;
    let (a, b) = (std::fs::metadata(a)?, std::fs::metadata(b)?);
    Ok(a.dev() == b.dev() && a.ino() == b.ino())
}

/// Records `pid` if it runs `base`; otherwise removes any earlier record and
/// returns false.
#[cfg(target_os = "linux")]
fn record(path: &Path, pid: u32, base: &Path) -> anyhow::Result<bool> {
    use anyhow::Context;
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
    let start_of = |pid: u32| -> anyhow::Result<u64> {
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).with_context(|| format!("main process {pid}"))?;
        proc_start(&stat).ok_or_else(|| anyhow::anyhow!("unreadable start time of {pid}"))
    };
    let start = start_of(pid)?;
    if !same_file(Path::new(&format!("/proc/{pid}/exe")), base).with_context(|| format!("executable of main process {pid}"))? {
        match std::fs::remove_file(path) {
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => return Err(error.into()),
            _ => return Ok(false),
        }
    }
    // The executable checked belongs to this process, not a later one reusing its PID.
    anyhow::ensure!(start_of(pid)? == start, "main process {pid} exited while being recorded");
    let directory = path.parent().unwrap_or(Path::new("/"));
    if !directory.is_dir() {
        std::fs::DirBuilder::new().recursive(true).mode(0o755).create(directory)?;
        std::fs::set_permissions(directory, std::fs::Permissions::from_mode(0o755))?;
    }
    let staging = directory.join(".daemon.main.tmp");
    let _ = std::fs::remove_file(&staging);
    let mut file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o644).open(&staging)?;
    // The unit's UMask=0077 applies here too; the signer user must read it.
    file.set_permissions(std::fs::Permissions::from_mode(0o644))?;
    writeln!(file, "{pid} {start}")?;
    file.sync_all()?;
    std::fs::rename(&staging, path)?;
    Ok(true)
}

pub const SIGNER_USER: &str = "suifleet-signer";
pub const DAEMON_USER: &str = "suifleet";

/// `suifleet --key-holder CONFIG_PATH`, for root provisioning: the user that
/// must own the device key file. The signing service's user only when the
/// daemon's configuration uses `[device.signer] kind = "service"` (or, with no
/// daemon configuration yet, when `signer.toml` is installed beside it);
/// otherwise the daemon's user, whose unchanged configuration still loads the
/// key itself. So a package update never strands a legacy configuration.
pub fn key_holder(config: &Path) -> anyhow::Result<&'static str> {
    use anyhow::Context;
    let text = match std::fs::read_to_string(config) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let signer = config.with_file_name("signer.toml");
            return Ok(if signer.exists() { SIGNER_USER } else { DAEMON_USER });
        }
        Err(error) => return Err(error).with_context(|| format!("reading {}", config.display())),
    };
    let value: toml::Value = toml::from_str(&text).with_context(|| format!("parsing {}", config.display()))?;
    let kind = value.get("device").and_then(|device| device.get("signer")).and_then(|signer| signer.get("kind"));
    Ok(if kind.and_then(toml::Value::as_str) == Some("service") { SIGNER_USER } else { DAEMON_USER })
}

/// The peer's process as a pidfd (Linux 6.5+), so a request is never
/// attributed to a later process reusing the peer's PID.
#[cfg(target_os = "linux")]
fn peer_pidfd(stream: &UnixStream) -> Option<std::os::fd::OwnedFd> {
    use std::os::fd::{AsRawFd, FromRawFd};
    let mut fd: libc::c_int = -1;
    let mut length = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    // SAFETY: SO_PEERPIDFD writes one descriptor into the int we own.
    let result = unsafe {
        libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERPIDFD, &mut fd as *mut _ as *mut libc::c_void, &mut length)
    };
    // SAFETY: on success the kernel returned a new descriptor we now own.
    (result == 0 && fd >= 0).then(|| unsafe { std::os::fd::OwnedFd::from_raw_fd(fd) })
}

/// The PID a pidfd refers to, or None once that process has exited.
#[cfg(target_os = "linux")]
fn pidfd_pid(pidfd: &std::os::fd::OwnedFd) -> Option<u32> {
    use std::os::fd::AsRawFd;
    let info = std::fs::read_to_string(format!("/proc/self/fdinfo/{}", pidfd.as_raw_fd())).ok()?;
    info.lines().find_map(|line| line.strip_prefix("Pid:"))?.trim().parse().ok()
}

#[cfg(target_os = "linux")]
fn peer(stream: &UnixStream) -> std::io::Result<Peer> {
    use std::os::fd::AsRawFd;
    let mut credentials = libc::ucred { pid: 0, uid: 0, gid: 0 };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: SO_PEERCRED writes one ucred into the buffer we own.
    let result = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut credentials as *mut _ as *mut libc::c_void,
            &mut length,
        )
    };
    if result != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(Peer { pid: credentials.pid as u32, uid: credentials.uid })
}

/// A Seal key request: an approval transaction on this fleet's package and
/// a well-formed ElGamal response key, signed by the current session.
fn seal_request(request: &Value, policy: &Policy, sessions: &Sessions) -> anyhow::Result<Value> {
    let field = |name: &str| STANDARD.decode(request[name].as_str().unwrap_or(""));
    let ptb = field("ptb")?;
    let decoded: sui_sdk_types::ProgrammableTransaction =
        bcs::from_bytes(&ptb).map_err(|e| anyhow::anyhow!("not a programmable transaction: {e}"))?;
    check_approval(&decoded, &policy.package)?;
    let enc_key = field("enc_key")?;
    let enc_verification_key = field("enc_verification_key")?;
    anyhow::ensure!(crate::seal::elgamal_pair_valid(&enc_key, &enc_verification_key), "invalid response encryption key");
    let (certificate, signature) = sessions.authorize(&ptb, &enc_key, &enc_verification_key)?;
    Ok(json!({ "certificate": certificate.to_json(), "signature": STANDARD.encode(signature) }))
}

/// One request: authorize the caller, then the transaction, then sign.
pub fn handle(
    request: &Value,
    peer: Peer,
    key: &dyn TxSigner,
    policy: &Policy,
    sessions: Option<&Sessions>,
    daemon: impl FnOnce() -> bool,
) -> Value {
    let result = (|| -> anyhow::Result<Value> {
        match request["op"].as_str() {
            Some("address") => Ok(json!({ "address": key.address() })),
            Some("seal") => {
                let sessions = sessions.ok_or_else(|| anyhow::anyhow!("Seal sessions are not enabled in signer.toml"))?;
                anyhow::ensure!(daemon(), "caller pid {} uid {} may not use Seal sessions (only the daemon's main process)", peer.pid, peer.uid);
                seal_request(request, policy, sessions)
            }
            Some("sign") => {
                let daemon = daemon();
                let root = peer.uid == 0;
                anyhow::ensure!(
                    daemon || root,
                    "caller pid {} uid {} may not request signatures (only the daemon's main process, or root for enrollment)",
                    peer.pid,
                    peer.uid
                );
                let bytes = STANDARD.decode(request["tx"].as_str().unwrap_or(""))?;
                match check(policy, &bytes)? {
                    Intent::Report => anyhow::ensure!(daemon, "status reports are signed only for the daemon's main process"),
                    Intent::FleetData => anyhow::ensure!(daemon, "fleet data is signed only for the daemon main process"),
                    Intent::Draw => anyhow::ensure!(daemon, "gas draws are signed only for the daemon's main process"),
                    Intent::Enroll => anyhow::ensure!(root, "enrollment is signed only for root at provisioning"),
                }
                Ok(json!({ "signature": STANDARD.encode(key.sign(&bytes)?) }))
            }
            Some("sign_telemetry") => {
                anyhow::ensure!(
                    daemon(),
                    "caller pid {} uid {} may not request telemetry signatures (only the daemon's main process)",
                    peer.pid,
                    peer.uid
                );
                let message = STANDARD.decode(request["message"].as_str().unwrap_or(""))?;
                check_personal(policy, &message)?;
                Ok(json!({ "signature": STANDARD.encode(key.sign_personal_message(&message)?) }))
            }
            _ => anyhow::bail!("unknown request"),
        }
    })();
    result.unwrap_or_else(|error| json!({ "error": format!("{error:#}") }))
}

/// `suifleet --signer-service CONFIG`: serve until stopped.
#[cfg(target_os = "linux")]
pub fn serve(path: &Path) -> anyhow::Result<()> {
    let (config, mut policy) = ServiceConfig::load(path)?;
    let key = config.key()?;
    policy.device = Address::from_str(&key.address())?;
    let sessions = config.sessions(key.clone(), &policy)?;
    let callers = match (&config.caller_unit, config.caller_uid) {
        (Some(unit), _) => Callers::Unit { unit: unit.clone(), record: config.caller_record.clone() },
        (None, Some(uid)) => Callers::Uid(uid),
        _ => unreachable!(),
    };
    let listener = listener(&config.socket)?;
    tracing::info!(address = key.address(), ?callers, "Device signing service ready");
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else { continue };
        let _ = stream.set_read_timeout(Some(IO_TIMEOUT));
        let _ = stream.set_write_timeout(Some(IO_TIMEOUT));
        let response = match (peer(&stream), read_request(&stream)) {
            (Ok(peer), Ok(request)) => {
                let pidfd = peer_pidfd(&stream);
                let daemon = || {
                    let main = match &callers {
                        Callers::Unit { record, .. } => std::fs::read_to_string(record).ok().and_then(|text| MainProcess::parse(&text)),
                        Callers::Uid(_) => None,
                    };
                    // Read /proc, then confirm the pidfd still names that live
                    // process: the PID was not reused in between.
                    is_daemon(&callers, peer, main, |pid| {
                        let process = proc_process(pid)?;
                        match &pidfd {
                            Some(pidfd) => (pidfd_pid(pidfd) == Some(pid)).then_some(process),
                            None => Some(process),
                        }
                    })
                };
                let response = handle(&request, peer, key.as_ref(), &policy, sessions.as_ref(), daemon);
                if let Some(error) = response["error"].as_str() {
                    tracing::warn!(pid = peer.pid, uid = peer.uid, error, "Signing request refused");
                }
                response
            }
            (Err(error), _) | (_, Err(error)) => json!({ "error": format!("{error}") }),
        };
        let _ = writeln!(stream, "{response}");
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn read_request(stream: &UnixStream) -> std::io::Result<Value> {
    let mut line = String::new();
    BufReader::new(stream.take(MAX_REQUEST_BYTES)).read_line(&mut line)?;
    serde_json::from_str(&line).map_err(std::io::Error::other)
}

/// The systemd-activated socket (LISTEN_FDS), or the configured path.
#[cfg(target_os = "linux")]
fn listener(path: &Path) -> anyhow::Result<UnixListener> {
    use std::os::fd::FromRawFd;
    let ours = std::env::var("LISTEN_PID").ok().and_then(|p| p.parse::<u32>().ok()) == Some(std::process::id());
    if ours && std::env::var("LISTEN_FDS").as_deref() == Ok("1") {
        // SAFETY: systemd passes the listening socket as fd 3 (SD_LISTEN_FDS_START).
        return Ok(unsafe { UnixListener::from_raw_fd(3) });
    }
    let _ = std::fs::remove_file(path);
    Ok(UnixListener::bind(path)?)
}

/// The signing service trusts the daemon by its systemd unit and cgroup, so
/// it exists only on Linux.
#[cfg(not(target_os = "linux"))]
pub fn serve(_: &Path) -> anyhow::Result<()> {
    anyhow::bail!("the device signing service is not supported on {}", std::env::consts::OS)
}
#[cfg(not(target_os = "linux"))]
pub fn record_main_process(_: &Path) -> anyhow::Result<()> {
    anyhow::bail!("recording a systemd main process is not supported on {}", std::env::consts::OS)
}

/// The daemon side: `[device.signer] kind = "service"`.
pub struct ServiceSigner {
    socket: PathBuf,
    address: String,
}

impl ServiceSigner {
    pub fn connect(socket: &Path) -> anyhow::Result<Self> {
        anyhow::ensure!(cfg!(target_os = "linux"), "the device signing service is not supported on {}", std::env::consts::OS);
        let response = request(socket, &json!({ "op": "address" }))?;
        let address = response["address"].as_str().ok_or_else(|| anyhow::anyhow!("signer returned no address"))?.to_owned();
        Ok(Self { socket: socket.to_owned(), address })
    }
}

impl TxSigner for ServiceSigner {
    fn address(&self) -> String {
        self.address.clone()
    }
    fn sign(&self, tx: &[u8]) -> anyhow::Result<Vec<u8>> {
        let response = request(&self.socket, &json!({ "op": "sign", "tx": STANDARD.encode(tx) }))?;
        Ok(STANDARD.decode(response["signature"].as_str().ok_or_else(|| anyhow::anyhow!("signer returned no signature"))?)?)
    }
    fn sign_personal_message(&self, message: &[u8]) -> anyhow::Result<Vec<u8>> {
        let response = request(&self.socket, &json!({ "op": "sign_telemetry", "message": STANDARD.encode(message) }))?;
        Ok(STANDARD.decode(response["signature"].as_str().ok_or_else(|| anyhow::anyhow!("signer returned no signature"))?)?)
    }
    fn sign_digest(&self, _: &[u8]) -> anyhow::Result<Vec<u8>> {
        anyhow::bail!("the signing service signs only requests it can check, never a bare digest")
    }
}

/// The daemon side of Seal sessions with `kind = "service"`: the service
/// holds the session key and certifies it with the device key.
pub struct ServiceSessions {
    socket: PathBuf,
}

impl ServiceSessions {
    pub fn new(socket: &Path) -> Self {
        Self { socket: socket.to_owned() }
    }
}

impl Authority for ServiceSessions {
    fn authorize(&self, ptb: &[u8], enc_key: &[u8], enc_verification_key: &[u8]) -> anyhow::Result<(Certificate, Vec<u8>)> {
        let response = request(
            &self.socket,
            &json!({ "op": "seal", "ptb": STANDARD.encode(ptb), "enc_key": STANDARD.encode(enc_key),
                     "enc_verification_key": STANDARD.encode(enc_verification_key) }),
        )?;
        let certificate = Certificate::from_json(&response["certificate"])?;
        Ok((certificate, STANDARD.decode(response["signature"].as_str().unwrap_or(""))?))
    }
}

fn request(socket: &Path, body: &Value) -> anyhow::Result<Value> {
    let mut stream = UnixStream::connect(socket)
        .map_err(|e| anyhow::anyhow!("signing service {} unavailable: {e}", socket.display()))?;
    stream.set_read_timeout(Some(IO_TIMEOUT))?;
    stream.set_write_timeout(Some(IO_TIMEOUT))?;
    writeln!(stream, "{body}")?;
    let mut line = String::new();
    BufReader::new(stream.take(MAX_REQUEST_BYTES)).read_line(&mut line)?;
    let response: Value = serde_json::from_str(&line)?;
    if let Some(error) = response["error"].as_str() {
        anyhow::bail!("signing service refused: {error}");
    }
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    use sui_sdk_types::Digest;
    use sui_transaction_builder::{Function, ObjectInput, TransactionBuilder};

    fn policy() -> Policy {
        Policy {
            device: Address::from_str("0xa0").unwrap(),
            package: Address::from_str("0x3").unwrap(),
            namespace: Address::from_str("0x2a").unwrap(),
            registry: Address::from_str("0x13").unwrap(),
            cap: Address::from_str("0x11").unwrap(),
            max_budget: 50_000_000,
            chain_identifier: "4c78adac".into(),
            budget: Some((Address::from_str("0xb0").unwrap(), 100_000_000)),
            fleet_data: None,
        }
    }

    struct Draw {
        package: &'static str,
        budget: &'static str,
        budget_mutable: bool,
        account: &'static str,
        account_owned: bool,
        registry: &'static str,
        cap: &'static str,
        clock: &'static str,
        amount: Vec<u8>,
        sender: &'static str,
        gas_owner: &'static str,
        gas_budget: u64,
        extra_command: bool,
    }
    impl Default for Draw {
        fn default() -> Self {
            Draw { package: "0x3", budget: "0xb0", budget_mutable: false, account: "0xac", account_owned: false, registry: "0x13", cap: "0x11", clock: "0x6", amount: 50_000_000u64.to_le_bytes().to_vec(),
                   sender: "0xa0", gas_owner: "0xa0", gas_budget: 10_000_000, extra_command: false }
        }
    }
    impl Draw {
        fn bytes(&self) -> Vec<u8> {
            let a = |s: &str| Address::from_str(s).unwrap();
            let mut b = TransactionBuilder::new();
            let args = vec![
                b.object(ObjectInput::shared(a(self.budget), 1, self.budget_mutable)),
                b.object(if self.account_owned { ObjectInput::owned(a(self.account), 2, Digest::ZERO) } else { ObjectInput::shared(a(self.account), 1, true) }),
                b.object(ObjectInput::shared(a(self.registry), 1, false)),
                b.object(ObjectInput::owned(a(self.cap), 2, Digest::ZERO)),
                b.pure_bytes(self.amount.clone()),
                b.object(ObjectInput::shared(a(self.clock), 1, false)),
            ];
            let function = || Function::new(a(self.package), fleet_budget::MODULE.parse().unwrap(), fleet_budget::DRAW_GAS.parse().unwrap());
            b.move_call(function(), args.clone());
            if self.extra_command {
                b.move_call(function(), args);
            }
            b.set_sender(a(self.sender));
            b.set_sponsor(a(self.gas_owner));
            b.set_gas_budget(self.gas_budget);
            b.set_gas_price(1000);
            b.add_gas_objects([ObjectInput::owned(a("0x12"), 3, Digest::ZERO)]);
            bcs::to_bytes(&b.try_build().unwrap()).unwrap()
        }
    }

    #[test]
    fn signs_only_bounded_draws_from_this_fleets_budget() {
        let p = policy();
        assert_eq!(check(&p, &Draw::default().bytes()).unwrap(), Intent::Draw);
        assert_eq!(check(&p, &Draw { amount: 100_000_000u64.to_le_bytes().to_vec(), ..Draw::default() }.bytes()).unwrap(), Intent::Draw);
        for (label, bad) in [
            ("over the configured amount", Draw { amount: 100_000_001u64.to_le_bytes().to_vec(), ..Draw::default() }),
            ("zero", Draw { amount: 0u64.to_le_bytes().to_vec(), ..Draw::default() }),
            ("not a u64", Draw { amount: vec![1, 2, 3], ..Draw::default() }),
            ("other budget", Draw { budget: "0xb1", ..Draw::default() }),
            ("budget taken mutably", Draw { budget_mutable: true, ..Draw::default() }),
            ("account not shared", Draw { account_owned: true, ..Draw::default() }),
            ("other registry", Draw { registry: "0x99", ..Draw::default() }),
            ("other cap", Draw { cap: "0x98", ..Draw::default() }),
            ("not the clock", Draw { clock: "0x7", ..Draw::default() }),
            ("other package", Draw { package: "0x4", ..Draw::default() }),
            ("other sender", Draw { sender: "0xbad", gas_owner: "0xbad", ..Draw::default() }),
            ("sponsored", Draw { gas_owner: "0xbad", ..Draw::default() }),
            ("gas budget", Draw { gas_budget: 50_000_001, ..Draw::default() }),
            ("two commands", Draw { extra_command: true, ..Draw::default() }),
        ] {
            assert!(check(&p, &bad.bytes()).is_err(), "{label} was accepted");
        }
        let disabled = Policy { budget: None, ..policy() };
        assert!(check(&disabled, &Draw::default().bytes()).unwrap_err().to_string().contains("not enabled"));
    }

    #[test]
    fn draw_signatures_only_for_the_daemon() {
        let p = policy();
        let sign = json!({ "op": "sign", "tx": STANDARD.encode(Draw::default().bytes()) });
        assert_eq!(handle(&sign, Peer { pid: 100, uid: 990 }, &Key, &p, None, || true)["signature"], STANDARD.encode([7]));
        // A component (same UID, not the main process) and root get nothing.
        assert!(handle(&sign, Peer { pid: 101, uid: 990 }, &Key, &p, None, || false)["error"].as_str().unwrap().contains("may not request signatures"));
        assert!(handle(&sign, Peer { pid: 1, uid: 0 }, &Key, &p, None, || false)["error"].as_str().unwrap().contains("only for the daemon"));
    }

    struct Tx {
        module: &'static str,
        function: &'static str,
        package: &'static str,
        registry: &'static str,
        cap: &'static str,
        status: u8,
        group: bool,
        sender: &'static str,
        gas_owner: &'static str,
        budget: u64,
        extra_command: bool,
        health_clock: Option<(&'static str, bool)>,
        registry_mutable: bool,
        deployment_mutable: bool,
    }
    impl Default for Tx {
        fn default() -> Self {
            Tx { module: dm::MODULE, function: dm::REPORT_VERIFIED_STATUS, package: "0x3", registry: "0x13", cap: "0x11",
                 status: 1, group: false, sender: "0xa0", gas_owner: "0xa0", budget: 10_000_000, extra_command: false, health_clock: None, registry_mutable: false, deployment_mutable: true }
        }
    }
    impl Tx {
        fn bytes(&self) -> Vec<u8> {
            let a = |s: &str| Address::from_str(s).unwrap();
            let mut b = TransactionBuilder::new();
            let record = b.object(ObjectInput::shared(a("0x10"), 1, self.deployment_mutable));
            let registry = b.object(ObjectInput::shared(a(self.registry), 1, self.registry_mutable));
            let cap = b.object(ObjectInput::owned(a(self.cap), 2, Digest::ZERO));
            let mut args = vec![record, registry, cap];
            if self.group {
                args.push(b.object(ObjectInput::shared(a("0x14"), 1, false)));
            }
            args.push(b.pure(&self.status));
            if let Some((clock, mutable)) = self.health_clock {
                args.push(b.object(ObjectInput::shared(a(clock), 1, mutable)));
            }
            let function = || Function::new(a(self.package), self.module.parse().unwrap(), self.function.parse().unwrap());
            b.move_call(function(), args.clone());
            if self.extra_command {
                b.move_call(function(), args);
            }
            b.set_sender(a(self.sender));
            b.set_sponsor(a(self.gas_owner));
            b.set_gas_budget(self.budget);
            b.set_gas_price(1000);
            b.add_gas_objects([ObjectInput::owned(a("0x12"), 3, Digest::ZERO)]);
            bcs::to_bytes(&b.try_build().unwrap()).unwrap()
        }
    }

    #[test]
    fn signs_only_this_devices_status_reports() {
        let p = policy();
        assert_eq!(check(&p, &Tx::default().bytes()).unwrap(), Intent::Report);
        assert_eq!(check(&p, &Tx { status: 2, ..Tx::default() }.bytes()).unwrap(), Intent::Report);
        assert_eq!(check(&p, &Tx { group: true, function: dm::REPORT_VERIFIED_STATUS_FOR_GROUP, ..Tx::default() }.bytes()).unwrap(), Intent::Report);
        for (label, bad) in [
            ("other sender", Tx { sender: "0xbad", gas_owner: "0xbad", ..Tx::default() }),
            ("other gas owner", Tx { gas_owner: "0xbad", ..Tx::default() }),
            ("budget", Tx { budget: 50_000_001, ..Tx::default() }),
            ("package", Tx { package: "0x4", ..Tx::default() }),
            ("unverified report", Tx { function: dm::REPORT_STATUS, ..Tx::default() }),
            ("other function", Tx { function: dm::DELETE_DEPLOYMENT, ..Tx::default() }),
            ("other module", Tx { module: crate::protocol::device_registry::MODULE, function: crate::protocol::device_registry::REGISTER_DEVICE, ..Tx::default() }),
            ("registry", Tx { registry: "0x99", ..Tx::default() }),
            ("cap", Tx { cap: "0x98", ..Tx::default() }),
            ("status", Tx { status: 3, ..Tx::default() }),
            ("two commands", Tx { extra_command: true, ..Tx::default() }),
            ("group args on direct call", Tx { group: true, ..Tx::default() }),
        ] {
            assert!(check(&p, &bad.bytes()).is_err(), "{label} was accepted");
        }
        assert!(check(&p, b"not a transaction").is_err());
        let mut trailing = Tx::default().bytes();
        trailing.push(0);
        assert!(check(&p, &trailing).is_err(), "trailing bytes were accepted");
    }

    #[test]
    fn signs_only_bounded_device_rollout_health_intent() {
        let p = policy();
        let health = || Tx { function: dm::REPORT_ROLLOUT_HEALTH, health_clock: Some(("0x6", false)), ..Tx::default() };
        assert_eq!(check(&p, &health().bytes()).unwrap(), Intent::Report);
        assert_eq!(check(&p, &Tx { status: 0, ..health() }.bytes()).unwrap(), Intent::Report);
        for bad in [
            Tx { status: 2, ..health() }, Tx { health_clock: None, ..health() },
            Tx { health_clock: Some(("0x7", false)), ..health() },
            Tx { health_clock: Some(("0x6", true)), ..health() },
            Tx { registry: "0x99", ..health() }, Tx { cap: "0x99", ..health() },
            Tx { registry_mutable: true, ..health() }, Tx { deployment_mutable: false, ..health() },
            Tx { package: "0x99", ..health() }, Tx { gas_owner: "0x99", ..health() },
            Tx { budget: 50_000_001, ..health() }, Tx { extra_command: true, ..health() },
            Tx { group: true, ..health() },
        ] { assert!(check(&p, &bad.bytes()).is_err()); }
    }

    struct Key;
    impl TxSigner for Key {
        fn address(&self) -> String {
            "0xa0".into()
        }
        fn sign(&self, _: &[u8]) -> anyhow::Result<Vec<u8>> {
            Ok(vec![7])
        }
        fn sign_digest(&self, _: &[u8]) -> anyhow::Result<Vec<u8>> {
            Ok(vec![8])
        }
    }

    fn telemetry(edit: impl FnOnce(&mut crate::telemetry::Snapshot)) -> Vec<u8> {
        use crate::telemetry::*;
        let binding = Binding {
            chain_identifier: "4c78adac".into(),
            registry: "0x13".into(),
            device_id: "0x10".into(),
            interval_seconds: 60,
            poll_seconds: 30,
            started_ms: 1,
            boot_id: None,
        };
        let mut snapshot = snapshot(&binding, "0xa0", 1, 0, SyncHealth::default(), Inventory::default(), 2);
        edit(&mut snapshot);
        serde_json::to_vec(&snapshot).unwrap()
    }

    fn log_batch(edit: impl FnOnce(&mut crate::logs::Batch)) -> Vec<u8> {
        let mut batch = crate::logs::Batch {
            schema: 1,
            kind: "logs".into(),
            chain_identifier: "4c78adac".into(),
            registry: "0x13".into(),
            device_id: "0x10".into(),
            signer: "0xa0".into(),
            seq: 7,
            observed_ms: 2,
            dropped_lines: 0,
            redactions: 1,
            records: vec![crate::logs::Record { source: "component:app".into(), line: "started".into() }],
        };
        edit(&mut batch);
        serde_json::to_vec(&batch).unwrap()
    }

    #[test]
    fn signs_only_this_devices_client_device_ca_binding() {
        let binding = |signer: &str, kind: &str| serde_json::to_vec(&crate::clientdevices::CaBinding {
            schema: 1, kind: kind.into(), chain_identifier: "4c78adac".into(), registry: "0x13".into(), device_id: "0x10".into(),
            signer: signer.into(), ca_sha256: "ab".repeat(32), created_ms: 1,
        }).unwrap();
        let p = policy();
        check_personal(&p, &binding("0xa0", crate::clientdevices::CA_BINDING_KIND)).unwrap();
        assert!(check_personal(&p, &binding("0xa1", crate::clientdevices::CA_BINDING_KIND)).is_err(), "another signer");
        assert!(check_personal(&p, &binding("0xa0", "client-device-ca2")).is_err(), "unknown kind falls to telemetry");
        assert!(check(&p, &binding("0xa0", crate::clientdevices::CA_BINDING_KIND)).is_err(), "never a transaction");
    }

    #[test]
    fn signs_only_this_devices_canonical_log_batches() {
        let p = policy();
        check_personal(&p, &log_batch(|_| {})).unwrap();
        for (label, bad) in [
            ("another chain", log_batch(|b| b.chain_identifier = "35834a8a".into())),
            ("another registry", log_batch(|b| b.registry = "0x14".into())),
            ("another signer", log_batch(|b| b.signer = "0xa1".into())),
            ("no records", log_batch(|b| b.records.clear())),
            ("overlong line", log_batch(|b| b.records[0].line = "x".repeat(crate::logs::MAX_LINE + 1))),
            ("bad source", log_batch(|b| b.records[0].source = "root".into())),
            ("schema", log_batch(|b| b.schema = 2)),
        ] {
            assert!(check_personal(&p, &bad).is_err(), "{label} was accepted");
        }
        let mut spaced = log_batch(|_| {});
        spaced.insert(1, b' ');
        assert!(check_personal(&p, &spaced).is_err(), "non-canonical batch was accepted");
        // A transaction is never signed as a log batch, nor vice versa.
        assert!(check_personal(&p, &Tx::default().bytes()).is_err());
        assert!(check(&p, &log_batch(|_| {})).is_err());
    }

    #[test]
    fn signs_only_this_devices_canonical_telemetry() {
        let p = policy();
        check_telemetry(&p, &telemetry(|_| {})).unwrap();
        // The registry may be written in any address form.
        check_telemetry(&p, &telemetry(|s| s.registry = format!("0x{}", "0".repeat(62) + "13"))).unwrap();
        // Schema 2 carries a services section (SF-085), and only it does.
        let services = || Some(crate::service_health::Services { version: 1, ..Default::default() });
        check_telemetry(&p, &telemetry(|s| (s.schema, s.services) = (2, services()))).unwrap();
        let mut null_services: serde_json::Value = serde_json::from_slice(&telemetry(|_| {})).unwrap();
        null_services["services"] = json!(null);
        for (label, bad) in [
            ("services without schema 2", telemetry(|s| s.services = services())),
            ("services: null (not canonical)", serde_json::to_vec(&null_services).unwrap()),
            ("unknown services field", {
                let mut v: serde_json::Value = serde_json::from_slice(&telemetry(|s| (s.schema, s.services) = (2, services()))).unwrap();
                v["services"]["payloads"] = json!([]);
                serde_json::to_vec(&v).unwrap()
            }),
            ("a service error text", telemetry(|s| {
                let bridge = crate::service_health::BridgeHealth { last_error: Some("subscriber queue full: sub confidential/#".into()), ..Default::default() };
                (s.schema, s.services) = (2, Some(crate::service_health::Services { version: 1, bridge: Some(bridge), ..Default::default() }));
            })),
            ("another chain", telemetry(|s| s.chain_identifier = "35834a8a".into())),
            ("another registry", telemetry(|s| s.registry = "0x14".into())),
            ("another signer", telemetry(|s| s.signer = "0xa1".into())),
            ("unparsable signer", telemetry(|s| s.signer = "me".into())),
            ("another schema", telemetry(|s| s.schema = 2)),
        ] {
            assert!(check_telemetry(&p, &bad).is_err(), "{label} was accepted");
        }
        let canonical = telemetry(|_| {});
        let mut padded = canonical.clone();
        padded.push(b' ');
        let mut extra: serde_json::Value = serde_json::from_slice(&canonical).unwrap();
        extra["extra"] = json!(1);
        let oversized = telemetry(|s| s.reasons = vec!["x".repeat(crate::telemetry::MAX_MESSAGE_BYTES)]);
        for (label, bad) in [
            ("trailing whitespace", padded),
            ("unknown field", serde_json::to_vec(&extra).unwrap()),
            ("oversized", oversized),
            ("a transaction", Tx::default().bytes()),
            ("garbage", b"not telemetry".to_vec()),
        ] {
            assert!(check_telemetry(&p, &bad).is_err(), "{label} was accepted");
        }
    }

    #[test]
    fn telemetry_signatures_only_for_the_daemon() {
        let p = policy();
        let sign = json!({ "op": "sign_telemetry", "message": STANDARD.encode(telemetry(|_| {})) });
        let daemon = Peer { pid: 100, uid: 990 };
        assert_eq!(handle(&sign, daemon, &Key, &p, None, || true)["signature"], STANDARD.encode([8]));
        let refused = handle(&sign, Peer { pid: 101, uid: 990 }, &Key, &p, None, || false);
        assert!(refused["error"].as_str().unwrap().contains("only the daemon's main process"), "{refused}");
        // Root (provisioning) gets no telemetry signatures either.
        assert!(handle(&sign, Peer { pid: 1, uid: 0 }, &Key, &p, None, || false)["error"].is_string());
        let forged = json!({ "op": "sign_telemetry", "message": STANDARD.encode(telemetry(|s| s.registry = "0x14".into())) });
        assert!(handle(&forged, daemon, &Key, &p, None, || true)["error"].as_str().unwrap().contains("registry"));
        // A transaction is never signed as a personal message, nor telemetry as a transaction.
        let tx = json!({ "op": "sign_telemetry", "message": STANDARD.encode(Tx::default().bytes()) });
        assert!(handle(&tx, daemon, &Key, &p, None, || true)["error"].is_string());
        let swapped = json!({ "op": "sign", "tx": STANDARD.encode(telemetry(|_| {})) });
        assert!(handle(&swapped, daemon, &Key, &p, None, || true)["error"].is_string());
    }

    #[test]
    fn only_the_units_recorded_main_process_is_the_daemon() {
        let callers = Callers::Unit { unit: "suifleet.service".into(), record: MAIN_RECORD.into() };
        let unit = "/system.slice/suifleet.service";
        let processes = |pid: u32| match pid {
            100 => Some(Process { start: 5000, cgroup: unit.into() }),
            // A component that killed the daemon: now a child of PID 1 in the
            // unit's cgroup, which is what the old ancestry rule accepted.
            101 => Some(Process { start: 5100, cgroup: unit.into() }),
            102 => Some(Process { start: 5000, cgroup: "/system.slice/other.service".into() }),
            _ => None,
        };
        let main = Some(MainProcess { pid: 100, start: 5000 });
        let daemon = Peer { pid: 100, uid: 990 };
        assert!(is_daemon(&callers, daemon, main, processes));
        assert!(!is_daemon(&callers, Peer { pid: 101, uid: 990 }, main, processes), "orphaned component");
        // A later process reusing the recorded PID has another start time.
        assert!(!is_daemon(&callers, daemon, Some(MainProcess { pid: 100, start: 4000 }), processes), "reused PID");
        assert!(!is_daemon(&callers, Peer { pid: 102, uid: 990 }, Some(MainProcess { pid: 102, start: 5000 }), processes), "other unit");
        // No record (unit not started) or an exited peer: nobody.
        assert!(!is_daemon(&callers, daemon, None, processes));
        assert!(!is_daemon(&callers, daemon, main, |_| None));
        assert_eq!(MainProcess::parse("100 5000\n"), main);
        assert_eq!(MainProcess::parse("100"), None);
        let stat = "100 (suifleet (x) y) S 1 100 100 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 5000 1000 10";
        assert_eq!(proc_start(stat), Some(5000));
        // The development rule is the UID alone.
        assert!(is_daemon(&Callers::Uid(990), daemon, None, |_| None));
        assert!(!is_daemon(&Callers::Uid(991), daemon, None, |_| None));
    }

    #[test]
    fn report_signatures_only_for_the_daemon() {
        let p = policy();
        let sign = json!({ "op": "sign", "tx": STANDARD.encode(Tx::default().bytes()) });
        let daemon = Peer { pid: 100, uid: 990 };
        let component = Peer { pid: 101, uid: 990 };
        assert_eq!(handle(&sign, daemon, &Key, &p, None, || true)["signature"], STANDARD.encode([7]));
        let refused = handle(&sign, component, &Key, &p, None, || false);
        assert!(refused["error"].as_str().unwrap().contains("may not request signatures"), "{refused}");
        // Root is not the daemon: no report signatures, only enrollment.
        assert!(handle(&sign, Peer { pid: 1, uid: 0 }, &Key, &p, None, || false)["error"].as_str().unwrap().contains("only for the daemon"));
        // The public address is not a secret.
        assert_eq!(handle(&json!({ "op": "address" }), component, &Key, &p, None, || false)["address"], "0xa0");
        assert!(handle(&json!({ "op": "export" }), daemon, &Key, &p, None, || true)["error"].is_string());
    }

    #[test]
    fn seal_sessions_sign_only_fleet_approvals_for_the_daemon() {
        use ed25519_dalek::Verifier;
        use sui_sdk_types::{Identifier, MoveCall, ProgrammableTransaction, SharedInput};
        let p = policy();
        let device = ed25519_dalek::SigningKey::from_bytes(&[5; 32]);
        let sessions = Sessions::new(
            Arc::new(crate::identity::Ed25519File(device.clone())),
            SessionPolicy { package: p.namespace, ttl_min: 10, clock_skew_ms: 60_000 },
        )
        .unwrap();
        let approval = |package: &str, function: &str| {
            let inputs = vec![
                Input::Pure(bcs::to_bytes(&vec![7u8; 56]).unwrap()),
                Input::Shared(SharedInput::new(Address::from_str("0x40").unwrap(), 3, false)),
            ];
            let call = MoveCall {
                package: Address::from_str(package).unwrap(),
                module: Identifier::from_str("seal_policy").unwrap(),
                function: Identifier::from_str(function).unwrap(),
                type_arguments: vec![],
                arguments: vec![Argument::Input(0), Argument::Input(1)],
            };
            STANDARD.encode(bcs::to_bytes(&ProgrammableTransaction { inputs, commands: vec![Command::MoveCall(call)] }).unwrap())
        };
        let (enc_key, enc_vk) = crate::seal::elgamal_keys(&crate::seal::random_scalar());
        let request = |ptb: String, vk: &[u8]| json!({ "op": "seal", "ptb": ptb, "enc_key": STANDARD.encode(enc_key), "enc_verification_key": STANDARD.encode(vk) });
        let good = request(approval("0x3", "seal_approve_device"), &enc_vk);
        let daemon = Peer { pid: 100, uid: 990 };
        let response = handle(&good, daemon, &Key, &p, Some(&sessions), || true);
        let certificate = Certificate::from_json(&response["certificate"]).unwrap();
        // Certified by the device's own key, for the fleet package's namespace.
        assert_eq!(certificate.user, crate::identity::address(&device));
        let message = crate::seal_client::session_message(&p.namespace, 10, certificate.creation_ms, &certificate.session_key);
        assert_eq!(certificate.signature, crate::identity::personal_message_signature(&device, message.as_bytes()));
        let ptb = STANDARD.decode(good["ptb"].as_str().unwrap()).unwrap();
        let signature = ed25519_dalek::Signature::from_slice(&STANDARD.decode(response["signature"].as_str().unwrap()).unwrap()).unwrap();
        ed25519_dalek::VerifyingKey::from_bytes(&certificate.session_key)
            .unwrap()
            .verify(&crate::seal_client::request_message(&ptb, &enc_key, &enc_vk), &signature)
            .unwrap();
        assert!(response.get("session_key").is_none() && !response.to_string().contains(&hex::encode(device.to_bytes())));
        let refused = |request: &Value, daemon: bool, sessions: Option<&Sessions>| {
            handle(request, Peer { pid: 101, uid: 990 }, &Key, &p, sessions, || daemon)["error"].as_str().unwrap_or("").to_owned()
        };
        assert!(refused(&good, false, Some(&sessions)).contains("only the daemon"));
        assert!(refused(&good, true, None).contains("not enabled"));
        assert!(refused(&request(approval("0x4", "seal_approve_device"), &enc_vk), true, Some(&sessions)).contains("not a SuiFleet Seal approval"));
        assert!(refused(&request(approval("0x3", "grant_account"), &enc_vk), true, Some(&sessions)).contains("not a SuiFleet Seal approval"));
        let other_vk = crate::seal::elgamal_keys(&crate::seal::random_scalar()).1;
        assert!(refused(&request(approval("0x3", "seal_approve_device"), &other_vk), true, Some(&sessions)).contains("response encryption key"));
        assert!(refused(&request(STANDARD.encode(Tx::default().bytes()), &enc_vk), true, Some(&sessions)).contains("not a programmable transaction"));
    }

    #[test]
    fn the_key_follows_the_configured_daemon_signer() {
        let dir = std::env::temp_dir().join(format!("suifleet-key-holder-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let config = dir.join("config.toml");
        // No daemon configuration yet: the signer only once signer.toml exists.
        assert_eq!(key_holder(&config).unwrap(), DAEMON_USER);
        std::fs::write(dir.join("signer.toml"), "").unwrap();
        assert_eq!(key_holder(&config).unwrap(), SIGNER_USER);
        // A legacy configuration keeps loading the key itself, even with
        // signer.toml installed; PKCS#11 does not use the key file.
        std::fs::write(&config, "[device]\nkeypair_path = '/etc/suifleet/device.key'\n").unwrap();
        assert_eq!(key_holder(&config).unwrap(), DAEMON_USER);
        std::fs::write(&config, "[device.signer]\nkind = 'pkcs11'\n").unwrap();
        assert_eq!(key_holder(&config).unwrap(), DAEMON_USER);
        std::fs::write(&config, "[device]\ndevice_cap_id = '0x2'\n[device.signer]\nkind = 'service'\n").unwrap();
        assert_eq!(key_holder(&config).unwrap(), SIGNER_USER);
        std::fs::write(&config, "[device\n").unwrap();
        assert!(key_holder(&config).is_err(), "an unparsable configuration must not move the key");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn records_and_pins_a_live_process() {
        let dir = std::env::temp_dir().join(format!("suifleet-record-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("daemon.main");
        // Registered, or a concurrently running supervisor test's orphan sweep
        // kills it (the test process is a subreaper there).
        use crate::supervisor::{forget_std_tracked, spawn_std_tracked};
        // Establish that the foreign executable is running before inspecting
        // /proc, rather than relying on spawn timing. Keep stdin open so the
        // shell stays in its read builtin throughout the identity checks.
        fn ready_child() -> std::process::Child {
            use std::io::{BufRead, BufReader};
            use std::process::{Command, Stdio};
            let mut child = spawn_std_tracked(Command::new("sh")
                .args(["-c", "printf 'ready\\n'; read -r finish"])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())).unwrap();
            let mut ready = String::new();
            BufReader::new(child.stdout.take().unwrap()).read_line(&mut ready).unwrap();
            assert_eq!(ready, "ready\n");
            child
        }
        let mut child = ready_child();
        let executable = std::fs::read_link(format!("/proc/{}/exe", child.id())).unwrap();
        assert!(!same_file(&executable, Path::new("/proc/self/exe")).unwrap());
        assert!(record(&path, child.id(), &executable).unwrap());
        let main = MainProcess::parse(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(main.pid, child.id());
        assert_eq!(proc_process(child.id()).unwrap().start, main.start);
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o644);
        // SAFETY: pidfd_open on our own child.
        let raw = unsafe { libc::syscall(libc::SYS_pidfd_open, child.id() as libc::c_int, 0) } as libc::c_int;
        if raw >= 0 {
            use std::os::fd::FromRawFd;
            // SAFETY: a new descriptor we own.
            let pidfd = unsafe { std::os::fd::OwnedFd::from_raw_fd(raw) };
            assert_eq!(pidfd_pid(&pidfd), Some(child.id()));
            child.kill().unwrap();
            child.wait().unwrap();
            assert_eq!(pidfd_pid(&pidfd), None, "an exited process still resolved");
        } else {
            child.kill().unwrap();
            child.wait().unwrap();
        }
        forget_std_tracked(child.id());
        // A main process not running the base binary (here: sh, against
        // this test binary) is not recorded, and an earlier record is removed.
        let mut other = ready_child();
        assert!(path.exists());
        assert!(!record(&path, other.id(), Path::new("/proc/self/exe")).unwrap());
        assert!(!path.exists(), "a stale record survived");
        assert!(!record(&path, other.id(), Path::new("/proc/self/exe")).unwrap());
        other.kill().unwrap();
        other.wait().unwrap();
        forget_std_tracked(other.id());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn service_round_trip_over_a_socket() {
        let dir = std::env::temp_dir().join(format!("suifleet-signer-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let socket = dir.join("sign.sock");
        let listener = listener(&socket).unwrap();
        let uid = unsafe { libc::getuid() };
        std::thread::spawn(move || {
            let p = policy();
            for stream in listener.incoming().take(5) {
                let mut stream = stream.unwrap();
                let request = read_request(&stream).unwrap();
                let peer = peer(&stream).unwrap();
                let response = handle(&request, peer, &Key, &p, None, || is_daemon(&Callers::Uid(uid), peer, None, |_| None));
                writeln!(stream, "{response}").unwrap();
            }
        });
        let signer = ServiceSigner::connect(&socket).unwrap();
        assert_eq!(signer.address(), "0xa0");
        assert_eq!(signer.sign(&Tx::default().bytes()).unwrap(), vec![7]);
        let refused = signer.sign(&Tx { function: dm::DELETE_DEPLOYMENT, ..Tx::default() }.bytes()).unwrap_err();
        assert!(format!("{refused:#}").contains("not a permitted signing intent"), "{refused:#}");
        // A full-size telemetry snapshot fits in one request.
        let large = telemetry(|s| {
            s.reasons = vec![];
            s.boot_id = Some("b".repeat(crate::telemetry::MAX_MESSAGE_BYTES - 400));
        });
        assert!(large.len() > crate::telemetry::MAX_MESSAGE_BYTES - 200, "{}", large.len());
        assert_eq!(signer.sign_personal_message(&large).unwrap(), vec![8]);
        let refused = signer.sign_personal_message(b"anything").unwrap_err();
        assert!(format!("{refused:#}").contains("not a telemetry snapshot"), "{refused:#}");
    }
}

#[cfg(test)]
mod fleet_intent_tests {
    use super::*;
    use sui_transaction_builder::{Function, ObjectInput, TransactionBuilder};
    fn a(s: &str) -> Address { s.parse().unwrap() }
    fn policy() -> Policy {
        Policy { device: a("0xa0"), package: a("0x3"), namespace: a("0x3"), registry: a("0x13"),
            cap: a("0x11"), max_budget: 50_000_000, chain_identifier: "4c78adac".into(), budget: None,
            fleet_data: Some(crate::fleet_data::Config { package_id: "0xf0".into(), type_origin: "0xf0".into(),
                fleet_id: "0xf1".into(), writer_id: "0xf2".into(), budget_id: "0xb0".into(), account_id: "0xac".into(), storage_id: "0x5".into(), archives: None }) }
    }
    fn archive_policy() -> Policy {
        let mut p=policy();
        p.fleet_data.as_mut().unwrap().archives=Some(crate::fleet_archive::Config {
            storage_package:"0xf6".into(), storage_type_origin:"0xf6".into(), system_id:"0xf4".into(), walrus_package:"0xf7".into(),
            key_server_package:"0xf5".into(), key_servers:vec![crate::config::KeyServerConfig { object_id:"0xf8".into(),weight:1,api_key_name:None,api_key_file:None }],
            threshold:1,node:"/usr/bin/node".into(),codec:"/usr/lib/codec.cjs".into(),codec_sha256:"00".repeat(32),compact_history:false,quilt:false,stream_retention:false,opaque:false,walrus_streams:vec![],
        });p
    }
    fn snapshot(p: &Policy) -> crate::telemetry::Snapshot {
        crate::telemetry::snapshot(&crate::telemetry::Binding { chain_identifier: p.chain_identifier.clone(), registry: p.registry.to_string(),
            device_id: "0xd0".into(), interval_seconds: 60, poll_seconds: 10, started_ms: 1, boot_id: None },
            &p.device.to_string(), 1, 0, Default::default(), Default::default(), 100)
    }
    fn transaction(change: Option<(usize, &str, bool)>, package: &str, sequence: u64, extra: bool) -> Vec<u8> {
        let p = policy(); let mut b = TransactionBuilder::new();
        let mut args = Vec::new();
        for (index, id, mutable) in [(0,"0xf1",false),(1,"0xf2",true),(2,"0xb0",false),(3,"0xac",false),(4,"0x13",false)] {
            let (id, mutable) = change.filter(|c| c.0 == index).map(|c| (c.1,c.2)).unwrap_or((id,mutable));
            args.push(b.object(ObjectInput::shared(a(id),1,mutable)));
        }
        args.push(b.object(ObjectInput::owned(a("0x11"), 1, sui_sdk_types::Digest::ZERO)));
        args.push(b.pure(&sequence)); args.push(b.pure(&serde_json::to_vec(&snapshot(&p)).unwrap())); args.push(b.pure(&vec![0u8;97]));
        args.push(b.object(ObjectInput::shared(a("0x6"),1,false)));
        b.move_call(Function::new(a(package),"fleet_data".parse().unwrap(),"report_health".parse().unwrap()),args);
        if extra { b.pure(&123u64); }
        b.set_sender(p.device); b.set_gas_budget(10_000_000); b.set_gas_price(1000);
        b.add_gas_objects([ObjectInput::owned(a("0x12"),1,sui_sdk_types::Digest::ZERO)]);
        bcs::to_bytes(&b.try_build().unwrap()).unwrap()
    }
    #[test]
    fn health_intent_is_opt_in_and_binds_all_objects_mutability_and_sequence() {
        let p = policy(); let good = transaction(None,"0xf0",1,false);
        assert_eq!(check(&p,&good).unwrap(),Intent::FleetData);
        assert!(check(&Policy { fleet_data: None, ..p.clone() },&good).is_err());
        for index in 0..5 {
            assert!(check(&p,&transaction(Some((index,"0xfe",index==1)),"0xf0",1,false)).is_err());
            assert!(check(&p,&transaction(Some((index,["0xf1","0xf2","0xb0","0xac","0x13"][index],index!=1)),"0xf0",1,false)).is_err());
        }
        for bad in [transaction(None,"0xf3",1,false), transaction(None,"0xf0",2,false), transaction(None,"0xf0",1,true)] { assert!(check(&p,&bad).is_err()); }
    }
    #[test]
    fn archive_signatures_are_bound_to_chain_registry_fleet_and_signer() {
        let p = archive_policy();
        let mut archive = crate::fleet_data::Archive { schema: 1, kind: "fleet-data".into(), chain_identifier: p.chain_identifier.clone(), registry: p.registry.to_string(),
            fleet_id: "0xf1".into(), device_id: "0xd0".into(), signer: p.device.to_string(), seq: 1, observed_ms: 100,
            reason: "diagnostics".into(), dropped_lines: 0, records: vec![], shadows: None, previous_head: None, items: None };
        let bytes = serde_json::to_vec(&archive).unwrap(); check_personal(&p,&bytes).unwrap();
        assert!(check_personal(&Policy { fleet_data: None, ..p.clone() },&bytes).is_err());
        for field in ["chain_identifier","registry","fleet_id","signer"] {
            let mut value = serde_json::to_value(&archive).unwrap(); value[field] = if field == "chain_identifier" { "ffffffff" } else { "0xff" }.into();
            let bad: crate::fleet_data::Archive = serde_json::from_value(value).unwrap();
            assert!(check_personal(&p,&serde_json::to_vec(&bad).unwrap()).is_err());
        }
        archive.records.push(crate::fleet_data::Record { source: "component".into(), line: "x".repeat(4097) });
        assert!(check_personal(&p,&serde_json::to_vec(&archive).unwrap()).is_err());
        archive.records.clear(); archive.schema = 4;
        archive.items = Some(vec![crate::fleet_data::QuiltItem {identifier:"logs-1".into(),size:128,sha256:"aa".repeat(32)},
            crate::fleet_data::QuiltItem {identifier:"diagnostic-2".into(),size:128,sha256:"bb".repeat(32)}]);
        let bytes = serde_json::to_vec(&archive).unwrap();
        assert!(check_personal(&p,&bytes).is_err());
        let mut enabled = p.clone();
        enabled.fleet_data.as_mut().unwrap().archives.as_mut().unwrap().quilt = true;
        check_personal(&enabled,&bytes).unwrap();
        archive.previous_head = Some(vec![]);
        assert!(check_personal(&enabled,&serde_json::to_vec(&archive).unwrap()).is_err());
        enabled.fleet_data.as_mut().unwrap().archives.as_mut().unwrap().compact_history = true;
        check_personal(&enabled,&serde_json::to_vec(&archive).unwrap()).unwrap();
    }
    #[test]
    fn opaque_personal_and_anchor_intents_are_opt_in_bounded_and_bound() {
        let mut p=archive_policy();
        let item=crate::opaque::Item {sequence:0,descriptor:crate::opaque::Metadata {stream:"sensor".into(),key:"raw".into(),content_type:"application/octet-stream".into(),metadata:Default::default(),upload:"ab".repeat(32),chunk:0,chunks:1,total_size:1,size:1,sha256:<sha2::Sha256 as sha2::Digest>::digest([0]).to_vec()},data:"AA==".into(),source_sequence:None};
        let mut m=crate::opaque::Message {kind:"fleet-opaque".into(),schema:1,chain_identifier:p.chain_identifier.clone(),registry:p.registry.to_string(),fleet_id:"0xf1".into(),device_id:"0xd0".into(),signer:p.device.to_string(),batch_sequence:1,item};
        let bytes=serde_json::to_vec(&m).unwrap();assert!(check_personal(&p,&bytes).is_err());
        p.fleet_data.as_mut().unwrap().archives.as_mut().unwrap().opaque=true;
        check_personal(&p,&bytes).unwrap();
        for field in ["chain_identifier","registry","fleet_id","signer"] {
            let mut bad=serde_json::to_value(&m).unwrap();bad[field]=if field=="chain_identifier" {"ffffffff"}else{"0xff"}.into();
            let bad:crate::opaque::Message=serde_json::from_value(bad).unwrap();assert!(check_personal(&p,&serde_json::to_vec(&bad).unwrap()).is_err());
        }
        m.item.data="AQ==".into();assert!(check_personal(&p,&serde_json::to_vec(&m).unwrap()).is_err());
        let good=archive_transaction("anchor_items",None,100,"0xf0");check(&p,&good).unwrap();
        assert!(check(&archive_policy(),&good).is_err());
        assert!(check(&p,&archive_transaction("anchor_items",None,131073,"0xf0")).is_err());
        assert!(check(&p,&archive_transaction("anchor_items",None,100,"0xfe")).is_err());
        for index in 0..5 {assert!(check(&p,&archive_transaction("anchor_items",Some((index,"0xfe",index==1)),100,"0xf0")).is_err());}
    }
    fn archive_transaction(kind:&str, change:Option<(usize,&str,bool)>,size:u64,package:&str)->Vec<u8> {
        let p=archive_policy();let mut b=TransactionBuilder::new();let mut args=Vec::new();
        let anchor=kind=="anchor" || kind=="anchor_compact" || kind=="anchor_items";
        let stream=kind=="register_stream_blob";
        let objects:Vec<(&str,bool)>=match kind {
            "register_stream_blob"=>vec![("0xf1",false),("0xf2",false),("0xb0",false),("0xac",false),("0x13",false)],
            "register_blob"=>vec![("0x5",true),("0xb0",false),("0xac",false),("0x13",false)],
            "certify_blob"=>vec![("0x5",true),("0xf4",false)],
            _=>vec![("0xf1",false),("0xf2",true),("0xb0",false),("0xac",false),("0x13",false)],
        };
        for(index,(id,mutable))in objects.iter().enumerate(){let(id,mutable)=change.filter(|c|c.0==index).map(|c|(c.1,c.2)).unwrap_or((*id,*mutable));args.push(b.object(ObjectInput::shared(a(id),1,mutable)));}
        if kind=="register_blob" || anchor || stream {
            let cap = if stream { change.filter(|c| c.0 == 5).map_or("0x11", |c| c.1) } else { "0x11" };
            args.push(b.object(ObjectInput::owned(a(cap),1,sui_sdk_types::Digest::ZERO)));
        }
        if stream {
            for (index, id) in [(6, "0x5"), (7, "0xf4")] {
                let (id, mutable) = change.filter(|c| c.0 == index).map_or((id, true), |c| (c.1, c.2));
                args.push(b.object(ObjectInput::shared(a(id),1,mutable)));
            }
            args.push(b.pure(&0u8)); args.push(b.pure_bytes(vec![2;32])); args.push(b.pure_bytes(vec![3;32]));
            args.push(b.pure(&size));
            let (clock, mutable) = change.filter(|c| c.0 == 12).map_or(("0x6", false), |c| (c.1, c.2));
            args.push(b.object(ObjectInput::shared(a(clock),1,mutable)));
        } else if kind=="register_blob" {
            args.push(b.object(ObjectInput::shared(a("0xf4"),1,true)));args.push(b.pure_bytes(vec![2;32]));args.push(b.pure_bytes(vec![3;32]));args.push(b.pure(&size));args.push(b.pure(&1u8));args.push(b.object(ObjectInput::shared(a("0x6"),1,false)));
        }else if kind=="certify_blob"{
            args.push(b.pure(&a("0xc0")));args.push(b.pure(&vec![0u8;96]));args.push(b.pure(&vec![1u8]));args.push(b.pure(&b"certificate".to_vec()));
        }else{
            args.push(b.object(ObjectInput::shared(a("0x5"),1,false)));args.push(b.pure(&a("0xc0")));args.push(b.pure(&1u64));args.push(b.pure(&0u8));args.push(b.pure(&vec![4u8;32]));
            if kind=="anchor_compact" {args.push(b.pure(&Vec::<u8>::new()));}
            if kind=="anchor_items" {
                let item=crate::opaque::Index {sequence:0,stream:"sensor".into(),key:"raw".into(),content_type:"application/octet-stream".into(),metadata:b"{}".to_vec(),upload:"ab".repeat(32),chunk:0,chunks:1,total_size:1,size:1,sha256:vec![0;32],identifier:"data-00".into(),ciphertext_size:size,ciphertext_sha256:vec![1;32]};
                args.push(b.pure(&vec![bcs::to_bytes(&item).unwrap()]));
            }
            args.push(b.object(ObjectInput::shared(a("0x6"),1,false)));
        }
        b.move_call(Function::new(a(package),if anchor || stream {"fleet_data"}else{"fleet_storage"}.parse().unwrap(),kind.parse().unwrap()),args);
        b.set_sender(p.device);b.set_gas_budget(10_000_000);b.set_gas_price(1000);b.add_gas_objects([ObjectInput::owned(a("0x12"),1,sui_sdk_types::Digest::ZERO)]);
        bcs::to_bytes(&b.try_build().unwrap()).unwrap()
    }
    #[test]
    fn stream_purchase_signer_binds_all_authority_and_requires_opt_in() {
        let mut p = archive_policy();
        let good = archive_transaction("register_stream_blob", None, 100, "0xf0");
        assert!(check(&p, &good).is_err());
        p.fleet_data.as_mut().unwrap().archives.as_mut().unwrap().stream_retention = true;
        assert_eq!(check(&p, &good).unwrap(), Intent::FleetData);
        for (index, id, mutable) in [(0,"0xf1",false), (1,"0xf2",false), (2,"0xb0",false),
            (3,"0xac",false), (4,"0x13",false), (6,"0x5",true), (7,"0xf4",true), (12,"0x6",false)] {
            assert!(check(&p, &archive_transaction("register_stream_blob", Some((index,"0xff",mutable)),100,"0xf0")).is_err(), "foreign object at {index}");
            assert!(check(&p, &archive_transaction("register_stream_blob", Some((index,id,!mutable)),100,"0xf0")).is_err(), "wrong mutability at {index}");
        }
        assert!(check(&p, &archive_transaction("register_stream_blob", Some((5,"0xff",false)),100,"0xf0")).is_err(), "foreign device capability");
        assert!(check(&p, &archive_transaction("register_stream_blob", None, 0,"0xf0")).is_err());
        assert!(check(&p, &archive_transaction("register_stream_blob", None, crate::fleet_data::MAX_ARCHIVE as u64+1,"0xf0")).is_err());
        assert!(check(&p, &archive_transaction("register_stream_blob", None, 100,"0xff")).is_err());
    }
    #[test]
    fn compact_archive_transaction_requires_explicit_signer_opt_in() {
        let mut p = archive_policy();
        let tx = archive_transaction("anchor_compact",None,2048,"0xf0");
        assert!(check(&p,&tx).is_err());
        p.fleet_data.as_mut().unwrap().archives.as_mut().unwrap().compact_history=true;
        assert_eq!(check(&p,&tx).unwrap(),Intent::FleetData);
        for index in 0..5 {
            assert!(check(&p,&archive_transaction("anchor_compact",Some((index,"0xfe",false)),2048,"0xf0")).is_err());
        }
    }
    #[test]
    fn archive_transactions_are_opt_in_and_cannot_replace_authority_or_storage() {
        let p=archive_policy();
        for(kind,package,count)in [("register_blob","0xf6",4),("certify_blob","0xf6",2),("anchor","0xf0",5)]{
            let good=archive_transaction(kind,None,2048,package);assert_eq!(check(&p,&good).unwrap(),Intent::FleetData);
            assert!(check(&policy(),&good).is_err(),"health-only policy cannot publish archives");
            for index in 0..count{assert!(check(&p,&archive_transaction(kind,Some((index,"0xfe",false)),2048,package)).is_err());}
            assert!(check(&p,&archive_transaction(kind,None,2048,"0xff")).is_err());
        }
        assert!(check(&p,&archive_transaction("register_blob",None,crate::fleet_data::MAX_ARCHIVE as u64+1,"0xf6")).is_err());
        assert!(check(&p,&archive_transaction("register_blob",Some((1,"0xb0",true)),2048,"0xf6")).is_err());
    }

}
