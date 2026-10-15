"""Shared plumbing for live-network acceptance runs (SF-010; docs/TESTNET.md).

Everything here reads and writes the chain through the Sui CLI and gRPC
(`chain_facts`): public testnet fullnodes do not serve JSON-RPC. Nothing
here faucets, reads a default wallet or prints a key.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import deployment_manifest as dm
import test_component as component
from testnet_budget import Budget, Spent  # noqa: F401 (re-exported to the suites)

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
CLOCK = "@0x6"
CHAINS = {"testnet": "4c78adac"}
# The public Walrus endpoints refuse Python's default User-Agent (HTTP 403).
AGENT = {"User-Agent": "suifleet-acceptance/1"}
WALRUS_TESTNET = ("https://publisher.walrus-testnet.walrus.space", "https://aggregator.walrus-testnet.walrus.space")
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

    def __init__(self, wallet, network, max_spend_mist):
        self.wallet = Path(wallet).resolve()
        self.network = network
        self.max_spend = max_spend_mist
        self.evidence = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "network": network,
                         "transactions": [], "checks": []}
        self.ledger = Budget(max_spend_mist, self.evidence["transactions"])

    def client(self, *command, json_output=True, check=True, config=None):
        full = ["sui", "client", "--client.config", str(config or self.wallet), *map(str, command)]
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

    def preflight(self):
        envs, active_env = self.client("envs")
        self.rpc_url = next(e["rpc"] for e in envs if e["alias"] == active_env)
        self.operator = dm.object_id(self.client("active-address", json_output=False).stdout.strip().split()[-1])
        self.chain = dm.chain_facts(self.rpc_url)["chain_identifier"]
        if self.network == "localnet":
            assert any(h in self.rpc_url for h in ("127.0.0.1", "localhost")), "localnet profile needs a loopback node"
        else:
            assert self.chain == CHAINS[self.network], f"active env {active_env} is chain {self.chain}, not {self.network}"
            assert self.operator in TESTNET_WALLETS, "wallet is outside the dedicated testnet grant"
        self.start_balance = self.balance()
        assert self.start_balance >= self.max_spend, (
            f"{self.operator} holds {self.start_balance} MIST; this run may spend up to {self.max_spend}. "
            "Fund it first (docs/TESTNET.md)")
        self.evidence.update(chain=self.chain, rpc=self.rpc_url, operator=self.operator, start_balance_mist=self.start_balance,
                             max_spend_mist=self.max_spend,
                             sui=subprocess.run(["sui", "--version"], capture_output=True, text=True).stdout.strip(),
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

    def publish(self, label, package_dir, pubfile, upgrade_cap=None):
        """Ephemeral publication (or upgrade) of a contracts tree."""
        common = ["--build-env", "testnet", "--pubfile-path", pubfile, "--silence-warnings"]
        args = ["test-upgrade", package_dir, "--upgrade-capability", upgrade_cap] if upgrade_cap else ["test-publish", package_dir]
        return self.execute(label, [*args, *common], gas=400_000_000)

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
        request = urllib.request.Request(f"{self.publisher}/v1/blobs?epochs=1", data=body, method="PUT", headers=AGENT)
        with urllib.request.urlopen(request, timeout=120) as response:
            stored = json.load(response)
        blob = stored.get("newlyCreated", {}).get("blobObject", {}).get("blobId") or stored.get("alreadyCertified", {}).get("blobId")
        assert blob, f"Walrus publisher returned no blob ID: {stored}"
        # Read it back through the aggregator before relying on it.
        wait_for(lambda: self.readable(blob, body), f"Walrus blob {blob} not readable from the aggregator", timeout=120)
        return blob

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
                        f'timeout_seconds = 60\n[runtime]\nstate_dir = {json.dumps(str(state))}\npoll_seconds = 3\n')
    release = root / f"release-{name}"
    dm.generate(manifest, release, template)
    log = (root / f"daemon-{name}.log").open("w")
    process = subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(release / "config.toml")},
                               stdout=log, stderr=log)
    journal = state / "state.json"
    wait_for(lambda: journal.exists() and json.loads(journal.read_text()).get("initialized"), f"{name} daemon did not enroll", process)
    return process, components


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
