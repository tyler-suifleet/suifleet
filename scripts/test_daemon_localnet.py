#!/usr/bin/env python3
"""The real daemon against a real local Sui network over gRPC (SF-004).

Publishes the actual contracts to a disposable localnet, registers a device
whose key the daemon provisions, serves recipes/artifacts from a local Walrus
stand-in, and runs the daemon with the SF-030 test updater. Judges every
outcome from on-chain state (the deployment's authoritative receipts) and the
device's version/run-ID receipts:

  - direct deployment v1 -> Applied (report_status)
  - group deployment v2 -> Applied (report_status_for_group)
  - injected health failure -> Failed, reported as Failed
  - deployment for another device -> never applied
  - daemon offline while a deployment is created -> caught up after restart
  - journal with a JSON-RPC-era cursor -> migrated without replaying work
  - device revoked in the registry -> nothing applied; reinstated -> caught up

Run inside the dev image with a throwaway node and faucet:

    SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
    python3 scripts/test_daemon_localnet.py

It refuses non-loopback endpoints and never reads the normal wallet.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import deployment_manifest as dm
import test_component as component

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
CLOCK = "@0x6"


def wait_for(condition, message, process=None, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(0.5)
    raise AssertionError(message)


def run(rpc_url, faucet_url, catalog_upgrade=False, poll_cache=False, reset_node_pid=None, poll_cache_only=False, baseline_binary=None):
    for url in (rpc_url, faucet_url):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("this test requires an isolated loopback node and faucet")
    blobs = {}
    downloads = []

    class Walrus(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            downloads.append(self.path)
            body = blobs.get(self.path.removeprefix("/v1/blobs/"))
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

    walrus = ThreadingHTTPServer(("127.0.0.1", 0), Walrus)
    threading.Thread(target=walrus.serve_forever, daemon=True).start()
    aggregator = f"http://127.0.0.1:{walrus.server_port}"
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-daemon-localnet-") as temporary:
            root = Path(temporary)
            config_dir = root / "wallet"
            config_dir.mkdir()

            def client(*args, json_output=True):
                command = ["sui", "client", "--client.config", str(config_dir / "client.yaml"), *map(str, args)]
                if json_output:
                    command.append("--json")
                result = subprocess.run(command, text=True, capture_output=True, timeout=300)
                # Wallet initialization may print a recovery phrase: never forward it.
                if result.returncode:
                    raise RuntimeError(f"sui client {args[0]} failed:\n{result.stderr[-3000:]}\n{result.stdout[-3000:]}")
                if not json_output:
                    return result.stdout
                out = result.stdout
                return json.loads(out[min(i for i in (out.find("{"), out.find("[")) if i >= 0):])

            # Configure loopback before any CLI operation: the default generated
            # wallet points at public testnet and cannot be used network-none.
            (config_dir / "sui.keystore").write_text("[]")
            (config_dir / "client.yaml").write_text(f"keystore:\n  File: {config_dir / 'sui.keystore'}\nenvs:\n- alias: daemon-test\n  rpc: {rpc_url}\nactive_env: daemon-test\nactive_address: null\n")
            address = client("new-address", "ed25519")["address"]
            client("switch", "--address", address, json_output=False)
            operator = client("active-address", json_output=False).strip().split()[-1]
            client("faucet", "--url", faucet_url, json_output=False)
            def funded():
                gas = client("gas")  # A coin list, or {"gasCoins", "addressMistBalance"}.
                if isinstance(gas, dict):
                    return bool(gas.get("gasCoins")) or int(gas.get("addressMistBalance") or 0) > 0
                return bool(gas)
            wait_for(funded, "local faucet did not supply gas")
            # Current package verification/storage can exceed the historical
            # 500M budget on the pinned disposable chain. Reserve 1.5 SUI
            # with a separately funded local-only fixture wallet.
            for _ in range(2):
                client("faucet", "--url", faucet_url, json_output=False)
            chain_id = dm.chain_facts(rpc_url)["chain_identifier"]

            def transaction(*ptb):
                result = client("ptb", *ptb, "--gas-budget", "200000000")
                assert result["effects"]["status"]["status"] == "success", result["effects"]["status"]
                return result

            def created(result, suffix):
                return [c["objectId"] for c in result["objectChanges"]
                        if c["type"] == "created" and c["objectType"].endswith(suffix)]

            # The actual contracts, as a fresh publish.
            contracts = root / "contracts"
            shutil.copytree(ROOT / "contracts", contracts, ignore=shutil.ignore_patterns("build", "Published.toml"))
            if catalog_upgrade:
                # Pre-catalog main includes registry custody and identity activation.
                # Upgrade these existing objects rather than calling new identity APIs
                # against the earlier pre-identity package.
                for source in (contracts / "sources").glob("*.move"):
                    source.unlink()
                names = subprocess.check_output(["git", "ls-tree", "--name-only", "9c69582bd040cf82169940a6d14a1a288fd0bda4:contracts/sources"], cwd=ROOT, text=True).splitlines()
                for name in names:
                    (contracts / "sources" / name).write_bytes(subprocess.check_output(["git", "show", f"9c69582bd040cf82169940a6d14a1a288fd0bda4:contracts/sources/{name}"], cwd=ROOT))
            published = client("test-publish", contracts, "--build-env", "testnet", "--pubfile-path",
                               root / "Pub.local.toml", "--gas-budget", "1500000000", "--silence-warnings")
            assert published["effects"]["status"]["status"] == "success", published["effects"]["status"]
            package = next(c["packageId"] for c in published["objectChanges"] if c["type"] == "published")
            registry = created(published, "::device_registry::DeviceRegistry")[0]
            upgrade_cap = created(published, "::package::UpgradeCap")[0]
            if catalog_upgrade:
                # The baseline predates later required types, so read its origins
                # directly; the full manifest is resolved after the upgrade.
                original_origins = dm.origins(dm.chain_facts(rpc_url, package)["objects"][dm.object_id(package)])
            else:
                manifest = dm.resolve(SimpleNamespace(chain_identifier=chain_id, rpc=rpc_url, package=package,
                                                      upgrade_cap=upgrade_cap, registry=registry, network="localnet",
                                                      publisher=aggregator, aggregator=aggregator))

            # Device identity is provisioned by the daemon; it never leaves the device.
            key = root / "device.key"
            device_address = subprocess.check_output([str(DAEMON), "--provision", str(key)], text=True).strip()
            client("faucet", "--url", faucet_url, "--address", device_address, json_output=False)
            registered = transaction(
                "--move-call", f"{package}::device_registry::register_device", f"@{registry}", '"pi"',
                f"@{device_address}", '"aarch64"', CLOCK, "--assign", "device",
                "--move-call", f"{package}::device_cap::issue_cap", "device",
                "--move-call", f"{package}::device_registry::activate_device", f"@{registry}", "device",
                "--assign", "owner", "--transfer-objects", "[owner]", f"@{operator}")
            device = created(registered, "::device_registry::Device")[0]
            cap = created(registered, "::device_cap::DeviceCap")[0]
            owner_cap = created(registered, "::device_registry::DeviceOwnerCap")[0]

            def add_to_group(group):
                """The activated Device is borrowed from registry custody for `&Device`."""
                transaction("--move-call", f"{package}::device_registry::borrow_device", f"@{registry}", f"@{owner_cap}",
                            "--assign", "loaned",
                            "--move-call", f"{package}::device_group::add_device", f"@{group}", "loaned.0",
                            "--move-call", f"{package}::device_registry::return_device", f"@{registry}",
                            "loaned.0", "loaned.1")

            other = created(transaction(
                "--move-call", f"{package}::device_registry::register_device", f"@{registry}", '"other"',
                f"@{operator}", '"x86_64"', CLOCK, "--assign", "device",
                "--transfer-objects", "[device]", f"@{operator}"), "::device_registry::Device")[0]
            group_tx = transaction("--move-call", f"{package}::device_group::create_group", '"fleet"', '"test"', CLOCK)
            group = created(group_tx, "::device_group::DeviceGroup")[0]
            add_to_group(group)

            if catalog_upgrade:
                original_package = package
                # Existing objects were created by the baseline package before upgrade.
                shutil.copytree(ROOT / "contracts/sources", contracts / "sources", dirs_exist_ok=True)
                upgraded = client("test-upgrade", contracts, "--build-env", "testnet", "--pubfile-path",
                                  root / "Pub.local.toml", "--upgrade-capability", upgrade_cap,
                                  "--gas-budget", "1500000000", "--silence-warnings")
                assert upgraded["effects"]["status"]["status"] == "success", upgraded["effects"]["status"]
                package = next(c["packageId"] for c in upgraded["objectChanges"] if c["type"] == "published")
                manifest = dm.resolve(SimpleNamespace(chain_identifier=chain_id, rpc=rpc_url, package=package,
                    upgrade_cap=upgrade_cap, registry=registry, network="localnet", publisher=aggregator, aggregator=aggregator))
                assert all(manifest["type_origins"][n] == o for n,o in original_origins.items() if n in manifest["type_origins"])
                introduced = [n for n in manifest["type_origins"] if n not in original_origins]
                assert set(dm.CATALOG_TYPES) <= set(introduced), introduced
                assert all(manifest["type_origins"][n] == dm.object_id(package) for n in introduced)
                # Old package stays callable; existing-object compatible APIs still execute.
                transaction("--move-call", f"{original_package}::deployment_manager::create_deployment_verified",
                    '\"old-call\"', '\"1.0.0\"', '\"old-recipe\"', '\"'+"a"*64+'\"', '\"'+"b"*64+'\"',
                    "vector[]", f"vector[@{other}]", CLOCK)
                catalog = created(transaction("--move-call", f"{package}::component_catalog::create_catalog"), "::component_catalog::ComponentCatalog")[0]
                print("PASS: compatible upgrade from 9c69582bd040cf82169940a6d14a1a288fd0bda4, existing registry/device/group objects, old call remains callable, new catalog and enrollment/role introduction origins")

            run_id = "localnet-" + uuid.uuid4().hex[:10]

            def create_call(version, *, devices=(), groups=(), fail="none"):
                """PTB arguments creating one deployment, with its blobs served."""
                artifact = component.build(version, run_id, fail)
                tag = version.replace(".", "-")  # Walrus blob IDs are [A-Za-z0-9_-]
                artifact_blob, recipe_blob = f"artifact-{tag}", f"recipe-{tag}"
                recipe = component.recipe(artifact, artifact_blob)
                blobs[artifact_blob], blobs[recipe_blob] = artifact, recipe
                ids = lambda values: "[" + ", ".join(f"@{v}" for v in values) + "]"
                return ["--move-call", f"{package}::deployment_manager::create_deployment_verified",
                        f'"{component.DEFAULT_COMPONENT}"', f'"{version}"', f'"{recipe_blob}"',
                        f'"{hashlib.sha256(artifact).hexdigest()}"', f'"{hashlib.sha256(recipe).hexdigest()}"',
                        f"vector{ids(groups)}", f"vector{ids(devices)}", CLOCK]

            def deploy(version, **targets):
                result = transaction(*create_call(version, **targets))
                return created(result, "::deployment_manager::DeploymentRecord")[0]

            def deploy_catalog(version, *, devices=(), groups=(), unsupported=False,
                               name=component.DEFAULT_COMPONENT, dependencies=None, register_only=False,
                               first=("linux", "aarch64")):
                artifact = component.build(version, run_id, component=name)
                tag = (version if name == component.DEFAULT_COMPONENT else f"{name}-{version}").replace(".", "-")
                selected = "catalog-artifact-" + tag
                recipe_blob = "catalog-recipe-" + tag
                recipe = component.recipe(artifact, selected)
                if dependencies is not None:  # Recipe schema 1.1 (SF-012).
                    document = json.loads(recipe)
                    document.update(schemaVersion="1.1", componentDependencies=dependencies)
                    recipe = json.dumps(document).encode()
                blobs[selected], blobs[recipe_blob] = artifact, recipe
                # First manifest is intentionally incompatible on this x86_64 test host.
                strings = lambda values: "vector[" + ",".join(json.dumps(v) for v in values) + "]"
                registration = transaction("--move-call", f"{package}::component_catalog::publish_version", f"@{catalog}",
                    json.dumps(name), json.dumps(version), strings([first[0], "linux"]),
                    strings([first[1], "arm" if unsupported else "x86_64"]), strings(["never-download", recipe_blob]),
                    strings(["a"*64, hashlib.sha256(recipe).hexdigest()]), strings(["b"*64, hashlib.sha256(artifact).hexdigest()]), CLOCK)
                version_id = created(registration, "::component_catalog::ComponentVersion")[0]
                obj = dm.rpc(rpc_url,"sui_getObject",[version_id,{"showOwner":True,"showContent":True}])["data"]
                assert obj["owner"] == "Immutable"
                assert dm.object_id(obj["content"]["type"].split("::")[0]) == dm.object_id(package)
                if register_only:
                    return version_id
                ids = lambda values: "vector[" + ",".join("@"+v for v in values) + "]"
                result = transaction("--move-call", f"{package}::component_catalog::create_deployment", f"@{version_id}", ids(groups), ids(devices), CLOCK)
                deployment = created(result,"::deployment_manager::DeploymentRecord")[0]
                binding = dm.rpc(rpc_url,"suix_getDynamicFieldObject",[deployment,{"type":f"{package}::component_catalog::CatalogBindingKey","value":{"dummy_field":False}}])["data"]["content"]["fields"]["value"]["fields"]
                assert dm.object_id(binding["version_id"]) == dm.object_id(version_id)
                return deployment

            components = root / "state" / "test-components"
            template = root / "device-template.toml"
            template.write_text(
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{device}"\n'
                f'device_cap_id = "{cap}"\n'
                f'[apply]\ncommand = {json.dumps(f"/bin/sh {component.UPDATER} --root {components}")}\n'
                f'timeout_seconds = 30\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 1\nevent_discovery_seconds = 1\n')
            bundle = root / "release"
            dm.generate(manifest, bundle, template)
            config = bundle / "config.toml"
            journal_path = root / "state" / "state.json"

            def journal():
                return json.loads(journal_path.read_text()) if journal_path.exists() else {}

            def outcome(id):
                for key, value in journal().get("deployments", {}).items():
                    if dm.object_id(key) == dm.object_id(id):
                        return value
                return {}

            def receipts(deployment):
                """Authoritative on-chain statuses: device ID -> status."""
                for entry in dm.rpc(rpc_url, "suix_getDynamicFields", [deployment, None, 50])["data"]:
                    if entry["name"]["type"].endswith("::deployment_manager::ReceiptsKey"):
                        field = dm.rpc(rpc_url, "sui_getObject", [entry["objectId"], {"showContent": True}])
                        value = field["data"]["content"]["fields"]["value"]
                        contents = value["fields"]["statuses"]["fields"]["contents"]
                        return {dm.object_id(e["fields"]["key"]): int(e["fields"]["value"]) for e in contents}
                return {}

            def receipt_is(deployment, status):
                # Reads can briefly trail an executed transaction.
                expected = {dm.object_id(device): status}
                wait_for(lambda: receipts(deployment) == expected,
                         f"on-chain receipt for {deployment} never became {expected}")

            def history():
                path = components / component.DEFAULT_COMPONENT / "history.jsonl"
                return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

            log = (root / "daemon.log").open("w")

            def start(binary=DAEMON):
                return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)},
                                        stdout=log, stderr=log)

            def stop(process):
                process.send_signal(signal.SIGINT)
                process.wait(timeout=30)
                assert process.returncode == 0, process.returncode

            def settled(id, status):
                return lambda: outcome(id).get("status") == status and outcome(id).get("reported")

            try:
                before = deploy("0.9.0", devices=[device])  # Before enrollment: never applied.
                if not poll_cache_only:
                    process = start()
                    wait_for(lambda: journal().get("initialized"), "enrollment did not establish a cursor", process)

                    if catalog_upgrade:
                        unsupported = deploy_catalog("0.9.5", devices=[device], unsupported=True)
                        wait_for(settled(unsupported, 2), "unsupported platform did not fail and report", process)
                        receipt_is(unsupported, 2)
                        assert not downloads, downloads
                        assert not outcome(unsupported).get("firmware_request")
                        print("PASS: live unsupported platform Failed with on-chain receipt and no downloads, then valid deployment follows")
                    v1 = (deploy_catalog if catalog_upgrade else deploy)("1.0.0", devices=[device])
                    wait_for(settled(v1, 1), "direct v1 was not applied and reported", process)
                    receipt_is(v1, 1)

                    v2 = (deploy_catalog if catalog_upgrade else deploy)("2.0.0", groups=[group])
                    wait_for(settled(v2, 1), "group v2 was not applied and reported", process)
                    receipt_is(v2, 1)

                    if catalog_upgrade:
                        # SF-012: dependencies resolved from the real shared catalog's BCS index.
                        lib, app = "suifleet-dep-lib", "suifleet-dep-app"
                        for version in ("1.0.0", "1.5.0", "2.0.0"):
                            deploy_catalog(version, name=lib, register_only=True)
                        with_lib = deploy_catalog("1.0.0", name=app, devices=[device],
                                                  dependencies={lib: {"versionRequirement": "^1.0.0"}})
                        wait_for(settled(with_lib, 1), "dependency deployment was not applied and reported", process)
                        receipt_is(with_lib, 1)
                        installed = journal()["components"]
                        assert (installed[lib]["version"], installed[lib]["confirmed"]) == ("1.5.0", True), installed
                        assert (installed[app]["version"], installed[app]["confirmed"]) == ("1.0.0", True), installed
                        for name, version in ((lib, "1.5.0"), (app, "1.0.0")):
                            receipts_seen = (components / name / "history.jsonl").read_text().splitlines()
                            assert [json.loads(r)["version"] for r in receipts_seen] == [version], receipts_seen
                        conflict = deploy_catalog("2.0.0", name=app, devices=[device],
                                                  dependencies={lib: {"versionRequirement": "^2.0.0"}, "suifleet-dep-absent": {"versionRequirement": "*"}})
                        wait_for(settled(conflict, 2), "unresolvable dependencies were not rejected and reported", process)
                        receipt_is(conflict, 2)
                        assert "no available version of suifleet-dep-absent" in outcome(conflict)["error"], outcome(conflict)
                        assert not any(p.endswith(("catalog-artifact-suifleet-dep-lib-2-0-0", "catalog-artifact-suifleet-dep-app-2-0-0"))
                                       for p in downloads), downloads
                        assert journal()["components"] == installed, "a rejected deployment changed installed components"
                        print("PASS: live catalog BCS index resolved a schema-1.1 dependency (highest matching 1.5.0) "
                              "before its dependent, with on-chain receipt; unresolvable dependencies rejected before artifact downloads")

                        # SF-075: the upgraded package accepts a macOS manifest for
                        # the same architecture; this Linux daemon selects by OS.
                        mixed = deploy_catalog("2.1.0", devices=[device], first=("macos", "x86_64"))
                        wait_for(settled(mixed, 1), "a version listing a macOS manifest was not applied on Linux", process)
                        receipt_is(mixed, 1)
                        assert not any(p.endswith("never-download") for p in downloads), downloads
                        print("PASS: upgraded catalog publishes a macos/x86_64 manifest; the linux/x86_64 daemon selected "
                              "its own manifest and never fetched the macOS recipe")

                    bad = deploy("3.0.0", devices=[device], fail="health")
                    elsewhere = deploy("3.1.0", devices=[other])
                    wait_for(settled(bad, 2), "health failure was not reported as Failed", process)
                    receipt_is(bad, 2)
                    print("PASS: real localnet over gRPC: direct and group deployments Applied, health failure Failed, "
                          "each confirmed by on-chain receipts")

                    stop(process)
                    process = None
                    offline = deploy("4.0.0", devices=[device])
                    process = start()
                    wait_for(settled(offline, 1), "deployment created while offline was not caught up", process)
                    receipt_is(offline, 1)
                    print("PASS: offline catch-up after restart")

                    # Replace the cursor with the form the JSON-RPC daemon wrote, pointing
                    # at v1's event: everything after it is rescanned, nothing re-applied.
                    stop(process)
                    process = None
                    events = dm.rpc(rpc_url, "suix_queryEvents", [
                        {"MoveEventType": f"{manifest['type_origins']['deployment_manager::DeploymentCreated']}"
                                          "::deployment_manager::DeploymentCreated"}, None, 50, False])["data"]
                    legacy = next(e["id"] for e in events
                                  if dm.object_id(e["parsedJson"]["deployment_id"]) == dm.object_id(v1))
                    state = journal()
                    state.pop("discovery_cursor", None)
                    state.pop("discovery_merge_checkpoint", None)
                    state["cursor"] = legacy
                    journal_path.write_text(json.dumps(state))
                    applied_before = len(history())
                    after_migration = deploy("5.0.0", devices=[device])
                    process = start()
                    wait_for(settled(after_migration, 1), "deployment after a migrated cursor was not applied", process)
                    stop(process)
                    process = None
                    versions = [r["version"] for r in history()]
                    assert versions == ["1.0.0", "2.0.0", *(["2.1.0"] if catalog_upgrade else []), "3.0.0", "4.0.0", "5.0.0"], versions
                    assert len(history()) == applied_before + 1, "migration replayed applied work"
                    assert "after" in journal()["cursor"], journal()["cursor"]
                    assert not outcome(before) and not outcome(elsewhere), "untargeted or pre-enrollment deployment ran"
                    assert all(r["runId"] == run_id for r in history())
                    print("PASS: JSON-RPC-era cursor migrated to a gRPC cursor without replaying applied work; "
                          "pre-enrollment and other-device deployments never ran")

                    # Exact legacy boundary inside one transaction: A (a group the device
                    # is not in yet) precedes the cursor's event B, and C follows it.
                    # After the device joins A's group, migrating must still skip A, not
                    # replay B, and apply C exactly once.
                    late_group = created(transaction("--move-call", f"{package}::device_group::create_group",
                                                     '"late"', '"test"', CLOCK), "::device_group::DeviceGroup")[0]
                    batch = transaction(*create_call("6.0.0", groups=[late_group]),
                                        *create_call("6.1.0", devices=[device]),
                                        *create_call("6.2.0", devices=[device]))
                    digest = batch["digest"]
                    records = {}
                    for e in dm.rpc(rpc_url, "suix_queryEvents", [{"Transaction": digest}])["data"]:
                        if e["type"].endswith("::deployment_manager::DeploymentCreated"):
                            records[e["parsedJson"]["version"]] = (e["parsedJson"]["deployment_id"], e["id"])
                    skipped, boundary, later = (records[v] for v in ("6.0.0", "6.1.0", "6.2.0"))
                    assert boundary[1]["txDigest"] == digest and boundary[1]["eventSeq"] not in ("0", 0), boundary
                    state = journal()
                    state.pop("discovery_cursor", None)
                    state.pop("discovery_merge_checkpoint", None)
                    state["cursor"] = boundary[1]
                    state["deployments"][boundary[0]] = {"status": 1, "reported": True, "error": None}
                    journal_path.write_text(json.dumps(state))
                    add_to_group(late_group)
                    before_versions = [r["version"] for r in history()]
                    process = start()
                    wait_for(settled(later[0], 1), "event after the legacy boundary was not applied", process)
                    time.sleep(3)
                    stop(process)
                    process = None
                    versions = [r["version"] for r in history()]
                    assert versions == before_versions + ["6.2.0"], versions
                    assert not outcome(skipped[0]), "event before the legacy boundary was replayed"
                    print("PASS: legacy cursor resumes exactly after its event: an earlier, newly targeted deployment "
                          "in the same transaction is not replayed and a later one is applied once")

                    # SF-119 ports PR53 (09bcdc9); preserve the same-transaction check above.
                    # Same boundary across transactions: two transactions in one
                    # checkpoint, both targeting a group the device is not in yet.
                    # The cursor points at the later one; once the device joins the
                    # group, the earlier one must still not run.
                    cross_group = created(transaction("--move-call", f"{package}::device_group::create_group",
                                                      '"cross"', '"test"', CLOCK), "::device_group::DeviceGroup")[0]
                    # Concurrent transactions need distinct gas coins; earlier calls
                    # merged the operator's coins, so split two off.
                    split = transaction("--split-coins", "gas", "[5000000000, 5000000000]", "--assign", "parts",
                                        "--transfer-objects", "[parts.0, parts.1]", f"@{operator}")
                    coins = [c["objectId"] for c in split["objectChanges"]
                             if c["type"] == "created" and c["objectType"].endswith("::coin::Coin<0x2::sui::SUI>")]
                    assert len(coins) == 2, coins
                    pair = None
                    for attempt in range(8):
                        results = [None, None]
                        errors = []

                        def submit(slot, version, coin):
                            try:
                                result = client("ptb", *create_call(version, groups=[cross_group]),
                                                "--gas-coin", f"@{coin}", "--gas-budget", "200000000")
                                assert result["effects"]["status"]["status"] == "success", result["effects"]["status"]
                                results[slot] = result
                            except BaseException as error:
                                errors.append(error)
                        threads = [threading.Thread(target=submit, args=(slot, f"8.{attempt}.{slot}", coins[slot]))
                                   for slot in (0, 1)]
                        for thread in threads:
                            thread.start()
                        for thread in threads:
                            thread.join()
                        if errors:
                            raise errors[0]
                        digests = [r["digest"] for r in results]
                        assert len(set(digests)) == 2, digests
                        wait_for(lambda: all(dm.rpc(rpc_url, "sui_getTransactionBlock", [d, {}]).get("checkpoint")
                                             for d in digests), "transactions were not checkpointed")
                        checkpoints = {d: dm.rpc(rpc_url, "sui_getTransactionBlock", [d, {}])["checkpoint"] for d in digests}
                        if len(set(checkpoints.values())) == 1:
                            order = dm.rpc(rpc_url, "sui_getCheckpoint", [checkpoints[digests[0]]])["transactions"]
                            pair = sorted(digests, key=order.index)
                            break
                    assert pair, "could not place two transactions in one checkpoint"

                    def created_event(digest):
                        [event] = [e for e in dm.rpc(rpc_url, "suix_queryEvents", [{"Transaction": digest}])["data"]
                                   if e["type"].endswith("::deployment_manager::DeploymentCreated")]
                        return event["parsedJson"]["deployment_id"], event["id"]
                    print(f"Cross-transaction fixture: checkpoint={checkpoints[pair[0]]} ordered_digests={pair}")
                    earlier, boundary = created_event(pair[0]), created_event(pair[1])
                    state = journal()
                    state.pop("discovery_cursor", None)
                    state.pop("discovery_merge_checkpoint", None)
                    state["cursor"] = boundary[1]
                    state["deployments"][boundary[0]] = {"status": 1, "reported": True, "error": None}
                    journal_path.write_text(json.dumps(state))
                    add_to_group(cross_group)
                    following = deploy("9.0.0", devices=[device])
                    before_versions = [r["version"] for r in history()]
                    process = start()
                    wait_for(settled(following, 1), "deployment after the cross-transaction boundary was not applied",
                             process)
                    receipt_is(following, 1)
                    time.sleep(3)
                    stop(process)
                    process = None
                    versions = [r["version"] for r in history()]
                    assert versions == before_versions + ["9.0.0"], versions
                    assert "after" in journal()["cursor"], journal()["cursor"]
                    assert receipts(earlier[0]) == {}, "earlier transaction received an on-chain receipt"
                    print(f"Legacy boundary={boundary[1]} resumed_cursor={journal()['cursor']} following={following}")
                    assert not outcome(earlier[0]), "earlier transaction in the cursor's checkpoint was replayed"
                    print("PASS: legacy cursor resumes exactly after its event across transactions: an earlier "
                          "transaction in the same checkpoint is not replayed after it becomes targeted")

                    # Revocation is enforced on chain and honored by the daemon: while
                    # revoked it applies nothing; reinstated, it catches up.
                    transaction("--move-call", f"{package}::device_registry::revoke_device", f"@{registry}", f"@{owner_cap}")
                    applied_count = len(history())
                    revoked = deploy("7.0.0", devices=[device])
                    process = start()
                    time.sleep(10)
                    assert not outcome(revoked), "revoked device applied a deployment"
                    assert len(history()) == applied_count
                    transaction("--move-call", f"{package}::device_registry::reinstate_device", f"@{registry}", f"@{owner_cap}")
                    wait_for(settled(revoked, 1), "reinstated device did not apply and report", process)
                    receipt_is(revoked, 1)
                    stop(process)
                    process = None
                    assert "revoked or deregistered" in (root / "daemon.log").read_text()
                    print("PASS: on-chain revocation stops the real daemon; reinstatement resumes it with a verified receipt")
                if poll_cache:
                    import re
                    config.write_text(config.read_text().replace("event_discovery_seconds = 1", "event_discovery_seconds = 3600"))
                    metrics_start = len((root / "daemon.log").read_text())
                    def metrics():
                        return [(int(n), int(b)) for n,b in re.findall(r"read_requests=(\d+) response_bytes=(\d+)", re.sub(r"\x1b\[[0-9;]*m", "", (root / "daemon.log").read_text()[metrics_start:]))]
                    if baseline_binary:
                        process = start(baseline_binary)
                        wait_for(lambda: len(metrics()) >= 5, "baseline read profile incomplete", process)
                        print(f"BASELINE: localnet poll metrics (requests, protobuf response bytes): {metrics()[-3:]}")
                        stop(process); process = None
                        metrics_start = len((root / "daemon.log").read_text())
                    process = start()
                    wait_for(lambda: len(metrics()) >= 5 and all(n == 1 for n,b in metrics()[-3:]), "localnet did not reach one batched read per poll", process)
                    print(f"PASS: localnet idle poll metrics (requests, protobuf response bytes): {metrics()[-3:]}")
                    before = len(history())
                    errors = (root / "daemon.log").read_text().count("revoked or deregistered")
                    transaction("--move-call", f"{package}::device_registry::revoke_device", f"@{registry}", f"@{owner_cap}")
                    wait_for(lambda: (root / "daemon.log").read_text().count("revoked or deregistered") > errors, "cached poll missed live revocation", process, timeout=10)
                    assert len(history()) == before
                    transaction("--move-call", f"{package}::device_registry::reinstate_device", f"@{registry}", f"@{owner_cap}")
                    time.sleep(3)
                    transaction("--move-call", f"{package}::device_registry::rotate_device_key", f"@{registry}", f"@{owner_cap}", f"@{operator}")
                    wait_for(lambda: "rotated away" in (root / "daemon.log").read_text()[metrics_start:], "cached poll missed live key rotation", process, timeout=10)
                    assert len(history()) == before
                    transaction("--move-call", f"{package}::device_registry::rotate_device_key", f"@{registry}", f"@{owner_cap}", f"@{device_address}")
                    time.sleep(3)
                    upgraded = client("test-upgrade", contracts, "--upgrade-capability", upgrade_cap, "--build-env", "testnet",
                        "--pubfile-path", root / "Pub.local.toml", "--gas-budget", "500000000", "--silence-warnings")
                    assert upgraded["effects"]["status"]["status"] == "success"
                    wait_for(lambda: "UpgradeCap does not identify" in (root / "daemon.log").read_text()[metrics_start:], "cached poll missed live package upgrade", process, timeout=10)
                    assert len(history()) == before
                    print("PASS: warm localnet cache detects live revocation, rotation and package upgrade, without apply")
                    if reset_node_pid is not None:
                        stop(process); process = None
                        latest = next(c["packageId"] for c in upgraded["objectChanges"] if c["type"] == "published")
                        current_manifest = dm.resolve(SimpleNamespace(chain_identifier=chain_id, rpc=rpc_url, package=latest,
                            upgrade_cap=upgrade_cap, registry=registry, network="localnet", publisher=aggregator, aggregator=aggregator))
                        next_bundle = root / "release-after-upgrade"
                        dm.generate(current_manifest, next_bundle, template)
                        config = next_bundle / "config.toml"
                        config.write_text(config.read_text().replace("event_discovery_seconds = 1", "event_discovery_seconds = 3600"))
                        metrics_start = len((root / "daemon.log").read_text())
                        process = start()
                        wait_for(lambda: len(metrics()) >= 5 and all(n == 1 for n,b in metrics()[-3:]), "upgraded manifest did not warm its cache", process)
                        os.kill(reset_node_pid, signal.SIGTERM)
                        time.sleep(2)
                        reset_log = (root / "reset-node.log").open("w")
                        reset_node = subprocess.Popen(["sui", "start", "--with-faucet", "--force-regenesis"],
                            env={**os.environ, "SUI_CONFIG_DIR": str(root / "reset-node")}, stdout=reset_log, stderr=reset_log)
                        try:
                            # Stale objects disappear on the replacement genesis;
                            # version NotFound must never permit the idle fast path.
                            def replaced():
                                try: return dm.chain_facts(rpc_url)["chain_identifier"] != chain_id
                                except Exception: return False
                            wait_for(replaced, "replacement local genesis did not start", reset_node)
                            time.sleep(3)
                            assert len(history()) == before
                            assert "chain identifier mismatch" in (root / "daemon.log").read_text()[metrics_start:]
                            print("PASS: actual localnet reset rejected under the warm manifest/cache; no apply")
                        finally:
                            reset_node.terminate(); reset_node.wait(timeout=30); reset_log.close()
                    stop(process); process = None

            except BaseException:
                log.flush()
                print((root / "daemon.log").read_text()[-4000:])
                raise
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        walrus.shutdown()
        walrus.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", default="http://127.0.0.1:9000")
    parser.add_argument("--faucet", default="http://127.0.0.1:9123/v2/gas")
    parser.add_argument("--catalog-upgrade", action="store_true")
    parser.add_argument("--poll-cache", action="store_true")
    parser.add_argument("--cache-reset-node-pid", type=int, help="owned disposable node PID; reset it after warm-cache checks")
    parser.add_argument("--poll-cache-only", action="store_true", help="run warm-cache checks after fresh local fixture setup")
    parser.add_argument("--baseline-binary", type=Path, help="optional metrics-only instrumented baseline daemon")
    args = parser.parse_args()
    run(args.rpc, args.faucet, args.catalog_upgrade, args.poll_cache or args.poll_cache_only, args.cache_reset_node_pid, args.poll_cache_only, args.baseline_binary)
