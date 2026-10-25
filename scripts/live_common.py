"""Shared plumbing for live-network acceptance runs (SF-010; docs/TESTNET.md).

Everything here reads and writes the chain through the Sui CLI and gRPC
(`chain_facts`): public testnet fullnodes do not serve JSON-RPC. Nothing
here reads a default wallet or prints a key. Explicit testnet preflight may
request one bounded faucet top-up after chain, account, CLI and lock checks.

Toolchain (scripts/sui_toolchain.py, SF-056): every `sui client` command
runs on the pinned transaction CLI, which must support the chain's protocol
version (checked read-only before any write). Package bytecode comes from
the pinned release compiler and is verified in the unsigned transaction
before signing.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import deployment_manifest as dm
import sui_toolchain as toolchain
import test_component as component
from testnet_budget import Budget, Spent  # noqa: F401 (re-exported to the suites)

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
CLOCK = "@0x6"
CHAINS = {"testnet": "4c78adac"}
# The public Walrus endpoints refuse Python's default User-Agent (HTTP 403).
AGENT = {"User-Agent": "suifleet-acceptance/1"}
WALRUS_TESTNET = ("https://publisher.walrus-testnet.walrus.space", "https://aggregator.walrus-testnet.walrus.space")
# Mysten's publisher pays for storage from its own WAL and can run dry (CI run
# 40140128767: five HTTP 500 "could not find WAL coins with sufficient
# balance"), which no retry fixes. Uploads then fall back, in order, to
# community testnet publishers from https://docs.wal.app/operators.json (each
# stored and served a probe blob on 2026-10-24). A publisher is only trusted to
# store: every blob is read back through Mysten's aggregator and compared byte
# for byte before use, and the publisher pays, so a fallback spends nothing.
WALRUS_FALLBACK_PUBLISHERS = (
    "https://publisher.walrus-01.tududes.com",
    "https://wal-publisher-testnet.staketab.org",
    "https://walrus-testnet-publisher.nodes.guru",
    "https://sui-walrus-testnet-publisher.bwarelabs.com",
    "https://testnet-publisher.walrus.graphyte.dev",
    "https://publisher.walrus.banansen.dev",
)
# Each pass tries every publisher once; passes are separated by a backoff and
# the whole upload stops at the deadline.
WALRUS_PASSES, WALRUS_RETRY_SECONDS, WALRUS_DEADLINE_SECONDS = 2, 15, 600
# Move aborts as the CLI reports them: "... aborted within function
# '0x…::module::function' at instruction N with code C", or the older
# MoveAbort(MoveLocation { … name: Identifier("module") … function_name: Some("function") }, C).
_ABORTS = (re.compile(r"aborted within function '0x[0-9a-fA-F]+::(\w+)::(\w+)' at instruction \d+ with code (\d+)"),
           re.compile(r'name: Identifier\("(\w+)"\) \}, function: \d+, instruction: \d+, function_name: Some\("(\w+)"\) \}, (\d+)\)'))


class _Abort:
    @staticmethod
    def search(text):
        for pattern in _ABORTS:
            found = pattern.search(text)
            if found:
                return found
        return None


ABORT = _Abort()


# Every mutation is charged to testnet_budget's strict, non-replenishing allowance.
DIGEST = re.compile(r"transaction '([1-9A-HJ-NP-Za-km-z]{32,44})'")
# The dedicated testnet wallets (agent, CI) of docs/TESTNET.md.
TESTNET_WALLETS = {"0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965",
                   "0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac"}


def wait_for(condition, message, process=None, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(1)
    raise AssertionError(message)


def plain(value):
    """Move JSON with or without JSON-RPC-style {"fields": ...} wrappers."""
    if isinstance(value, dict):
        if set(value) <= {"type", "fields"} and "fields" in value:
            return plain(value["fields"])
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


class Live:
    """A funded wallet on a live network. Every mutation goes through a
    `Budget` (testnet_budget): its gas budget and any funding it sends are
    reserved against the run's ceiling before execution and settled from
    the effects, so failures, aborts and unknown outcomes are charged and
    rebates never replenish the allowance."""

    def __init__(self, wallet, network, max_spend_mist, build_sui="sui",
                 network_sui=os.environ.get("SUIFLEET_NETWORK_SUI", toolchain.NETWORK_DEFAULT)):
        self.wallet = Path(wallet).resolve()
        self.build_sui, self.network_sui = build_sui, network_sui
        self.network = network
        self.max_spend = max_spend_mist
        self.evidence = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "network": network,
                         "transactions": [], "checks": []}
        self.ledger = Budget(max_spend_mist, self.evidence["transactions"])

    def client(self, *command, json_output=True, check=True, config=None):
        full = [self.network_sui, "client", "--client.config", str(config or self.wallet), *map(str, command)]
        if json_output:
            full.append("--json")
        result = subprocess.run(full, text=True, capture_output=True, timeout=600)
        if result.returncode and check:
            raise RuntimeError(f"sui client {command[0]} failed:\n{result.stderr[-3000:]}\n{result.stdout[-3000:]}")
        if not json_output or result.returncode:
            return result
        out = result.stdout
        return json.loads(out[min(i for i in (out.find("{"), out.find("[")) if i >= 0):])

    def executor(self, config=None):
        """A Budget client: runs a mutating `sui client` command and returns
        its execution effects. A failed execution is read back by digest; no
        digest means an unknown outcome (the reservation stays charged)."""
        def execute(*command):
            result = self.client(*command, check=False, config=config)
            if isinstance(result, dict) and "effects" in result:
                return result
            text = result.stderr + result.stdout if hasattr(result, "stderr") else json.dumps(result)
            if '"effects"' in getattr(result, "stdout", ""):
                try:
                    parsed = json.loads(result.stdout[result.stdout.find("{"):])
                    if "effects" in parsed:
                        return parsed
                except ValueError:
                    pass
            digest = DIGEST.search(text)
            if not digest:
                raise RuntimeError(f"sui client {command[0]}: no execution outcome\n{text[-3000:]}")
            found = {}
            wait_for(lambda: found.update(self.executed(digest.group(1), config)) or bool(found),
                     f"transaction {digest.group(1)} not readable", timeout=60)
            return found
        return execute

    def executed(self, digest, config=None):
        result = self.client("tx-block", digest, check=False, config=config)
        return result if isinstance(result, dict) and "effects" in result else {}

    def assert_lock(self):
        """Machine sessions hold network:suifleet-testnet for mutations."""
        if os.environ.get("SUIFLEET_TESTNET_LOCK_TOKEN"):
            subprocess.run(["ticket", "--project", "suifleet", "lock-assert", "network:suifleet-testnet",
                            "--lock-token", os.environ["SUIFLEET_TESTNET_LOCK_TOKEN"]], check=True, stdout=subprocess.DEVNULL)

    def balance(self, address=None):
        gas = self.client("gas", *([address] if address else []))
        coins = gas.get("gasCoins", []) if isinstance(gas, dict) else gas
        return sum(int(c.get("mistBalance", 0)) for c in coins) + (int(gas.get("addressMistBalance") or 0) if isinstance(gas, dict) else 0)

    def preflight(self, *, auto_fund=False):
        # Pinned release compiler and transaction CLI, never PATH substitutes.
        self.tools = toolchain.check_tools(self.build_sui, self.network_sui)
        self.build_sui, self.network_sui = self.tools["build"]["path"], self.tools["network"]["path"]
        envs, active_env = self.client("envs")
        self.rpc_url = next(e["rpc"] for e in envs if e["alias"] == active_env)
        self.operator = dm.object_id(self.client("active-address", json_output=False).stdout.strip().split()[-1])
        facts = dm.chain_facts(self.rpc_url)
        self.chain = facts["chain_identifier"]
        if self.network == "localnet":
            assert any(h in self.rpc_url for h in ("127.0.0.1", "localhost")), "localnet profile needs a loopback node"
        else:
            assert self.chain == CHAINS[self.network], f"active env {active_env} is chain {self.chain}, not {self.network}"
            assert self.operator in TESTNET_WALLETS, "wallet is outside the dedicated testnet grant"
        # Read-only protocol gate before any transaction.
        self.evidence["toolchain"] = dict(self.tools, compatibility=toolchain.preflight(
            self.tools, facts, CHAINS.get(self.network)))
        self.start_balance = self.balance()
        if self.start_balance < self.max_spend and auto_fund:
            assert self.network == "testnet" and self.operator in TESTNET_WALLETS
            self.assert_lock()
            funded = self.ledger.faucet(lambda: subprocess.run(
                [self.network_sui, "client", "--client.config", str(self.wallet), "faucet", "--address", self.operator],
                capture_output=True, timeout=180))
            self.start_balance = self.balance()
            self.evidence["faucet"] = {"attempts": 1, "cli_returncode": funded.returncode,
                                      "balance_after_mist": self.start_balance}
        assert self.start_balance >= self.max_spend, (
            f"{self.operator} holds {self.start_balance} MIST; this run may spend up to {self.max_spend}. "
            "Fund it first (docs/TESTNET.md)")
        self.evidence.update(chain=self.chain, rpc=self.rpc_url, operator=self.operator, start_balance_mist=self.start_balance,
                             max_spend_mist=self.max_spend,
                             sui={k: self.tools[k]["version"] for k in self.tools},
                             head=subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip())
        print(f"{self.network} chain {self.chain}, operator {self.operator}, balance {self.start_balance / 1e9:.3f} SUI")

    def execute(self, label, command, *, gas, funding=0, expect="success", sender=None):
        """One charged mutation. `sender` is (client.yaml, Budget) for a
        child account the run funded: its own gas is bounded by (and
        recorded against) that funding, which the run already charged."""
        self.assert_lock()
        config, ledger = sender or (None, self.ledger)
        return ledger.execute(self.executor(config), label, command, gas=gas, funding=funding, expect=expect)

    def transaction(self, label, *ptb, budget=200_000_000, funding=0, sender=None):
        return self.execute(label, ["ptb", *ptb], gas=budget, funding=funding, sender=sender)

    def child(self, config, funding):
        """The Budget for a child account funded with `funding` MIST."""
        return (config, Budget(funding, self.evidence["transactions"]))

    def attack(self, label, *ptb, module, function, code, budget=50_000_000, sender=None):
        """Submit a call that must abort on chain with exactly this Move abort
        (an explicit budget skips the CLI's dry-run gate, so it executes and
        the failure is final, not a client-side refusal). The abort is read
        from the transaction's effects."""
        result = self.execute(label, ["ptb", *ptb], gas=budget, expect="failure", sender=sender)
        found = ABORT.search(str(result["effects"]["status"].get("error", "")))
        observed = found.groups() if found else None
        entry = next(e for e in reversed(self.evidence["transactions"]) if e.get("digest") == result["digest"])
        entry["abort"] = observed
        assert observed == (module, function, str(code)), (
            f"{label}: expected abort {module}::{function} {code}, got {result['effects']['status']}")
        return entry

    def check(self, label):
        self.evidence["checks"].append(label)
        print("PASS:", label)

    @staticmethod
    def created(result, suffix):
        return [c["objectId"] for c in result["objectChanges"] if c["type"] == "created" and c["objectType"].endswith(suffix)]

    @staticmethod
    def published(result):
        return next(c["packageId"] for c in result["objectChanges"] if c["type"] == "published")

    def publish(self, label, package_dir, pubfile, upgrade=None):
        """Ephemeral publication of a contracts tree, or its upgrade
        (`upgrade` = {"cap": UpgradeCap, "package": current package}). The
        pinned compiler's bytecode is verified in the unsigned transaction
        before it is signed; a publication records the pubfile the upgrade
        reads (serialized publication does not write it)."""
        common = ["--build-env", "testnet", "--pubfile-path", str(pubfile), "--silence-warnings"]
        execute = self.executor()

        def client(*command, json_output=True, check=True):
            # The verified bytes are executed like any other mutation.
            if command[0] == "serialized-tx":
                return execute(*command)
            return self.client(*command, json_output=json_output, check=check)
        built = self.evidence.setdefault("verified_package_transactions", {})
        result = self.execute(label, toolchain.package_submitter(
            client, self.build_sui, package_dir, common, sender=self.operator, upgrade=upgrade,
            record=built.setdefault(label, {})), gas=400_000_000)
        if upgrade is None:
            toolchain.write_pubfile(pubfile, self.chain, package_dir, self.published(result),
                                    self.created(result, "::package::UpgradeCap")[0])
        return result

    def object(self, object_id):
        return self.client("object", object_id)

    def owner(self, object_id):
        owner = self.object(object_id).get("owner") or {}
        return owner.get("AddressOwner") or ("shared" if "Shared" in owner else ("immutable" if owner == "Immutable" else None))

    def readable(self, object_id):
        wait_for(lambda: isinstance(self.client("object", object_id, check=False), dict), f"{object_id} never became readable")

    def receipts(self, deployment):
        """Authoritative statuses read with the Sui CLI, independent of the daemon."""
        for field in self.client("dynamic-field", deployment)["dynamicFields"]:
            value = self.client("object", field["fieldId"])
            if "ReceiptsKey" in str(value.get("objType", "")):
                statuses = plain(value.get("content", {}))["value"]["statuses"]["contents"]
                return {dm.object_id(e["key"]): int(e["value"]) for e in statuses}
        return {}

    def finish(self, path):
        end = self.balance()
        self.evidence.update(end_balance_mist=end, spent_mist=self.start_balance - end, allowance_used_mist=self.ledger.used,
                             finished=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if path:
            Path(path).write_text(json.dumps(self.evidence, indent=2) + "\n")
        print(f"spent {(self.start_balance - end) / 1e9:.4f} SUI (allowance used {self.ledger.used} MIST); {len(self.evidence['transactions'])} transactions; "
              f"evidence: {path or '(not written)'}")


def _walrus_message(error):
    """The publisher's error message, bounded and single-line, for the log."""
    try:
        message = json.loads(error.read() or b"{}").get("error", {}).get("message", "")
    except (OSError, ValueError, AttributeError):
        return ""
    return f": {' '.join(str(message).split())[:160]}" if message else ""


class Walrus:
    """Blob storage for recipes and artifacts: the public Walrus testnet
    publisher/aggregator (real storage, paid by the publisher, 1 epoch), or
    a local stand-in."""

    def __init__(self, mode):
        self.mode = mode
        self.blobs = {}
        if mode == "testnet":
            self.publisher, self.aggregator = WALRUS_TESTNET
            return
        blobs = self.blobs

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = blobs.get(self.path.removeprefix("/v1/blobs/"))
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.publisher = self.aggregator = f"http://127.0.0.1:{self.server.server_port}"

    def put(self, name, body):
        if self.mode != "testnet":
            self.blobs[name] = body
            return name
        stored = self.store(body)
        blob = stored.get("newlyCreated", {}).get("blobObject", {}).get("blobId") or stored.get("alreadyCertified", {}).get("blobId")
        assert blob, f"Walrus publisher returned no blob ID: {stored}"
        # Read it back through the aggregator before relying on it.
        wait_for(lambda: self.readable(blob, body), f"Walrus blob {blob} not readable from the aggregator", timeout=120)
        return blob

    def store(self, body):
        """PUT a blob to the testnet publishers, primary first. Transient
        failures (timeouts, resets, 5xx) move on to the next publisher; a 4xx
        drops that publisher for the rest of the upload. Bounded by passes and
        a deadline. Blob IDs are content-addressed and the caller verifies the
        bytes through the aggregator, so any publisher's ID is checked."""
        publishers = [self.publisher, *WALRUS_FALLBACK_PUBLISHERS]
        deadline, error = time.monotonic() + WALRUS_DEADLINE_SECONDS, None
        for attempt in range(1, WALRUS_PASSES + 1):
            if attempt > 1:
                time.sleep(WALRUS_RETRY_SECONDS)
            for publisher in list(publishers):
                if time.monotonic() > deadline:
                    raise error
                request = urllib.request.Request(f"{publisher}/v1/blobs?epochs=1", data=body, method="PUT", headers=AGENT)
                try:
                    with urllib.request.urlopen(request, timeout=120) as response:
                        stored = json.load(response)
                    if publisher != self.publisher:
                        print(f"Walrus blob stored by fallback publisher {publisher}", file=sys.stderr)
                    self.stored_by = publisher
                    return stored
                except urllib.error.HTTPError as failure:
                    error, reason = failure, f"HTTP {failure.code}{_walrus_message(failure)}"
                    if failure.code < 500:
                        publishers.remove(publisher)
                except OSError as failure:
                    error, reason = failure, type(failure).__name__
                print(f"Walrus publisher {publisher} pass {attempt}/{WALRUS_PASSES} failed ({reason})", file=sys.stderr)
            if not publishers:
                break
        raise error

    def working_publisher(self):
        """A publisher that just stored a fresh probe blob which then read
        back byte for byte through the aggregator. For uploads made outside
        this class (the dApp in the browser), which have no fallback of their
        own: the dApp is pointed at this publisher instead of a dry primary."""
        if self.mode != "testnet":
            return self.publisher
        self.put("probe", b"suifleet publisher probe " + os.urandom(16).hex().encode())
        return self.stored_by

    def readable(self, blob, body):
        try:
            request = urllib.request.Request(f"{self.aggregator}/v1/blobs/{blob}", headers=AGENT)
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read() == body
        except OSError:
            return False

    def close(self):
        if self.mode != "testnet":
            self.server.shutdown()


SWEEP_GAS = 3_000_000


def bech32_suiprivkey(seed):
    """Bech32 encoding (HRP "suiprivkey") of the ed25519 flag and seed."""
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    data = bytes([0]) + seed
    bits, acc, values = 0, 0, []
    for byte in data:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            values.append((acc >> bits) & 31)
    if bits:
        values.append((acc << (5 - bits)) & 31)
    hrp = "suiprivkey"

    def polymod(items):
        generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
        check = 1
        for item in items:
            top = check >> 25
            check = (check & 0x1ffffff) << 5 ^ item
            for i in range(5):
                check ^= generator[i] if (top >> i) & 1 else 0
        return check
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    mod = polymod(expanded + values + [0] * 6) ^ 1
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(charset[v] for v in values + checksum)


def key_wallet(live, seed, root, name):
    """A throwaway `sui client` config under `root` holding one test key (hex
    ed25519 seed); returns (client.yaml, address)."""
    wallet = root / name
    wallet.mkdir(exist_ok=True)
    (wallet / "sui.keystore").write_text("[]\n")
    (wallet / "client.yaml").write_text(
        f"keystore:\n  File: {wallet / 'sui.keystore'}\nenvs:\n- alias: live\n  rpc: {live.rpc_url}\n  ws: null\n  basic_auth: null\n"
        "active_env: live\nactive_address: null\n")
    # keytool also loads a client config; point it at this throwaway wallet.
    env = {**os.environ, "SUI_CONFIG_DIR": str(wallet)}
    imported = subprocess.run([live.network_sui, "keytool", "--keystore-path", str(wallet / "sui.keystore"), "import",
                               bech32_suiprivkey(bytes.fromhex(seed)), "ed25519"], capture_output=True, env=env)
    assert imported.returncode == 0, "could not import the throwaway test key"
    key = wallet / "test.key"
    key.touch(mode=0o600)
    key.write_text(seed + "\n")
    address = subprocess.check_output([str(DAEMON), "--derive-address", str(key)], text=True).strip()
    subprocess.run([live.network_sui, "client", "--client.config", str(wallet / "client.yaml"), "switch", "--address", address],
                   check=True, capture_output=True)
    return wallet / "client.yaml", address


def sweep(live, seed, root, ledger, label="sweep the test key"):
    """Send everything a throwaway key holds back to the operator. The sweep
    is a charged mutation like any other: its gas is reserved against
    `ledger` before submission (this run's key: its charged funding; a
    previous run's key: this run's own allowance, since that funding was
    charged to the earlier run), and what it returns never replenishes
    either."""
    config, address = key_wallet(live, seed, root, "sweep-wallet")
    time.sleep(3)  # Reads trail the key's own last transactions.
    amount = live.balance(address)
    if amount < 5_000_000:
        return 0
    try:
        live.execute(label, ["ptb", "--transfer-objects", "[gas]", f"@{live.operator}"], gas=SWEEP_GAS, sender=(config, ledger))
    except Spent:
        raise
    except RuntimeError as error:
        # Charged (or, with no outcome, fully reserved) all the same.
        time.sleep(5)
        assert live.balance(address) < 5_000_000, f"sweep failed and the test key still holds funds: {error}"
        return 0  # Already swept (the read was stale).
    return amount


def pending_key(path, seed):
    """Keep a funded test key's seed (0600, beside the wallet) until its sweep
    succeeds, so a failed run never strands the funding."""
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(seed + "\n")


def recover_key(live, path, root):
    """Sweep a test key an earlier run left funded, charged to this run's
    allowance; returns the MIST recovered. The file goes only after a sweep."""
    if not path.exists():
        return 0
    recovered = sweep(live, path.read_text().strip(), root, live.ledger, label=f"recover {path.name}")
    path.unlink()
    live.evidence.setdefault("recovered_from_previous_run_mist", {})[path.name] = recovered
    print(f"recovered {recovered / 1e9:.4f} SUI from a previous run's {path.name}")
    return recovered


def deployment_args(walrus, version, run_id, *, fail="none", name=component.DEFAULT_COMPONENT):
    """create_deployment_verified's first five arguments, with the recipe and
    artifact stored in `walrus`."""
    artifact = component.build(version, run_id, fail, component=name)
    tag = f"{name}-{version}".replace(".", "-")
    artifact_blob = walrus.put(f"artifact-{tag}", artifact)
    recipe = component.recipe(artifact, artifact_blob)
    recipe_blob = walrus.put(f"recipe-{tag}", recipe)
    return [f'"{name}"', f'"{version}"', f'"{recipe_blob}"', f'"{hashlib.sha256(artifact).hexdigest()}"',
            f'"{hashlib.sha256(recipe).hexdigest()}"']


def ids(values):
    return "vector[" + ", ".join(f"@{v}" for v in values) + "]"


def start_daemon(root, name, manifest, device_section):
    """The real daemon with the SF-030 test updater; returns (process, components dir)."""
    state = root / f"state-{name}"
    components = state / "test-components"
    template = root / f"template-{name}.toml"
    template.write_text(device_section + f'[apply]\ncommand = {json.dumps(f"/bin/sh {component.UPDATER} --root {components}")}\n'
                        f'timeout_seconds = 60\n[runtime]\nstate_dir = {json.dumps(str(state))}\npoll_seconds = 3\nevent_scan_seconds = 0\n')
    release = root / f"release-{name}"
    dm.generate(manifest, release, template)
    # The child keeps its own copy of the log descriptor; the parent's closes here.
    with (root / f"daemon-{name}.log").open("w") as log:
        process = subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(release / "config.toml")},
                                   stdout=log, stderr=log)
    journal = state / "state.json"
    try:
        wait_for(lambda: journal.exists() and json.loads(journal.read_text()).get("initialized"), f"{name} daemon did not enroll", process)
    except BaseException:
        # The caller never receives the handle, so a funded daemon must not
        # outlive a failed startup (or the run's network lock).
        stop_daemon(process)
        raise
    return process, components


def stop_daemon(process):
    """Kill and reap a daemon that is still running."""
    if process.poll() is None:
        process.kill()
    process.wait(timeout=30)


def history(components):
    path = components / component.DEFAULT_COMPONENT / "history.jsonl"
    return [json.loads(line)["version"] for line in path.read_text().splitlines()] if path.exists() else []


def contracts_at(ref, destination):
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", ref, "contracts"], check=True, capture_output=True).stdout
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-x", "-C", str(destination)], input=archive, check=True)
    return destination / "contracts"


def stage(source, package_dir):
    if package_dir.exists():
        shutil.rmtree(package_dir)
    shutil.copytree(source, package_dir, ignore=shutil.ignore_patterns("build", "Published.toml"))
