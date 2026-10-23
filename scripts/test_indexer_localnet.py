#!/usr/bin/env python3
"""The fleet indexer, API and CLI (SF-024) against a real local Sui network.

Publishes the actual contracts to a disposable localnet, builds real fleet
state (activated devices in registry custody and an unactivated one, of two
operators, a group, direct and group deployments, an identity-checked
receipt), indexes it, and checks:

  - the API matches chain state: devices, operators (DeviceOwnerCap owners
    for activated devices), identities, groups and members, deployments,
    targets, resolved devices and authoritative receipts;
  - an identity change (revocation) is indexed from its event;
  - filters and keyset pagination return every item exactly once;
  - restart resumes from durable cursors: later removals and deletions
    appear once in the change feed, earlier changes are not repeated;
  - a plain transfer of an unactivated Device or of an activated device's
    DeviceOwnerCap (no fleet event) is reconciled while running and across
    a restart: owner filters, the change feed and the bulk ownership and
    identity prechecks follow the new operator;
  - devices wrapped in their registering transaction (the event names an
    absent object) are kept and indexed once unwrapped, while running and
    across a restart;
  - auth: no token 401, read token cannot operate (403), operate token can;
  - bulk operations: `ctl add-devices` (an activated device, borrowed from
    custody with its owner cap) and `ctl create-deployment` build through
    the API, verify every call and argument against the request and the
    CLI's own chain reads, sign and execute; `ctl ... --out` writes an unsigned transaction; the
    indexer then shows the new membership and deployment;
  - device telemetry (SF-019): real daemons sign snapshots to `POST
    /v1/telemetry`; the activated device's are accepted against its indexed
    identity and served as healthy by `/v1/health` (and verified again by the
    dApp's verifier), the revoked device's are rejected and it stays
    unreported; stored telemetry survives an indexer restart;
  - a database bound to another chain, or to another publication on the
    same chain, is refused; --reset discards it and indexes only the new one;
  - load: concurrent paginated reads over ~45k synthetic rows.

Run inside the dev image with a throwaway node and faucet (make test-indexer-localnet):

    SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
    python3 scripts/test_indexer_localnet.py
"""
import argparse
import base64
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from urllib.parse import urlparse

import deployment_manifest as dm

ROOT = Path(__file__).resolve().parents[1]
INDEXER = ROOT / "suifleet-indexer/target/debug/suifleet-indexer"
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
CLOCK = "@0x6"
API = "http://127.0.0.1:8650"


def wait_for(condition, message, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.3)
    raise AssertionError(message)


def http(method, path, token=None, body=None):
    request = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")
    except urllib.error.URLError:
        return 0, {}  # Not listening (yet).


def run(rpc_url, faucet_url):
    for url in (rpc_url, faucet_url):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("this test requires an isolated loopback node and faucet")
    with tempfile.TemporaryDirectory(prefix="suifleet-indexer-") as temporary:
        root = Path(temporary)
        (root / "wallet").mkdir()

        def client(*args, json_output=True):
            command = ["sui", "client", "--client.config", str(root / "wallet/client.yaml"), *map(str, args)]
            if json_output:
                command.append("--json")
            result = subprocess.run(command, text=True, capture_output=True, timeout=300)
            if result.returncode:
                raise RuntimeError(f"sui client {args[0]} failed:\n{result.stderr[-3000:]}")
            if not json_output:
                return result.stdout
            out = result.stdout
            return json.loads(out[min(i for i in (out.find("{"), out.find("[")) if i >= 0):])

        client("-y", "active-address", json_output=False)
        client("new-env", "--alias", "indexer-test", "--rpc", rpc_url, json_output=False)
        client("switch", "--env", "indexer-test", json_output=False)
        operator = client("active-address", json_output=False).strip().split()[-1]

        def new_address():
            out = client("new-address", "ed25519", json_output=False)
            return next(word for word in out.replace("│", " ").split() if word.startswith("0x") and len(word) == 66)
        device_signer, other = new_address(), new_address()
        for address in (operator, device_signer, other):
            client("faucet", "--url", faucet_url, "--address", address, json_output=False)

        def funded(address):
            gas = client("gas", address)
            coins = gas.get("gasCoins", []) if isinstance(gas, dict) else gas
            return bool(coins)
        for address in (operator, device_signer, other):
            wait_for(lambda: funded(address), f"{address} not funded")

        def use(address):
            client("switch", "--address", address, json_output=False)

        def transaction(*ptb):
            result = client("ptb", *ptb, "--gas-budget", "200000000")
            assert result["effects"]["status"]["status"] == "success", result["effects"]["status"]
            return result

        def created(result, suffix):
            return [c["objectId"] for c in result["objectChanges"]
                    if c["type"] == "created" and c["objectType"].endswith(suffix)]

        # ── Real fleet state ────────────────────────────────────────────────
        contracts = root / "contracts"
        shutil.copytree(ROOT / "contracts", contracts, ignore=shutil.ignore_patterns("build", "Published.toml"))
        published = client("test-publish", contracts, "--build-env", "testnet", "--pubfile-path", root / "Pub.local.toml",
                           "--gas-budget", "500000000", "--silence-warnings")
        assert published["effects"]["status"]["status"] == "success"
        package = next(c["packageId"] for c in published["objectChanges"] if c["type"] == "published")
        registry = created(published, "::device_registry::DeviceRegistry")[0]
        upgrade_cap = created(published, "::package::UpgradeCap")[0]
        chain_id = dm.rpc(rpc_url, "sui_getChainIdentifier", [])
        manifest = dm.resolve(SimpleNamespace(chain_identifier=chain_id, rpc=rpc_url, package=package, upgrade_cap=upgrade_cap,
                                              registry=registry, network="localnet", publisher="http://127.0.0.1:1",
                                              aggregator="http://127.0.0.1:1"))
        (root / "deployment.json").write_text(json.dumps(manifest))

        def register(name, address, owner, activate=True):
            """Register and issue a cap; activated devices go into registry custody."""
            keep = (["--move-call", f"{package}::device_registry::activate_device", f"@{registry}", "device",
                     "--assign", "owner", "--transfer-objects", "[owner]", f"@{owner}"] if activate
                    else ["--transfer-objects", "[device]", f"@{owner}"])
            result = transaction("--move-call", f"{package}::device_registry::register_device", f"@{registry}", f'"{name}"',
                                 f"@{address}", '"aarch64"', CLOCK, "--assign", "device",
                                 "--move-call", f"{package}::device_cap::issue_cap", "device", *keep)
            device = next(e["parsedJson"]["device_id"] for e in result["events"]
                          if e["type"].endswith("::device_registry::DeviceRegistered"))
            owner_cap = created(result, "::device_registry::DeviceOwnerCap")[0] if activate else None
            return device, created(result, "::device_cap::DeviceCap")[0], owner_cap
        use(operator)
        pi, pi_cap, pi_owner = register("pi", device_signer, operator)
        spare, _, _ = register("spare", operator, operator, activate=False)
        third, _, third_owner = register("third", operator, operator)
        use(other)
        foreign, foreign_cap, foreign_owner = register("foreign", other, other)
        use(operator)

        def borrowed(owner_cap, *call):
            """A `&Device` call on a custodied device: borrow, call, return."""
            return ["--move-call", f"{package}::device_registry::borrow_device", f"@{registry}", f"@{owner_cap}",
                    "--assign", "loan", *call, "--move-call", f"{package}::device_registry::return_device",
                    f"@{registry}", "loan.0", "loan.1"]
        group = created(transaction("--move-call", f"{package}::device_group::create_group", '"lab"', '"indexer"', CLOCK),
                        "::device_group::DeviceGroup")[0]
        transaction(*borrowed(pi_owner, "--move-call", f"{package}::device_group::add_device", f"@{group}", "loan.0"))
        transaction("--move-call", f"{package}::device_group::add_device", f"@{group}", f"@{spare}")

        def deploy(version, groups=(), devices=()):
            ids = lambda values: "[" + ", ".join(f"@{v}" for v in values) + "]"
            return created(transaction("--move-call", f"{package}::deployment_manager::create_deployment_verified",
                                       '"fw"', f'"{version}"', '"recipe"', f'"{"a" * 64}"', f'"{"b" * 64}"',
                                       f"vector{ids(groups)}", f"vector{ids(devices)}", CLOCK),
                           "::deployment_manager::DeploymentRecord")[0]
        by_group, direct = deploy("1.0.0", groups=[group]), deploy("1.1.0", devices=[third])
        use(device_signer)
        transaction("--move-call", f"{package}::deployment_manager::report_verified_status_for_group", f"@{by_group}",
                    f"@{registry}", f"@{pi_cap}", f"@{group}", "1u8")
        use(operator)
        n = dm.object_id

        # ── Index, serve and compare with chain ─────────────────────────────
        db = root / "fleet.db"
        common = ["--manifest", str(root / "deployment.json"), "--db", str(db)]
        indexed = subprocess.run([str(INDEXER), "index", *common], capture_output=True, text=True, timeout=300)
        assert indexed.returncode == 0, indexed.stderr[-3000:]
        read_token, operate_token, support_token = "r" * 40, "o" * 48, "s" * 44
        (root / "tokens").write_text(f"read {read_token}\noperate {operate_token}\nsupport {support_token}\n")
        log = (root / "indexer.log").open("w")

        def serve(*extra):
            process = subprocess.Popen([str(INDEXER), "serve", *common, "--tokens", str(root / "tokens"),
                                        "--interval-seconds", "1", "--telemetry-db", str(root / "telemetry.db"), *extra],
                                       stdout=log, stderr=log)
            wait_for(lambda: process.poll() is None and http("GET", "/v1/status", read_token)[0] == 200, "API did not start")
            return process
        server = serve()
        try:
            def get(path):
                status, body = http("GET", path, read_token)
                assert status == 200, (path, status, body)
                return body

            def every(path):
                items, cursor = [], None
                while True:
                    page = get(path + ("&" if "?" in path else "?") + "limit=2" + (f"&cursor={cursor}" if cursor else ""))
                    items += page["items"]
                    if not page["next"]:
                        return items
                    cursor = page["next"]

            devices = {d["id"]: d for d in every("/v1/devices")}
            assert set(devices) == {n(pi), n(spare), n(third), n(foreign)}, devices.keys()
            assert devices[n(pi)]["owner"] == n(operator) and devices[n(foreign)]["owner"] == n(other)
            assert devices[n(pi)]["device_address"] == n(device_signer) and devices[n(pi)]["groups"] == [n(group)]
            assert (devices[n(pi)]["activated"], devices[n(pi)]["revoked"], devices[n(pi)]["signer"],
                    devices[n(pi)]["owner_cap"]) == (True, False, n(device_signer), n(pi_owner)), devices[n(pi)]
            assert not devices[n(spare)]["activated"] and devices[n(spare)]["owner_cap"] is None
            assert [d["id"] for d in every(f"/v1/devices?owner={operator}")] == sorted([n(pi), n(spare), n(third)])
            assert sorted(get(f"/v1/groups/{group}")["devices"]) == sorted([n(pi), n(spare)])
            deployment = get(f"/v1/deployments/{by_group}")
            assert deployment["target_groups"] == [n(group)] and deployment["resolved_devices"] == sorted([n(pi), n(spare)])
            assert deployment["receipts"] == [{"device_id": n(pi), "status": 1, "label": "applied"}]
            assert deployment["recipe_sha256"] == "b" * 64
            assert get(f"/v1/deployments/{direct}")["resolved_devices"] == [n(third)]
            assert [d["id"] for d in every(f"/v1/deployments?device={pi}")] == [n(by_group)]
            assert [d["id"] for d in every(f"/v1/deployments?device={third}")] == [n(direct)]
            assert every(f"/v1/deployments?device={foreign}") == []
            assert sorted(d["version"] for d in every("/v1/deployments?component=fw")) == ["1.0.0", "1.1.0"]
            print("PASS: API matches chain state (custodied devices' operators and identities, members, targets, "
                  "resolved devices, identity-checked receipt); filters and limit=2 pagination return every item once")

            # An identity change is its own event stream.
            use(other)
            transaction("--move-call", f"{package}::device_registry::revoke_device", f"@{registry}", f"@{foreign_owner}")
            use(operator)
            wait_for(lambda: get(f"/v1/devices/{foreign}")["revoked"], "revocation not indexed")
            assert get(f"/v1/devices/{foreign}")["owner"] == n(other)
            print("PASS: a revocation (DeviceIdentityChanged) is indexed")

            # Telemetry (SF-019): real daemons sign with the wallet's device keys.
            seeds = {}
            for entry in json.loads((root / "wallet/sui.keystore").read_text()):
                raw = base64.b64decode(entry)
                if raw[0] == 0:
                    key = root / f"key-{len(seeds)}"
                    key.write_text(raw[1:].hex())
                    address = subprocess.run([str(DAEMON), "--derive-address", str(key)], capture_output=True, text=True,
                                             check=True).stdout.strip()
                    seeds[n(address)] = key
            daemons = []

            def daemon(name, device, cap, key):
                directory = root / name
                directory.mkdir()
                (directory / "config.toml").write_text(
                    f'[sui]\nmanifest_path = {json.dumps(str(root / "deployment.json"))}\n'
                    f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{device}"\ndevice_cap_id = "{cap}"\n'
                    '[apply]\ncommand = "/bin/false"\n'
                    f'[runtime]\nstate_dir = {json.dumps(str(directory / "state"))}\npoll_seconds = 2\nevent_scan_seconds = 0\n'
                    f'[telemetry]\nurl = "{API}"\ninterval_seconds = 10\n'
                    '[logs]\ninterval_seconds = 10\n')
                process = subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(directory / "config.toml")},
                                           stdout=(directory / "daemon.log").open("w"), stderr=subprocess.STDOUT)
                daemons.append(process)
                return directory
            try:
                daemon("pi", pi, pi_cap, seeds[n(device_signer)])
                revoked = daemon("foreign", foreign, foreign_cap, seeds[n(other)])

                def health(owner):
                    return {d["device_id"]: d for d in every(f"/v1/health?owner={owner}")}
                wait_for(lambda: health(operator)[n(pi)]["status"] == "healthy", "the activated device never reported healthy",
                         timeout=90)
                pi_health = health(operator)[n(pi)]
                assert pi_health["codes"] == [] and pi_health["signer"] == n(device_signer), pi_health
                assert pi_health["report"]["signer"] == n(device_signer) and pi_health["daemon_version"], pi_health
                assert health(operator)[n(spare)]["codes"] == ["no-telemetry"], "an unactivated device got health"
                summary = get(f"/v1/health/summary?owner={operator}")
                assert summary["counts"]["healthy"] == 1 and summary["total"] == 3, summary
                wait_for(lambda: "Telemetry snapshot rejected" in (revoked / "daemon.log").read_text(),
                         "the revoked device's telemetry was not rejected", timeout=60)
                gone = health(other)[n(foreign)]
                assert (gone["status"], gone["codes"], gone["report"]) == ("unreported", ["revoked"], None), gone
                history = get(f"/v1/devices/{foreign}/telemetry")["items"]
                assert history == [], "a revoked identity's telemetry was stored"
                stored = get(f"/v1/devices/{pi}/telemetry")["items"]
                assert stored and stored[0]["seq"] == max(r["seq"] for r in stored)
                captured = root / "envelopes.json"
                captured.write_text(json.dumps([{"message": r["message"], "signature": r["signature"]} for r in stored]))
                subprocess.run(["node", str(ROOT / "scripts/test_telemetry.cjs"), "--verify", str(captured),
                                n(device_signer), n(pi), chain_id, n(registry)], check=True)

                # Remote logs, diagnostics and alerts (SF-020) through the real indexer.
                def support(path, token=support_token):
                    return http("GET", path, token)
                wait_for(lambda: any("Daemon started" in line["line"]
                                     for line in support(f"/v1/devices/{pi}/logs?source=daemon&limit=500")[1].get("items", [])),
                         "the activated device's daemon log never reached the indexer", timeout=90)
                assert support(f"/v1/devices/{pi}/logs", read_token)[0] == 403, "a read token searched device logs"
                found = support(f"/v1/devices/{pi}/logs?q=daemon%20started")[1]["items"]
                assert found and all("daemon started" in line["line"].lower() for line in found), found
                wait_for(lambda: "Log batch rejected" in (revoked / "daemon.log").read_text(),
                         "the revoked device's log batches were not rejected", timeout=60)
                assert support(f"/v1/devices/{foreign}/logs")[1]["items"] == [], "a revoked identity's logs were stored"
                status, bundle = support(f"/v1/devices/{pi}/diagnostics")
                assert status == 200 and bundle["health"]["status"] == "healthy", bundle.get("health")
                receipts = {d["deployment_id"]: d["receipt"] for d in bundle["deployments"]}
                assert receipts.get(n(by_group)) == "applied", receipts
                assert bundle["log_stats"]["lines"] > 0 and bundle["logs"], bundle["log_stats"]
                status, alerts = http("GET", f"/v1/alerts?owner={operator}", read_token)
                assert status == 200 and not [a for a in alerts["items"] if a["device_id"] == n(pi)], alerts
                status, audit = http("GET", "/v1/support/audit", operate_token)
                assert status == 200 and {"logs", "diagnostics"} <= {e["route"] for e in audit["items"]}, audit
                assert support_token not in json.dumps(audit)
                print("PASS: remote logs: the activated device's redacted daemon log searchable with a support token (read "
                      "token 403), the revoked device's batches rejected; diagnostics bundle with the on-chain receipt; "
                      "alerts served; support access audited by token fingerprint")
            finally:
                for process in daemons:
                    process.terminate()
                    process.wait(timeout=30)
            print("PASS: telemetry: the activated device's signed snapshots accepted and served healthy (verified again "
                  "by the dApp), the unactivated device unreported, the revoked device's rejected and unreported")

            # A catalog deployment (SF-011) names its ComponentVersion, not a recipe.
            catalog = created(transaction("--move-call", f"{package}::component_catalog::create_catalog"),
                              "::component_catalog::ComponentCatalog")[0]
            version = created(transaction("--move-call", f"{package}::component_catalog::publish_version", f"@{catalog}",
                                          '"cat-fw"', '"2.0.0"', 'vector["linux"]', 'vector["aarch64"]',
                                          'vector["recipe"]', f'vector["{"c" * 64}"]', f'vector["{"d" * 64}"]', CLOCK),
                              "::component_catalog::ComponentVersion")[0]
            bound = created(transaction("--move-call", f"{package}::component_catalog::create_deployment", f"@{version}",
                                        "vector[]", f"vector[@{third}]", CLOCK),
                            "::deployment_manager::DeploymentRecord")[0]
            wait_for(lambda: http("GET", f"/v1/deployments/{bound}", read_token)[0] == 200, "catalog deployment not indexed")
            catalogued = get(f"/v1/deployments/{bound}")
            assert (catalogued["schema_version"], catalogued["catalog_version"], catalogued["component_name"],
                    catalogued["version"], catalogued["recipe_sha256"], catalogued["resolved_devices"]) == (
                        2, n(version), "cat-fw", "2.0.0", None, [n(third)]), catalogued
            assert deployment["catalog_version"] is None
            transaction("--move-call", f"{package}::component_catalog::delete_deployment", f"@{bound}")
            wait_for(lambda: get(f"/v1/deployments/{bound}")["deleted"], "catalog deployment deletion not indexed")
            assert get(f"/v1/deployments/{bound}")["catalog_version"] == n(version)
            print("PASS: a catalog deployment is indexed with its ComponentVersion and no recipe digest; "
                  "its deletion keeps a tombstone")

            # Auth.
            assert http("GET", "/v1/devices")[0] == 401
            assert http("GET", "/v1/devices", "x" * 40)[0] == 401
            body = {"sender": operator, "group": group, "devices": [third]}
            assert http("POST", "/v1/transactions/add-devices", read_token, body)[0] == 403
            status, invalid = http("POST", "/v1/transactions/add-devices", operate_token, {**body, "devices": [foreign]})
            assert status == 400 and "not owned by" in invalid["error"], invalid
            print("PASS: auth: no/unknown token 401, read token cannot operate (403); invalid bulk request 400")

            # Restart: changes made while stopped appear once; earlier ones are not repeated.
            server.terminate(); server.wait(timeout=30)
            before = sqlite3.connect(db).execute("SELECT COUNT(*), MAX(seq) FROM changes").fetchone()
            transaction("--move-call", f"{package}::device_group::remove_device", f"@{group}", f"@{spare}")
            transaction("--move-call", f"{package}::deployment_manager::delete_deployment", f"@{direct}")
            server = serve()
            wait_for(lambda: get(f"/v1/deployments/{direct}")["deleted"], "deletion not indexed after restart")
            wait_for(lambda: n(spare) not in get(f"/v1/groups/{group}")["devices"], "removal not indexed after restart")
            assert get(f"/v1/devices/{pi}/telemetry")["items"] == stored, "telemetry did not survive a restart"
            new = get(f"/v1/changes?after={before[1]}")["items"]
            kinds = sorted(c["kind"] for c in new)
            assert kinds == ["deployment.deleted", "group.updated"], kinds
            assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM changes WHERE seq <= ?", (before[1],)).fetchone()[0] == before[0]
            assert every(f"/v1/deployments?device={third}") == [] and len(every("/v1/deployments?include_deleted=true")) == 3
            print("PASS: restart resumes from durable cursors; removal and deletion recorded once, earlier changes not repeated; "
                  "deleted record kept as a tombstone")

            # A plain transfer emits no fleet event; the device sweep reconciles it.
            def owned_by(address):
                return [d["id"] for d in every(f"/v1/devices?owner={address}")]
            # The unactivated Device moves itself; the activated one moves with its owner cap.
            mark = get("/v1/status")["last_change"]
            transaction("--transfer-objects", f"[@{spare}, @{third_owner}]", f"@{other}")
            for device in (spare, third):
                wait_for(lambda: get(f"/v1/devices/{device}")["owner"] == n(other), "transfer not reconciled while running")
                assert n(device) in owned_by(other) and n(device) not in owned_by(operator)
            updates = [c for c in get(f"/v1/changes?after={mark}")["items"] if c["object_id"] in (n(spare), n(third))]
            assert sorted(c["object_id"] for c in updates) == sorted([n(spare), n(third)]), updates
            assert all(c["kind"] == "device.updated" for c in updates), updates
            status, refused = http("POST", "/v1/transactions/add-devices", operate_token,
                                   {"sender": operator, "group": group, "devices": [spare]})
            assert status == 400 and "not owned by" in refused["error"], refused
            new_owner = {"sender": other, "component_name": "fw", "version": "9.0.0", "recipe_blob_id": "recipe",
                         "artifact_sha256": "a" * 64, "recipe_sha256": "b" * 64, "target_devices": [third]}
            status, built = http("POST", "/v1/transactions/create-deployment", operate_token, new_owner)
            assert status == 200 and built["sender"] == n(other), built
            status, refused = http("POST", "/v1/transactions/create-deployment", operate_token,
                                   {**new_owner, "target_devices": [spare]})
            assert status == 400 and "no active registry identity" in refused["error"], refused
            # Transferred back while the indexer is stopped: correct as soon as it serves again.
            server.terminate(); server.wait(timeout=30)
            use(other)
            transaction("--transfer-objects", f"[@{spare}, @{third_owner}]", f"@{operator}")
            use(operator)
            server = serve()
            for device in (spare, third):
                assert get(f"/v1/devices/{device}")["owner"] == n(operator), "restart served a pre-restart owner"
                assert n(device) in owned_by(operator) and n(device) not in owned_by(other)
            updates = [c for c in get(f"/v1/changes?after={mark}")["items"] if c["object_id"] in (n(spare), n(third))]
            assert len(updates) == 4 and all(c["kind"] == "device.updated" for c in updates), updates
            assert get("/v1/status")["devices_reconciled_at"]
            print("PASS: transfers of an unactivated Device and of an activated device's owner cap, without fleet "
                  "events, reconciled while running and across a restart (owner filters, one change each, live "
                  "bulk ownership and identity prechecks)")

            # Registered and wrapped (into a Bag) in one transaction: the event names an absent object.
            device_type = f"{package}::device_registry::Device"
            wrapping = transaction(
                "--move-call", "0x2::bag::new", "--assign", "bag",
                *[arg for name, key in (("live", 0), ("restart", 1)) for arg in (
                    "--move-call", f"{package}::device_registry::register_device", f"@{registry}", f'"{name}"',
                    f"@{operator}", '"aarch64"', CLOCK, "--assign", f"d{key}",
                    "--move-call", "0x2::bag::add", f"<u64,{device_type}>", "bag", f"{key}u64", f"d{key}")],
                "--transfer-objects", "[bag]", f"@{operator}")
            live, later = [e["parsedJson"]["device_id"] for e in wrapping["events"]
                           if e["type"].endswith("::device_registry::DeviceRegistered")]
            bag = created(wrapping, "::bag::Bag")[0]
            assert not created(wrapping, "::device_registry::Device"), "devices should be wrapped, not created"
            wait_for(lambda: all(http("GET", f"/v1/devices/{d}", read_token)[0] == 200 for d in (live, later)),
                     "wrapped registrations not kept")
            assert all(get(f"/v1/devices/{d}")["deleted"] for d in (live, later))
            assert n(live) not in owned_by(operator)

            def unwrap(key):
                transaction("--move-call", "0x2::bag::remove", f"<u64,{device_type}>", f"@{bag}", f"{key}u64",
                            "--assign", "d", "--transfer-objects", "[d]", f"@{operator}")
            unwrap(0)
            wait_for(lambda: not get(f"/v1/devices/{live}")["deleted"], "unwrapped device not reconciled while running")
            assert get(f"/v1/devices/{live}")["name"] == "live" and n(live) in owned_by(operator)
            server.terminate(); server.wait(timeout=30)
            unwrap(1)
            server = serve()
            restarted = get(f"/v1/devices/{later}")
            assert not restarted["deleted"] and restarted["owner"] == n(operator), restarted
            print("PASS: devices wrapped in their registering transaction are kept and indexed once unwrapped, "
                  "while running and across a restart")

            # Bulk via the CLI: unsigned to a file, then verified, signed and executed.
            keystore = json.loads((root / "wallet/sui.keystore").read_text())
            seeds = {}
            for entry in keystore:
                raw = base64.b64decode(entry)
                if raw[0] == 0:
                    seeds[raw[1:]] = None
            ctl = [str(INDEXER), "ctl", "--api", API]
            token_file = root / "operate.token"; token_file.write_text(operate_token)
            unsigned = root / "unsigned.json"
            subprocess.run([*ctl, "--token-file", str(token_file), "add-devices", "--sender", operator, "--group", group, third,
                            "--out", str(unsigned)], check=True, capture_output=True, timeout=120)
            assert json.loads(unsigned.read_text())["summary"] == {"operation": "add-devices", "group": n(group), "devices": [n(third)]}
            executed = None
            for seed in seeds:
                key = root / "operator.key"; key.write_text(seed.hex())
                derived = subprocess.run([*ctl, "--token-file", str(token_file), "add-devices", "--sender", operator, "--group", group,
                                          third, "--key", str(key), "--manifest", str(root / "deployment.json")],
                                         capture_output=True, text=True, timeout=180)
                if derived.returncode == 0:
                    executed = json.loads(derived.stdout)
                    break
                assert "not the --sender's key" in derived.stderr, derived.stderr
            assert executed and executed["executed"], "no keystore key executed the bulk operation"
            wait_for(lambda: n(third) in get(f"/v1/groups/{group}")["devices"], "bulk membership not indexed")
            effects = dm.rpc(rpc_url, "sui_getTransactionBlock", [executed["executed"], {"showEffects": True}])
            assert effects["effects"]["status"]["status"] == "success"
            created_by_ctl = subprocess.run(
                [*ctl, "--token-file", str(token_file), "create-deployment", "--sender", operator, "--component", "fw",
                 "--version", "2.0.0", "--recipe-blob", "recipe", "--artifact-sha256", "a" * 64, "--recipe-sha256", "c" * 64,
                 "--target-device", third, "--key", str(key), "--manifest", str(root / "deployment.json")],
                capture_output=True, text=True, timeout=180)
            assert created_by_ctl.returncode == 0, created_by_ctl.stderr
            wait_for(lambda: any(d["version"] == "2.0.0" and d["recipe_sha256"] == "c" * 64
                                 for d in every(f"/v1/deployments?device={third}")), "ctl deployment not indexed")
            print("PASS: ctl add-devices (custodied device: borrow/add/return) and create-deployment: unsigned --out file, "
                  "then API-built transactions verified call-by-call against the request and the CLI's chain reads, "
                  "signed, executed (effects success) and indexed")

            # Change feed via the CLI follows sequence numbers.
            out = subprocess.run([*ctl, "--token-file", str(token_file), "changes", "--after", str(before[1])],
                                 capture_output=True, text=True, timeout=60, check=True).stdout.splitlines()
            assert [json.loads(line)["seq"] for line in out] == sorted(json.loads(line)["seq"] for line in out) and len(out) >= 3
            print("PASS: ctl changes returns the integration feed in sequence order")

            # Load: ~45k synthetic rows, concurrent paginated reads.
            server.terminate(); server.wait(timeout=30)
            synthetic = lambda i: f"0xfeed{i:060x}"
            connection = sqlite3.connect(db)
            with connection:
                # Not on chain (low IDs would hit system objects such as 0x1): the device sweep
                # would tombstone them, so insert them as tombstones.
                connection.executemany("INSERT INTO devices (id, owner, name, device_address, arch, registered_at, deleted) VALUES (?,?,?,?,?,0,1)",
                    [(synthetic(i), n(operator) if i % 3 else n(other), f"load{i}", f"0x{i + 10**6:064x}", "aarch64")
                     for i in range(1, 20001)])
                connection.executemany("INSERT INTO groups (id, name, description, admin, created_at) VALUES (?,?,?,?,0)",
                    [(f"0x{i + 10**7:064x}", f"g{i}", "", n(operator)) for i in range(5000)])
                connection.executemany("INSERT INTO group_members (group_id, device_id) VALUES (?,?)",
                    [(f"0x{(i % 5000) + 10**7:064x}", synthetic(i)) for i in range(1, 20001)])
                connection.executemany("INSERT INTO deployments (id, creator, component_name, version, recipe_blob_id, artifact_sha256, created_at, schema_version) VALUES (?,?,?,?,?,?,0,1)",
                    [(f"0x{i + 10**8:064x}", n(operator), f"c{i % 50}", "1", "r", "a" * 64) for i in range(20000)])
            server = serve()
            paths = ["/v1/devices?include_deleted=true&limit=100", f"/v1/devices?owner={operator}&include_deleted=true&limit=100",
                     "/v1/groups?limit=100",
                     "/v1/deployments?component=c7&limit=100", f"/v1/deployments?device={synthetic(0x10)}&limit=100"]
            latencies = []

            def timed(path):
                start = time.perf_counter()
                status, body = http("GET", path, read_token)
                assert status == 200 and isinstance(body["items"], list)
                return time.perf_counter() - start
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                latencies = list(pool.map(timed, [paths[i % len(paths)] for i in range(400)]))
            p50, p95 = statistics.median(latencies), sorted(latencies)[int(len(latencies) * 0.95)]
            assert p95 < 1.0, f"p95 {p95:.3f}s"
            print(f"PASS: load: 400 concurrent paginated reads (8 clients) over ~45k rows: p50 {p50 * 1000:.1f} ms, "
                  f"p95 {p95 * 1000:.1f} ms")

            # A database bound to another chain is refused; --reset re-binds it.
            server.terminate(); server.wait(timeout=30)
            server = None
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE meta SET value = 'deadbeef' WHERE key = 'chain_identifier'")
            refused = subprocess.run([str(INDEXER), "index", *common], capture_output=True, text=True, timeout=120)
            assert refused.returncode != 0 and "network may have reset" in refused.stderr, refused.stderr
            subprocess.run([str(INDEXER), "index", *common, "--reset"], check=True, capture_output=True, timeout=300)
            rebound = sqlite3.connect(db)
            assert rebound.execute("SELECT value FROM meta WHERE key='chain_identifier'").fetchone()[0] == chain_id
            assert rebound.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 6, "reset did not rebuild from chain only"
            print("PASS: database bound to another chain refused; --reset discards it and re-indexes this chain")

            # Another publication on the same chain is not an upgrade: its event
            # types and objects differ, so the old cursors and rows are refused.
            contracts2 = root / "contracts2"
            shutil.copytree(ROOT / "contracts", contracts2, ignore=shutil.ignore_patterns("build", "Published.toml"))
            second = client("test-publish", contracts2, "--build-env", "testnet", "--pubfile-path", root / "Pub2.local.toml",
                            "--gas-budget", "500000000", "--silence-warnings")
            assert second["effects"]["status"]["status"] == "success"
            package2 = next(c["packageId"] for c in second["objectChanges"] if c["type"] == "published")
            registry2 = created(second, "::device_registry::DeviceRegistry")[0]
            manifest2 = dm.resolve(SimpleNamespace(chain_identifier=chain_id, rpc=rpc_url, package=package2,
                                                   upgrade_cap=created(second, "::package::UpgradeCap")[0], registry=registry2,
                                                   network="localnet", publisher="http://127.0.0.1:1",
                                                   aggregator="http://127.0.0.1:1"))
            (root / "deployment2.json").write_text(json.dumps(manifest2))
            only = transaction("--move-call", f"{package2}::device_registry::register_device", f"@{registry2}", '"only"',
                               f"@{operator}", '"aarch64"', CLOCK, "--assign", "device",
                               "--transfer-objects", "[device]", f"@{operator}")
            only = next(e["parsedJson"]["device_id"] for e in only["events"]
                        if e["type"].endswith("::device_registry::DeviceRegistered"))
            common2 = ["--manifest", str(root / "deployment2.json"), "--db", str(db)]
            rebound.close()
            refused = subprocess.run([str(INDEXER), "index", *common2], capture_output=True, text=True, timeout=120)
            assert refused.returncode != 0 and "another publication" in refused.stderr, refused.stderr
            with sqlite3.connect(db) as connection:
                assert connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 6, "refusal changed the index"
            reset = subprocess.run([str(INDEXER), "index", *common2, "--reset"], capture_output=True, text=True, timeout=300)
            assert reset.returncode == 0, reset.stderr[-3000:]
            with sqlite3.connect(db) as connection:
                ids = [row[0] for row in connection.execute("SELECT id FROM devices")]
                assert ids == [n(only)], f"reset index is not exactly the second publication: {ids}"
                assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
                assert connection.execute("SELECT COUNT(*) FROM deployments").fetchone()[0] == 0
                binding = json.loads(connection.execute("SELECT value FROM meta WHERE key='deployment_binding'").fetchone()[0])
                assert binding["registry"] == n(registry2), binding
            refused = subprocess.run([str(INDEXER), "index", *common], capture_output=True, text=True, timeout=120)
            assert refused.returncode != 0 and "another publication" in refused.stderr, refused.stderr
            print("PASS: another publication on the same chain refused; --reset indexes exactly the new deployment")
        except BaseException:
            log.flush()
            print((root / "indexer.log").read_text()[-4000:])
            raise
        finally:
            if server and server.poll() is None:
                server.terminate()
                server.wait(timeout=30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", default="http://127.0.0.1:9000")
    parser.add_argument("--faucet", default="http://127.0.0.1:9123/v2/gas")
    args = parser.parse_args()
    run(args.rpc, args.faucet)
