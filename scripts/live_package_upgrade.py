#!/usr/bin/env python3
"""Live package-upgrade acceptance on Sui testnet (the `package-upgrade` live gate).

Spends real testnet gas from an explicitly supplied wallet; never faucets,
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
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import deployment_manifest as dm
import test_component as component

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
CLOCK = "@0x6"
CHAINS = {"testnet": "4c78adac"}
DEVICE_FUNDING = 30_000_000  # MIST for the device's status reports.


class Spent(Exception):
    pass


def plain(value):
    """Move JSON with or without JSON-RPC-style {"fields": ...} wrappers."""
    if isinstance(value, dict):
        if set(value) <= {"type", "fields"} and "fields" in value:
            return plain(value["fields"])
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def wait_for(condition, message, process=None, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(1)
    raise AssertionError(message)


def run(args):
    evidence = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "network": args.network,
                "transactions": [], "checks": []}
    wallet = Path(args.wallet).resolve()

    def client(*command, json_output=True, check=True):
        full = ["sui", "client", "--client.config", str(wallet), *map(str, command)]
        if json_output:
            full.append("--json")
        result = subprocess.run(full, text=True, capture_output=True, timeout=600)
        if result.returncode and check:
            raise RuntimeError(f"sui client {command[0]} failed:\n{result.stderr[-3000:]}\n{result.stdout[-3000:]}")
        if not json_output or result.returncode:
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
    chain = dm.chain_facts(rpc_url)["chain_identifier"]
    expected = CHAINS.get(args.network)
    if args.network == "localnet":
        assert any(h in rpc_url for h in ("127.0.0.1", "localhost")), "localnet profile needs a loopback node"
    else:
        assert chain == expected, f"active env {active_env} is chain {chain}, not {args.network} ({expected})"
    start_balance = balance()
    assert start_balance >= args.max_spend_mist, (
        f"{operator} holds {start_balance} MIST; this run may spend up to {args.max_spend_mist}. Fund it first "
        f"(docs/TESTNET.md)")
    sui_version = subprocess.run(["sui", "--version"], capture_output=True, text=True).stdout.strip()
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    base = args.base_ref or subprocess.check_output(
        ["git", "-C", str(ROOT), "merge-base", "HEAD", "origin/main"], text=True).strip()
    base = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", base], text=True).strip()
    evidence.update(chain=chain, rpc=rpc_url, operator=operator, sui=sui_version, head=head, base=base,
                    start_balance_mist=start_balance)
    print(f"{args.network} chain {chain}, operator {operator}, balance {start_balance / 1e9:.3f} SUI; "
          f"base {base[:12]} -> head {head[:12]}")

    def transaction(label, *ptb, budget=200_000_000):
        if start_balance - balance() > args.max_spend_mist:
            raise Spent(f"spend ceiling {args.max_spend_mist} MIST reached before {label}")
        result = client("ptb", *ptb, "--gas-budget", str(budget))
        status = result["effects"]["status"]["status"]
        evidence["transactions"].append({"label": label, "digest": result["digest"], "status": status})
        assert status == "success", (label, result["effects"]["status"])
        return result

    def created(result, suffix):
        return [c["objectId"] for c in result["objectChanges"] if c["type"] == "created" and c["objectType"].endswith(suffix)]

    def published(result):
        return next(c["packageId"] for c in result["objectChanges"] if c["type"] == "published")

    def check(label):
        evidence["checks"].append(label)
        print("PASS:", label)

    blobs = {}

    class Walrus(BaseHTTPRequestHandler):
        def log_message(self, *a):
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
        with tempfile.TemporaryDirectory(prefix="suifleet-live-upgrade-") as temporary:
            root = Path(temporary)
            package_dir, pubfile = root / "package", root / "Pub.live.toml"
            staging = root / "base"; staging.mkdir()
            archive = subprocess.run(["git", "-C", str(ROOT), "archive", base, "contracts"], check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", str(staging)], input=archive, check=True)
            shutil.copytree(staging / "contracts", package_dir, ignore=shutil.ignore_patterns("build", "Published.toml"))
            common = ["--build-env", "testnet", "--pubfile-path", pubfile, "--gas-budget", "400000000", "--silence-warnings"]

            # 1. Base package and live state.
            first = client("test-publish", package_dir, *common)
            assert first["effects"]["status"]["status"] == "success", first["effects"]["status"]
            evidence["transactions"].append({"label": "publish base", "digest": first["digest"], "status": "success"})
            old = published(first)
            registry = created(first, "::device_registry::DeviceRegistry")[0]
            upgrade_cap = created(first, "::package::UpgradeCap")[0]
            key = root / "device.key"
            device_address = subprocess.check_output([str(DAEMON), "--provision", str(key)], text=True).strip()
            registered = transaction("register device + issue cap (base)",
                "--move-call", f"{old}::device_registry::register_device", f"@{registry}", '"live-pi"',
                f"@{device_address}", '"aarch64"', CLOCK, "--assign", "device",
                "--move-call", f"{old}::device_cap::issue_cap", "device",
                "--transfer-objects", "[device]", f"@{operator}",
                "--split-coins", "gas", f"[{DEVICE_FUNDING}]", "--assign", "fee",
                "--transfer-objects", "[fee]", f"@{device_address}")
            device = created(registered, "::device_registry::Device")[0]
            cap = created(registered, "::device_cap::DeviceCap")[0]
            group = created(transaction("create group (base)", "--move-call", f"{old}::device_group::create_group",
                                        '"live"', '"package-upgrade gate"', CLOCK), "::device_group::DeviceGroup")[0]
            transaction("add device to group (base)", "--move-call", f"{old}::device_group::add_device", f"@{group}", f"@{device}")
            check(f"base package {old} published; device, cap and group created with it")

            # 2. Upgrade in place.
            shutil.rmtree(package_dir)
            shutil.copytree(ROOT / "contracts", package_dir, ignore=shutil.ignore_patterns("build", "Published.toml"))
            upgraded = client("test-upgrade", package_dir, "--upgrade-capability", upgrade_cap, *common)
            assert upgraded["effects"]["status"]["status"] == "success", upgraded["effects"]["status"]
            evidence["transactions"].append({"label": "upgrade to head", "digest": upgraded["digest"], "status": "success"})
            new = published(upgraded)

            # 3. Manifest over gRPC.
            manifest = dm.resolve(SimpleNamespace(chain_identifier=chain, rpc=rpc_url, package=new, upgrade_cap=upgrade_cap,
                                                  registry=registry, network=args.network if args.network != "localnet" else "localnet",
                                                  publisher=aggregator, aggregator=aggregator))
            evidence.update(base_package=old, head_package=new, registry=registry, upgrade_cap=upgrade_cap,
                            type_origins=manifest["type_origins"])
            check(f"upgraded in place to {new} (package version {manifest['package_version']}); manifest verified over gRPC")

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

            def create(package, version, *, groups=(), devices=(), fail="none"):
                artifact = component.build(version, run_id, fail)
                tag = version.replace(".", "-")
                artifact_blob, recipe_blob = f"artifact-{tag}", f"recipe-{tag}"
                recipe = component.recipe(artifact, artifact_blob)
                blobs[artifact_blob], blobs[recipe_blob] = artifact, recipe
                ids = lambda values: "[" + ", ".join(f"@{v}" for v in values) + "]"
                return created(transaction(f"deployment {version} via {'base' if package == old else 'head'}",
                    "--move-call", f"{package}::deployment_manager::create_deployment_verified",
                    f'"{component.DEFAULT_COMPONENT}"', f'"{version}"', f'"{recipe_blob}"',
                    f'"{hashlib.sha256(artifact).hexdigest()}"', f'"{hashlib.sha256(recipe).hexdigest()}"',
                    f"vector{ids(groups)}", f"vector{ids(devices)}", CLOCK), "::deployment_manager::DeploymentRecord")[0]

            def receipts(deployment):
                """Authoritative statuses read with the Sui CLI, independent of the daemon."""
                for field in client("dynamic-field", deployment)["dynamicFields"]:
                    value = client("object", field["fieldId"])
                    content = value.get("content", {})
                    if not str(value.get("objType", "")).count("ReceiptsKey"):
                        continue
                    statuses = plain(content)["value"]["statuses"]["contents"]
                    return {dm.object_id(e["key"]): int(e["value"]) for e in statuses}
                return {}

            def settled(deployment, status):
                wait_for(lambda: receipts(deployment) == {dm.object_id(device): status},
                         f"receipt for {deployment} never became {status}", process)

            def history():
                path = components / component.DEFAULT_COMPONENT / "history.jsonl"
                return [json.loads(line)["version"] for line in path.read_text().splitlines()] if path.exists() else []

            direct = create(new, "1.0.0", devices=[device])
            settled(direct, 1)
            check("head-package deployment to the device: applied, receipt Applied on chain")
            via_group = create(new, "2.0.0", groups=[group])
            settled(via_group, 1)
            check("head-package deployment to the pre-upgrade group: applied via report_status_for_group")
            via_old = create(old, "3.0.0", devices=[device])
            settled(via_old, 1)
            check("deployment created through the OLD package's entry point: applied and receipted (mixed versions)")
            failed = create(new, "4.0.0", devices=[device], fail="health")
            settled(failed, 2)
            check("injected health failure reported Failed on chain")
            assert history() == ["1.0.0", "2.0.0", "3.0.0", "4.0.0"], history()
            evidence["receipts"] = {d: receipts(d) for d in (direct, via_group, via_old, failed)}
            process.send_signal(signal.SIGINT)
            process.wait(timeout=60)
            process = None

            # 6. Cleanup: storage rebate.
            for deployment in (direct, via_group, via_old, failed):
                transaction("delete deployment", "--move-call", f"{new}::deployment_manager::delete_deployment", f"@{deployment}")
            check("deployments deleted (storage rebate)")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        walrus.shutdown()
        end = balance()
        evidence.update(end_balance_mist=end, spent_mist=start_balance - end,
                        finished=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if args.evidence:
            Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"spent {(start_balance - end) / 1e9:.4f} SUI; {len(evidence['transactions'])} transactions; "
              f"evidence: {args.evidence or '(not written)'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallet", required=True, help="client.yaml of the funded wallet to spend from")
    parser.add_argument("--network", default="testnet", choices=["testnet", "localnet"])
    parser.add_argument("--base-ref", help="contracts to publish first (default: merge base with origin/main)")
    parser.add_argument("--max-spend-mist", type=int, default=600_000_000)
    parser.add_argument("--evidence", help="write evidence JSON here")
    try:
        run(parser.parse_args())
    except Spent as error:
        sys.exit(f"stopped: {error}")
