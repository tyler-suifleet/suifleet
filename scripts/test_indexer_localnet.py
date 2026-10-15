#!/usr/bin/env python3
"""The fleet indexer, API and CLI (SF-024) against a real local Sui network.

Publishes the actual contracts to a disposable localnet, builds real fleet
state (devices of two operators, a group, direct and group deployments, a
signer-checked receipt), indexes it, and checks:

  - the API matches chain state: devices, owners, groups and members,
    deployments, targets, resolved devices and authoritative receipts;
  - filters and keyset pagination return every item exactly once;
  - restart resumes from durable cursors: later removals and deletions
    appear once in the change feed, earlier changes are not repeated;
  - a plain transfer of a Device (no fleet event) is reconciled while
    running and across a restart: owner filters, the change feed and the
    bulk ownership precheck follow the new owner;
  - devices wrapped in their registering transaction (the event names an
    absent object) are kept and indexed once unwrapped, while running and
    across a restart;
  - auth: no token 401, read token cannot operate (403), operate token can;
  - bulk operations: `ctl add-devices` and `ctl create-deployment` build
    through the API, verify every call and argument against the request,
    sign and execute; `ctl ... --out` writes an unsigned transaction; the
    indexer then shows the new membership and deployment;
  - a database bound to another chain is refused; --reset re-binds it;
  - load: concurrent paginated reads over ~45k synthetic rows.

Run inside the dev image with a throwaway node and faucet (make test-indexer-localnet):

    SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
    python3 scripts/test_indexer_localnet.py
"""
import argparse
import base64
import concurrent.futures
import json
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

        def register(name, address, owner):
            result = transaction("--move-call", f"{package}::device_registry::register_device", f"@{registry}", f'"{name}"',
                                 f"@{address}", '"aarch64"', CLOCK, "--assign", "device",
                                 "--move-call", f"{package}::device_cap::issue_cap", "device",
                                 "--transfer-objects", "[device]", f"@{owner}")
            return created(result, "::device_registry::Device")[0], created(result, "::device_cap::DeviceCap")[0]
        use(operator)
        pi, pi_cap = register("pi", device_signer, operator)
        spare, _ = register("spare", operator, operator)
        third, _ = register("third", operator, operator)
        use(other)
        foreign, _ = register("foreign", other, other)
        use(operator)
        group = created(transaction("--move-call", f"{package}::device_group::create_group", '"lab"', '"indexer"', CLOCK),
                        "::device_group::DeviceGroup")[0]
        transaction("--move-call", f"{package}::device_group::add_device", f"@{group}", f"@{pi}")
        transaction("--move-call", f"{package}::device_group::add_device", f"@{group}", f"@{spare}")

        def deploy(version, groups=(), devices=()):
            ids = lambda values: "[" + ", ".join(f"@{v}" for v in values) + "]"
            return created(transaction("--move-call", f"{package}::deployment_manager::create_deployment_verified",
                                       '"fw"', f'"{version}"', '"recipe"', f'"{"a" * 64}"', f'"{"b" * 64}"',
                                       f"vector{ids(groups)}", f"vector{ids(devices)}", CLOCK),
                           "::deployment_manager::DeploymentRecord")[0]
        by_group, direct = deploy("1.0.0", groups=[group]), deploy("1.1.0", devices=[third])
        use(device_signer)
        transaction("--move-call", f"{package}::deployment_manager::report_status_for_group", f"@{by_group}", f"@{pi_cap}",
                    f"@{group}", "1u8")
        use(operator)
        n = dm.object_id

        # ── Index, serve and compare with chain ─────────────────────────────
        db = root / "fleet.db"
        common = ["--manifest", str(root / "deployment.json"), "--db", str(db)]
        indexed = subprocess.run([str(INDEXER), "index", *common], capture_output=True, text=True, timeout=300)
        assert indexed.returncode == 0, indexed.stderr[-3000:]
        read_token, operate_token = "r" * 40, "o" * 48
        (root / "tokens").write_text(f"read {read_token}\noperate {operate_token}\n")
        log = (root / "indexer.log").open("w")

        def serve(*extra):
            process = subprocess.Popen([str(INDEXER), "serve", *common, "--tokens", str(root / "tokens"),
                                        "--interval-seconds", "1", *extra], stdout=log, stderr=log)
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
            print("PASS: API matches chain state (owners, members, targets, resolved devices, signer-checked receipt); "
                  "filters and limit=2 pagination return every item once")

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
            new = get(f"/v1/changes?after={before[1]}")["items"]
            kinds = sorted(c["kind"] for c in new)
            assert kinds == ["deployment.deleted", "group.updated"], kinds
            assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM changes WHERE seq <= ?", (before[1],)).fetchone()[0] == before[0]
            assert every(f"/v1/deployments?device={third}") == [] and len(every("/v1/deployments?include_deleted=true")) == 2
            print("PASS: restart resumes from durable cursors; removal and deletion recorded once, earlier changes not repeated; "
                  "deleted record kept as a tombstone")

            # A plain transfer emits no fleet event; the device sweep reconciles it.
            def owned_by(address):
                return [d["id"] for d in every(f"/v1/devices?owner={address}")]
            mark = get("/v1/status")["last_change"]
            transaction("--transfer-objects", f"[@{spare}]", f"@{other}")
            wait_for(lambda: get(f"/v1/devices/{spare}")["owner"] == n(other), "transfer not reconciled while running")
            assert n(spare) in owned_by(other) and n(spare) not in owned_by(operator)
            updates = [c for c in get(f"/v1/changes?after={mark}")["items"] if c["object_id"] == n(spare)]
            assert [c["kind"] for c in updates] == ["device.updated"], updates
            status, refused = http("POST", "/v1/transactions/add-devices", operate_token,
                                   {"sender": operator, "group": group, "devices": [spare]})
            assert status == 400 and "not owned by" in refused["error"], refused
            new_owner = {"sender": other, "component_name": "fw", "version": "9.0.0", "recipe_blob_id": "recipe",
                         "artifact_sha256": "a" * 64, "recipe_sha256": "b" * 64, "target_devices": [spare]}
            status, built = http("POST", "/v1/transactions/create-deployment", operate_token, new_owner)
            assert status == 200 and built["sender"] == n(other), built
            # Transferred back while the indexer is stopped: correct as soon as it serves again.
            server.terminate(); server.wait(timeout=30)
            use(other)
            transaction("--transfer-objects", f"[@{spare}]", f"@{operator}")
            use(operator)
            server = serve()
            assert get(f"/v1/devices/{spare}")["owner"] == n(operator), "restart served a pre-restart owner"
            assert n(spare) in owned_by(operator) and n(spare) not in owned_by(other)
            updates = [c for c in get(f"/v1/changes?after={mark}")["items"] if c["object_id"] == n(spare)]
            assert [c["kind"] for c in updates] == ["device.updated"] * 2, updates
            assert get("/v1/status")["devices_reconciled_at"]
            print("PASS: device transfers without fleet events reconciled while running and across a restart "
                  "(owner filters, one change each, live bulk ownership precheck)")

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
            print("PASS: ctl add-devices and create-deployment: unsigned --out file, then API-built transactions verified "
                  "call-by-call against the request, signed, executed (effects success) and indexed")

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
