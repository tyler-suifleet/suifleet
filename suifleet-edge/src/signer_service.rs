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
//!   production, the main process of `suifleet.service`: a direct child of
//!   PID 1 in that unit's cgroup, which no component process can be while the
//!   daemon, their child subreaper, lives);
//! - enrollment: exactly `enrollment::enroll` with the manifest's registry,
//!   sponsored by another address; only from root (provisioning).
//!
//! Any process may ask for the device's public address. Everything else is
//! refused. Component processes run as the daemon user and may reach the
//! socket, but they are not the unit's main process.
use crate::identity::TxSigner;
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
const MAX_REQUEST_BYTES: u64 = 64 * 1024;
const IO_TIMEOUT: Duration = Duration::from_secs(10);

fn default_socket() -> PathBuf {
    "/run/suifleet-signer/sign.sock".into()
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
        ("deployment_manager", function @ ("report_verified_status" | "report_verified_status_for_group")) => {
            anyhow::ensure!(tx.gas_payment.owner == policy.device, "a status report must be paid by the device");
            anyhow::ensure!(tx.gas_payment.budget <= policy.max_budget, "gas budget {} exceeds {}", tx.gas_payment.budget, policy.max_budget);
            let group = function == "report_verified_status_for_group";
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
        ("enrollment", "enroll") => {
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

/// Who may ask for what.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Callers {
    Unit(String),
    Uid(u32),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Peer {
    pub pid: u32,
    pub uid: u32,
}

/// A process's parent PID and cgroup v2 path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Lineage {
    pub ppid: u32,
    pub cgroup: String,
}

/// Whether `peer` is the daemon allowed to request status reports. For a
/// unit: its main process, i.e. a direct child of the service manager (PID 1)
/// inside the unit's own cgroup. Component processes share the cgroup but are
/// descendants of the daemon (their child subreaper) and cannot leave the
/// cgroup (ProtectControlGroups), so they never pass.
pub fn is_daemon(callers: &Callers, peer: Peer, lineage: impl Fn(u32) -> Option<Lineage>) -> bool {
    match callers {
        Callers::Unit(unit) => lineage(peer.pid).is_some_and(|l| {
            l.ppid == 1 && l.cgroup.rsplit('/').next() == Some(unit.as_str())
        }),
        Callers::Uid(uid) => peer.uid == *uid,
    }
}

fn proc_lineage(pid: u32) -> Option<Lineage> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let ppid = stat.rsplit_once(')')?.1.split_whitespace().nth(1)?.parse().ok()?;
    let cgroup = std::fs::read_to_string(format!("/proc/{pid}/cgroup")).ok()?;
    let cgroup = cgroup.lines().find_map(|line| line.strip_prefix("0::"))?.trim().to_owned();
    Some(Lineage { ppid, cgroup })
}

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
    callers: &Callers,
    lineage: impl Fn(u32) -> Option<Lineage>,
) -> Value {
    let result = (|| -> anyhow::Result<Value> {
        match request["op"].as_str() {
            Some("address") => Ok(json!({ "address": key.address() })),
            Some("sign") => {
                let daemon = is_daemon(callers, peer, lineage);
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
            _ => anyhow::bail!("unknown request"),
        }
    })();
    result.unwrap_or_else(|error| json!({ "error": format!("{error:#}") }))
}

/// `suifleet --signer-service CONFIG`: serve until stopped.
pub fn serve(path: &Path) -> anyhow::Result<()> {
    let (config, mut policy) = ServiceConfig::load(path)?;
    let key = config.key()?;
    policy.device = Address::from_str(&key.address())?;
    let callers = match (&config.caller_unit, config.caller_uid) {
        (Some(unit), _) => Callers::Unit(unit.clone()),
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
                let response = handle(&request, peer, key.as_ref(), &policy, &callers, proc_lineage);
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

fn read_request(stream: &UnixStream) -> std::io::Result<Value> {
    let mut line = String::new();
    BufReader::new(stream.take(MAX_REQUEST_BYTES)).read_line(&mut line)?;
    serde_json::from_str(&line).map_err(std::io::Error::other)
}

/// The systemd-activated socket (LISTEN_FDS), or the configured path.
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

/// The daemon side: `[device.signer] kind = "service"`.
pub struct ServiceSigner {
    socket: PathBuf,
    address: String,
}

impl ServiceSigner {
    pub fn connect(socket: &Path) -> anyhow::Result<Self> {
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
            Tx { module: "deployment_manager", function: "report_verified_status", package: "0x3", registry: "0x13", cap: "0x11",
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
        assert_eq!(check(&p, &Tx { group: true, function: "report_verified_status_for_group", ..Tx::default() }.bytes()).unwrap(), Intent::Report);
        for (label, bad) in [
            ("other sender", Tx { sender: "0xbad", gas_owner: "0xbad", ..Tx::default() }),
            ("other gas owner", Tx { gas_owner: "0xbad", ..Tx::default() }),
            ("budget", Tx { budget: 50_000_001, ..Tx::default() }),
            ("package", Tx { package: "0x4", ..Tx::default() }),
            ("unverified report", Tx { function: "report_status", ..Tx::default() }),
            ("other function", Tx { function: "delete_deployment", ..Tx::default() }),
            ("other module", Tx { module: "device_registry", function: "register_device", ..Tx::default() }),
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
    }

    #[test]
    fn only_the_units_main_process_gets_report_signatures() {
        let (p, callers) = (policy(), Callers::Unit("suifleet.service".into()));
        let main = |pid: u32| match pid {
            100 => Some(Lineage { ppid: 1, cgroup: "/system.slice/suifleet.service".into() }),
            // A component: same cgroup, child of the daemon.
            101 => Some(Lineage { ppid: 100, cgroup: "/system.slice/suifleet.service".into() }),
            // A child of PID 1 in another unit.
            102 => Some(Lineage { ppid: 1, cgroup: "/system.slice/other.service".into() }),
            _ => None,
        };
        let sign = json!({ "op": "sign", "tx": STANDARD.encode(Tx::default().bytes()) });
        let daemon = Peer { pid: 100, uid: 990 };
        let component = Peer { pid: 101, uid: 990 };
        assert_eq!(handle(&sign, daemon, &Key, &p, &callers, main)["signature"], STANDARD.encode([7]));
        let refused = handle(&sign, component, &Key, &p, &callers, main);
        assert!(refused["error"].as_str().unwrap().contains("may not request signatures"), "{refused}");
        let other_unit = handle(&sign, Peer { pid: 102, uid: 990 }, &Key, &p, &callers, main);
        assert!(other_unit["error"].is_string(), "{other_unit}");
        // Without a running unit nobody gets a report signature, root included.
        let none = |_: u32| None;
        assert!(handle(&sign, Peer { pid: 1, uid: 0 }, &Key, &p, &callers, none)["error"].as_str().unwrap().contains("only for the daemon"));
        // The public address is not a secret.
        assert_eq!(handle(&json!({ "op": "address" }), component, &Key, &p, &callers, main)["address"], "0xa0");
        assert!(handle(&json!({ "op": "export" }), daemon, &Key, &p, &callers, main)["error"].is_string());
    }

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
            for stream in listener.incoming().take(3) {
                let mut stream = stream.unwrap();
                let request = read_request(&stream).unwrap();
                let response = handle(&request, peer(&stream).unwrap(), &Key, &p, &Callers::Uid(uid), |_| None);
                writeln!(stream, "{response}").unwrap();
            }
        });
        let signer = ServiceSigner::connect(&socket).unwrap();
        assert_eq!(signer.address(), "0xa0");
        assert_eq!(signer.sign(&Tx::default().bytes()).unwrap(), vec![7]);
        let refused = signer.sign(&Tx { function: "delete_deployment", ..Tx::default() }.bytes()).unwrap_err();
        assert!(format!("{refused:#}").contains("not a permitted signing intent"), "{refused:#}");
    }
}
