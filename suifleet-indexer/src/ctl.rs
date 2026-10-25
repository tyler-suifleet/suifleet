//! `suifleet-indexer ctl`: the operations CLI, an API client.
//!
//! Lists follow pagination to the end and print one JSON object per line.
//! Bulk operations are built by the API, which is not trusted with intent:
//! with `--manifest` the CLI verifies the manifest against the chain, reads
//! the requested devices itself (custody and owner caps), rebuilds the exact
//! calls from the operator's own request and requires the returned
//! transaction to be exactly those calls into the manifest's package, every
//! argument bound to the requested value, object or earlier result, from and
//! paid by the sender within the gas ceiling. Only then does `--key` sign and
//! execute it, reading success from its effects.
use crate::chain::{self, Chain};
use crate::manifest::Manifest;
use crate::txbuild;
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;
use sui_crypto::SuiSigner;
use std::str::FromStr;
use sui_sdk_types::{Address, Argument, Command, Input, Transaction, TransactionKind};

pub const USAGE: &str = "usage: suifleet-indexer ctl [--api URL] [--token-file FILE] COMMAND
  status
  devices      [--owner ADDR] [--group ID] [--include-deleted]
  groups       [--admin ADDR] [--device ID] [--include-deleted]
  deployments  [--creator ADDR] [--component NAME] [--group ID] [--device ID] [--include-deleted]
  device|group|deployment ID
  changes      [--after SEQ] [--follow]
  alerts       [--owner ADDR] [--group ID]
  logs DEVICE  [--q TEXT] [--source daemon|component:NAME] [--since-ms MS] [--max N]   (support token)
  diagnostics DEVICE [--out FILE]                                                (support token)
  audit        [--limit N]                                                       (operate token)
  add-devices    --sender ADDR --group ID DEVICE...           (bulk)
  remove-devices --sender ADDR --group ID DEVICE...           (bulk)
  create-deployment --sender ADDR --component NAME --version V --recipe-blob ID
                    --artifact-sha256 HEX --recipe-sha256 HEX [--target-group ID]... [--target-device ID]...
Bulk commands write the unsigned transaction to --out FILE, or with
--key FILE (32-byte hex ed25519 seed) sign and execute it. Either way,
--manifest deployment.json [--max-gas MIST] first checks that the API's
transaction is exactly the requested calls and arguments (required with --key).";

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

/// Whether transaction input `input` is exactly the planned argument.
fn input_matches(input: &Input, expected: &txbuild::Argument) -> anyhow::Result<bool> {
    use txbuild::Argument as Planned;
    let address = |id: &str| Address::from_str(id).map_err(anyhow::Error::from);
    Ok(match (expected, input) {
        (Planned::Object(id), Input::ImmutableOrOwned(object)) => {
            *object.object_id() == address(id)?
        }
        (Planned::Object(id), Input::Shared(object)) => object.object_id() == address(id)?,
        (Planned::Clock, Input::Shared(object)) => {
            object.object_id() == address("0x6")? && !object.mutability().is_mutable()
        }
        (Planned::Text(value), Input::Pure(bytes)) => *bytes == bcs::to_bytes(value)?,
        (Planned::Id(id), Input::Pure(bytes)) => *bytes == bcs::to_bytes(&address(id)?)?,
        (Planned::Ids(ids), Input::Pure(bytes)) => {
            let ids: Vec<Address> = ids.iter().map(|id| address(id)).collect::<Result<_, _>>()?;
            *bytes == bcs::to_bytes(&ids)?
        }
        _ => false,
    })
}

/// Check an API-built transaction before signing it: it must be exactly
/// `expected` (rebuilt locally from the operator's request): the same calls in
/// order, each argument an input holding the requested value or object, no
/// other commands or inputs, from and paid by `sender`, within budget.
pub fn verify(
    transaction: &Transaction,
    expected: &[txbuild::Call],
    package: &str,
    sender: &str,
    max_gas: u64,
) -> anyhow::Result<()> {
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
    anyhow::ensure!(
        program.commands.len() == expected.len(),
        "transaction has {} commands but the request needs {}",
        program.commands.len(),
        expected.len()
    );
    let mut used = vec![false; program.inputs.len()];
    for (index, (command, (module, function, arguments))) in
        program.commands.iter().zip(expected).enumerate()
    {
        let Command::MoveCall(call) = command else {
            anyhow::bail!("transaction contains a non-call command: {command:?}");
        };
        anyhow::ensure!(
            chain::same_id(&call.package.to_string(), package),
            "call into another package {}",
            call.package
        );
        anyhow::ensure!(
            call.module.as_str() == *module && call.function.as_str() == *function,
            "command {index} calls {}::{}, not the requested {module}::{function}",
            call.module,
            call.function
        );
        anyhow::ensure!(call.type_arguments.is_empty(), "unexpected type arguments");
        anyhow::ensure!(
            call.arguments.len() == arguments.len(),
            "command {index} has {} arguments, not {}",
            call.arguments.len(),
            arguments.len()
        );
        for (position, (argument, wanted)) in call.arguments.iter().zip(arguments).enumerate() {
            let input = match (argument, wanted) {
                (Argument::Input(input), _) => *input as usize,
                (Argument::NestedResult(command, result), txbuild::Argument::Result(c, r))
                    if (command, result) == (c, r) =>
                {
                    continue
                }
                _ => anyhow::bail!(
                    "command {index} argument {position} is not the requested {wanted:?}: {argument:?}"
                ),
            };
            let value = program.inputs.get(input).ok_or_else(|| {
                anyhow::anyhow!("command {index} argument {position} names a missing input")
            })?;
            anyhow::ensure!(
                input_matches(value, wanted)?,
                "command {index} argument {position} is not the requested {wanted:?}"
            );
            used[input] = true;
        }
    }
    anyhow::ensure!(
        used.iter().all(|u| *u),
        "transaction has inputs the request does not use"
    );
    Ok(())
}

fn load_key(path: &str) -> anyhow::Result<sui_crypto::ed25519::Ed25519PrivateKey> {
    let seed: [u8; 32] = hex::decode(std::fs::read_to_string(path)?.trim())?
        .try_into()
        .map_err(|_| anyhow::anyhow!("key file must hold a 32-byte hex ed25519 seed"))?;
    Ok(sui_crypto::ed25519::Ed25519PrivateKey::new(seed))
}

/// The calls `request` needs, from the operator's own reads of the chain the
/// manifest names (verified first): which devices are in custody, and their
/// owner caps.
async fn expected_calls(
    manifest: &Manifest,
    operation: &str,
    request: &txbuild::Request,
) -> anyhow::Result<Vec<txbuild::Call>> {
    let chain = Chain::new(&manifest.rpc_url)?;
    let types = manifest.verify(&chain).await?;
    let registry = chain::normalize(&manifest.shared_objects.registry.id).unwrap_or_default();
    let ids = txbuild::devices_to_check(operation, request);
    let read = crate::devices::read(&chain, &types, &registry, &ids).await?;
    let live: BTreeMap<_, _> = ids.into_iter().zip(read).collect();
    let inboxes = crate::devices::inboxes(&chain, &types, &registry, &txbuild::inbox_devices(operation, request)).await?;
    txbuild::calls(operation, request, &registry, &crate::devices::owner_caps(&live), &inboxes)
}

async fn bulk(api: &Api, args: &Args, operation: &str, request: txbuild::Request) -> anyhow::Result<()> {
    // Reject a malformed request before asking the API.
    txbuild::calls(operation, &request, "", &BTreeMap::new(), &None)?;
    let built = api
        .post(
            &format!("/v1/transactions/{operation}"),
            &serde_json::to_value(&request)?,
        )
        .await?;
    let bytes = STANDARD.decode(built["transaction"].as_str().unwrap_or_default())?;
    let transaction: Transaction = bcs::from_bytes(&bytes)?;
    anyhow::ensure!(
        transaction.digest().to_string() == built["digest"].as_str().unwrap_or_default(),
        "API digest does not match the transaction bytes"
    );
    let manifest = args
        .one("--manifest")
        .map(|path| Manifest::load(&PathBuf::from(path)))
        .transpose()?;
    if let Some(manifest) = &manifest {
        let max_gas = args
            .one("--max-gas")
            .map(str::parse)
            .transpose()?
            .unwrap_or(MAX_GAS);
        // The operator's intent, rebuilt locally; the API's answer must match it.
        let expected = expected_calls(manifest, operation, &request).await?;
        verify(
            &transaction,
            &expected,
            &manifest.call_package_id,
            &request.sender,
            max_gas,
        )?;
    }
    eprintln!("{}", serde_json::to_string_pretty(&built["summary"])?);
    if let Some(out) = args.one("--out") {
        std::fs::write(out, serde_json::to_string_pretty(&built)? + "\n")?;
        eprintln!(
            "Unsigned transaction written to {out} ({}, {})",
            built["digest"].as_str().unwrap_or_default(),
            if manifest.is_some() {
                "verified against the request"
            } else {
                "NOT verified: pass --manifest to check it"
            }
        );
        return Ok(());
    }
    let key = load_key(
        args.one("--key")
            .ok_or_else(|| anyhow::anyhow!("pass --out FILE or --key FILE\n{USAGE}"))?,
    )?;
    let manifest = manifest
        .ok_or_else(|| anyhow::anyhow!("--key needs --manifest to verify the transaction"))?;
    anyhow::ensure!(
        chain::same_id(&key.public_key().derive_address().to_string(), &request.sender),
        "--key is not the --sender's key"
    );
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
        "alerts" => {
            let body = api.get("/v1/alerts", &filters(&[("--owner", "owner"), ("--group", "group")])).await?;
            for alert in body["items"].as_array().into_iter().flatten() {
                println!("{alert}");
            }
        }
        "logs" => {
            let device = args.positional.get(1).ok_or_else(|| anyhow::anyhow!("logs needs a device ID\n{USAGE}"))?;
            let max: usize = args.one("--max").map(str::parse).transpose()?.unwrap_or(1000);
            let mut query = filters(&[("--q", "q"), ("--source", "source"), ("--since-ms", "since_ms")]);
            let mut printed = 0;
            loop {
                let body = api.get(&format!("/v1/devices/{device}/logs"), &query).await?;
                for line in body["items"].as_array().into_iter().flatten() {
                    if printed == max {
                        return Ok(());
                    }
                    println!("{line}");
                    printed += 1;
                }
                match body["next"].as_str() {
                    Some(next) if printed < max => {
                        query.retain(|(name, _)| *name != "before");
                        query.push(("before", next.to_owned()));
                    }
                    _ => break,
                }
            }
        }
        "diagnostics" => {
            let device = args.positional.get(1).ok_or_else(|| anyhow::anyhow!("diagnostics needs a device ID\n{USAGE}"))?;
            let body = api.get(&format!("/v1/devices/{device}/diagnostics"), &[]).await?;
            let text = serde_json::to_string_pretty(&body)?;
            match args.one("--out") {
                Some(path) => {
                    use std::os::unix::fs::OpenOptionsExt;
                    use std::io::Write;
                    // Support data: readable by its owner only.
                    std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(path)?.write_all(text.as_bytes())?;
                    eprintln!("wrote {path}");
                }
                None => println!("{text}"),
            }
        }
        "audit" => {
            let limit = args.one("--limit").unwrap_or("100").to_owned();
            let body = api.get("/v1/support/audit", &[("limit", limit)]).await?;
            for entry in body["items"].as_array().into_iter().flatten() {
                println!("{entry}");
            }
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
            let request = txbuild::Request {
                sender: args.required("--sender")?.to_owned(),
                group: Some(args.required("--group")?.to_owned()),
                devices: args.positional[1..].to_vec(),
                ..Default::default()
            };
            bulk(&api, &args, command, request).await?
        }
        "create-deployment" => {
            let request = txbuild::Request {
                sender: args.required("--sender")?.to_owned(),
                component_name: Some(args.required("--component")?.to_owned()),
                version: Some(args.required("--version")?.to_owned()),
                recipe_blob_id: Some(args.required("--recipe-blob")?.to_owned()),
                artifact_sha256: Some(args.required("--artifact-sha256")?.to_owned()),
                recipe_sha256: Some(args.required("--recipe-sha256")?.to_owned()),
                target_groups: args.all("--target-group"),
                target_devices: args.all("--target-device"),
                ..Default::default()
            };
            bulk(&api, &args, command, request).await?
        }
        _ => anyhow::bail!("{USAGE}"),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use sui_sdk_types::{
        Digest, GasPayment, Identifier, MoveCall, ObjectReference, ProgrammableTransaction,
        SharedInput, TransactionExpiration,
    };

    const PACKAGE: &str = "0x00000000000000000000000000000000000000000000000000000000000000aa";
    const SENDER: &str = "0x00000000000000000000000000000000000000000000000000000000000000bb";

    fn hex(n: u32) -> String {
        format!("0x{n:064x}")
    }

    fn address(id: &str) -> Address {
        Address::from_str(id).unwrap()
    }

    fn call(package: &str, function: &str, arguments: &[u16]) -> Command {
        Command::MoveCall(MoveCall {
            package: address(package),
            module: Identifier::from_str("device_group").unwrap(),
            function: Identifier::from_str(function).unwrap(),
            type_arguments: vec![],
            arguments: arguments.iter().map(|i| Argument::Input(*i)).collect(),
        })
    }

    fn shared(id: &str) -> Input {
        Input::Shared(SharedInput::new(address(id), 1, true))
    }

    fn owned(id: &str) -> Input {
        Input::ImmutableOrOwned(ObjectReference::new(address(id), 1, Digest::ZERO))
    }

    fn transaction(
        inputs: Vec<Input>,
        commands: Vec<Command>,
        sender: &str,
        owner: &str,
        budget: u64,
    ) -> Transaction {
        Transaction {
            kind: TransactionKind::ProgrammableTransaction(ProgrammableTransaction {
                inputs,
                commands,
            }),
            sender: address(sender),
            gas_payment: GasPayment {
                objects: vec![],
                owner: address(owner),
                price: 1000,
                budget,
            },
            expiration: TransactionExpiration::None,
        }
    }

    /// What the operator asked for: add devices 1 and 2 to group 50.
    fn expected() -> Vec<txbuild::Call> {
        txbuild::calls(
            "add-devices",
            &txbuild::Request {
                sender: SENDER.into(),
                group: Some(hex(50)),
                devices: vec![hex(2), hex(1)],
                ..Default::default()
            },
            &hex(60),
            &BTreeMap::new(),
            &None,
        )
        .unwrap()
    }

    /// The honest API answer: inputs group, device 1, device 2.
    fn honest_inputs() -> Vec<Input> {
        vec![shared(&hex(50)), owned(&hex(1)), owned(&hex(2))]
    }

    fn honest_commands() -> Vec<Command> {
        vec![
            call(PACKAGE, "add_device", &[0, 1]),
            call(PACKAGE, "add_device", &[0, 2]),
        ]
    }

    fn check(tx: &Transaction, expected: &[txbuild::Call]) -> anyhow::Result<()> {
        verify(tx, expected, PACKAGE, SENDER, MAX_GAS)
    }

    #[test]
    fn verify_before_sign_accepts_exactly_the_request() {
        let honest = transaction(honest_inputs(), honest_commands(), SENDER, SENDER, 1_000);
        check(&honest, &expected()).unwrap();
        let swap = |inputs: Vec<Input>, commands: Vec<Command>| {
            transaction(inputs, commands, SENDER, SENDER, 1_000)
        };
        let cases = [
            // Same function, another group or device than requested.
            (
                swap(
                    vec![shared(&hex(51)), owned(&hex(1)), owned(&hex(2))],
                    honest_commands(),
                ),
                "argument 0",
            ),
            (
                swap(
                    vec![shared(&hex(50)), owned(&hex(1)), owned(&hex(3))],
                    honest_commands(),
                ),
                "argument 1",
            ),
            // The same pure bytes cannot stand in for an object.
            (
                swap(
                    vec![
                        shared(&hex(50)),
                        owned(&hex(1)),
                        Input::Pure(bcs::to_bytes(&address(&hex(2))).unwrap()),
                    ],
                    honest_commands(),
                ),
                "argument 1",
            ),
            // An extra call, a missing call, a reordered call.
            (
                swap(
                    honest_inputs(),
                    vec![
                        call(PACKAGE, "add_device", &[0, 1]),
                        call(PACKAGE, "add_device", &[0, 2]),
                        call(PACKAGE, "add_device", &[0, 2]),
                    ],
                ),
                "3 commands",
            ),
            (
                swap(honest_inputs(), vec![call(PACKAGE, "add_device", &[0, 1])]),
                "1 commands",
            ),
            (
                swap(
                    honest_inputs(),
                    vec![
                        call(PACKAGE, "add_device", &[0, 2]),
                        call(PACKAGE, "add_device", &[0, 1]),
                    ],
                ),
                "argument 1",
            ),
            // Another function, package, argument shape or input set.
            (
                swap(
                    honest_inputs(),
                    vec![
                        call(PACKAGE, "add_device", &[0, 1]),
                        call(PACKAGE, "remove_device", &[0, 2]),
                    ],
                ),
                "not the requested",
            ),
            (
                swap(
                    honest_inputs(),
                    vec![call(PACKAGE, "add_device", &[0, 1]), call("0x2", "add_device", &[0, 2])],
                ),
                "another package",
            ),
            (
                swap(
                    honest_inputs(),
                    vec![call(PACKAGE, "add_device", &[0, 1]), call(PACKAGE, "add_device", &[0])],
                ),
                "arguments",
            ),
            (
                swap(
                    {
                        let mut inputs = honest_inputs();
                        inputs.push(owned(&hex(9)));
                        inputs
                    },
                    honest_commands(),
                ),
                "inputs the request does not use",
            ),
            (
                swap(
                    honest_inputs(),
                    vec![
                        call(PACKAGE, "add_device", &[0, 1]),
                        Command::TransferObjects(sui_sdk_types::TransferObjects {
                            objects: vec![Argument::Input(2)],
                            address: Argument::Gas,
                        }),
                    ],
                ),
                "non-call",
            ),
            (
                transaction(honest_inputs(), honest_commands(), PACKAGE, SENDER, 1_000),
                "sender",
            ),
            (
                transaction(honest_inputs(), honest_commands(), SENDER, PACKAGE, 1_000),
                "gas is not paid",
            ),
            (
                transaction(honest_inputs(), honest_commands(), SENDER, SENDER, MAX_GAS + 1),
                "exceeds",
            ),
        ];
        for (tx, needle) in cases {
            let error = check(&tx, &expected()).expect_err(needle).to_string();
            assert!(error.contains(needle), "{error} does not mention {needle}");
        }
    }

    #[test]
    fn verify_binds_custody_borrows_to_the_devices_owner_cap() {
        let (registry, group, cap) = (hex(60), hex(50), hex(804));
        let request = txbuild::Request {
            sender: SENDER.into(),
            group: Some(group.clone()),
            devices: vec![hex(4)],
            ..Default::default()
        };
        let caps = BTreeMap::from([(hex(4), cap.clone())]);
        let expected = txbuild::calls("add-devices", &request, &registry, &caps, &None).unwrap();
        let in_module = |module: &str, function: &str, arguments: Vec<Argument>| {
            Command::MoveCall(MoveCall {
                package: address(PACKAGE),
                module: Identifier::from_str(module).unwrap(),
                function: Identifier::from_str(function).unwrap(),
                type_arguments: vec![],
                arguments,
            })
        };
        let build = |cap: &str, add: Argument, loan: Argument| {
            transaction(
                vec![shared(&registry), owned(cap), shared(&group)],
                vec![
                    in_module("device_registry", "borrow_device", vec![Argument::Input(0), Argument::Input(1)]),
                    in_module("device_group", "add_device", vec![Argument::Input(2), add]),
                    in_module(
                        "device_registry",
                        "return_device",
                        vec![Argument::Input(0), Argument::NestedResult(0, 0), loan],
                    ),
                ],
                SENDER,
                SENDER,
                1_000,
            )
        };
        let (device, loan) = (Argument::NestedResult(0, 0), Argument::NestedResult(0, 1));
        check(&build(&cap, device, loan), &expected).unwrap();
        // Another device's cap, the loan passed as the device, a whole-result
        // argument, or an input in place of the borrowed device.
        for (tx, needle) in [
            (build(&hex(805), device, loan), "argument 1"),
            (build(&cap, loan, loan), "command 1 argument 1"),
            (build(&cap, device, Argument::Result(0)), "command 2 argument 2"),
            (build(&cap, Argument::Input(1), loan), "command 1 argument 1"),
        ] {
            let error = check(&tx, &expected).unwrap_err().to_string();
            assert!(error.contains(needle), "{error} does not mention {needle}");
        }
        // Without custody the same request is a direct add_device: a borrow is refused.
        let direct = txbuild::calls("add-devices", &request, &registry, &BTreeMap::new(), &None).unwrap();
        assert!(check(&build(&cap, device, loan), &direct).is_err());
    }

    #[test]
    fn verify_binds_deployment_values() {
        let request = txbuild::Request {
            sender: SENDER.into(),
            component_name: Some("fw".into()),
            version: Some("1.0.0".into()),
            recipe_blob_id: Some("blob".into()),
            artifact_sha256: Some("a".repeat(64)),
            recipe_sha256: Some("b".repeat(64)),
            target_groups: vec![hex(50)],
            ..Default::default()
        };
        let expected =
            txbuild::calls("create-deployment", &request, &hex(60), &BTreeMap::new(), &None).unwrap();
        let pure = |v: &str| Input::Pure(bcs::to_bytes(&v.to_owned()).unwrap());
        let build = |recipe: &str, groups: Vec<Address>, clock_mutable: bool| {
            let inputs = vec![
                pure("fw"),
                pure("1.0.0"),
                pure("blob"),
                pure(&"a".repeat(64)),
                pure(recipe),
                Input::Pure(bcs::to_bytes(&groups).unwrap()),
                Input::Pure(bcs::to_bytes(&Vec::<Address>::new()).unwrap()),
                Input::Shared(SharedInput::new(address("0x6"), 1, clock_mutable)),
            ];
            let command = Command::MoveCall(MoveCall {
                package: address(PACKAGE),
                module: Identifier::from_str("deployment_manager").unwrap(),
                function: Identifier::from_str("create_deployment_verified").unwrap(),
                type_arguments: vec![],
                arguments: (0..8).map(Argument::Input).collect(),
            });
            transaction(inputs, vec![command], SENDER, SENDER, 1_000)
        };
        let b = "b".repeat(64);
        check(&build(&b, vec![address(&hex(50))], false), &expected).unwrap();
        // An attacker-chosen recipe digest, extra target, or mutable clock.
        let c = "c".repeat(64);
        for tx in [
            build(&c, vec![address(&hex(50))], false),
            build(&b, vec![address(&hex(50)), address(&hex(51))], false),
            build(&b, vec![address(&hex(50))], true),
        ] {
            let error = check(&tx, &expected).unwrap_err().to_string();
            assert!(error.contains("is not the requested"), "{error}");
        }
    }
}
