//! Test-only Seal key server for disposable localnets
//! (scripts/test_seal_localnet.py). Mysten's key server is the production
//! implementation; this one follows the same protocol so the daemon's
//! complete client path can run against a real chain without public
//! services:
//!
//! - GET /v1/service: the master key's proof of possession;
//! - POST /v1/fetch_key: the session certificate (TTL, age, the user's
//!   personal-message signature checked with Mysten's sui-crypto), the
//!   session key's request signature, the PTB's shape (only `seal_approve*`
//!   calls into one package that upgrades the namespace package, identity
//!   first), then the calls simulated on the chain with the certificate's
//!   user as sender. Only a successful simulation releases keys, each
//!   ElGamal-encrypted to the request's key. Each key request is logged
//!   (`fetch_key OBJECT_ID` on stderr) so tests can count them.
//!
//! Usage:
//!   seal_key_server public-key MASTER_SEED_HEX        (prints the G2 public key)
//!   seal_key_server serve RPC_URL NAMESPACE_PACKAGE KEY_SERVER_OBJECT_ID MASTER_SEED_HEX PORT
//!     [--max-ttl-minutes N]                           (prints "listening PORT")
#[path = "../src/seal.rs"]
#[allow(dead_code)]
mod seal;

use ark_ec::AffineRepr;
use base64::{engine::general_purpose::STANDARD, Engine};
use ed25519_dalek::Verifier;
use serde_json::{json, Value};
use std::{str::FromStr, sync::Arc, time::SystemTime};
use sui_crypto::SuiVerifier;
use sui_rpc::field::{FieldMask, FieldMaskUtil};
use sui_rpc::proto::sui::rpc::v2 as proto;
use sui_sdk_types::{Address, Argument, Command, GasPayment, Input, PersonalMessage, ProgrammableTransaction, Transaction, TransactionExpiration, TransactionKind, UserSignature};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

struct Server {
    rpc: sui_rpc::Client,
    namespace: Address,
    object_id: [u8; 32],
    master: ark_bls12_381::Fr,
    public_key: [u8; 96],
    max_ttl_minutes: u64,
}

type Reply = Result<Value, (u16, String)>;

fn refuse<T>(status: u16, message: impl Into<String>) -> Result<T, (u16, String)> {
    Err((status, message.into()))
}

fn utc(ms: u64) -> String {
    let seconds = ms / 1000;
    let (days, rest) = ((seconds / 86_400) as i64, seconds % 86_400);
    let (mut year, mut day) = (1970i64, days);
    let leap = |y: i64| (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
    while day >= if leap(year) { 366 } else { 365 } {
        day -= if leap(year) { 366 } else { 365 };
        year += 1;
    }
    let lengths = [31, if leap(year) { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let mut month = 0;
    while day >= lengths[month] {
        day -= lengths[month];
        month += 1;
    }
    format!("{year:04}-{:02}-{:02} {:02}:{:02}:{:02} UTC", month + 1, day + 1, rest / 3600, rest % 3600 / 60, rest % 60)
}

fn decode(value: &Value) -> Result<Vec<u8>, (u16, String)> {
    STANDARD.decode(value.as_str().unwrap_or_default()).or_else(|_| refuse(400, "invalid base64"))
}

impl Server {
    fn service(&self, query: &str) -> Reply {
        let id = format!("0x{}", hex::encode(self.object_id));
        if !query.contains(&format!("service_id={id}")) {
            return refuse(400, "InvalidServiceId");
        }
        let mut message = seal::POP_DST.to_vec();
        message.extend_from_slice(&self.public_key);
        message.extend_from_slice(&self.object_id);
        let pop = seal::g1_bytes(&(seal::hash_to_g1(&message) * self.master));
        Ok(json!({ "service_id": id, "pop": STANDARD.encode(pop) }))
    }

    /// The package's original ID is this namespace (it is that package or an upgrade of it).
    async fn upgrades_namespace(&self, package: &Address) -> bool {
        if *package == self.namespace {
            return true;
        }
        let mut request = proto::GetObjectRequest::default();
        request.object_id = Some(package.to_string());
        request.read_mask = Some(FieldMask::from_paths(["bcs"]));
        let Ok(response) = self.rpc.clone().ledger_client().get_object(request).await else { return false };
        let Some(bytes) = response.into_inner().object.and_then(|o| o.bcs).and_then(|b| b.value) else { return false };
        let Ok(object) = bcs::from_bytes::<sui_sdk_types::Object>(&bytes) else { return false };
        match object.data() {
            sui_sdk_types::ObjectData::Package(package) => package.type_origin_table.iter().any(|origin| origin.package == self.namespace),
            _ => false,
        }
    }

    async fn fetch_key(&self, body: &Value) -> Reply {
        let ptb_bytes = decode(&body["ptb"])?;
        let ptb: ProgrammableTransaction = bcs::from_bytes(&ptb_bytes).or_else(|_| refuse(400, "InvalidPTB: invalid BCS"))?;
        let mut ids = Vec::new();
        let mut package = None;
        if ptb.commands.is_empty() || ptb.commands.len() > 100 || ptb.inputs.is_empty() {
            return refuse(400, "InvalidPTB: empty or too long");
        }
        for command in &ptb.commands {
            let Command::MoveCall(call) = command else { return refuse(400, "InvalidPTB: non MoveCall command") };
            if !call.function.as_str().starts_with("seal_approve") || *package.get_or_insert(call.package) != call.package {
                return refuse(400, "InvalidPTB: invalid function or package id");
            }
            if call.arguments.iter().any(|a| !matches!(a, Argument::Input(_))) {
                return refuse(400, "InvalidPTB: only inputs are allowed");
            }
            let id = match call.arguments.first().and_then(|a| match a { Argument::Input(i) => ptb.inputs.get(*i as usize), _ => None }) {
                Some(Input::Pure(bytes)) => bcs::from_bytes::<Vec<u8>>(bytes).or_else(|_| refuse(400, "InvalidPTB: invalid identity"))?,
                _ => return refuse(400, "InvalidPTB: invalid first parameter for seal_approve"),
            };
            ids.push(id);
        }
        if !self.upgrades_namespace(&package.unwrap()).await {
            return refuse(403, "UnsupportedPackageId");
        }
        let enc_key = decode(&body["enc_key"])?;
        let enc_verification_key = decode(&body["enc_verification_key"])?;
        let enc_public = seal::g1(&enc_key).or_else(|_| refuse(400, "InvalidParameter: enc_key"))?;
        let certificate = &body["certificate"];
        let user = Address::from_str(certificate["user"].as_str().unwrap_or_default()).or_else(|_| refuse(400, "InvalidCertificate"))?;
        let session_key: [u8; 32] = decode(&certificate["session_vk"])?.try_into().or_else(|_| refuse(400, "InvalidCertificate"))?;
        let creation = certificate["creation_time"].as_u64().unwrap_or(u64::MAX);
        let ttl = certificate["ttl_min"].as_u64().unwrap_or(u64::MAX);
        let now = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).unwrap().as_millis() as u64;
        if ttl > self.max_ttl_minutes || creation > now || now - creation > ttl * 60_000 {
            return refuse(403, "InvalidCertificate: expired, future or too long");
        }
        let message = format!(
            "Accessing keys of package {} for {ttl} mins from {}, session key {}",
            self.namespace,
            utc(creation),
            STANDARD.encode(session_key)
        );
        let signature = UserSignature::from_bytes(&decode(&certificate["signature"])?).or_else(|_| refuse(403, "InvalidSignature"))?;
        let signer = match &signature {
            UserSignature::Simple(sui_sdk_types::SimpleSignature::Ed25519 { public_key, .. }) => public_key.derive_address(),
            UserSignature::Simple(sui_sdk_types::SimpleSignature::Secp256r1 { public_key, .. }) => public_key.derive_address(),
            UserSignature::Simple(sui_sdk_types::SimpleSignature::Secp256k1 { public_key, .. }) => public_key.derive_address(),
            _ => return refuse(403, "InvalidSignature: unsupported scheme"),
        };
        if signer != user
            || sui_crypto::UserSignatureVerifier::new()
                .verify_personal_message(&PersonalMessage(message.as_bytes().into()), &signature)
                .is_err()
        {
            return refuse(403, "InvalidSignature");
        }
        let mut request_message = Vec::new();
        for part in [&ptb_bytes, &enc_key, &enc_verification_key] {
            bcs::serialize_into(&mut request_message, part).unwrap();
        }
        let request_signature = ed25519_dalek::Signature::from_slice(&decode(&body["request_signature"])?).or_else(|_| refuse(403, "InvalidSessionSignature"))?;
        let session = ed25519_dalek::VerifyingKey::from_bytes(&session_key).or_else(|_| refuse(403, "InvalidSessionSignature"))?;
        if session.verify(&request_message, &request_signature).is_err() {
            return refuse(403, "InvalidSessionSignature");
        }
        self.check_policy(user, ptb).await?;
        let keys: Vec<Value> = ids
            .into_iter()
            .map(|id| {
                let full_id = [self.namespace.into_inner().as_slice(), &id].concat();
                let key = seal::hash_identity(&full_id) * self.master;
                let r = seal::random_scalar();
                let c0 = seal::g1_bytes(&(ark_bls12_381::G1Projective::from(ark_bls12_381::G1Affine::generator()) * r));
                let c1 = seal::g1_bytes(&(key + enc_public * r));
                json!({ "id": full_id, "encrypted_key": [STANDARD.encode(c0), STANDARD.encode(c1)] })
            })
            .collect();
        Ok(json!({ "decryption_keys": keys }))
    }

    /// Simulate the approval calls as the certificate's user; any abort denies.
    async fn check_policy(&self, sender: Address, ptb: ProgrammableTransaction) -> Result<(), (u16, String)> {
        let transaction = Transaction {
            kind: TransactionKind::ProgrammableTransaction(ptb),
            sender,
            gas_payment: GasPayment { objects: vec![], owner: sender, price: 1000, budget: 55_000_000 },
            expiration: TransactionExpiration::None,
        };
        let mut request = proto::SimulateTransactionRequest::default();
        let mut tx = proto::Transaction::default();
        let mut bcs_value = proto::Bcs::default();
        bcs_value.value = Some(bcs::to_bytes(&transaction).unwrap().into());
        tx.bcs = Some(bcs_value);
        request.transaction = Some(tx);
        request.checks = Some(proto::simulate_transaction_request::TransactionChecks::Disabled as i32);
        request.read_mask = Some(FieldMask::from_paths(["transaction.effects.status"]));
        let response = self
            .rpc
            .clone()
            .execution_client()
            .simulate_transaction(request)
            .await
            .map_err(|e| (503, format!("Failure: simulation failed: {}", e.message())))?
            .into_inner();
        let status = response.transaction.and_then(|t| t.effects).and_then(|e| e.status);
        match status {
            Some(status) if status.success == Some(true) => Ok(()),
            Some(status) => refuse(403, format!("NoAccess: {:?}", status.error.and_then(|e| e.description))),
            None => refuse(403, "NoAccess: no simulation status"),
        }
    }
}

async fn handle(server: Arc<Server>, mut socket: tokio::net::TcpStream) {
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 8192];
    let (head, body) = loop {
        let Ok(n) = socket.read(&mut chunk).await else { return };
        if n == 0 {
            return;
        }
        buffer.extend_from_slice(&chunk[..n]);
        if let Some(end) = buffer.windows(4).position(|w| w == b"\r\n\r\n") {
            let head = String::from_utf8_lossy(&buffer[..end]).into_owned();
            let length = head
                .lines()
                .find_map(|line| line.to_ascii_lowercase().strip_prefix("content-length:").map(|v| v.trim().parse::<usize>().unwrap_or(0)))
                .unwrap_or(0);
            if length > 256 * 1024 {
                return;
            }
            while buffer.len() < end + 4 + length {
                let Ok(n) = socket.read(&mut chunk).await else { return };
                if n == 0 {
                    return;
                }
                buffer.extend_from_slice(&chunk[..n]);
            }
            break (head, buffer[end + 4..end + 4 + length].to_vec());
        }
        if buffer.len() > 64 * 1024 {
            return;
        }
    };
    let request_line = head.lines().next().unwrap_or_default().to_owned();
    let mut parts = request_line.split_whitespace();
    let (method, target) = (parts.next().unwrap_or_default(), parts.next().unwrap_or_default());
    let has_version = head.lines().any(|line| line.to_ascii_lowercase().starts_with("client-sdk-version:"));
    let reply = if !has_version {
        refuse(400, "MissingRequiredHeader")
    } else {
        match (method, target.split_once('?').map(|(p, q)| (p, q)).unwrap_or((target, ""))) {
            ("GET", ("/v1/service", query)) => server.service(query),
            ("POST", ("/v1/fetch_key", _)) => match serde_json::from_slice::<Value>(&body) {
                Ok(body) => {
                    // Counted by the harness: requests are billed per server.
                    // One write, so concurrent servers' lines never interleave.
                    let line = format!("fetch_key 0x{}\n", hex::encode(server.object_id));
                    let _ = std::io::Write::write_all(&mut std::io::stderr(), line.as_bytes());
                    server.fetch_key(&body).await
                }
                Err(_) => refuse(400, "invalid JSON"),
            },
            _ => refuse(404, "not found"),
        }
    };
    let (status, body) = match reply {
        Ok(value) => (200, value.to_string()),
        Err((status, message)) => {
            eprintln!("refused: {message}");
            // Seal's error body: a code the SDKs map to error types, and details.
            let (code, detail) = message.split_once(": ").unwrap_or((message.as_str(), ""));
            (status, json!({ "error": code, "message": detail }).to_string())
        }
    };
    let response = format!(
        "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nX-KeyServer-Version: 0.4.1\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = socket.write_all(response.as_bytes()).await;
    let _ = socket.shutdown().await;
}

fn master_key(seed_hex: &str) -> anyhow::Result<(ark_bls12_381::Fr, [u8; 96])> {
    use sha2::Digest;
    let master = ark_ff::PrimeField::from_le_bytes_mod_order(&sha2::Sha512::digest(hex::decode(seed_hex)?));
    let generator = ark_bls12_381::G2Projective::from(ark_bls12_381::G2Affine::generator());
    Ok((master, seal::g2_bytes(&(generator * master))))
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("public-key") && args.len() == 3 {
        println!("{}", hex::encode(master_key(&args[2])?.1));
        return Ok(());
    }
    anyhow::ensure!(
        args.len() >= 7 && args[1] == "serve",
        "usage: seal_key_server public-key SEED | serve RPC_URL NAMESPACE_PACKAGE KEY_SERVER_OBJECT_ID SEED PORT [--max-ttl-minutes N]"
    );
    let host = reqwest::Url::parse(&args[2])?.host_str().unwrap_or_default().to_owned();
    anyhow::ensure!(matches!(host.as_str(), "127.0.0.1" | "localhost" | "[::1]"), "test key server: loopback chains only");
    let (master, public_key) = master_key(&args[5])?;
    let max_ttl_minutes = match args.iter().position(|a| a == "--max-ttl-minutes") {
        Some(i) => args.get(i + 1).ok_or_else(|| anyhow::anyhow!("--max-ttl-minutes needs a value"))?.parse()?,
        None => 30,
    };
    let server = Arc::new(Server {
        rpc: sui_rpc::Client::new(args[2].as_str())?,
        namespace: Address::from_str(&args[3])?,
        object_id: Address::from_str(&args[4])?.into_inner(),
        master,
        public_key,
        max_ttl_minutes,
    });
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", args[6].parse::<u16>()?)).await?;
    println!("listening {}", listener.local_addr()?.port());
    use std::io::Write;
    std::io::stdout().flush()?;
    loop {
        let (socket, _) = listener.accept().await?;
        tokio::spawn(handle(server.clone(), socket));
    }
}
