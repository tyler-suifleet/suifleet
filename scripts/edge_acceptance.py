#!/usr/bin/env python3
"""Wallet-to-edge OTA acceptance runner (SF-065, the SF-031 harness).

    edge_acceptance.py plan      --profile PROFILE.json
    edge_acceptance.py preflight --profile PROFILE.json [--hardware] [lock identity]
    edge_acceptance.py run       --profile PROFILE.json --evidence OUT.json [--hardware] [lock identity]
                                 [--allow-incomplete]
    edge_acceptance.py summarize EVIDENCE.json...

A profile names everything explicitly: the network (localnet or testnet), a
verified deployment manifest, the wallet account, the Walrus endpoints and the
device. Nothing is discovered or guessed: no default SSH host, no scanned
disk, no default wallet. Mainnet and every other network are refused.

Devices:
  local  the real daemon on this machine as a stand-in for the Pi, with a
         throwaway key. It exercises the runner, chain and Walrus end to end,
         but is never hardware evidence.
  ssh    a physical Pi running the SF-029 image, reached over SSH with a
         pinned host-key fingerprint. Needs --hardware as well; the
         hardware:raspberrypi3bplus lock is held only while the run touches
         the device. The harness never reads, copies or replaces the device
         key and never deletes journals: it only compares the key's digest
         before and after.

Wallets:
  scripted   a test account through the Sui CLI (explicit client.yaml).
  extension  a real browser-extension wallet: the operator performs each
             step in the dApp and enters its transaction digest, which the
             harness verifies on chain. Without a digest the step is blocked.

Every acceptance case ends passed, failed, skipped or blocked. The verdict is
"passed" only when every baseline case passed. A local stand-in, a scripted
wallet or a pending SF-022 encrypted profile always leaves cases skipped or
blocked, so the verdict is then "incomplete", never a pass. `summarize`
combines runs (for example an extension-wallet run and a scripted
failure-path run on the same Pi) into the SF-031 matrix.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import deployment_manifest as dm
import test_component as component
from testnet_budget import Budget

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
CLOCK = "@0x6"
TESTNET_CHAIN = "4c78adac"
# The user's standing grant covers only these dedicated testnet accounts.
DEDICATED_TESTNET = {
    "agent": "0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965",
    "ci": "0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac",
}
TESTNET_MAX_SPEND = 500_000_000  # MIST; a single run never needs more.
DEVICE_FUNDING = 30_000_000  # MIST of report gas sent to the device.
HARDWARE_LOCK = "hardware:raspberrypi3bplus"
TESTNET_LOCK = "network:suifleet-testnet"
LOCK_RENEW_SECONDS = 240
STATUSES = ("passed", "failed", "skipped", "blocked")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
HOSTNAME = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
DEVICE_PATH = re.compile(r"/[A-Za-z0-9._/-]+")
SECRET_KEYS = re.compile(r"(private|secret|mnemonic|phrase|password|passphrase|seed|token)", re.I)
SECRET_VALUES = re.compile(r"suiprivkey1[0-9a-z]+|-----BEGIN [A-Z ]*PRIVATE KEY-----")


class ProfileError(ValueError):
    """The profile or environment cannot support the requested run."""


class CaseFailure(AssertionError):
    pass


class CaseBlocked(Exception):
    pass


# ── Acceptance matrix ─────────────────────────────────────────────────────────

class Case:
    def __init__(self, id, capability, needs=(), future=False):
        self.id, self.capability, self.needs, self.future = id, capability, frozenset(needs), future


# needs: hardware (physical Pi), extension (real browser wallet), scripted
# (test-account automation constructing deliberately bad deployments),
# encrypted (SF-022 confidential delivery).
CASES = (
    Case("preflight", "network, manifest, account, funding, Walrus and device preflight"),
    Case("image-identity", "SF-029 image: aarch64, packaged binary/updater hashes, first-boot identity and key mode", {"hardware"}),
    Case("artifacts", "isolated test-component artifacts and recipes uploaded and read back by SHA-256"),
    Case("enrollment", "register/issue-cap/activate through the wallet, verified effects; enrolled daemon started before any test deployment"),
    Case("baseline-skip", "a deployment created before enrollment never runs"),
    Case("direct-v1", "direct deployment v1: Applied on chain, device receipt names v1, this run ID and the artifact SHA-256"),
    Case("report-retry", "apply succeeds while the device cannot pay for its report; the report is retried once gas arrives", {"scripted"}),
    Case("group-v2", "group deployment v2: Applied on chain, device receipt names v2"),
    Case("updater-install-failure", "injected install failure: Failed on chain, previous version kept", {"scripted"}),
    Case("updater-health-failure", "injected health failure: Failed on chain, receipt says health failed", {"scripted"}),
    Case("recipe-tamper", "recipe bytes not matching the on-chain digest: Failed, updater never invoked", {"scripted"}),
    Case("artifact-tamper", "artifact bytes not matching the authenticated hash: Failed, updater never invoked", {"scripted"}),
    Case("unauthorized-target", "deployment for another device never runs on this one", {"scripted"}),
    Case("offline-catch-up", "deployment created while the daemon is stopped is applied after it starts", {"scripted"}),
    Case("daemon-restart", "daemon restart keeps the journal and replays no applied work"),
    Case("key-journal-preserved", "device key digest, address and journal unchanged by the run"),
    Case("cleanup", "test-owned deployments, group and decoy device deleted; device identity kept"),
    Case("extension-wallet", "a real browser-extension wallet signed enrollment and deployments in the dApp", {"extension"}),
    Case("encrypted-delivery", "SF-022 Seal-encrypted artifact/recipe: permitted device decrypts; unauthorized, revoked, tampered and key-service failure never run", {"encrypted"}),
    # Growth: features that exist or will land; each needs its own case.
    Case("supervised-lifecycle", "SF-014 supervised run/stop/restart of a component on the Pi", future=True),
    Case("component-configuration", "SF-015 configuration merge/reset on the Pi", future=True),
    Case("desired-state", "SF-013 desired-state reconciliation on the Pi", future=True),
    Case("daemon-self-update", "SF-026 daemon self-update and fallback on the Pi", future=True),
    Case("platform-restart-recovery", "platform-owned reboot/power-loss with journal preservation (SF-016/SF-025)", future=True),
)
CASE_IDS = tuple(case.id for case in CASES)


class Results:
    def __init__(self):
        self.cases = {case.id: {"status": None, "capability": case.capability, "detail": ""} for case in CASES}

    def set(self, id, status, detail=""):
        assert status in STATUSES and id in self.cases, (id, status)
        entry = self.cases[id]
        # A failure is final; a pass never overrides a failure or block.
        if entry["status"] in ("failed", "blocked") and status == "passed":
            return
        if entry["status"] == "failed":
            return
        entry.update(status=status, detail=str(detail)[:2000])
        print(f"{status.upper()}: {id}: {detail}" if detail else f"{status.upper()}: {id}", flush=True)

    def pending(self, id):
        return self.cases[id]["status"] is None

    def finish(self, reason="not reached"):
        for id, entry in self.cases.items():
            if entry["status"] is None:
                entry.update(status="blocked", detail=reason)
        return self.cases


def mode_gaps(profile):
    """Cases this run cannot establish, by mode: {case: (status, reason)}."""
    gaps = {}
    hardware = profile["device"]["mode"] == "ssh"
    wallet = profile["wallet"]["mode"]
    for case in CASES:
        if case.future:
            gaps[case.id] = ("skipped", "not exercised by the public-artifact baseline; add a case when acceptance covers it")
        elif "hardware" in case.needs and not hardware:
            gaps[case.id] = ("skipped", "local stand-in device; physical Raspberry Pi evidence is required (SF-031)")
        elif "extension" in case.needs and wallet != "extension":
            gaps[case.id] = ("blocked", "scripted test-account automation does not establish browser-wallet behavior; run an extension-wallet profile")
        elif "scripted" in case.needs and wallet != "scripted":
            gaps[case.id] = ("skipped", "deliberately invalid deployments need the scripted test-account profile")
        elif "encrypted" in case.needs:
            gaps[case.id] = ("blocked", "SF-022 Seal-encrypted delivery is not implemented; the public-artifact baseline does not establish it")
    if profile["artifact_profile"] == "sf022-encrypted":
        for case in CASES:
            gaps.setdefault(case.id, ("blocked", "the SF-022 encrypted profile cannot run until SF-022 lands"))
    return gaps


def verdict(cases):
    statuses = [entry["status"] for entry in cases.values()]
    if "failed" in statuses:
        return "failed"
    return "passed" if all(s == "passed" for id, s in zip(cases, statuses) if not _future(id)) else "incomplete"


def _future(id):
    return next(case.future for case in CASES if case.id == id)


# ── Profile validation ────────────────────────────────────────────────────────

def _address(value, what):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value):
        raise ProfileError(f"{what} must be a 0x Sui address")
    return dm.object_id(value)


def _no_secrets(value, path="profile"):
    """Profiles name files and accounts, never key material."""
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEYS.search(str(key)):
                raise ProfileError(f"{path}.{key}: profiles must not contain keys, tokens or recovery phrases")
            _no_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _no_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str) and SECRET_VALUES.search(value):
        raise ProfileError(f"{path}: looks like private key material")


def _loopback(url):
    return urlparse(url).hostname in LOOPBACK


def _host(value):
    if not isinstance(value, str) or not value or value in ("auto", "*") or any(c in value for c in "*?[] @"):
        raise ProfileError("device.host must be an explicit address or host name; nothing is discovered")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not HOSTNAME.fullmatch(value):
            raise ProfileError("device.host is not a valid host name") from None
        return value
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ProfileError("device.host must name the Pi itself")
    return value


def _device_path(value, what):
    if not isinstance(value, str) or not DEVICE_PATH.fullmatch(value) or ".." in value.split("/"):
        raise ProfileError(f"{what} must be an absolute path without '..'")
    return value


def load_profile(path, base=None):
    path = Path(path)
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ProfileError(f"cannot read profile {path}: {error}") from None
    return validate_profile(document, base or path.parent)


def validate_profile(document, base):
    """Normalized profile, or ProfileError naming the first problem."""
    base = Path(base)
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ProfileError("profile schema must be 1")
    _no_secrets(document)
    known = {"schema", "profile", "manifest", "wallet", "walrus", "device", "artifact_profile", "budget", "faucet_url", "enrollment"}
    if set(document) - known:
        raise ProfileError(f"unknown profile fields: {sorted(set(document) - known)}")
    network = document.get("profile")
    if network not in ("localnet", "testnet"):
        raise ProfileError("profile must be localnet or testnet; mainnet and other networks are refused")
    resolve = lambda value: (base / value).resolve() if not Path(value).is_absolute() else Path(value)
    if not isinstance(document.get("manifest"), str):
        raise ProfileError("manifest path is required")
    manifest_path = resolve(document["manifest"])
    try:
        manifest = dm.validate(json.loads(manifest_path.read_text()))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProfileError(f"manifest {manifest_path}: {error}") from None
    if manifest["network"] != network:
        raise ProfileError(f"manifest is for {manifest['network']}, profile is {network}")
    if network == "testnet" and manifest["chain_identifier"] != TESTNET_CHAIN:
        raise ProfileError("testnet manifest names another chain")
    if network == "localnet" and not _loopback(manifest["rpc_url"]):
        raise ProfileError("localnet profiles need a loopback (disposable) node")

    wallet = dict(document.get("wallet") or {})
    if wallet.get("mode") not in ("scripted", "extension"):
        raise ProfileError("wallet.mode must be scripted or extension")
    wallet["address"] = _address(wallet.get("address"), "wallet.address")
    if wallet["mode"] == "extension":
        if not isinstance(wallet.get("dapp_url"), str) or urlparse(wallet["dapp_url"]).scheme not in ("http", "https"):
            raise ProfileError("an extension wallet needs wallet.dapp_url (the dApp the operator signs in)")
        if not isinstance(wallet.get("client_config"), str):
            raise ProfileError("an extension profile still needs wallet.client_config for chain reads and device funding")
    if not isinstance(wallet.get("client_config"), str):
        raise ProfileError("a scripted wallet needs an explicit wallet.client_config (never the default wallet)")
    wallet["client_config"] = resolve(wallet["client_config"])
    if not wallet["client_config"].is_file():
        raise ProfileError(f"wallet.client_config {wallet['client_config']} does not exist")
    if network == "testnet" and wallet["address"] not in DEDICATED_TESTNET.values():
        raise ProfileError("testnet runs may only use the dedicated agent or CI testnet account")

    budget = dict(document.get("budget") or {})
    limit = budget.get("max_spend_mist")
    if network == "testnet":
        if type(limit) is not int or not 0 < limit <= TESTNET_MAX_SPEND:
            raise ProfileError(f"testnet needs budget.max_spend_mist in 1..{TESTNET_MAX_SPEND}")
    elif limit is None:
        limit = 5_000_000_000
    elif type(limit) is not int or limit <= 0:
        raise ProfileError("budget.max_spend_mist must be a positive integer")
    budget["max_spend_mist"] = limit

    faucet = document.get("faucet_url")
    if faucet is not None and (network != "localnet" or not _loopback(faucet)):
        raise ProfileError("faucet_url is only for a loopback localnet faucet")

    device = dict(document.get("device") or {})
    if device.get("mode") == "local":
        device["daemon"] = resolve(device.get("daemon") or str(DAEMON))
        if device.get("root") is not None:
            device["root"] = resolve(device["root"])
    elif device.get("mode") == "ssh":
        device["host"] = _host(device.get("host"))
        if not isinstance(device.get("user"), str) or not USER.fullmatch(device["user"]):
            raise ProfileError("device.user must be the explicit SSH login (operator on the SF-029 image)")
        port = device.get("port", 22)
        if type(port) is not int or not 0 < port < 65536:
            raise ProfileError("device.port is invalid")
        device["port"] = port
        if not isinstance(device.get("host_key_sha256"), str) or not FINGERPRINT.fullmatch(device["host_key_sha256"]):
            raise ProfileError("device.host_key_sha256 must be the independently verified SHA256: host-key fingerprint")
        if not isinstance(device.get("identity_file"), str):
            raise ProfileError("device.identity_file (the SSH private key path on this host) is required")
        device["identity_file"] = resolve(device["identity_file"])
        if not device["identity_file"].is_file():
            raise ProfileError(f"device.identity_file {device['identity_file']} does not exist")
        expected = device.get("expected_binary_sha256")
        if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
            raise ProfileError("device.expected_binary_sha256 must be a SHA-256 hex digest")
        device.setdefault("expected_arch", "aarch64")
        for name, default in (("config", "/etc/suifleet/config.toml"), ("key", "/etc/suifleet/device.key"),
                              ("state_dir", "/var/lib/suifleet"), ("binary", "/usr/bin/suifleet"),
                              ("updater", "/usr/bin/suifleet-test-updater")):
            device[name] = _device_path(device.get(name, default), f"device.{name}")
        if device["key"] != "/etc/suifleet/device.key" or device["state_dir"] != "/var/lib/suifleet":
            raise ProfileError("the SF-029 image keeps the key at /etc/suifleet/device.key and state at /var/lib/suifleet")
        device["allow_replace_enrollment"] = device.get("allow_replace_enrollment") is True
    else:
        raise ProfileError("device.mode must be local (stand-in) or ssh (physical Pi)")

    walrus = dict(document.get("walrus") or {})
    if walrus.get("mode") == "stand-in":
        bind = walrus.get("bind", "127.0.0.1")
        try:
            bind_ip = ipaddress.ip_address(bind)
        except ValueError:
            raise ProfileError("walrus.bind must be an IP address") from None
        if device["mode"] == "ssh" and (bind_ip.is_loopback or bind_ip.is_unspecified):
            raise ProfileError("a Pi cannot reach a loopback stand-in; bind it to this host's explicit LAN address")
        if device["mode"] == "local" and not bind_ip.is_loopback:
            raise ProfileError("a local stand-in device uses a loopback Walrus stand-in")
        walrus["bind"] = bind
    elif walrus.get("mode") == "http":
        for name in ("publisher_url", "aggregator_url"):
            try:
                walrus[name] = dm.endpoint(walrus.get(name))
            except (ValueError, TypeError, AttributeError):
                raise ProfileError(f"walrus.{name} must be an http(s) URL") from None
            if walrus[name] != manifest["walrus"][name]:
                raise ProfileError(f"walrus.{name} differs from the manifest's")
        epochs = walrus.get("epochs", 1)
        if type(epochs) is not int or not 0 < epochs <= 5:
            raise ProfileError("walrus.epochs must be 1..5")
        walrus["epochs"] = epochs
    else:
        raise ProfileError("walrus.mode must be stand-in or http")

    artifact_profile = document.get("artifact_profile", "public")
    if artifact_profile not in ("public", "sf022-encrypted"):
        raise ProfileError("artifact_profile must be public or sf022-encrypted")
    enrollment = document.get("enrollment")
    if enrollment is not None:
        enrollment = {name: _address(enrollment.get(name), f"enrollment.{name}")
                      for name in ("device_object_id", "device_cap_id", "owner_cap_id")}
    return {"profile": network, "manifest_path": manifest_path, "manifest": manifest, "wallet": wallet,
            "walrus": walrus, "device": device, "artifact_profile": artifact_profile, "budget": budget,
            "faucet_url": faucet, "enrollment": enrollment}


def needed_locks(profile, mutate=True):
    locks = []
    if profile["device"]["mode"] == "ssh":
        locks.append(HARDWARE_LOCK)
    if profile["profile"] == "testnet" and mutate:
        locks.append(TESTNET_LOCK)
    return locks


# ── Coordination locks ────────────────────────────────────────────────────────

class ResourceLock:
    """A global ticket lock, asserted before each use and renewed while held."""

    def __init__(self, resource, identity, purpose, runner=subprocess.run, clock=time.monotonic, token=None):
        self.resource, self.identity, self.purpose = resource, identity, purpose
        self.runner, self.clock = runner, clock
        self.token, self.external = token, token is not None
        self.renewed = clock()

    def _ticket(self, *args):
        result = self.runner(["ticket", "--project", "suifleet", *args], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ProfileError(f"ticket {args[0]} {self.resource} failed: {(result.stderr or result.stdout).strip()[-500:]}")
        return result.stdout

    def acquire(self):
        if self.token:
            return self.assert_()
        if not self.identity:
            raise ProfileError(f"{self.resource} must be locked: pass --agent, --model and --session")
        out = self._ticket("lock", self.resource, "--agent", self.identity["agent"], "--model", self.identity["model"],
                           "--session", self.identity["session"], "--purpose", self.purpose,
                           *(["--ticket", self.identity["ticket"]] if self.identity.get("ticket") else []))
        try:
            parsed = json.loads(out)
            self.token = parsed.get("lock_token") or parsed.get("token")
        except ValueError:
            self.token = out.strip().split()[-1] if out.strip() else None
        if not self.token:
            raise ProfileError(f"ticket lock {self.resource} returned no token")
        self.renewed = self.clock()
        return self

    def assert_(self):
        self._ticket("lock-assert", self.resource, "--lock-token", self.token)
        if self.clock() - self.renewed >= LOCK_RENEW_SECONDS:
            self._ticket("lock-renew", self.resource, "--lock-token", self.token)
            self.renewed = self.clock()
        return self

    def release(self):
        if self.token and not self.external:
            try:
                self._ticket("unlock", self.resource, "--lock-token", self.token)
            finally:
                self.token = None


class NoLock:
    def acquire(self):
        return self

    def assert_(self):
        return self

    def release(self):
        pass


# ── Sui CLI wallet and chain reads ────────────────────────────────────────────

def plain(value):
    """Move JSON with or without JSON-RPC-style {"fields": ...} wrappers."""
    if isinstance(value, dict):
        if set(value) <= {"type", "fields"} and "fields" in value:
            return plain(value["fields"])
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


class SuiCli:
    """`sui client` with an explicit client.yaml; never the default wallet."""

    def __init__(self, config, runner=subprocess.run):
        self.config, self.runner = Path(config), runner

    def __call__(self, *args, json_output=True):
        command = ["sui", "client", "--client.config", str(self.config), *map(str, args)]
        if json_output:
            command.append("--json")
        result = self.runner(command, text=True, capture_output=True, timeout=600)
        # Executed-but-failed transactions still print effects for the ledger.
        if result.returncode and not (json_output and '"effects"' in (result.stdout or "")):
            raise RuntimeError(f"sui client {args[0]} failed:\n{(result.stderr or '')[-2000:]}\n{(result.stdout or '')[-2000:]}")
        if not json_output:
            return result.stdout
        out = result.stdout
        return json.loads(out[min(i for i in (out.find("{"), out.find("[")) if i >= 0):])

    def active_address(self):
        return dm.object_id(self("active-address", json_output=False).strip().split()[-1])

    def balance(self, address=None):
        gas = self("gas", *([address] if address else []))
        coins = gas.get("gasCoins", []) if isinstance(gas, dict) else gas
        return sum(int(c.get("mistBalance", 0)) for c in coins) + (
            int(gas.get("addressMistBalance") or 0) if isinstance(gas, dict) else 0)

    def receipts(self, deployment):
        """Authoritative statuses read independently of the daemon: device -> status."""
        for field in self("dynamic-field", deployment).get("dynamicFields", []):
            value = self("object", field["fieldId"])
            if "ReceiptsKey" not in str(value.get("objType") or value.get("type") or ""):
                continue
            receipt = plain(value.get("content", {}))["value"]
            if int(receipt["version"]) < 2:
                return {}  # Legacy statuses are never authoritative.
            return {dm.object_id(e["key"]): int(e["value"]) for e in receipt["statuses"]["contents"]}
        return {}

    def transaction_block(self, digest):
        return self("tx-block", digest)


def created(result, suffix):
    return [c["objectId"] for c in result.get("objectChanges", [])
            if c.get("type") == "created" and str(c.get("objectType", "")).endswith(suffix)]


def sender_of(result):
    tx = result.get("transaction", {})
    data = tx.get("data", tx)
    return data.get("sender")


# ── Walrus ────────────────────────────────────────────────────────────────────

class StandInWalrus:
    """Serves this run's blobs over HTTP: the local Walrus aggregator stand-in."""

    def __init__(self, bind):
        self.blobs, self.downloads = {}, []
        walrus = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                walrus.downloads.append(self.path)
                body = walrus.blobs.get(self.path.removeprefix("/v1/blobs/"))
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

        self.server = ThreadingHTTPServer((bind, 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host = f"[{bind}]" if ":" in bind else bind
        self.url = f"http://{host}:{self.server.server_port}"
        self.publisher_url = self.aggregator_url = self.url

    def store(self, name, data):
        blob_id = name  # Walrus blob IDs are [A-Za-z0-9_-]; stand-in IDs are names.
        self.blobs[blob_id] = data
        return blob_id

    def fetch(self, blob_id):
        with urlopen(f"{self.url}/v1/blobs/{blob_id}", timeout=30) as response:
            return response.read()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HttpWalrus:
    """A real Walrus publisher/aggregator named by the profile and manifest."""

    def __init__(self, publisher_url, aggregator_url, epochs, opener=urlopen):
        self.publisher_url, self.aggregator_url, self.epochs, self.opener = publisher_url, aggregator_url, epochs, opener

    def store(self, name, data):
        request = Request(f"{self.publisher_url.rstrip('/')}/v1/blobs?epochs={self.epochs}", data=data, method="PUT")
        with self.opener(request, timeout=120) as response:
            reply = json.loads(response.read())
        info = reply.get("newlyCreated", {}).get("blobObject") or reply.get("alreadyCertified") or {}
        blob_id = info.get("blobId")
        if not blob_id or not re.fullmatch(r"[A-Za-z0-9_-]+", blob_id):
            raise CaseFailure(f"Walrus publisher returned no blob ID for {name}")
        return blob_id

    def fetch(self, blob_id):
        with self.opener(f"{self.aggregator_url.rstrip('/')}/v1/blobs/{blob_id}", timeout=120) as response:
            return response.read()

    def close(self):
        pass


# ── Devices ───────────────────────────────────────────────────────────────────

def device_template(device, ids, apply_command, state_dir, poll, signer=None, keypair=None):
    """The device half of config.toml, for deployment_manifest.generate."""
    lines = ["[device]"]
    if keypair:
        lines.append(f"keypair_path = {json.dumps(str(keypair))}")
    lines += [f'device_object_id = "{ids["device"]}"', f'device_cap_id = "{ids["cap"]}"']
    if signer:
        lines += ["", "[device.signer]", 'kind = "service"', f"socket = {json.dumps(signer)}"]
    lines += ["", "[apply]", f"command = {json.dumps(apply_command)}", 'mode = "direct"',
              "allow_recipe_commands = false", "timeout_seconds = 120",
              "", "[runtime]", f"state_dir = {json.dumps(str(state_dir))}", f"poll_seconds = {poll}", ""]
    return "\n".join(lines)


def enrolled_ids(config_text):
    found = dict(re.findall(r'^\s*(device_object_id|device_cap_id)\s*=\s*"(0x[0-9a-fA-F]+)"', config_text, re.M))
    return {k: dm.object_id(v) for k, v in found.items()}


class LocalDevice:
    """The real daemon on this host: a stand-in that is never hardware evidence."""

    kind = "local-stand-in"

    def __init__(self, profile):
        self.daemon = Path(profile["device"]["daemon"])
        if not self.daemon.is_file():
            raise ProfileError(f"daemon {self.daemon} is not built (cargo build --locked --bins)")
        given = profile["device"].get("root")
        self.temporary = None if given else tempfile.mkdtemp(prefix="suifleet-edge-acceptance-")
        self.root = Path(given or self.temporary)
        self.root.mkdir(parents=True, exist_ok=True)
        self.key = self.root / "device.key"
        self.state = self.root / "state"
        self.etc = self.root / "etc"
        self.components = self.state / "test-components"
        self.process = None
        self.log_path = self.root / "daemon.log"
        self.log = None

    def assert_lock(self):
        pass

    def address(self):
        if not self.key.exists():
            subprocess.run([str(self.daemon), "--provision", str(self.key)], check=True, capture_output=True, text=True)
        return dm.object_id(subprocess.check_output([str(self.daemon), "--derive-address", str(self.key)], text=True).strip())

    def facts(self):
        address = self.address()
        return {"kind": self.kind, "arch": platform.machine(),
                "binary_sha256": hashlib.sha256(self.daemon.read_bytes()).hexdigest(),
                "updater_sha256": hashlib.sha256(component.UPDATER.read_bytes()).hexdigest(),
                "address": address, "key_mode": oct(self.key.stat().st_mode & 0o777)}

    def key_digest(self):
        return hashlib.sha256(self.key.read_bytes()).hexdigest()

    def current_enrollment(self):
        config = self.etc / "config.toml"
        return enrolled_ids(config.read_text()) if config.exists() else None

    def template(self, ids):
        return device_template(self, ids, f"/bin/sh {component.UPDATER} --root {self.components}",
                               self.state, 1, keypair=self.key)

    def install(self, bundle):
        self.etc.mkdir(exist_ok=True)
        for name in ("deployment.json", "config.toml"):  # Manifest first, config last.
            staging = self.etc / f".{name}.new"
            shutil.copyfile(bundle / name, staging)
            staging.replace(self.etc / name)

    def start(self):
        if self.running():
            return
        self.log = self.log or self.log_path.open("a")
        self.process = subprocess.Popen([str(self.daemon)], env={**os.environ, "EDGE_CONFIG": str(self.etc / "config.toml")},
                                        stdout=self.log, stderr=self.log)

    def stop(self):
        if self.running():
            self.process.send_signal(signal.SIGINT)
            self.process.wait(timeout=60)
            if self.process.returncode != 0:
                raise CaseFailure(f"daemon exited {self.process.returncode} on SIGINT")
        self.process = None

    def restart(self):
        self.stop()
        self.start()

    def running(self):
        return self.process is not None and self.process.poll() is None

    def exited(self):
        return self.process is not None and self.process.poll() is not None

    def journal(self):
        path = self.state / "state.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def history(self):
        path = self.components / component.DEFAULT_COMPONENT / "history.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def versions(self):
        path = self.components / component.DEFAULT_COMPONENT / "versions"
        return sorted(p.name for p in path.iterdir()) if path.exists() else []

    def logs(self):
        if self.log:
            self.log.flush()
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""

    def close(self):
        try:
            if self.running():
                self.process.kill()
                self.process.wait()
        finally:
            if self.log:
                self.log.close()
            if self.temporary:
                shutil.rmtree(self.temporary, ignore_errors=True)


class SshDevice:
    """A physical Pi over SSH with a pinned host key. Only fixed commands run."""

    kind = "hardware"
    SIGNER_SOCKET = "/run/suifleet-signer/sign.sock"

    def __init__(self, profile, lock, runner=subprocess.run):
        self.profile, self.device, self.lock, self.runner = profile, profile["device"], lock, runner
        self.workdir = Path(tempfile.mkdtemp(prefix="suifleet-edge-ssh-"))
        self.known_hosts = self.workdir / "known_hosts"
        self.components = f"{self.device['state_dir']}/test-components"

    def assert_lock(self):
        self.lock.assert_()

    def pin_host_key(self):
        """Accept only host keys matching the independently verified fingerprint."""
        self.assert_lock()
        scan = self.runner(["ssh-keyscan", "-T", "10", "-p", str(self.device["port"]), self.device["host"]],
                           capture_output=True, text=True, timeout=60)
        matching = []
        for line in scan.stdout.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            fingerprint = self.runner(["ssh-keygen", "-lf", "-", "-E", "sha256"], input=line + "\n",
                                      capture_output=True, text=True, timeout=30)
            if fingerprint.returncode == 0 and self.device["host_key_sha256"] in fingerprint.stdout.split():
                matching.append(line)
        if not matching:
            raise ProfileError(f"no host key at {self.device['host']} matches the pinned fingerprint; refusing to connect")
        self.known_hosts.write_text("\n".join(matching) + "\n")

    def ssh(self, *command, input=None, check=True):
        self.assert_lock()
        remote = shlex.join(command)
        result = self.runner(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                              "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
                              "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
                              "-i", str(self.device["identity_file"]), "-p", str(self.device["port"]),
                              f"{self.device['user']}@{self.device['host']}", "--", remote],
                             input=input, capture_output=True, text=True, timeout=180)
        if check and result.returncode:
            raise CaseFailure(f"device command {command[:3]} failed ({result.returncode}): {result.stderr.strip()[-500:]}")
        return result

    def _sudo(self, *command, **kwargs):
        return self.ssh("sudo", "-n", *command, **kwargs)

    def address(self):
        return dm.object_id(self._sudo("cat", "/etc/suifleet/device.address").stdout.strip())

    def facts(self):
        hashes = dict(reversed(line.split(maxsplit=1)) for line in
                      self.ssh("sha256sum", self.device["binary"], self.device["updater"]).stdout.splitlines())
        mode = self._sudo("stat", "-c", "%a %U %G", self.device["key"]).stdout.split()
        return {"kind": self.kind, "arch": self.ssh("uname", "-m").stdout.strip(),
                "build_info": self.ssh("cat", "/usr/share/suifleet/build-info", check=False).stdout.strip()[:2000],
                "binary_sha256": hashes.get(self.device["binary"]), "updater_sha256": hashes.get(self.device["updater"]),
                "address": self.address(), "key_mode": mode[0] if mode else None, "key_owner": mode[1:],
                "provision_service": self.ssh("systemctl", "is-active", "suifleet-provision.service", check=False).stdout.strip(),
                "time_synchronized": self.ssh("timedatectl", "show", "-p", "NTPSynchronized", "--value", check=False).stdout.strip()}

    def key_digest(self):
        """Digest only: the key's bytes never leave the device."""
        return self._sudo("sha256sum", self.device["key"]).stdout.split()[0]

    def current_enrollment(self):
        result = self._sudo("cat", self.device["config"], check=False)
        return enrolled_ids(result.stdout) if result.returncode == 0 else None

    def template(self, ids):
        return device_template(self, ids, self.device["updater"], self.device["state_dir"], 5, signer=self.SIGNER_SOCKET)

    def signer_toml(self, ids):
        return ('manifest_path = "deployment.json"\n'
                f'device_cap_id = "{ids["cap"]}"\n'
                f'keypair_path = "{self.device["key"]}"\n'
                'caller_unit = "suifleet.service"\n')

    def _install(self, path, data, group):
        directory, name = path.rsplit("/", 1)
        staging = f"{directory}/.{name}.new"
        self._sudo("install", "-o", "root", "-g", group, "-m", "0640", "/dev/stdin", staging, input=data)
        self._sudo("mv", "-f", staging, path)

    def install(self, bundle, ids):
        etc = self.device["config"].rsplit("/", 1)[0]
        # deployment.json, then the signer's settings, then config.toml last.
        self._install(f"{etc}/deployment.json", (bundle / "deployment.json").read_text(), "suifleet")
        self._install(f"{etc}/signer.toml", self.signer_toml(ids), "suifleet-signer")
        self._install(self.device["config"], (bundle / "config.toml").read_text(), "suifleet")

    def start(self):
        self._sudo("systemctl", "start", "suifleet-signer.socket", "suifleet.service", "suifleet-enrollment.path")

    def stop(self):
        # The path unit would restart the daemon while config.toml exists.
        self._sudo("systemctl", "stop", "suifleet-enrollment.path", "suifleet.service")

    def restart(self):
        self._sudo("systemctl", "restart", "suifleet.service")

    def running(self):
        return self.ssh("systemctl", "is-active", "suifleet.service", check=False).stdout.strip() == "active"

    def exited(self):
        return self.ssh("systemctl", "is-failed", "suifleet.service", check=False).stdout.strip() == "failed"

    def _read(self, path):
        result = self._sudo("cat", path, check=False)
        return result.stdout if result.returncode == 0 else ""

    def journal(self):
        text = self._read(f"{self.device['state_dir']}/state.json")
        return json.loads(text) if text.strip() else {}

    def history(self):
        text = self._read(f"{self.components}/{component.DEFAULT_COMPONENT}/history.jsonl")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def versions(self):
        result = self._sudo("ls", "-1", f"{self.components}/{component.DEFAULT_COMPONENT}/versions", check=False)
        return sorted(result.stdout.split()) if result.returncode == 0 else []

    def logs(self):
        return self._sudo("journalctl", "--no-pager", "-o", "short-iso", "-n", "400", "-u", "suifleet.service",
                          "-u", "suifleet-signer.service", "-u", "suifleet-provision.service", check=False).stdout

    def close(self):
        shutil.rmtree(self.workdir, ignore_errors=True)


# ── Evidence ──────────────────────────────────────────────────────────────────

def redact(text, extra=()):
    text = SECRET_VALUES.sub("[REDACTED]", text)
    for secret in extra:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def write_evidence(path, evidence, forbidden=()):
    encoded = json.dumps(evidence, indent=2, default=str) + "\n"
    if SECRET_VALUES.search(encoded) or any(s and s in encoded for s in forbidden):
        raise RuntimeError("refusing to write evidence containing key material")
    Path(path).write_text(encoded)


def tool_versions():
    def version(*command):
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=30).stdout.strip()
        except OSError:
            return None
    head = version("git", "-C", str(ROOT), "rev-parse", "HEAD")
    return {"sui": version("sui", "--version"), "python": platform.python_version(), "git_head": head}


# ── The run ───────────────────────────────────────────────────────────────────

def wait_for(condition, message, device=None, timeout=180, interval=1.0, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while clock() < deadline:
        if condition():
            return
        if device is not None and device.exited():
            raise CaseFailure(f"daemon exited: {message}")
        sleep(interval)
    raise CaseFailure(message)


class Prompt:
    """Asks the operator for an extension-wallet transaction digest."""

    def __init__(self, stream=None, out=sys.stdout):
        self.stream, self.out = stream, out

    def __call__(self, step):
        if self.stream is None:
            return None
        print(f"\n[extension wallet] {step}\nEnter the transaction digest (blank if not performed): ", end="", file=self.out, flush=True)
        line = self.stream.readline()
        digest = line.strip()
        return digest if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", digest) else None


class Runner:
    def __init__(self, profile, device, cli, walrus, ledger, results, evidence, prompt=None,
                 network_lock=None, read_facts=dm.chain_facts, wait=wait_for, settle_seconds=8, checkpoint=None):
        self.profile, self.device, self.cli, self.walrus = profile, device, cli, walrus
        self.ledger, self.results, self.evidence = ledger, results, evidence
        self.prompt = prompt or Prompt()
        self.network_lock = network_lock or NoLock()
        self.read_facts, self.wait, self.settle_seconds = read_facts, wait, settle_seconds
        self.manifest = profile["manifest"]
        self.package = self.manifest["call_package_id"]
        self.registry = self.manifest["shared_objects"]["registry"]["id"]
        self.run_id = "accept-" + uuid.uuid4().hex[:12]
        self.artifacts, self.deployments, self.cleanup = {}, {}, []
        self.ids = {}
        self.extension = profile["wallet"]["mode"] == "extension"
        self.stage = "preflight"  # The case a failure is charged to.
        # Partial evidence after every mutation: an interrupted run still names what it created.
        self.checkpoint = checkpoint or (lambda: None)

    # chain helpers -------------------------------------------------------
    def transaction(self, label, *ptb, funding=0, gas=200_000_000):
        self.network_lock.assert_()
        count = len(self.evidence["transactions"])
        try:
            return self.ledger.execute(self.cli, label, ["ptb", *ptb], gas=gas, funding=funding)
        finally:
            if len(self.evidence["transactions"]) > count:
                self.evidence["transactions"][-1]["source"] = "scripted"
            self.evidence["test_objects"] = {"deployments": list(self.cleanup), "group": self.ids.get("group"),
                                             "decoy": self.ids.get("decoy"), "device": self.ids.get("device")}
            self.checkpoint()

    def verify_extension(self, label, step, expect):
        """A digest from the operator, checked on chain: success, signer and objects."""
        digest = self.prompt(step)
        if not digest:
            raise CaseBlocked(f"no extension-wallet digest for: {label}")
        result = self.cli.transaction_block(digest)
        status = result.get("effects", {}).get("status", {}).get("status")
        sender = sender_of(result)
        self.evidence["transactions"].append({"label": label, "digest": digest, "status": status,
                                              "source": "extension-wallet", "sender": sender})
        if status != "success":
            raise CaseFailure(f"{label}: execution effects {status}")
        if not sender or dm.object_id(sender) != self.profile["wallet"]["address"]:
            raise CaseFailure(f"{label}: signed by {sender}, not the profile's wallet account")
        objects = {suffix: created(result, suffix) for suffix in expect}
        if not all(objects.values()):
            raise CaseFailure(f"{label}: transaction did not create {[s for s, v in objects.items() if not v]}")
        return result, objects

    def receipt(self, deployment):
        return self.cli.receipts(deployment).get(dm.object_id(self.ids["device"]))

    def outcome(self, deployment):
        for key, value in self.device.journal().get("deployments", {}).items():
            if dm.object_id(key) == dm.object_id(deployment):
                return value
        return {}

    def settled(self, deployment, status, message):
        self.wait(lambda: self.outcome(deployment).get("status") == status and self.outcome(deployment).get("reported"),
                  message + " (device journal)", self.device)
        self.wait(lambda: self.receipt(deployment) == status, message + " (on-chain receipt)", self.device)

    # steps ---------------------------------------------------------------
    def preflight(self):
        facts = self.read_facts(self.manifest["rpc_url"])
        if facts["chain_identifier"] != self.manifest["chain_identifier"]:
            raise CaseFailure(f"node is chain {facts['chain_identifier']}, manifest says {self.manifest['chain_identifier']}")
        dm.verify(self.manifest, read=self.read_facts)
        operator = self.cli.active_address()
        if operator != self.profile["wallet"]["address"]:
            raise CaseFailure(f"client.yaml's active address {operator} is not the profile's wallet {self.profile['wallet']['address']}")
        envs, active = self.cli("envs")
        rpc = next((e["rpc"] for e in envs if e.get("alias") == active), None)
        if rpc is None or self.read_facts(rpc)["chain_identifier"] != self.manifest["chain_identifier"]:
            raise CaseFailure("client.yaml's active environment is not the manifest's chain")
        balance = self.cli.balance()
        if self.profile["profile"] == "localnet" and balance == 0 and self.profile["faucet_url"]:
            self.cli("faucet", "--url", self.profile["faucet_url"], json_output=False)
            self.wait(lambda: self.cli.balance() > 0, "local faucet supplied no gas")
            balance = self.cli.balance()
        if balance < self.ledger.limit:
            raise CaseFailure(f"{operator} holds {balance} MIST; this run may spend up to {self.ledger.limit} (docs/TESTNET.md)")
        device = self.device.facts()
        self.evidence.update(chain=facts["chain_identifier"], operator=operator, start_balance_mist=balance,
                             device=device, manifest_package=self.package, registry=self.registry)
        self.results.set("preflight", "passed", f"chain {facts['chain_identifier']}, package {self.package}, "
                         f"operator {operator}, {balance} MIST, device {device['kind']} {device['arch']} {device['address']}")
        return device

    def image_identity(self, device):
        expected = self.profile["device"]
        problems = []
        if device["arch"] != expected["expected_arch"]:
            problems.append(f"arch {device['arch']} != {expected['expected_arch']}")
        if expected.get("expected_binary_sha256") and device["binary_sha256"] != expected["expected_binary_sha256"]:
            problems.append("packaged daemon hash differs from the image manifest")
        if not device["updater_sha256"]:
            problems.append("suifleet-test-updater is not installed")
        if device["key_mode"] != "600":
            problems.append(f"device key mode {device['key_mode']}")
        if device["provision_service"] not in ("active", "inactive"):  # oneshot: inactive after success
            problems.append(f"suifleet-provision.service is {device['provision_service']}")
        if device["time_synchronized"] != "yes":
            problems.append("system clock is not synchronized (TLS to public endpoints will fail)")
        if not device["build_info"]:
            problems.append("no /usr/share/suifleet/build-info")
        if problems:
            raise CaseFailure("; ".join(problems))
        self.results.set("image-identity", "passed", f"{device['arch']} daemon {device['binary_sha256'][:16]}, address {device['address']}")

    def build_artifacts(self):
        plan = {  # name -> (version, injected failure)
            "pre": ("0.9.0", "none"), "v1": ("1.0.0", "none"), "v2": ("2.0.0", "none"),
            "install-fail": ("3.0.0", "install"), "health-fail": ("3.1.0", "health"),
            "recipe-tamper": ("3.2.0", "none"), "artifact-tamper": ("3.3.0", "none"),
            "elsewhere": ("3.4.0", "none"), "offline": ("4.0.0", "none"),
        }
        if self.extension:
            plan = {name: plan[name] for name in ("pre", "v1", "v2")}
        for name, (version, fail) in plan.items():
            artifact = component.build(version, self.run_id, fail)
            tag = f"{self.run_id}-{version.replace('.', '-')}"
            artifact_blob = self.walrus.store(f"artifact-{tag}", artifact)
            served_artifact = artifact
            if name == "artifact-tamper":
                # The recipe and chain bind the genuine bytes; the blob it names holds others.
                served_artifact = artifact.replace(b"# version=3.3.0", b"# version=3.3.9")
                artifact_blob = self.walrus.store(f"artifact-tampered-{tag}", served_artifact)
            recipe = component.recipe(artifact, artifact_blob)
            recipe_blob = self.walrus.store(f"recipe-{tag}", recipe)
            recipe_digest = hashlib.sha256(recipe).hexdigest()
            if name == "recipe-tamper":
                recipe_digest = hashlib.sha256(recipe + b"\n").hexdigest()  # Chain binds other bytes.
            for blob_id, expected in ((artifact_blob, served_artifact), (recipe_blob, recipe)):
                fetched = self.walrus.fetch(blob_id)
                if hashlib.sha256(fetched).digest() != hashlib.sha256(expected).digest():
                    raise CaseFailure(f"Walrus read-back of {blob_id} does not match the uploaded bytes")
            self.artifacts[name] = {"version": version, "fail": fail, "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
                                    "served_sha256": hashlib.sha256(served_artifact).hexdigest(),
                                    "artifact_blob": artifact_blob, "recipe_blob": recipe_blob,
                                    "recipe_sha256": recipe_digest, "bytes": len(artifact)}
        self.evidence["run_id"] = self.run_id
        self.evidence["artifacts"] = self.artifacts
        self.results.set("artifacts", "passed", f"{len(self.artifacts)} artifacts/recipes for run {self.run_id}, read back by SHA-256")

    def deploy_args(self, name, devices=(), groups=()):
        a = self.artifacts[name]
        ids = lambda values: "vector[" + ", ".join(f"@{v}" for v in values) + "]"
        return ["--move-call", f"{self.package}::deployment_manager::create_deployment_verified",
                f'"{component.DEFAULT_COMPONENT}"', f'"{a["version"]}"', f'"{a["recipe_blob"]}"',
                f'"{a["artifact_sha256"]}"', f'"{a["recipe_sha256"]}"', ids(groups), ids(devices), CLOCK]

    def deploy(self, label, *specs):
        """Create deployments [(name, devices, groups)] in one PTB; returns name -> record."""
        if self.extension:
            records = {}
            for name, devices, groups in specs:
                a = self.artifacts[name]
                target = f"device {devices[0]}" if devices else f"group {groups[0]}"
                step = (f"In {self.profile['wallet']['dapp_url']} deploy component {component.DEFAULT_COMPONENT} "
                        f"version {a['version']} to {target}: recipe blob {a['recipe_blob']}, "
                        f"artifact SHA-256 {a['artifact_sha256']}, recipe SHA-256 {a['recipe_sha256']}")
                _, objects = self.verify_extension(f"{label}: {name}", step, ["::deployment_manager::DeploymentRecord"])
                records[name] = self._check_record(objects["::deployment_manager::DeploymentRecord"][0], name)
            self.deployments.update(records)
            self.cleanup += records.values()
            return records
        ptb = [arg for name, devices, groups in specs for arg in self.deploy_args(name, devices, groups)]
        result = self.transaction(label, *ptb)
        records = {}
        for record in created(result, "::deployment_manager::DeploymentRecord"):
            content = plain(self.cli("object", record).get("content", {}))
            name = next(n for n, a in self.artifacts.items() if a["version"] == content["version"])
            records[name] = record
        if set(records) != {name for name, _, _ in specs}:
            raise CaseFailure(f"{label}: created {sorted(records)}")
        self.deployments.update(records)
        self.cleanup += records.values()
        return records

    def _check_record(self, record, name):
        content = plain(self.cli("object", record).get("content", {}))
        a = self.artifacts[name]
        if (content.get("version"), content.get("recipe_blob_id"), content.get("artifact_sha256")) != (
                a["version"], a["recipe_blob"], a["artifact_sha256"]):
            raise CaseFailure(f"deployment {record} does not carry this run's {name} version/recipe/artifact")
        return record

    def enroll(self, address):
        existing = self.profile["enrollment"]
        if existing:
            self.ids = {"device": existing["device_object_id"], "cap": existing["device_cap_id"], "owner_cap": existing["owner_cap_id"]}
            self._verify_enrollment(address)
            source = "existing enrollment named by the profile"
        elif self.extension:
            step = (f"In {self.profile['wallet']['dapp_url']} register device address {address} (name "
                    f"'acceptance-{self.run_id}', arch {self.profile['device'].get('expected_arch', platform.machine())}), "
                    "which issues its cap and activates it")
            _, objects = self.verify_extension("register device", step, [
                "::device_registry::Device", "::device_cap::DeviceCap", "::device_registry::DeviceOwnerCap"])
            self.ids = {"device": objects["::device_registry::Device"][0], "cap": objects["::device_cap::DeviceCap"][0],
                        "owner_cap": objects["::device_registry::DeviceOwnerCap"][0]}
            self._verify_enrollment(address)
            source = "extension wallet"
        else:
            arch = self.profile["device"].get("expected_arch", platform.machine())
            result = self.transaction(
                "register, issue cap and activate device",
                "--move-call", f"{self.package}::device_registry::register_device", f"@{self.registry}",
                f'"acceptance-{self.run_id}"', f"@{address}", f'"{arch}"', CLOCK, "--assign", "device",
                "--move-call", f"{self.package}::device_cap::issue_cap", "device",
                "--move-call", f"{self.package}::device_registry::activate_device", f"@{self.registry}", "device",
                "--assign", "owner", "--transfer-objects", "[owner]", f"@{self.profile['wallet']['address']}")
            self.ids = {"device": created(result, "::device_registry::Device")[0],
                        "cap": created(result, "::device_cap::DeviceCap")[0],
                        "owner_cap": created(result, "::device_registry::DeviceOwnerCap")[0]}
            self._verify_enrollment(address)
            source = "scripted test account"
        self.evidence["enrollment"] = dict(self.ids, address=address, source=source)

    def _verify_enrollment(self, address):
        cap = self.cli("object", self.ids["cap"])
        owner = (cap.get("owner") or {}).get("AddressOwner")
        if not owner or dm.object_id(owner) != address:
            raise CaseFailure(f"DeviceCap {self.ids['cap']} is not owned by the device address {address}")
        owner_cap = self.cli("object", self.ids["owner_cap"])
        holder = (owner_cap.get("owner") or {}).get("AddressOwner")
        if not holder or dm.object_id(holder) != self.profile["wallet"]["address"]:
            raise CaseFailure("DeviceOwnerCap is not held by the profile's wallet account")
        if dm.object_id(plain(owner_cap.get("content", {})).get("device_id", "0x0")) != dm.object_id(self.ids["device"]):
            raise CaseFailure("DeviceOwnerCap names another device")

    def configure_device(self):
        current = self.device.current_enrollment()
        wanted = {"device_object_id": dm.object_id(self.ids["device"]), "device_cap_id": dm.object_id(self.ids["cap"])}
        if current and current != wanted and not self.profile["device"].get("allow_replace_enrollment"):
            raise CaseBlocked("the device is enrolled with other IDs; set device.allow_replace_enrollment to replace "
                              "its configuration (the key and journal are kept)")
        manifest = dict(self.manifest)
        if self.profile["walrus"]["mode"] == "stand-in":
            manifest["walrus"] = {"publisher_url": self.walrus.url, "aggregator_url": self.walrus.url}
        workdir = Path(tempfile.mkdtemp(prefix="suifleet-acceptance-bundle-"))
        try:
            template = workdir / "device.toml"
            template.write_text(self.device.template(self.ids))
            dm.generate(manifest, workdir / "bundle", template)
            if isinstance(self.device, SshDevice):
                self.device.install(workdir / "bundle", self.ids)
            else:
                self.device.install(workdir / "bundle")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        self.evidence["device_manifest_walrus"] = manifest["walrus"]

    def group_setup(self):
        if self.extension:
            _, objects = self.verify_extension("create group", f"In {self.profile['wallet']['dapp_url']} create a group "
                                               f"named 'acceptance-{self.run_id}'", ["::device_group::DeviceGroup"])
            self.ids["group"] = objects["::device_group::DeviceGroup"][0]
            self.verify_extension("add device to group", f"Add device {self.ids['device']} to group {self.ids['group']}", [])
        else:
            result = self.transaction("create group", "--move-call", f"{self.package}::device_group::create_group",
                                      f'"acceptance-{self.run_id}"', '"edge acceptance"', CLOCK)
            self.ids["group"] = created(result, "::device_group::DeviceGroup")[0]
            self.transaction("add device to group",
                             "--move-call", f"{self.package}::device_registry::borrow_device", f"@{self.registry}",
                             f"@{self.ids['owner_cap']}", "--assign", "loaned",
                             "--move-call", f"{self.package}::device_group::add_device", f"@{self.ids['group']}", "loaned.0",
                             "--move-call", f"{self.package}::device_registry::return_device", f"@{self.registry}",
                             "loaned.0", "loaned.1")
        members = plain(self.cli("object", self.ids["group"]).get("content", {})).get("device_ids", [])
        if dm.object_id(self.ids["device"]) not in {dm.object_id(m) for m in members}:
            raise CaseFailure("device is not a member of the acceptance group")

    def decoy_device(self):
        result = self.transaction("register decoy device", "--move-call", f"{self.package}::device_registry::register_device",
                                  f"@{self.registry}", f'"decoy-{self.run_id}"', f"@{self.profile['wallet']['address']}",
                                  '"x86_64"', CLOCK, "--assign", "device", "--transfer-objects", "[device]",
                                  f"@{self.profile['wallet']['address']}")
        self.ids["decoy"] = created(result, "::device_registry::Device")[0]

    def fund_device(self, address, amount=DEVICE_FUNDING):
        self.transaction("fund device report gas", "--split-coins", "gas", f"[{amount}]", "--assign", "coin",
                         "--transfer-objects", "[coin]", f"@{address}", funding=amount)

    def run(self):
        device = self.preflight()
        address = device["address"]
        if self.device.kind == "hardware" and self.results.pending("image-identity"):
            self.stage = "image-identity"
            self.image_identity(device)
        key_before = self.device.key_digest()
        self.stage = "artifacts"
        self.build_artifacts()
        self.stage = "enrollment"
        self.enroll(address)
        unfunded = self.device_balance(address) == 0
        if not self.extension:
            self.decoy_device()
        self.group_setup()
        # First enrollment skips history; an existing cursor catches up instead.
        fresh = not self.device.journal().get("initialized")
        pre = self.deploy("create pre-enrollment deployment", ("pre", [self.ids["device"]], []))["pre"] if fresh else None
        self.configure_device()
        self.device.start()
        self.wait(lambda: self.device.journal().get("initialized"), "enrolled daemon did not establish its cursor", self.device)
        self.results.set("enrollment", "passed", f"device {self.ids['device']}, cap {self.ids['cap']} "
                         f"({self.evidence['enrollment']['source']}); daemon enrolled before any test deployment")

        self.stage = "direct-v1"
        v1 = self.deploy("create direct v1", ("v1", [self.ids["device"]], []))["v1"]
        if not self.extension and unfunded:
            self.stage = "report-retry"
            # Apply needs no gas; reporting does. The report must wait, then retry.
            self.wait(lambda: self.outcome(v1).get("status") == 1, "v1 was not applied", self.device)
            if self.outcome(v1).get("reported") or self.receipt(v1) is not None:
                raise CaseFailure("an unfunded device reported v1")
            self.fund_device(address)
            self.settled(v1, 1, "v1 report was not retried after funding")
            self.results.set("report-retry", "passed", "applied while unable to pay; Applied receipt after gas arrived")
            self.stage = "direct-v1"
        elif not self.extension:
            self.results.set("report-retry", "skipped", "device already had gas; reuse a fresh, unfunded identity to test report retry")
        else:
            if unfunded:
                self.fund_device(address)
        self.settled(v1, 1, "direct v1 was not applied and reported")
        self.check_receipt("v1", "passed")
        self.results.set("direct-v1", "passed", f"deployment {v1}: Applied on chain; receipt v1, run {self.run_id}")

        self.stage = "group-v2"
        v2 = self.deploy("create group v2", ("v2", [], [self.ids["group"]]))["v2"]
        self.settled(v2, 1, "group v2 was not applied and reported")
        self.check_receipt("v2", "passed")
        self.results.set("group-v2", "passed", f"deployment {v2} via group {self.ids['group']}: Applied on chain")
        self.stage = "baseline-skip"
        if not fresh:
            self.results.set("baseline-skip", "skipped", "the device journal already has a cursor; "
                             "first-enrollment baseline needs a fresh device state")
        elif self.outcome(pre) or "0.9.0" in self.device.versions():
            raise CaseFailure("a deployment created before enrollment ran")
        else:
            self.results.set("baseline-skip", "passed", f"pre-enrollment deployment {pre} never ran")

        if not self.extension:
            self.failure_paths()
        self.stage = "daemon-restart"
        self.restart_case()
        self.stage = "key-journal-preserved"
        self.preserved(address, key_before)
        if self.extension:
            self.results.set("extension-wallet", "passed", "operator-signed enrollment, group and deployments verified on chain")

    def device_balance(self, address):
        try:
            return self.cli.balance(address)
        except RuntimeError:
            return 0

    def check_receipt(self, name, health):
        a = self.artifacts[name]
        latest = [r for r in self.device.history() if r["version"] == a["version"]]
        if not latest:
            raise CaseFailure(f"no device receipt for {name}")
        r = latest[-1]
        if (r["runId"], r["artifactSha256"]) != (self.run_id, a["artifact_sha256"]):
            raise CaseFailure(f"device receipt for {name} names another run or artifact")
        if health == "passed" and (r["install"], r["health"]) != ("succeeded", "passed"):
            raise CaseFailure(f"device receipt for {name}: install {r['install']}, health {r['health']}")
        if r.get("run") and r["run"].get("arch") != self.evidence["device"]["arch"]:
            raise CaseFailure(f"application reported arch {r['run'].get('arch')}")
        return r

    def failure_paths(self):
        device = self.ids["device"]
        before = len(self.device.history())
        self.stage = "updater-install-failure"
        records = self.deploy("create failure-path deployments",
                              ("install-fail", [device], []), ("health-fail", [device], []),
                              ("recipe-tamper", [device], []), ("artifact-tamper", [device], []),
                              ("elsewhere", [self.ids["decoy"]], []))
        def ran(name):
            """Updater receipts from this batch that came from `name`'s deployment."""
            a = self.artifacts[name]
            return [r for r in self.device.history()[before:]
                    if r["version"] == a["version"] or r["artifactSha256"] in (a["artifact_sha256"], a["served_sha256"])]

        for name, case in (("install-fail", "updater-install-failure"), ("health-fail", "updater-health-failure"),
                           ("recipe-tamper", "recipe-tamper"), ("artifact-tamper", "artifact-tamper")):
            try:
                self.settled(records[name], 2, f"{name} was not reported Failed")
                if name == "install-fail":
                    r = self.check_receipt(name, None)
                    if (r["install"], r["health"]) != ("failed", "not-run") or "3.0.0" in self.device.versions():
                        raise CaseFailure(f"install failure receipt {r['install']}/{r['health']}")
                elif name == "health-fail":
                    r = self.check_receipt(name, None)
                    if (r["install"], r["health"]) != ("succeeded", "failed"):
                        raise CaseFailure(f"health failure receipt {r['install']}/{r['health']}")
                elif ran(name):
                    raise CaseFailure(f"{name}: the updater ran tampered input")
                self.results.set(case, "passed", f"deployment {records[name]}: Failed on chain, outcome {self.outcome(records[name]).get('error', '')[:200]}")
            except CaseFailure as error:
                self.results.set(case, "failed", error)
        time.sleep(self.settle_seconds)
        elsewhere = records["elsewhere"]
        if self.outcome(elsewhere) or ran("elsewhere") or self.cli.receipts(elsewhere):
            self.results.set("unauthorized-target", "failed", "a deployment for another device ran here")
        else:
            self.results.set("unauthorized-target", "passed", f"deployment {elsewhere} for decoy {self.ids['decoy']} never ran")
        # Only the install and health failures may have reached the updater.
        unexpected = [r["version"] for r in self.device.history()[before:] if r["version"] not in ("3.0.0", "3.1.0")]
        if unexpected:
            self.results.set("unauthorized-target", "failed", f"the updater ran unexpected versions {unexpected}")

        self.stage = "offline-catch-up"
        self.device.stop()
        offline = self.deploy("create deployment while the daemon is stopped", ("offline", [device], []))["offline"]
        self.device.start()
        self.settled(offline, 1, "deployment created while offline was not caught up")
        self.check_receipt("offline", "passed")
        self.results.set("offline-catch-up", "passed", f"deployment {offline} created while stopped: Applied after start")

    def restart_case(self):
        journal = self.device.journal()
        applied = len(self.device.history())
        self.device.restart()
        self.wait(lambda: self.device.running(), "daemon did not come back after restart", None, timeout=60)
        time.sleep(self.settle_seconds)
        after = self.device.journal()
        lost = [id for id in journal.get("deployments", {}) if id not in after.get("deployments", {})]
        if lost or len(self.device.history()) != applied or not after.get("initialized"):
            raise CaseFailure(f"restart lost {lost} or replayed work ({applied} -> {len(self.device.history())} receipts)")
        self.results.set("daemon-restart", "passed", f"journal kept {len(after.get('deployments', {}))} outcomes; no replay")

    def preserved(self, address, key_before):
        if self.device.key_digest() != key_before or self.device.address() != address:
            raise CaseFailure("the device key or address changed during the run")
        if not self.device.journal().get("initialized"):
            raise CaseFailure("the journal is missing")
        self.results.set("key-journal-preserved", "passed", "key digest and address unchanged; journal present")

    def clean(self):
        """Delete test-owned objects (storage rebate); keep the device identity."""
        problems = []
        if self.extension:
            if self.cleanup:
                self.results.set("cleanup", "skipped", f"delete deployments {self.cleanup} in the dApp (extension wallet)")
            return
        if self.cleanup:
            try:
                self.transaction("delete test deployments", *[arg for record in self.cleanup for arg in (
                    "--move-call", f"{self.package}::deployment_manager::delete_deployment", f"@{record}")])
            except Exception as error:  # noqa: BLE001 - reported, never hidden
                problems.append(f"deployments: {error}")
        if self.ids.get("group"):
            try:
                self.transaction("delete acceptance group",
                                 "--move-call", f"{self.package}::device_group::remove_device", f"@{self.ids['group']}", f"@{self.ids['device']}",
                                 "--move-call", f"{self.package}::device_group::delete_group", f"@{self.ids['group']}")
            except Exception as error:  # noqa: BLE001
                problems.append(f"group: {error}")
        if self.ids.get("decoy"):
            try:
                self.transaction("deregister decoy device", "--move-call", f"{self.package}::device_registry::deregister_device",
                                 f"@{self.registry}", f"@{self.ids['decoy']}")
            except Exception as error:  # noqa: BLE001
                problems.append(f"decoy: {error}")
        if problems:
            # Name what is left so an operator can delete it (no object is lost track of).
            self.evidence["leftover_objects"] = {"deployments": list(self.cleanup), "group": self.ids.get("group"),
                                                 "decoy": self.ids.get("decoy")}
            self.results.set("cleanup", "failed", "; ".join(problems)[:1500])
        elif self.results.pending("cleanup"):
            self.results.set("cleanup", "passed", f"{len(self.cleanup)} deployments, group and decoy deleted; device {self.ids.get('device')} kept")


def execute(profile, args, *, device=None, cli=None, walrus=None, locks=None, prompt=None, read_facts=dm.chain_facts,
            wait=wait_for, evidence_path=None):
    """Run the acceptance cases; returns (evidence, exit code)."""
    results = Results()
    evidence = {"schema": 1, "harness": "scripts/edge_acceptance.py", "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "profile": profile["profile"], "wallet_mode": profile["wallet"]["mode"],
                "artifact_profile": profile["artifact_profile"], "device_mode": profile["device"]["mode"],
                "tools": tool_versions(), "transactions": []}
    gaps = mode_gaps(profile)
    for id, (status, reason) in gaps.items():
        results.set(id, status, reason)
    held, forbidden = [], []
    runner = None

    def checkpoint():
        if evidence_path:
            write_evidence(evidence_path, dict(evidence, cases=json.loads(json.dumps(results.cases)), verdict="in-progress"))

    try:
        if profile["artifact_profile"] == "sf022-encrypted":
            raise CaseBlocked("SF-022 encrypted delivery is not implemented; nothing was executed")
        for resource in needed_locks(profile):
            lock = (locks or {}).get(resource) or ResourceLock(
                resource, args.identity, f"SF-031 edge acceptance ({profile['profile']})",
                token=os.environ.get("SUIFLEET_TESTNET_LOCK_TOKEN") if resource == TESTNET_LOCK else None)
            held.append(lock.acquire())
        lock_for = {resource: lock for resource, lock in zip(needed_locks(profile), held)}
        if device is None:
            if profile["device"]["mode"] == "ssh":
                device = SshDevice(profile, lock_for[HARDWARE_LOCK])
                device.pin_host_key()
            else:
                device = LocalDevice(profile)
        cli = cli or SuiCli(profile["wallet"]["client_config"])
        if walrus is None:
            w = profile["walrus"]
            walrus = StandInWalrus(w["bind"]) if w["mode"] == "stand-in" else HttpWalrus(w["publisher_url"], w["aggregator_url"], w["epochs"])
        ledger = Budget(profile["budget"]["max_spend_mist"], evidence["transactions"])
        runner = Runner(profile, device, cli, walrus, ledger, results, evidence, prompt=prompt,
                        network_lock=lock_for.get(TESTNET_LOCK), read_facts=read_facts, wait=wait,
                        settle_seconds=getattr(args, "settle_seconds", 8), checkpoint=checkpoint)
        try:
            runner.run()
        finally:
            try:
                runner.clean()
            finally:
                evidence["spent_mist"] = ledger.used
    except CaseBlocked as blocked:
        results.finish(str(blocked))
    except (CaseFailure, RuntimeError, ProfileError, subprocess.SubprocessError, OSError, KeyError, ValueError) as error:
        current = runner.stage if runner and results.pending(runner.stage) else next(
            (id for id, entry in results.cases.items() if entry["status"] is None), "preflight")
        results.set(current, "failed", f"{type(error).__name__}: {error}")
    finally:
        if device is not None:
            try:
                evidence["device_log_tail"] = redact(device.logs())[-6000:]
            except Exception as error:  # noqa: BLE001 - diagnostics only
                evidence["device_log_tail"] = f"unavailable: {error}"
            if isinstance(device, LocalDevice) and device.key.exists():
                forbidden.append(device.key.read_text().strip())
        for closer in (device, walrus):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        for lock in reversed(held):
            lock.release()
    evidence["cases"] = results.finish("not reached: an earlier step failed or was blocked")
    evidence["verdict"] = verdict(evidence["cases"])
    evidence["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    encoded = json.dumps(evidence, default=str)
    if SECRET_VALUES.search(encoded) or any(key and key in encoded for key in forbidden):
        raise RuntimeError("refusing to return evidence containing key material")
    if evidence["verdict"] == "failed":
        code = 1
    elif evidence["verdict"] == "passed":
        code = 0
    else:
        # Runtime skips (e.g. an already funded device) are allowed; a blocked step is not.
        stuck = any(e["status"] == "blocked" for id, e in evidence["cases"].items() if id not in gaps)
        code = 0 if getattr(args, "allow_incomplete", False) and not stuck else 3
    return evidence, code


def summarize(paths):
    """Combine runs into one SF-031 matrix. A case counts only from hardware runs."""
    runs = [json.loads(Path(p).read_text()) for p in paths]
    matrix = {}
    for case in CASES:
        statuses = [(run.get("device_mode"), run["cases"][case.id]["status"]) for run in runs if case.id in run.get("cases", {})]
        if any(s == "failed" for _, s in statuses):
            status = "failed"
        elif any(mode == "ssh" and s == "passed" for mode, s in statuses):
            status = "passed"
        elif any(s == "passed" for _, s in statuses):
            status = "local-only"
        else:
            status = "blocked" if any(s == "blocked" for _, s in statuses) else "skipped"
        matrix[case.id] = {"status": status, "capability": case.capability, "future": case.future}
    baseline = [v["status"] for v in matrix.values() if not v["future"]]
    overall = "failed" if "failed" in baseline else ("passed" if all(s == "passed" for s in baseline) else "incomplete")
    return {"runs": [str(p) for p in paths], "matrix": matrix, "sf031_verdict": overall}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "preflight", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--profile", type=Path, required=True)
        if name != "plan":
            sub.add_argument("--hardware", action="store_true", help="confirm a physical-device (ssh) run")
            for flag in ("agent", "model", "session", "ticket"):
                sub.add_argument(f"--{flag}", help="lock identity for hardware/testnet locks")
        if name == "run":
            sub.add_argument("--evidence", type=Path, required=True)
            sub.add_argument("--allow-incomplete", action="store_true",
                             help="exit 0 when nothing failed or was blocked at run time (verdict stays incomplete)")
            sub.add_argument("--wallet-input", type=Path, help="read extension-wallet digests from this file instead of stdin")
    summary = commands.add_parser("summarize")
    summary.add_argument("evidence", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "summarize":
        result = summarize(args.evidence)
        print(json.dumps(result, indent=2))
        return 0 if result["sf031_verdict"] == "passed" else 3
    try:
        profile = load_profile(args.profile)
        if args.command == "plan":
            gaps = mode_gaps(profile)
            for case in CASES:
                status, reason = gaps.get(case.id, ("run", ""))
                print(f"{status:8} {case.id:26} {case.capability}" + (f"  [{reason}]" if reason else ""))
            print(f"locks during run: {', '.join(needed_locks(profile)) or 'none'}")
            return 0
        if profile["device"]["mode"] == "ssh" and not args.hardware:
            raise ProfileError("this profile uses a physical device: pass --hardware to confirm")
        args.identity = {"agent": args.agent, "model": args.model, "session": args.session, "ticket": args.ticket} \
            if args.agent and args.model and args.session else None
        if args.command == "preflight":
            return preflight_only(profile, args)
    except ProfileError as error:
        print(f"edge_acceptance: {error}", file=sys.stderr)
        return 2
    stream = None
    if profile["wallet"]["mode"] == "extension":
        stream = args.wallet_input.open() if args.wallet_input else (sys.stdin if sys.stdin.isatty() else None)
    evidence, code = execute(profile, args, prompt=Prompt(stream), evidence_path=args.evidence)
    write_evidence(args.evidence, evidence)
    print(f"verdict: {evidence['verdict']} (evidence {args.evidence})")
    if evidence["verdict"] != "passed":
        open_cases = [id for id, e in evidence["cases"].items() if e["status"] != "passed"]
        print(f"not established by this run: {', '.join(open_cases)}")
    return code


def preflight_only(profile, args):
    """Read-only: chain, manifest, account, funding and device facts. No mutation."""
    held = []
    device = None
    try:
        if profile["device"]["mode"] == "ssh":
            lock = ResourceLock(HARDWARE_LOCK, args.identity, "SF-031 edge acceptance preflight (read-only)").acquire()
            held.append(lock)
            device = SshDevice(profile, lock)
            device.pin_host_key()
        else:
            device = LocalDevice(profile)
        cli = SuiCli(profile["wallet"]["client_config"])
        results = Results()
        runner = Runner(profile, device, cli, None, Budget(profile["budget"]["max_spend_mist"], []), results, {"transactions": []})
        facts = runner.preflight()
        if device.kind == "hardware":
            runner.image_identity(facts)
        return 0
    except (CaseFailure, RuntimeError, ProfileError, OSError, KeyError, ValueError) as error:
        print(f"FAILED: preflight: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        if device is not None:
            device.close()
        for lock in held:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
