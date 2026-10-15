//! `suifleet-indexer ctl`: the operations CLI, an API client.
//!
//! Lists follow pagination to the end and print one JSON object per line.
//! Bulk operations are built by the API; with `--key` the CLI checks the
//! returned transaction against the operator's own manifest (sender, gas
//! owner and budget, and only the operation's calls into the fleet package)
//! before signing and executing it, and reads success from its effects.
use crate::chain::{self, Chain};
use crate::manifest::Manifest;
use crate::txbuild;
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;
use sui_crypto::SuiSigner;
use sui_sdk_types::{Command, Transaction, TransactionKind};

pub const USAGE: &str = "usage: suifleet-indexer ctl [--api URL] [--token-file FILE] COMMAND
  status
  devices      [--owner ADDR] [--group ID] [--include-deleted]
  groups       [--admin ADDR] [--device ID] [--include-deleted]
  deployments  [--creator ADDR] [--component NAME] [--group ID] [--device ID] [--include-deleted]
  device|group|deployment ID
  changes      [--after SEQ] [--follow]
  add-devices    --sender ADDR --group ID DEVICE...           (bulk)
  remove-devices --sender ADDR --group ID DEVICE...           (bulk)
  create-deployment --sender ADDR --component NAME --version V --recipe-blob ID
                    --artifact-sha256 HEX --recipe-sha256 HEX [--target-group ID]... [--target-device ID]...
Bulk commands write the unsigned transaction to --out FILE, or with
--key FILE (32-byte hex ed25519 seed) --manifest deployment.json
[--max-gas MIST] sign and execute it.";

/// Default ceiling on a bulk transaction's gas budget (0.5 SUI).
const MAX_GAS: u64 = 500_000_000;

struct Args {
    flags: BTreeMap<String, Vec<String>>,
    positional: Vec<String>,
}

impl Args {
    fn parse(raw: &[String]) -> anyhow::Result<Self> {
        let switches = ["--include-deleted", "--follow"];
        let mut flags: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut positional = Vec::new();
        let mut items = raw.iter();
        while let Some(item) = items.next() {
            if switches.contains(&item.as_str()) {
                flags.entry(item.clone()).or_default().push("true".into());
            } else if item.starts_with("--") {
                let value = items
                    .next()
                    .ok_or_else(|| anyhow::anyhow!("{item} needs a value\n{USAGE}"))?;
                flags.entry(item.clone()).or_default().push(value.clone());
            } else {
                positional.push(item.clone());
            }
        }
        Ok(Self { flags, positional })
    }
    fn one(&self, name: &str) -> Option<&str> {
        self.flags
            .get(name)
            .and_then(|v| v.last())
            .map(String::as_str)
    }
    fn all(&self, name: &str) -> Vec<String> {
        self.flags.get(name).cloned().unwrap_or_default()
    }
    fn has(&self, name: &str) -> bool {
        self.flags.contains_key(name)
    }
    fn required(&self, name: &str) -> anyhow::Result<&str> {
        self.one(name)
            .ok_or_else(|| anyhow::anyhow!("{name} is required\n{USAGE}"))
    }
}

struct Api {
    base: String,
    token: Option<String>,
    http: reqwest::Client,
}

impl Api {
    async fn get(&self, path: &str, query: &[(&str, String)]) -> anyhow::Result<Value> {
        let mut request = self.http.get(format!("{}{path}", self.base)).query(query);
        if let Some(token) = &self.token {
            request = request.bearer_auth(token);
        }
        Self::body(request.send().await?).await
    }
    async fn post(&self, path: &str, body: &Value) -> anyhow::Result<Value> {
        let mut request = self.http.post(format!("{}{path}", self.base)).json(body);
        if let Some(token) = &self.token {
            request = request.bearer_auth(token);
        }
        Self::body(request.send().await?).await
    }
    async fn body(response: reqwest::Response) -> anyhow::Result<Value> {
        let status = response.status();
        let body: Value = response.json().await.unwrap_or(Value::Null);
        anyhow::ensure!(
            status.is_success(),
            "API {status}: {}",
            body["error"].as_str().unwrap_or("no detail")
        );
        Ok(body)
    }
}

/// Check an API-built transaction before signing it: it must be exactly the
/// requested kind of operation, from `sender`, paid by `sender`, within budget.
pub fn verify(
    transaction: &Transaction,
    operation: &str,
    package: &str,
    sender: &str,
    max_gas: u64,
) -> anyhow::Result<()> {
    let allowed = txbuild::allowed(operation)
        .ok_or_else(|| anyhow::anyhow!("unknown operation {operation}"))?;
    anyhow::ensure!(
        chain::same_id(&transaction.sender.to_string(), sender),
        "transaction sender is not {sender}"
    );
    anyhow::ensure!(
        chain::same_id(&transaction.gas_payment.owner.to_string(), sender),
        "gas is not paid by {sender}"
    );
    anyhow::ensure!(
        transaction.gas_payment.budget <= max_gas,
        "gas budget {} exceeds the ceiling {max_gas}",
        transaction.gas_payment.budget
    );
    let TransactionKind::ProgrammableTransaction(program) = &transaction.kind else {
        anyhow::bail!("not a programmable transaction");
    };
    anyhow::ensure!(!program.commands.is_empty(), "transaction has no commands");
    for command in &program.commands {
        let Command::MoveCall(call) = command else {
            anyhow::bail!("transaction contains a non-call command: {command:?}");
        };
        anyhow::ensure!(
            chain::same_id(&call.package.to_string(), package),
            "call into another package {}",
            call.package
        );
        anyhow::ensure!(
            allowed
                .iter()
                .any(|(module, function)| call.module.as_str() == *module
                    && call.function.as_str() == *function),
            "{operation} does not call {}::{}",
            call.module,
            call.function
        );
        anyhow::ensure!(call.type_arguments.is_empty(), "unexpected type arguments");
    }
    Ok(())
}

fn load_key(path: &str) -> anyhow::Result<sui_crypto::ed25519::Ed25519PrivateKey> {
    let seed: [u8; 32] = hex::decode(std::fs::read_to_string(path)?.trim())?
        .try_into()
        .map_err(|_| anyhow::anyhow!("key file must hold a 32-byte hex ed25519 seed"))?;
    Ok(sui_crypto::ed25519::Ed25519PrivateKey::new(seed))
}

async fn bulk(api: &Api, args: &Args, operation: &str, body: Value) -> anyhow::Result<()> {
    let built = api
        .post(&format!("/v1/transactions/{operation}"), &body)
        .await?;
    let bytes = STANDARD.decode(built["transaction"].as_str().unwrap_or_default())?;
    let transaction: Transaction = bcs::from_bytes(&bytes)?;
    anyhow::ensure!(
        transaction.digest().to_string() == built["digest"].as_str().unwrap_or_default(),
        "API digest does not match the transaction bytes"
    );
    eprintln!("{}", serde_json::to_string_pretty(&built["summary"])?);
    if let Some(out) = args.one("--out") {
        std::fs::write(out, serde_json::to_string_pretty(&built)? + "\n")?;
        eprintln!(
            "Unsigned transaction written to {out} ({})",
            built["digest"].as_str().unwrap_or_default()
        );
        return Ok(());
    }
    let key = load_key(
        args.one("--key")
            .ok_or_else(|| anyhow::anyhow!("pass --out FILE or --key FILE\n{USAGE}"))?,
    )?;
    let manifest = Manifest::load(&PathBuf::from(args.required("--manifest")?))?;
    let max_gas = args
        .one("--max-gas")
        .map(str::parse)
        .transpose()?
        .unwrap_or(MAX_GAS);
    let sender = key.public_key().derive_address().to_string();
    anyhow::ensure!(
        chain::same_id(&sender, body["sender"].as_str().unwrap_or_default()),
        "--key is not the --sender's key"
    );
    verify(
        &transaction,
        operation,
        &manifest.call_package_id,
        &sender,
        max_gas,
    )?;
    let signature = key
        .sign_transaction(&transaction)
        .map_err(|e| anyhow::anyhow!("signing failed: {e}"))?;
    let digest = Chain::new(&manifest.rpc_url)?
        .execute(&transaction, &signature)
        .await?;
    println!(
        "{}",
        json!({ "executed": digest, "summary": built["summary"] })
    );
    Ok(())
}

async fn list(api: &Api, path: &str, query: Vec<(&str, String)>) -> anyhow::Result<()> {
    let mut cursor: Option<String> = None;
    loop {
        let mut page_query = query.clone();
        if let Some(cursor) = &cursor {
            page_query.push(("cursor", cursor.clone()));
        }
        let page = api.get(path, &page_query).await?;
        for item in page["items"].as_array().into_iter().flatten() {
            println!("{item}");
        }
        match page["next"].as_str() {
            Some(next) => cursor = Some(next.to_owned()),
            None => return Ok(()),
        }
    }
}

pub async fn main(raw: &[String]) -> anyhow::Result<()> {
    let args = Args::parse(raw)?;
    let token = match args.one("--token-file") {
        Some(path) => Some(std::fs::read_to_string(path)?.trim().to_owned()),
        None => std::env::var("SUIFLEET_API_TOKEN").ok(),
    };
    let api = Api {
        base: args
            .one("--api")
            .unwrap_or("http://127.0.0.1:8650")
            .trim_end_matches('/')
            .to_owned(),
        token,
        http: reqwest::Client::new(),
    };
    let filters = |names: &[(&'static str, &'static str)]| -> Vec<(&'static str, String)> {
        let mut query: Vec<(&str, String)> = names
            .iter()
            .filter_map(|(flag, param)| args.one(flag).map(|v| (*param, v.to_owned())))
            .collect();
        if args.has("--include-deleted") {
            query.push(("include_deleted", "true".into()));
        }
        query
    };
    let command = args.positional.first().map(String::as_str).unwrap_or("");
    match command {
        "status" => println!("{}", api.get("/v1/status", &[]).await?),
        "devices" => {
            list(
                &api,
                "/v1/devices",
                filters(&[("--owner", "owner"), ("--group", "group")]),
            )
            .await?
        }
        "groups" => {
            list(
                &api,
                "/v1/groups",
                filters(&[("--admin", "admin"), ("--device", "device")]),
            )
            .await?
        }
        "deployments" => {
            list(
                &api,
                "/v1/deployments",
                filters(&[
                    ("--creator", "creator"),
                    ("--component", "component"),
                    ("--group", "group"),
                    ("--device", "device"),
                ]),
            )
            .await?
        }
        "device" | "group" | "deployment" => {
            let id = args
                .positional
                .get(1)
                .ok_or_else(|| anyhow::anyhow!("{command} needs an ID\n{USAGE}"))?;
            println!("{}", api.get(&format!("/v1/{command}s/{id}"), &[]).await?);
        }
        "changes" => {
            let mut after: i64 = args
                .one("--after")
                .map(str::parse)
                .transpose()?
                .unwrap_or(0);
            loop {
                let wait = if args.has("--follow") { "25" } else { "0" };
                let page = api
                    .get(
                        "/v1/changes",
                        &[("after", after.to_string()), ("wait", wait.into())],
                    )
                    .await?;
                for item in page["items"].as_array().into_iter().flatten() {
                    println!("{item}");
                }
                let next = page["next"].as_i64().unwrap_or(after);
                if next == after && !args.has("--follow") {
                    return Ok(());
                }
                after = next;
            }
        }
        "add-devices" | "remove-devices" => {
            let body = json!({ "sender": args.required("--sender")?, "group": args.required("--group")?,
                               "devices": &args.positional[1..] });
            bulk(&api, &args, command, body).await?
        }
        "create-deployment" => {
            let body = json!({
                "sender": args.required("--sender")?, "component_name": args.required("--component")?,
                "version": args.required("--version")?, "recipe_blob_id": args.required("--recipe-blob")?,
                "artifact_sha256": args.required("--artifact-sha256")?, "recipe_sha256": args.required("--recipe-sha256")?,
                "target_groups": args.all("--target-group"), "target_devices": args.all("--target-device"),
            });
            bulk(&api, &args, command, body).await?
        }
        _ => anyhow::bail!("{USAGE}"),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;
    use sui_sdk_types::{
        Address, GasPayment, Identifier, MoveCall, ProgrammableTransaction, TransactionExpiration,
    };

    const PACKAGE: &str = "0x00000000000000000000000000000000000000000000000000000000000000aa";
    const SENDER: &str = "0x00000000000000000000000000000000000000000000000000000000000000bb";

    fn call(package: &str, module: &str, function: &str) -> Command {
        Command::MoveCall(MoveCall {
            package: Address::from_str(package).unwrap(),
            module: Identifier::from_str(module).unwrap(),
            function: Identifier::from_str(function).unwrap(),
            type_arguments: vec![],
            arguments: vec![],
        })
    }

    fn transaction(commands: Vec<Command>, sender: &str, owner: &str, budget: u64) -> Transaction {
        Transaction {
            kind: TransactionKind::ProgrammableTransaction(ProgrammableTransaction {
                inputs: vec![],
                commands,
            }),
            sender: Address::from_str(sender).unwrap(),
            gas_payment: GasPayment {
                objects: vec![],
                owner: Address::from_str(owner).unwrap(),
                price: 1000,
                budget,
            },
            expiration: TransactionExpiration::None,
        }
    }

    #[test]
    fn verify_before_sign_accepts_only_the_requested_operation() {
        let add = || call(PACKAGE, "device_group", "add_device");
        assert!(verify(
            &transaction(vec![add(), add()], SENDER, SENDER, 1_000),
            "add-devices",
            PACKAGE,
            SENDER,
            MAX_GAS
        )
        .is_ok());
        let cases = [
            (
                transaction(vec![add()], SENDER, SENDER, 1_000),
                "remove-devices",
                "does not call",
            ),
            (
                transaction(
                    vec![call(PACKAGE, "device_group", "delete_group")],
                    SENDER,
                    SENDER,
                    1_000,
                ),
                "add-devices",
                "does not call",
            ),
            (
                transaction(
                    vec![call("0x2", "device_group", "add_device")],
                    SENDER,
                    SENDER,
                    1_000,
                ),
                "add-devices",
                "another package",
            ),
            (
                transaction(
                    vec![
                        add(),
                        Command::TransferObjects(sui_sdk_types::TransferObjects {
                            objects: vec![],
                            address: sui_sdk_types::Argument::Gas,
                        }),
                    ],
                    SENDER,
                    SENDER,
                    1_000,
                ),
                "add-devices",
                "non-call",
            ),
            (
                transaction(vec![add()], PACKAGE, SENDER, 1_000),
                "add-devices",
                "sender",
            ),
            (
                transaction(vec![add()], SENDER, PACKAGE, 1_000),
                "add-devices",
                "gas is not paid",
            ),
            (
                transaction(vec![add()], SENDER, SENDER, MAX_GAS + 1),
                "add-devices",
                "exceeds",
            ),
            (
                transaction(vec![], SENDER, SENDER, 1_000),
                "add-devices",
                "no commands",
            ),
        ];
        for (tx, operation, needle) in cases {
            let error = verify(&tx, operation, PACKAGE, SENDER, MAX_GAS)
                .expect_err(needle)
                .to_string();
            assert!(error.contains(needle), "{error} does not mention {needle}");
        }
    }
}
