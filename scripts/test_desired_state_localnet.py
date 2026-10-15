#!/usr/bin/env python3
"""Desired-state reconciliation (SF-013) on a real local Sui network, across a
real package upgrade.

Publishes main's contracts (the merge base), creates an activated device, a
group and a one-shot deployment with that old package, then upgrades to this tree's
contracts and drives desired state on the pre-existing group through the new
`desired_state` module. The real daemon (gRPC) runs the SF-030 test updater;
outcomes are judged from on-chain receipts and the components' run history.

  - offline through two revisions -> only the latest is applied and reported
  - one PTB creates two deployments and commits both as one revision
  - a device added to a group with existing desired state receives it
  - a device removed from a group stops following it
  - two groups disagreeing on a component -> neither applied
  - a desired record deleted through the OLD package's entry point is skipped
  - a non-admin cannot revise a group's desired state (aborts on chain)
  - unmanaged one-shot deployments still apply

Run inside the dev image with a throwaway node and faucet, after building the
daemon (make test-desired-state-localnet does both builds):

    SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
    python3 scripts/test_desired_state_localnet.py

Refuses non-loopback endpoints and never reads the normal wallet.
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
FIRMWARE, AGENT, LOGGER = component.DEFAULT_COMPONENT, "suifleet-test-agent", "suifleet-test-logger"


def wait_for(condition, message, process=None, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(0.5)
    raise AssertionError(message)


def base_contracts(destination):
    """main's contracts at the merge base, as the pre-upgrade package."""
    base = subprocess.check_output(["git", "-C", str(ROOT), "merge-base", "HEAD", "origin/main"], text=True).strip()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", base, "contracts"], check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(destination)], input=archive, check=True)
    return base


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
        with tempfile.TemporaryDirectory(prefix="suifleet-desired-localnet-") as temporary:
            root = Path(temporary)
            (root / "wallet").mkdir()

            def client(*args, json_output=True, check=True):
                command = ["sui", "client", "--client.config", str(root / "wallet/client.yaml"), *map(str, args)]
                if json_output:
                    command.append("--json")
                result = subprocess.run(command, text=True, capture_output=True, timeout=300)
                # Wallet initialization may print a recovery phrase: never forward it.
                if result.returncode and check:
                    raise RuntimeError(f"sui client {args[0]} failed:\n{result.stderr[-3000:]}\n{result.stdout[-3000:]}")
                if not json_output or result.returncode:
                    return result
                out = result.stdout
                return json.loads(out[min(i for i in (out.find("{"), out.find("[")) if i >= 0):])

            # Configure loopback before any CLI operation: the default generated
            # wallet points at public testnet and cannot be used network-none.
            wallet = root / "wallet"
            (wallet / "sui.keystore").write_text("[]")
            (wallet / "client.yaml").write_text(f"keystore:\n  File: {wallet / 'sui.keystore'}\nenvs:\n- alias: desired-test\n  rpc: {rpc_url}\nactive_env: desired-test\nactive_address: null\n")
            address = client("new-address", "ed25519")["address"]
            client("switch", "--address", address, json_output=False)
            operator = client("active-address", json_output=False).stdout.strip().split()[-1]

            def fund(address):
                client("faucet", "--url", faucet_url, "--address", address, json_output=False)

            def funded():
                gas = client("gas")
                if isinstance(gas, dict):
                    return bool(gas.get("gasCoins")) or int(gas.get("addressMistBalance") or 0) > 0
                return bool(gas)
            fund(operator)
            wait_for(funded, "local faucet did not supply gas")
            chain_id = dm.rpc(rpc_url, "sui_getChainIdentifier", [])

            def transaction(*ptb):
                result = client("ptb", *ptb, "--gas-budget", "200000000")
                assert result["effects"]["status"]["status"] == "success", result["effects"]["status"]
                return result

            def created(result, suffix):
                return [c["objectId"] for c in result["objectChanges"]
                        if c["type"] == "created" and c["objectType"].endswith(suffix)]

            def published(result):
                return next(c["packageId"] for c in result["objectChanges"] if c["type"] == "published")

            # ── Old package: publish main's contracts and create live state ──
            package_dir, pubfile = root / "package", root / "Pub.local.toml"
            staging = root / "base"; staging.mkdir()
            base = base_contracts(staging)
            shutil.copytree(staging / "contracts", package_dir, ignore=shutil.ignore_patterns("build", "Published.toml"))
            common = ["--build-env", "testnet", "--pubfile-path", pubfile, "--gas-budget", "900000000", "--silence-warnings"]
            first = client("test-publish", package_dir, *common)
            assert first["effects"]["status"]["status"] == "success", first["effects"]["status"]
            old = published(first)
            registry = created(first, "::device_registry::DeviceRegistry")[0]
            upgrade_cap = created(first, "::package::UpgradeCap")[0]

            key = root / "device.key"
            device_address = subprocess.check_output([str(DAEMON), "--provision", str(key)], text=True).strip()
            fund(device_address)
            registered = transaction(
                "--move-call", f"{old}::device_registry::register_device", f"@{registry}", '"pi"',
                f"@{device_address}", '"aarch64"', CLOCK, "--assign", "device",
                "--move-call", f"{old}::device_cap::issue_cap", "device",
                "--move-call", f"{old}::device_registry::activate_device", f"@{registry}", "device",
                "--assign", "owner", "--transfer-objects", "[owner]", f"@{operator}")
            device = created(registered, "::device_registry::Device")[0]
            cap = created(registered, "::device_cap::DeviceCap")[0]
            owner_cap = created(registered, "::device_registry::DeviceOwnerCap")[0]

            def add_to_group(package, group):
                """The activated Device is borrowed from registry custody for `&Device`."""
                transaction("--move-call", f"{package}::device_registry::borrow_device", f"@{registry}", f"@{owner_cap}",
                            "--assign", "loaned",
                            "--move-call", f"{package}::device_group::add_device", f"@{group}", "loaned.0",
                            "--move-call", f"{package}::device_registry::return_device", f"@{registry}",
                            "loaned.0", "loaned.1")

            def new_group(package, name):
                return created(transaction("--move-call", f"{package}::device_group::create_group",
                                           f'"{name}"', '"test"', CLOCK), "::device_group::DeviceGroup")[0]
            fleet = new_group(old, "fleet")  # Pre-upgrade group: desired state is added to it later.
            add_to_group(old, fleet)

            # ── Upgrade to this tree's contracts ─────────────────────────────
            shutil.rmtree(package_dir)
            shutil.copytree(ROOT / "contracts", package_dir, ignore=shutil.ignore_patterns("build", "Published.toml"))
            upgrade = client("test-upgrade", package_dir, "--upgrade-capability", upgrade_cap, *common)
            assert upgrade["effects"]["status"]["status"] == "success", upgrade["effects"]["status"]
            new = published(upgrade)
            manifest = dm.resolve(SimpleNamespace(chain_identifier=chain_id, rpc=rpc_url, package=new,
                                                  upgrade_cap=upgrade_cap, registry=registry, network="localnet",
                                                  publisher=aggregator, aggregator=aggregator))
            origins = manifest["type_origins"]
            assert origins["desired_state::DesiredKey"] == dm.object_id(new) != origins["device_group::DeviceGroup"]
            assert origins["deployment_manager::ManagedKey"] == dm.object_id(new)
            print(f"PASS: main ({base[:12]}) published and upgraded in place; desired-state types originate in the upgrade")

            run_id = "desired-" + uuid.uuid4().hex[:10]

            def serve(version, name):
                artifact = component.build(version, run_id, component=name)
                tag = f"{name}-{version}".replace(".", "-")
                artifact_blob, recipe_blob = f"artifact-{tag}", f"recipe-{tag}"
                recipe = component.recipe(artifact, artifact_blob)
                blobs[artifact_blob], blobs[recipe_blob] = artifact, recipe
                return (f'"{name}"', f'"{version}"', f'"{recipe_blob}"', f'"{hashlib.sha256(artifact).hexdigest()}"',
                        f'"{hashlib.sha256(recipe).hexdigest()}"')

            def revise(group, *changes, remove=()):
                """One PTB: create each (name, version) deployment targeting `group`,
                make all of them desired in one revision, then share them."""
                ptb = ["--move-call", f"{new}::desired_state::begin", f"@{group}", "--assign", "plan"]
                for index, (name, version) in enumerate(changes):
                    ptb += ["--move-call", f"{new}::deployment_manager::new_deployment_verified", *serve(version, name),
                            f"vector[@{group}]", "vector[]", CLOCK, "--assign", f"record{index}",
                            "--move-call", f"{new}::desired_state::set_component", "plan", f"record{index}"]
                for name in remove:
                    ptb += ["--move-call", f"{new}::desired_state::remove_component", "plan", f'"{name}"']
                ptb += ["--move-call", f"{new}::desired_state::commit", "plan", f"@{group}", CLOCK]
                for index in range(len(changes)):
                    ptb += ["--move-call", f"{new}::deployment_manager::share_deployment", f"record{index}"]
                result = transaction(*ptb)
                records = created(result, "::deployment_manager::DeploymentRecord")
                assert len(records) == len(changes)
                by_version = {}
                for record in records:
                    fields = dm.rpc(rpc_url, "sui_getObject", [record, {"showContent": True}])["data"]["content"]["fields"]
                    by_version[(fields["component_name"], fields["version"])] = record
                return [by_version[change] for change in changes]

            components = root / "state" / "test-components"
            template = root / "device-template.toml"
            template.write_text(
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{device}"\n'
                f'device_cap_id = "{cap}"\n'
                f'[apply]\ncommand = {json.dumps(f"/bin/sh {component.UPDATER} --root {components}")}\n'
                f'timeout_seconds = 30\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 1\n')
            dm.generate(manifest, root / "release", template)
            config = root / "release" / "config.toml"
            journal_path = root / "state" / "state.json"

            def journal():
                return json.loads(journal_path.read_text()) if journal_path.exists() else {}

            def outcome(id):
                for key, value in journal().get("deployments", {}).items():
                    if dm.object_id(key) == dm.object_id(id):
                        return value
                return {}

            def receipts(deployment):
                for entry in dm.rpc(rpc_url, "suix_getDynamicFields", [deployment, None, 50])["data"]:
                    if entry["name"]["type"].endswith("::deployment_manager::ReceiptsKey"):
                        field = dm.rpc(rpc_url, "sui_getObject", [entry["objectId"], {"showContent": True}])
                        contents = field["data"]["content"]["fields"]["value"]["fields"]["statuses"]["fields"]["contents"]
                        return {dm.object_id(e["fields"]["key"]): int(e["fields"]["value"]) for e in contents}
                return {}

            def applied_and_receipted(deployment):
                wait_for(lambda: outcome(deployment).get("status") == 1 and outcome(deployment).get("reported"),
                         f"{deployment} was not applied and reported", process)
                wait_for(lambda: receipts(deployment) == {dm.object_id(device): 1},
                         f"on-chain receipt for {deployment} never became Applied", process)

            def history(name):
                path = components / name / "history.jsonl"
                return [json.loads(line)["version"] for line in path.read_text().splitlines()] if path.exists() else []

            def settle():
                time.sleep(4)  # Several 1 s polls.

            log = (root / "daemon.log").open("w")

            def start():
                return subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(config)},
                                        stdout=log, stderr=log)

            def stop(process):
                process.send_signal(signal.SIGINT)
                process.wait(timeout=30)
                assert process.returncode == 0, process.returncode

            try:
                # 1. Two revisions while the device is offline (never started).
                [fw1] = revise(fleet, (FIRMWARE, "1.0.0"))
                [fw2] = revise(fleet, (FIRMWARE, "1.1.0"))
                process = start()
                applied_and_receipted(fw2)
                settle()
                assert history(FIRMWARE) == ["1.1.0"], history(FIRMWARE)
                assert not outcome(fw1) and receipts(fw1) == {}, "superseded revision applied"
                print("PASS: offline device applies only the latest revision on the pre-upgrade group; "
                      "receipt Applied on chain, superseded record untouched")

                # 2. One PTB: two components, one revision.
                fw3, agent = revise(fleet, (FIRMWARE, "1.2.0"), (AGENT, "1.0.0"))
                applied_and_receipted(fw3)
                applied_and_receipted(agent)
                assert history(FIRMWARE)[-1] == "1.2.0" and history(AGENT) == ["1.0.0"]
                print("PASS: two deployments created and committed as one revision in one PTB; both applied")

                # 3. Joining a group that already has desired state.
                later = new_group(new, "later")
                [logger] = revise(later, (LOGGER, "1.0.0"))
                settle()
                assert not outcome(logger), "non-member applied a group's desired state"
                add_to_group(new, later)
                applied_and_receipted(logger)
                print("PASS: device added to a group receives that group's existing desired state")

                # 4. Leaving the group: retired, never re-applied.
                transaction("--move-call", f"{old}::device_group::remove_device", f"@{later}", f"@{device}")
                wait_for(lambda: LOGGER not in journal().get("components", {}), "removed member kept following", process)
                [logger2] = revise(later, (LOGGER, "2.0.0"))
                settle()
                assert not outcome(logger2) and history(LOGGER) == ["1.0.0"]
                print("PASS: device removed from a group (old package's remove_device) stops following it")

                # 5. Two groups disagree on the firmware.
                rival = new_group(new, "rival")
                add_to_group(new, rival)
                [rival_fw] = revise(rival, (FIRMWARE, "9.9.9"))
                settle()
                assert not outcome(rival_fw) and history(FIRMWARE)[-1] == "1.2.0"
                print("PASS: conflicting groups leave the component as installed")
                transaction("--move-call", f"{new}::device_group::remove_device", f"@{rival}", f"@{device}")

                # 6. A desired record deleted through the OLD package while offline.
                stop(process)
                process = None
                [doomed] = revise(fleet, (FIRMWARE, "1.3.0"))
                transaction("--move-call", f"{old}::deployment_manager::delete_deployment", f"@{doomed}")
                process = start()
                settle()
                assert not outcome(doomed) and history(FIRMWARE)[-1] == "1.2.0"
                assert process.poll() is None
                print("PASS: desired record deleted via the old package's delete_deployment is skipped safely")

                # 7. A non-admin cannot revise the group.
                outsider = client("new-address", "ed25519", json_output=False).stdout
                outsider = next(word for word in outsider.split() if word.startswith("0x") and len(word) == 66)
                fund(outsider)
                client("switch", "--address", outsider, json_output=False)
                wait_for(funded, "outsider was not funded")
                attempt = client("ptb", "--move-call", f"{new}::desired_state::begin", f"@{fleet}", "--assign", "plan",
                                 "--move-call", f"{new}::desired_state::commit", "plan", f"@{fleet}", CLOCK,
                                 "--gas-budget", "50000000", check=False)
                client("switch", "--address", operator, json_output=False)
                text = attempt.stderr + attempt.stdout if hasattr(attempt, "stderr") else json.dumps(attempt)
                failed = getattr(attempt, "returncode", 0) != 0 or attempt["effects"]["status"]["status"] != "success"
                assert failed and "desired_state" in text, text[-2000:]
                print("PASS: non-admin revision aborts on chain")

                # 8. Unmanaged one-shot deployments still work after the upgrade.
                one_shot = created(transaction("--move-call", f"{new}::deployment_manager::create_deployment_verified",
                                               *serve("5.0.0", "suifleet-test-tool"), "vector[]", f"vector[@{device}]",
                                               CLOCK), "::deployment_manager::DeploymentRecord")[0]
                applied_and_receipted(one_shot)
                stop(process)
                process = None
                assert history(FIRMWARE) == ["1.1.0", "1.2.0"], history(FIRMWARE)
                assert journal()["components"] == {FIRMWARE: fw3, AGENT: agent}, journal()["components"]
                print("PASS: unmanaged one-shot deployment applied alongside desired state")
            except BaseException:
                log.flush()
                print((root / "daemon.log").read_text()[-5000:])
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
