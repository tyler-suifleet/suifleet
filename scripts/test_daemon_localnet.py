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


def run(rpc_url, faucet_url):
    for url in (rpc_url, faucet_url):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("this test requires an isolated loopback node and faucet")
    blobs = {}

    class Walrus(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
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

            client("-y", "active-address", json_output=False)
            client("new-env", "--alias", "daemon-test", "--rpc", rpc_url, json_output=False)
            client("switch", "--env", "daemon-test", json_output=False)
            operator = client("active-address", json_output=False).strip().split()[-1]
            client("faucet", "--url", faucet_url, json_output=False)
            def funded():
                gas = client("gas")  # A coin list, or {"gasCoins", "addressMistBalance"}.
                if isinstance(gas, dict):
                    return bool(gas.get("gasCoins")) or int(gas.get("addressMistBalance") or 0) > 0
                return bool(gas)
            wait_for(funded, "local faucet did not supply gas")
            chain_id = dm.rpc(rpc_url, "sui_getChainIdentifier", [])

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
            published = client("test-publish", contracts, "--build-env", "testnet", "--pubfile-path",
                               root / "Pub.local.toml", "--gas-budget", "500000000", "--silence-warnings")
            assert published["effects"]["status"]["status"] == "success", published["effects"]["status"]
            package = next(c["packageId"] for c in published["objectChanges"] if c["type"] == "published")
            registry = created(published, "::device_registry::DeviceRegistry")[0]
            upgrade_cap = created(published, "::package::UpgradeCap")[0]
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
                "--transfer-objects", "[device]", f"@{operator}")
            device = created(registered, "::device_registry::Device")[0]
            cap = created(registered, "::device_cap::DeviceCap")[0]
            other = created(transaction(
                "--move-call", f"{package}::device_registry::register_device", f"@{registry}", '"other"',
                f"@{operator}", '"x86_64"', CLOCK, "--assign", "device",
                "--transfer-objects", "[device]", f"@{operator}"), "::device_registry::Device")[0]
            group_tx = transaction("--move-call", f"{package}::device_group::create_group", '"fleet"', '"test"', CLOCK)
            group = created(group_tx, "::device_group::DeviceGroup")[0]
            transaction("--move-call", f"{package}::device_group::add_device", f"@{group}", f"@{device}")

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

            components = root / "state" / "test-components"
            template = root / "device-template.toml"
            template.write_text(
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{device}"\n'
                f'device_cap_id = "{cap}"\n'
                f'[apply]\ncommand = {json.dumps(f"/bin/sh {component.UPDATER} --root {components}")}\n'
                f'timeout_seconds = 30\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 1\n')
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

            def start():
                return subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(config)},
                                        stdout=log, stderr=log)

            def stop(process):
                process.send_signal(signal.SIGINT)
                process.wait(timeout=30)
                assert process.returncode == 0, process.returncode

            def settled(id, status):
                return lambda: outcome(id).get("status") == status and outcome(id).get("reported")

            try:
                before = deploy("0.9.0", devices=[device])  # Before enrollment: never applied.
                process = start()
                wait_for(lambda: journal().get("initialized"), "enrollment did not establish a cursor", process)

                v1 = deploy("1.0.0", devices=[device])
                wait_for(settled(v1, 1), "direct v1 was not applied and reported", process)
                receipt_is(v1, 1)

                v2 = deploy("2.0.0", groups=[group])
                wait_for(settled(v2, 1), "group v2 was not applied and reported", process)
                receipt_is(v2, 1)

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
                state["cursor"] = legacy
                journal_path.write_text(json.dumps(state))
                applied_before = len(history())
                after_migration = deploy("5.0.0", devices=[device])
                process = start()
                wait_for(settled(after_migration, 1), "deployment after a migrated cursor was not applied", process)
                stop(process)
                process = None
                versions = [r["version"] for r in history()]
                assert versions == ["1.0.0", "2.0.0", "3.0.0", "4.0.0", "5.0.0"], versions
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
                state["cursor"] = boundary[1]
                state["deployments"][boundary[0]] = {"status": 1, "reported": True, "error": None}
                journal_path.write_text(json.dumps(state))
                transaction("--move-call", f"{package}::device_group::add_device", f"@{late_group}", f"@{device}")
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

                # Revocation is enforced on chain and honored by the daemon: while
                # revoked it applies nothing; reinstated, it catches up.
                transaction("--move-call", f"{package}::device_registry::revoke_device", f"@{registry}", f"@{device}")
                applied_count = len(history())
                revoked = deploy("7.0.0", devices=[device])
                process = start()
                time.sleep(10)
                assert not outcome(revoked), "revoked device applied a deployment"
                assert len(history()) == applied_count
                transaction("--move-call", f"{package}::device_registry::activate_device", f"@{registry}", f"@{device}")
                wait_for(settled(revoked, 1), "reinstated device did not apply and report", process)
                receipt_is(revoked, 1)
                stop(process)
                process = None
                assert "revoked or deregistered" in (root / "daemon.log").read_text()
                print("PASS: on-chain revocation stops the real daemon; reinstatement resumes it with a verified receipt")
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
    args = parser.parse_args()
    run(args.rpc, args.faucet)
