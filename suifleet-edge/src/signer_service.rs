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
//!   A personal-message signature is never valid as a transaction.
//!
//! Any process may ask for the device's public address. Everything else is
//! refused. Component processes run as the daemon user and may reach the
//! socket, but they are not the unit's main process.
// Only the client side exists elsewhere: it refuses to connect.
#![cfg_attr(not(target_os = "linux"), allow(dead_code, unused_imports))]
use crate::identity::TxSigner;
use crate::protocol::{deployment_manager as dm, enrollment};
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
const MAX_REQUEST_BYTES: u64 = 128 * 1024;
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
        let policy = Policy {
            device: Address::ZERO,
            package: Address::from_str(&manifest.call_package_id)?,
            registry: Address::from_str(&manifest.shared_objects.registry.id)?,
            cap: Address::from_str(&config.device_cap_id)?,
            max_budget: config.max_gas_budget,
            chain_identifier: manifest.chain_identifier.clone(),
        };
        Ok((config, policy))
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
}

#[derive(Debug, Clone)]
pub struct Policy {
    pub device: Address,
    pub package: Address,
    pub registry: Address,
    pub cap: Address,
    pub max_budget: u64,
    /// The manifest's chain; telemetry snapshots must name it.
    pub chain_identifier: String,
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

/// Check a telemetry personal message: exactly the canonical JSON of one
/// bounded snapshot, of this chain and registry, naming this key as signer.
/// Its content is otherwise the daemon's own report, like a status report's.
pub fn check_telemetry(policy: &Policy, message: &[u8]) -> anyhow::Result<()> {
    use crate::telemetry::{Snapshot, MAX_MESSAGE_BYTES, SCHEMA};
    anyhow::ensure!(message.len() <= MAX_MESSAGE_BYTES, "telemetry snapshot exceeds {MAX_MESSAGE_BYTES} bytes");
    let snapshot: Snapshot = serde_json::from_slice(message).map_err(|e| anyhow::anyhow!("not a telemetry snapshot: {e}"))?;
    anyhow::ensure!(serde_json::to_vec(&snapshot)? == message, "telemetry snapshot is not in canonical form");
    anyhow::ensure!(snapshot.schema == SCHEMA, "unsupported telemetry schema {}", snapshot.schema);
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

/// A personal message the service signs: a telemetry snapshot or a log batch.
pub fn check_personal(policy: &Policy, message: &[u8]) -> anyhow::Result<()> {
    let kind = serde_json::from_slice::<serde_json::Value>(message).ok().and_then(|v| v.get("kind").and_then(|k| k.as_str()).map(str::to_owned));
    match kind.as_deref() {
        Some(crate::logs::KIND) => check_logs(policy, message),
        Some(crate::streams::MANIFEST_KIND) => crate::streams::check_manifest(message, &policy.chain_identifier, &policy.registry.to_string(), &policy.device.to_string()),
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

/// One request: authorize the caller, then the transaction, then sign.
pub fn handle(
    request: &Value,
    peer: Peer,
    key: &dyn TxSigner,
    policy: &Policy,
    daemon: impl FnOnce() -> bool,
) -> Value {
    let result = (|| -> anyhow::Result<Value> {
        match request["op"].as_str() {
            Some("address") => Ok(json!({ "address": key.address() })),
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
                let response = handle(&request, peer, key.as_ref(), &policy, daemon);
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
            registry: Address::from_str("0x13").unwrap(),
            cap: Address::from_str("0x11").unwrap(),
            max_budget: 50_000_000,
            chain_identifier: "4c78adac".into(),
        }
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
    }
    impl Default for Tx {
        fn default() -> Self {
            Tx { module: dm::MODULE, function: dm::REPORT_VERIFIED_STATUS, package: "0x3", registry: "0x13", cap: "0x11",
                 status: 1, group: false, sender: "0xa0", gas_owner: "0xa0", budget: 10_000_000, extra_command: false }
        }
    }
    impl Tx {
        fn bytes(&self) -> Vec<u8> {
            let a = |s: &str| Address::from_str(s).unwrap();
            let mut b = TransactionBuilder::new();
            let record = b.object(ObjectInput::shared(a("0x10"), 1, true));
            let registry = b.object(ObjectInput::shared(a(self.registry), 1, false));
            let cap = b.object(ObjectInput::owned(a(self.cap), 2, Digest::ZERO));
            let mut args = vec![record, registry, cap];
            if self.group {
                args.push(b.object(ObjectInput::shared(a("0x14"), 1, false)));
            }
            args.push(b.pure(&self.status));
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
        for (label, bad) in [
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
        assert_eq!(handle(&sign, daemon, &Key, &p, || true)["signature"], STANDARD.encode([8]));
        let refused = handle(&sign, Peer { pid: 101, uid: 990 }, &Key, &p, || false);
        assert!(refused["error"].as_str().unwrap().contains("only the daemon's main process"), "{refused}");
        // Root (provisioning) gets no telemetry signatures either.
        assert!(handle(&sign, Peer { pid: 1, uid: 0 }, &Key, &p, || false)["error"].is_string());
        let forged = json!({ "op": "sign_telemetry", "message": STANDARD.encode(telemetry(|s| s.registry = "0x14".into())) });
        assert!(handle(&forged, daemon, &Key, &p, || true)["error"].as_str().unwrap().contains("registry"));
        // A transaction is never signed as a personal message, nor telemetry as a transaction.
        let tx = json!({ "op": "sign_telemetry", "message": STANDARD.encode(Tx::default().bytes()) });
        assert!(handle(&tx, daemon, &Key, &p, || true)["error"].is_string());
        let swapped = json!({ "op": "sign", "tx": STANDARD.encode(telemetry(|_| {})) });
        assert!(handle(&swapped, daemon, &Key, &p, || true)["error"].is_string());
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
        assert_eq!(handle(&sign, daemon, &Key, &p, || true)["signature"], STANDARD.encode([7]));
        let refused = handle(&sign, component, &Key, &p, || false);
        assert!(refused["error"].as_str().unwrap().contains("may not request signatures"), "{refused}");
        // Root is not the daemon: no report signatures, only enrollment.
        assert!(handle(&sign, Peer { pid: 1, uid: 0 }, &Key, &p, || false)["error"].as_str().unwrap().contains("only for the daemon"));
        // The public address is not a secret.
        assert_eq!(handle(&json!({ "op": "address" }), component, &Key, &p, || false)["address"], "0xa0");
        assert!(handle(&json!({ "op": "export" }), daemon, &Key, &p, || true)["error"].is_string());
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
        let mut child = spawn_std_tracked(std::process::Command::new("sleep").arg("30")).unwrap();
        let sleep = std::fs::read_link(format!("/proc/{}/exe", child.id())).unwrap();
        assert!(record(&path, child.id(), &sleep).unwrap());
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
        // A main process not running the base binary (here: sleep, against
        // this test binary) is not recorded, and an earlier record is removed.
        let mut other = spawn_std_tracked(std::process::Command::new("sleep").arg("30")).unwrap();
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
                let response = handle(&request, peer, &Key, &p, || is_daemon(&Callers::Uid(uid), peer, None, |_| None));
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
