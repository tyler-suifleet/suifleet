#!/usr/bin/env python3
"""Live package-upgrade acceptance on Sui testnet (the `package-upgrade` live gate).

Spends real testnet gas from an explicitly supplied wallet; funds only through an explicitly supplied funding-only tool,
never reads a default wallet, never prints keys. Each run:

  1. publishes BASE contracts (default: the merge base with origin/main) as a
     fresh ephemeral publication, and creates live state with that package:
     a device whose key the daemon provisions, its cap and a group;
  2. upgrades in place to this tree's contracts with the run's UpgradeCap;
  3. resolves and verifies the deployment manifest over gRPC;
  4. runs the real daemon (gRPC) with the SF-030 test updater and a local
     Walrus stand-in, and deploys through the NEW package (direct target,
     the pre-upgrade group) and through the OLD package's entry point
     (mixed versions), plus an injected health failure;
  5. checks each outcome from on-chain receipts read with the Sui CLI
     (independently of the daemon) and the component run history;
  6. deletes the deployments (storage rebate) and writes evidence JSON:
     tool versions, chain, packages, every transaction digest and status,
     receipts and the SUI actually spent.

Toolchain (scripts/sui_toolchain.py): the pinned release compiler builds the
bytecode offline; the pinned transaction CLI, which must support the chain's
protocol version (checked read-only before any write), builds, signs and
submits. Package bytecode is verified byte-for-byte in the unsigned
transaction before signing, and the evidence and fixture record both CLIs.

Cost: package storage is never refunded, so the published fixture (base
package, its upgrade, device, cap and group) is cached in --state, keyed by
the content of the base and head contracts, and re-verified on chain before
reuse. A run whose contracts are unchanged only pays for its deployments
(deleted afterwards, so their storage is rebated): about 0.01 SUI instead of
about 0.18. Identical base and head contracts skip the upgrade. The device and
its gas are reused and topped up only when low.

Usage (make live-gate-package-upgrade sets these from the environment):
  live_package_upgrade.py --wallet CLIENT_YAML [--network testnet] [--base-ref REF]
                          [--max-spend-mist N] [--evidence FILE]
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlparse
from types import SimpleNamespace

import deployment_manifest as dm
import live_common
import sui_toolchain as toolchain
import test_component as component
from testnet_budget import Budget, Spent

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
CLOCK = "@0x6"
CHAINS = {"testnet": "4c78adac"}
DEVICE_FUNDING = 30_000_000  # MIST of report gas per top-up.


DEVICE_MINIMUM = 10_000_000  # Top the device up below this.


def contracts_digest(path, *, compiled=None, build_tool="sui"):
    """Exact root-package bytecode identity, not separate packages or tests."""
    built = compiled if compiled is not None else toolchain.compile_package(build_tool, path)
    digest = bytes(built["digest"])
    if len(digest) != 32:
        raise ValueError("invalid compiled package digest")
    return "bytecode-v1:" + digest.hex()


def matching_fixture(state, base_digest, head_digest, tools, framework):
    """Only authenticated, compiler-verified equivalent package pairs migrate."""
    expected = (base_digest.removeprefix("bytecode-v1:"), head_digest.removeprefix("bytecode-v1:"))
    for fixture in state["fixtures"].values():
        proof = fixture.get("provenance") or {}
        verified = proof.get("verified_transactions") or {}
        base = verified.get("base") or {}
        head = verified.get("upgrade") or base
        if ((base.get("digest"), head.get("digest")) == expected
                and proof.get("build_cli") == tools["build"]["version"]
                and proof.get("network_cli_sha256") == tools["network"]["sha256"]
                and proof.get("framework") == framework):
            return fixture
    return None


def checkpoint_setup(state_path, state, publication, label, action, ledger):
    """Persist uncertainty before a mutation; never repeat an unknown outcome."""
    if publication.get("pending_operation"):
        raise RuntimeError("pending fixture operation requires on-chain reconciliation before reuse")
    publication["pending_operation"] = label
    save_state(state_path, state)
    try:
        result = action()
    except Exception as error:
        known = isinstance(error, toolchain.NotSubmitted) or (
            ledger.evidence and ledger.evidence[-1].get("label") == label
            and ledger.evidence[-1].get("status") in ("failure", "not_submitted"))
        if known:
            publication.pop("pending_operation", None)
        save_state(state_path, state)
        raise
    # Caller atomically saves the returned IDs together with this cleared mark.
    # A crash before that save leaves uncertainty on disk, never a republish.
    publication.pop("pending_operation")
    return result


def load_state(path, chain, operator):
    try:
        state = json.loads(Path(path).read_text())
    except FileNotFoundError:
        state = {}
    except ValueError as error:
        raise RuntimeError("invalid private fixture state; reconcile before publication") from error
    if state.get("chain") != chain or state.get("operator") != operator:
        state = {"chain": chain, "operator": operator, "fixtures": {}}  # New wallet or a reset network.
    state.setdefault("fixtures", {})
    state.setdefault("publications", {})
    return state


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create the key-bearing temporary file privately from the first byte.
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".fixture-", delete=False) as output:
        temporary = Path(output.name)
        output.write(json.dumps(state, indent=2) + "\n"); output.flush(); os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(directory)
    finally: os.close(directory)


def fixture_key(base_tree, head_tree):
    """Contract pair plus the release compiler that produced its bytecode.
    A fixture is never reused for a different base-to-head pair or compiler."""
    trees = base_tree if base_tree == head_tree else f"{base_tree}..{head_tree}"
    return f"{trees}@{toolchain.BUILD['version'].split()[1]}"


def provenance(tools, compatibility, framework, built, base, head):
    return {"base_commit": base, "head_commit": head,
            "build_cli": tools["build"]["version"], "network_cli": tools["network"]["version"],
            "network_cli_sha256": tools["network"]["sha256"], "protocol_version": compatibility["protocol_version"],
            "framework": framework, "verified_transactions": built}


def owner_of(client, object_id):
    try:
        found = client("object", object_id)
    except RuntimeError:
        return None
    owner = found.get("owner") or {}
    return owner.get("AddressOwner") or ("custodied" if "ObjectOwner" in owner else ("shared" if "Shared" in owner else None))


def fixture_alive(fixture, operator, client):
    """Free reads: every cached object still exists with the expected owner."""
    expected = {fixture["upgrade_cap"]: operator, fixture["device"]: "custodied",
                fixture["owner_cap"]: operator,
                fixture["device_cap"]: fixture["device_address"], fixture["group"]: "shared",
                fixture["registry"]: "shared"}
    for object_id, owner in expected.items():
        actual = owner_of(client, object_id)
        if actual is None or (owner in ("shared", "custodied") and actual != owner) or (owner not in ("shared", "custodied") and dm.object_id(actual) != dm.object_id(owner)):
            return False
    try:
        client("object", fixture["head_package"])  # Packages are immutable: existence is enough.
    except RuntimeError:
        return False
    return True



def verify_publication(publication, rpc_url, operator, client):
    # Reconcile checkpointed authority with the last completed upgrade.
    old, registry, cap = (publication[k] for k in ("base_package", "registry", "upgrade_cap"))
    head = publication.get("head_package", old)
    facts = dm.chain_facts(rpc_url, old, cap, registry)
    expected_version = "2" if head != old else "1"
    assert dm.upgrade_cap(facts["objects"][dm.object_id(cap)])[:2] == (dm.object_id(head), expected_version)
    assert owner_of(client, cap) == operator and owner_of(client, registry) == "shared"


def balance_of(client, address):
    gas = client("gas", address)
    coins = gas.get("gasCoins", []) if isinstance(gas, dict) else gas
    return sum(int(c.get("mistBalance", 0)) for c in coins) + (int(gas.get("addressMistBalance") or 0) if isinstance(gas, dict) else 0)


def plain(value):
    """Move JSON with or without JSON-RPC-style {"fields": ...} wrappers."""
    if isinstance(value, dict):
        if set(value) <= {"type", "fields"} and "fields" in value:
            return plain(value["fields"])
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def daemon_progress(journal, log):
    """Bounded public diagnostics: no raw log, configuration, cursor or error text."""
    try:
        state = json.loads(journal.read_text())
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    outcomes = state.get('deployments', {})
    public = {}
    if isinstance(outcomes, dict):
        for id, outcome in list(outcomes.items())[:32]:
            try:
                id = dm.object_id(id)
            except ValueError:
                continue
            if not isinstance(outcome, dict):
                continue
            status = outcome.get('status')
            public[id] = dict(status=status if type(status) is int and status in (0, 1, 2) else None,
                              reported=outcome.get('reported') is True, has_error=bool(outcome.get('error')))
    try:
        with log.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            text = stream.read(65536).decode('utf8', errors='replace')
    except OSError:
        text = ''
    categories = ('Sync failed', 'Deployment finished', 'Apply admitted',
                  'Status report remains queued', 'RPC request timeout', 'RPC identity timeout',
                  'chain identifier mismatch', 'event stream', 'not owned by device key',
                  'SHA-256 mismatch', 'Downloading blob', 'Downloaded')
    return dict(initialized=state.get('initialized') is True, outcomes=public,
                log_counts={name: text.count(name) for name in categories})


def wait_for(condition, message, process=None, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(1)
    raise AssertionError(message)


def transaction_gas_budget(value):
    # Operators may lower ordinary PTB budgets to fit separate address/coin
    # funding pools; this never raises the existing cap or run allowance.
    if type(value) is not int or not 2_000_000 <= value <= 200_000_000:
        raise ValueError("transaction gas budget must be 2000000..200000000 MIST")
    return value


def run(args):
    ptb_budget = transaction_gas_budget(getattr(args, "transaction_gas_budget", 200_000_000))
    os.umask(0o077)
    evidence = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "network": args.network,
                "transactions": [], "checks": []}
    if args.evidence:
        Path(args.evidence).write_text(json.dumps(evidence) + '\n')
    ledger = Budget(args.max_spend_mist, evidence['transactions'])
    wallet = Path(args.wallet).resolve()
    # Pinned release compiler and transaction CLI, never PATH substitutes.
    tools = toolchain.check_tools(args.build_sui, args.network_sui)
    network_sui = tools["network"]["path"]

    def client(*command, json_output=True, check=True):
        full = [network_sui, "client", "--client.config", str(wallet), *map(str, command)]
        if json_output:
            full.append("--json")
        result = subprocess.run(full, text=True, capture_output=True, timeout=600)
        if result.returncode and check and not (json_output and '"effects"' in result.stdout):
            raise RuntimeError(f"sui client {command[0]} failed:\n{result.stderr[-3000:]}\n{result.stdout[-3000:]}")
        if not json_output:
            return result
        out = result.stdout
        return json.loads(out[min(i for i in (out.find("{"), out.find("[")) if i >= 0):])

    def balance():
        gas = client("gas")
        coins = gas.get("gasCoins", []) if isinstance(gas, dict) else gas
        return sum(int(c.get("mistBalance", 0)) for c in coins) + (int(gas.get("addressMistBalance") or 0) if isinstance(gas, dict) else 0)

    envs, active_env = client("envs")
    rpc_url = next(e["rpc"] for e in envs if e["alias"] == active_env)
    operator = dm.object_id(client("active-address", json_output=False).stdout.strip().split()[-1])
    facts = dm.chain_facts(rpc_url)
    chain = facts["chain_identifier"]
    expected = CHAINS.get(args.network)
    if args.network == "localnet":
        assert urlparse(rpc_url).hostname in ("127.0.0.1", "localhost", "::1"), "localnet profile needs a loopback node"
    else:
        assert chain == expected, f"active env {active_env} is chain {chain}, not {args.network} ({expected})"
    # Read-only protocol gate before any faucet request or transaction.
    compatibility = toolchain.preflight(tools, facts, expected)
    evidence["toolchain"] = dict(tools, compatibility=compatibility)
    if args.network == 'testnet':
        assert operator in {
            '0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965',
            '0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac',
        }, 'wallet is outside dedicated testnet grant'
    start_balance = balance()
    if start_balance < args.max_spend_mist and args.faucet_tool:
        assert args.network == 'testnet', 'faucet only on testnet'
        # Only the verified, checksum-pinned transaction CLI requests funds.
        assert toolchain.resolve(args.faucet_tool) == network_sui, 'faucet tool must be the pinned network CLI'
        # At most one request; never reset the spending ledger from a faucet credit.
        funded = ledger.faucet(lambda: subprocess.run([network_sui, 'client', '--client.config', str(wallet),
                                 'faucet', '--address', operator], capture_output=True, timeout=180))
        start_balance = balance()
        evidence['faucet'] = {'attempts': 1, 'cli_returncode': funded.returncode,
                              'balance_after_mist': start_balance, 'success': start_balance >= args.max_spend_mist}
        if start_balance < args.max_spend_mist:
            raise RuntimeError('dedicated testnet faucet did not supply the required balance (one bounded attempt)')
    assert start_balance >= args.max_spend_mist, (
        f"{operator} holds {start_balance} MIST; this run may spend up to {args.max_spend_mist}. Fund it first "
        f"(docs/TESTNET.md)")
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    base = args.base_ref or subprocess.check_output(
        ["git", "-C", str(ROOT), "merge-base", "HEAD", "origin/main"], text=True).strip()
    base = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", base], text=True).strip()
    evidence.update(chain=chain, rpc=rpc_url, operator=operator, sui={k: tools[k]["version"] for k in tools},
                    head=head, base=base, start_balance_mist=start_balance)
    print(f"{args.network} chain {chain}, operator {operator}, balance {start_balance / 1e9:.3f} SUI; "
          f"base {base[:12]} -> head {head[:12]}")

    def mutate(label, command, **kwargs):
        if os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
            subprocess.run(['ticket', '--project', 'suifleet', 'lock-assert', 'network:suifleet-testnet',
                            '--lock-token', os.environ['SUIFLEET_TESTNET_LOCK_TOKEN']], check=True, stdout=subprocess.DEVNULL)
        return ledger.execute(client, label, command, **kwargs)

    def transaction(label, *ptb, budget=None, funding=0):
        return mutate(label, ['ptb', *ptb], gas=ptb_budget if budget is None else budget, funding=funding)

    def created(result, suffix):
        return [c["objectId"] for c in result["objectChanges"] if c["type"] == "created" and c["objectType"].endswith(suffix)]

    def published(result):
        return next(c["packageId"] for c in result["objectChanges"] if c["type"] == "published")

    def check(label):
        evidence["checks"].append(label)
        print("PASS:", label)

    # Recipe and artifact storage: a local stand-in (default) or real Walrus testnet.
    walrus = live_common.Walrus(args.walrus)
    aggregator = walrus.aggregator
    evidence["walrus"] = walrus.mode
    process = None
    cleanup = []
    cleanup_package = None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-live-upgrade-") as temporary:
            root = Path(temporary)
            package_dir, pubfile = root / "package", root / "Pub.live.toml"
            staging = root / "base"; staging.mkdir()
            archive = subprocess.run(["git", "-C", str(ROOT), "archive", base, "contracts"], check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", str(staging)], input=archive, check=True)
            base_built = toolchain.compile_package(tools["build"]["path"], staging / "contracts")
            head_built = toolchain.compile_package(tools["build"]["path"], ROOT / "contracts")
            base_tree, head_tree = contracts_digest(staging / "contracts", compiled=base_built), contracts_digest(ROOT / "contracts", compiled=head_built)
            key_name = fixture_key(base_tree, head_tree)
            evidence["toolchain"]["framework"] = {
                "base": toolchain.framework_pins(staging / "contracts/Move.lock"),
                "head": toolchain.framework_pins(ROOT / "contracts/Move.lock")}
            state_path = Path(args.state) if args.state else wallet.parent / "live-state.json"
            state = load_state(state_path, chain, operator)
            common = ["--build-env", "testnet", "--pubfile-path", pubfile, "--silence-warnings"]
            key = root / "device.key"
            fixture = state["fixtures"].get(key_name) or matching_fixture(
                state, base_tree, head_tree, tools, evidence["toolchain"]["framework"])
            if fixture and args.fresh:
                raise RuntimeError("verified bytecode already exists; fresh package republication is refused")
            if fixture and (not fixture.get("owner_cap") or not fixture_alive(fixture, operator, client)):
                raise RuntimeError("cached fixture needs object reconciliation; immutable code is not republished")
            if fixture:
                state["fixtures"][key_name] = fixture
                save_state(state_path, state)
                evidence["fixture_provenance"] = fixture.get("provenance")
                key.write_text(fixture["device_key"] + "\n"); key.chmod(0o600)
                derived = subprocess.check_output([str(DAEMON), "--derive-address", str(key)], text=True).strip()
                assert dm.object_id(derived) == dm.object_id(fixture["device_address"]), "cached device key does not match"
                check(f"reused fixture {key_name[:12]}…: base {fixture['base_package'][:12]}…, head {fixture['head_package'][:12]}… "
                      "(re-verified on chain; no package storage spent)")
            else:
                publication = state["publications"].setdefault(key_name, {
                    "source_pair": [base_tree, head_tree], "compiler": tools["build"]["version"], "built": {}})
                if publication.get("pending_operation"):
                    raise RuntimeError("uncertain fixture setup retained; reconcile on chain before another publication")
                assert publication["source_pair"] == [base_tree, head_tree] and publication["compiler"] == tools["build"]["version"]
                built = publication["built"]
                def step(label, action):
                    return checkpoint_setup(state_path, state, publication, label, action, ledger)
                def save_publication(**values):
                    publication.update(values)
                    publication["built"] = built
                    save_state(state_path, state)
                shutil.copytree(staging / "contracts", package_dir, ignore=shutil.ignore_patterns("build", "Published.toml"))
                if "base_package" not in publication:
                    first = step("publish base", lambda: mutate("publish base", toolchain.package_submitter(
                        client, tools["build"]["path"], package_dir, common, sender=operator,
                        record=built.setdefault("base", {})), gas=400_000_000))
                    save_publication(base_package=published(first), registry=created(first, "::device_registry::DeviceRegistry")[0],
                                     upgrade_cap=created(first, "::package::UpgradeCap")[0])
                old, registry, upgrade_cap = (publication[k] for k in ("base_package", "registry", "upgrade_cap"))
                verify_publication(publication, rpc_url, operator, client)
                toolchain.write_pubfile(pubfile, chain, package_dir, old, upgrade_cap)
                check(f"verified/reused base package {old}; no identical package is republished")
                if "device_key" not in publication:
                    address = subprocess.check_output([str(DAEMON), "--provision", str(key)], text=True).strip()
                    save_publication(device_key=key.read_text().strip(), device_address=address)
                key.write_text(publication["device_key"] + "\n"); key.chmod(0o600)
                device_address = publication["device_address"]
                assert dm.object_id(subprocess.check_output([str(DAEMON), "--derive-address", str(key)], text=True).strip()) == dm.object_id(device_address)
                if "device" not in publication:
                    registered = step("register device + issue cap (base)", lambda: transaction("register device + issue cap (base)",
                        "--move-call", f"{old}::device_registry::register_device", f"@{registry}", '"live-pi"',
                        f"@{device_address}", '"aarch64"', CLOCK, "--assign", "device",
                        "--move-call", f"{old}::device_cap::issue_cap", "device",
                        "--transfer-objects", "[device]", f"@{operator}",
                        "--move-call", f"{old}::device_group::create_group", '"live"', '"package-upgrade gate"', CLOCK))
                    save_publication(device=created(registered, "::device_registry::Device")[0],
                                     device_cap=created(registered, "::device_cap::DeviceCap")[0],
                                     group=created(registered, "::device_group::DeviceGroup")[0])
                device, device_cap, group = (publication[k] for k in ("device", "device_cap", "group"))
                if not publication.get("member_added"):
                    step("add device to group (base)", lambda: transaction("add device to group (base)",
                         "--move-call", f"{old}::device_group::add_device", f"@{group}", f"@{device}"))
                    save_publication(member_added=True)
                new = publication.get("head_package")
                if not new:
                    new = old
                    if head_tree != base_tree:
                        shutil.rmtree(package_dir)
                        shutil.copytree(ROOT / "contracts", package_dir, ignore=shutil.ignore_patterns("build", "Published.toml"))
                        upgraded = step("upgrade to head", lambda: mutate("upgrade to head", toolchain.package_submitter(
                            client, tools["build"]["path"], package_dir, common, sender=operator,
                            upgrade={"cap": upgrade_cap, "package": old}, record=built.setdefault("upgrade", {})),
                            # Growing additive packages can exceed the old 0.4 SUI
                            # reservation. Use only the existing run allowance;
                            # Budget still settles effects and retains unknown costs.
                            gas=ledger.remaining))
                        new = published(upgraded)
                    else:
                        check("exact root bytecode equals base; separate packages do not trigger a pointless core upgrade")
                    save_publication(head_package=new)
                if "owner_cap" not in publication:
                    activated = step("activate identity in registry custody", lambda: transaction("activate identity in registry custody",
                        "--move-call", f"{new}::device_registry::activate_device", f"@{registry}", f"@{device}",
                        "--assign", "owner", "--transfer-objects", "[owner]", f"@{operator}"))
                    save_publication(owner_cap=created(activated, "::device_registry::DeviceOwnerCap")[0])
                fixture = dict(owner_cap=publication["owner_cap"], base_package=old, head_package=new, registry=registry, upgrade_cap=upgrade_cap,
                               device=device, device_cap=device_cap, group=group, device_address=device_address,
                               device_key=publication["device_key"], created=evidence["started"],
                               provenance=provenance(tools, compatibility, evidence["toolchain"]["framework"], built, base, head))
                state["fixtures"][key_name] = fixture
                state["publications"].pop(key_name)
                save_state(state_path, state)
            old, new, registry, upgrade_cap = (fixture[k] for k in ("base_package", "head_package", "registry", "upgrade_cap"))
            device, cap, group, device_address = (fixture[k] for k in ("device", "device_cap", "group", "device_address"))
            # Report gas: top up only when low.
            device_gas = balance_of(client, device_address)
            ledger.grant(device_gas)  # Include all pre-existing device gas in the ceiling.
            if device_gas < DEVICE_MINIMUM:
                transaction("top up device gas", "--split-coins", "gas", f"[{DEVICE_FUNDING}]", "--assign", "fee",
                            "--transfer-objects", "[fee]", f"@{device_address}", budget=10_000_000, funding=DEVICE_FUNDING)

            # 3. Manifest over gRPC.
            manifest = dm.resolve(SimpleNamespace(chain_identifier=chain, rpc=rpc_url, package=new, upgrade_cap=upgrade_cap,
                                                  registry=registry, network=args.network,
                                                  publisher=walrus.publisher, aggregator=aggregator))
            evidence.update(base_package=old, head_package=new, registry=registry, upgrade_cap=upgrade_cap,
                            fixture=key_name, type_origins=manifest["type_origins"])
            check(f"head package {new} (package version {manifest['package_version']}); manifest verified over gRPC")

            # 4. Daemon and deployments.
            components = root / "state" / "test-components"
            template = root / "device-template.toml"
            template.write_text(
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{device}"\ndevice_cap_id = "{cap}"\n'
                f'[apply]\ncommand = {json.dumps(f"/bin/sh {component.UPDATER} --root {components}")}\ntimeout_seconds = 60\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 3\n')
            dm.generate(manifest, root / "release", template)
            log = (root / "daemon.log").open("w")
            process = subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(root / "release/config.toml")},
                                       stdout=log, stderr=log)
            journal = root / "state" / "state.json"
            wait_for(lambda: journal.exists() and json.loads(journal.read_text()).get("initialized"), "daemon did not enroll", process)
            run_id = "live-" + uuid.uuid4().hex[:10]

            def call(package, version, *, groups=(), devices=(), fail="none"):
                """PTB arguments creating one verified deployment, its recipe and artifact stored in Walrus."""
                return ["--move-call", f"{package}::deployment_manager::create_deployment_verified",
                        *live_common.deployment_args(walrus, version, run_id, fail=fail),
                        live_common.ids(groups), live_common.ids(devices), CLOCK]

            def receipts(deployment):
                """Authoritative statuses read with the Sui CLI, independent of the daemon."""
                for field in client("dynamic-field", deployment)["dynamicFields"]:
                    value = client("object", field["fieldId"])
                    content = value.get("content", {})
                    if not str(value.get("objType", "")).count("ReceiptsKey"):
                        continue
                    receipt = plain(content)["value"]
                    if int(receipt["version"]) != 2:
                        return {}  # Pending migration/report: never expose legacy statuses as verified.
                    statuses = receipt["statuses"]["contents"]
                    return {dm.object_id(e["key"]): int(e["value"]) for e in statuses}
                return {}

            def settled(deployment, status):
                try:
                    wait_for(lambda: receipts(deployment) == {dm.object_id(device): status},
                             f"receipt for {deployment} never became {status}", process)
                except Exception:
                    log.flush()
                    evidence['daemon_progress'] = daemon_progress(journal, root / 'daemon.log')
                    raise

            def history():
                path = components / component.DEFAULT_COMPONENT / "history.jsonl"
                return [json.loads(line)["version"] for line in path.read_text().splitlines()] if path.exists() else []

            # Batched to save computation fees. One transaction cannot call two
            # versions of a package (InvalidLinkage), so the old package's
            # entry point gets its own.
            by_version = {}
            for label, arguments in (
                ("create 3 deployments via head", [*call(new, "1.0.0", devices=[device]), *call(new, "2.0.0", groups=[group]),
                                                   *call(new, "4.0.0", devices=[device], fail="health")]),
                ("create 1 deployment via base", call(old, "3.0.0", devices=[device]))):
                for record_id in created(transaction(label, *arguments), "::deployment_manager::DeploymentRecord"):
                    cleanup.append(record_id)
                    cleanup_package = new
                    by_version[plain(client("object", record_id)["content"])["version"]] = record_id
            direct, via_group, via_old, failed = (by_version[v] for v in ("1.0.0", "2.0.0", "3.0.0", "4.0.0"))
            settled(direct, 1)
            check("head-package deployment to the device: applied, receipt Applied on chain")
            settled(via_group, 1)
            check("head-package deployment to the pre-upgrade group: applied via report_status_for_group")
            settled(via_old, 1)
            check("deployment created through the OLD package's entry point: applied and receipted (mixed versions)" if old != new
                  else "deployment through the base package (no upgrade in this run): applied and receipted")
            settled(failed, 2)
            check("injected health failure reported Failed on chain")
            assert sorted(history()) == ["1.0.0", "2.0.0", "3.0.0", "4.0.0"], history()
            evidence["receipts"] = {d: receipts(d) for d in (direct, via_group, via_old, failed)}
            if args.network == 'testnet':
                proof = subprocess.run(['node', str(ROOT / 'scripts/testnet_receipt_effects.cjs'),
                                        str(root / 'release/deployment.json'), direct, via_group, via_old, failed],
                                       capture_output=True, text=True, timeout=120, check=True)
                evidence['report_transactions'] = json.loads(proof.stdout)
                assert len(evidence['report_transactions']) == 4
                check('independent gRPC reader confirmed all four receipt-writing transaction effects')
            process.send_signal(signal.SIGINT)
            process.wait(timeout=60)
            process = None

            # 6. Cleanup in one transaction: storage rebate.
            deletes = []
            for deployment in (direct, via_group, via_old, failed):
                deletes += ["--move-call", f"{new}::deployment_manager::delete_deployment", f"@{deployment}"]
            transaction("delete 4 deployments", *deletes)
            cleanup.clear()
            check("deployments deleted (storage rebate)")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        if cleanup:
            try:
                deletes = []
                for deployment in cleanup:
                    deletes += ["--move-call", f"{cleanup_package}::deployment_manager::delete_deployment", f"@{deployment}"]
                transaction("bounded abort cleanup", *deletes)
            except Exception as error:
                evidence['cleanup_error'] = str(error)
        walrus.close()
        try:
            end = balance()
        except Exception:
            end = None
        evidence.update(success=sys.exc_info()[0] is None, allowance_used_mist=ledger.used, max_spend_mist=ledger.limit, end_balance_mist=end, spent_mist=(start_balance - end if end is not None else None),
                        finished=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if args.evidence:
            Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"allowance used {ledger.used} MIST; {len(evidence['transactions'])} transactions; "
              f"evidence: {args.evidence or '(not written)'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallet", required=True, help="client.yaml of the funded wallet to spend from")
    parser.add_argument("--network", default="testnet", choices=["testnet", "localnet"])
    parser.add_argument("--base-ref", help="contracts to publish first (default: merge base with origin/main)")
    # A fresh base->head fixture with the SF-083 budget costs about 0.58 SUI
    # (base publish 0.265 + head upgrade 0.316, run 39720082567).
    parser.add_argument("--max-spend-mist", type=int, default=800_000_000)
    parser.add_argument("--evidence", help="write evidence JSON here")
    parser.add_argument("--state", help="fixture cache (default: live-state.json beside the wallet)")
    parser.add_argument("--faucet-tool", help="request testnet funds (at most once) with this tool; must be the "
                        "pinned network CLI")
    parser.add_argument("--build-sui", default="sui", help="pinned release compiler (Dockerfile SUI_VERSION)")
    parser.add_argument("--network-sui", default=os.environ.get("SUIFLEET_NETWORK_SUI", toolchain.NETWORK_DEFAULT),
                        help="pinned transaction CLI (Dockerfile SUI_NETWORK_VERSION)")
    parser.add_argument("--transaction-gas-budget", type=int, default=200_000_000,
                        help="lower ordinary PTB gas budgets for available gas funding pools (2000000..200000000 MIST); the run ceiling still applies")
    parser.add_argument("--fresh", action="store_true", help="request a fresh fixture; refused if matching verified bytecode already exists")
    parser.add_argument("--walrus", default="local", choices=["local", "testnet"],
                        help="recipe/artifact storage: a local stand-in (default) or real Walrus testnet")
    args = parser.parse_args()
    try:
        run(args)
    except Exception as error:
        if args.evidence:
            path = Path(args.evidence)
            result = json.loads(path.read_text()) if path.exists() else {}
            result.update(success=False, error=str(error))
            path.write_text(json.dumps(result, indent=2) + "\n")
        raise
