#!/usr/bin/env python3
"""Live confidential delivery on Sui testnet (SF-022) with Mysten's Seal key servers.

Reuses the package-upgrade gate's cached fixture for this base/head pair
(run `make live-gate-package-upgrade` first): the upgraded package with
seal_policy, the activated device and its key. Then, spending from the same
dedicated wallet under its own non-replenishing allowance:

  1. creates (once, cached with the fixture) an access policy and grants the
     device;
  2. encrypts a test artifact and recipe with the dapp's own module and the
     real @mysten/seal SDK for Mysten's open testnet key servers (2 of 2),
     verified on chain and by proof of possession;
  3. creates a verified deployment through the head package;
  4. runs the real daemon with `[seal] mode = "required"`: it certifies a
     Seal session with the device's own key, the key servers evaluate
     seal_approve_device on testnet, it decrypts, verifies and applies, and
     reports; the receipt and its transaction effects are read back
     independently;
  5. checks that an ungranted account is refused by the same key servers;
  6. deletes the deployment (storage rebate) and writes evidence JSON.

Walrus is a local stand-in serving the ciphertext; the key servers, chain
state and approval evaluation are public testnet.

Usage: live_seal_delivery.py --wallet CLIENT_YAML [--max-spend-mist N] [--evidence FILE]
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
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
import live_package_upgrade as gate
import sui_toolchain as toolchain
import test_component as component
from testnet_budget import Budget

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
CLOCK = "@0x6"
# Mysten's open-mode testnet key servers (docs.sui.io/sui-stack/seal/pricing).
KEY_SERVERS = {
    "0x73d05d62c18d9374e3ea529e8e0ed6161da1a141a94d3f76ae3fe4e99356db75": "mysten-testnet-1",
    "0xf5d14a81a982144ae441cd7d64b09027f116a468bd36e7eca494f750591623c8": "mysten-testnet-2",
}


def seal_tool(request):
    result = subprocess.run(["node", str(ROOT / "scripts/seal_client_tool.cjs")], input=json.dumps(request),
                            text=True, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"seal_client_tool failed:\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def run(args):
    os.umask(0o077)
    evidence = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "network": "testnet",
                "transactions": [], "checks": []}
    ledger = Budget(args.max_spend_mist, evidence["transactions"])
    wallet = Path(args.wallet).resolve()
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

    envs, active_env = client("envs")
    rpc_url = next(e["rpc"] for e in envs if e["alias"] == active_env)
    operator = dm.object_id(client("active-address", json_output=False).stdout.strip().split()[-1])
    facts = dm.chain_facts(rpc_url)
    chain = facts["chain_identifier"]
    assert chain == gate.CHAINS["testnet"], f"active env {active_env} is chain {chain}, not testnet"
    compatibility = toolchain.preflight(tools, facts, gate.CHAINS["testnet"])
    assert operator in {
        "0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965",
        "0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac",
    }, "wallet is outside dedicated testnet grant"
    start_balance = gate.balance_of(client, operator)
    assert start_balance >= args.max_spend_mist, f"{operator} holds {start_balance} MIST; this run may spend {args.max_spend_mist}"
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    base = args.base_ref or subprocess.check_output(["git", "-C", str(ROOT), "merge-base", "HEAD", "origin/main"], text=True).strip()
    base = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", base], text=True).strip()
    evidence.update(chain=chain, rpc=rpc_url, operator=operator, head=head, base=base, start_balance_mist=start_balance,
                    toolchain=dict(tools, compatibility=compatibility))

    def mutate(label, *ptb, budget=50_000_000, funding=0):
        if os.environ.get("SUIFLEET_TESTNET_LOCK_TOKEN"):
            subprocess.run(["ticket", "--project", "suifleet", "lock-assert", "network:suifleet-testnet",
                            "--lock-token", os.environ["SUIFLEET_TESTNET_LOCK_TOKEN"]], check=True, stdout=subprocess.DEVNULL)
        return ledger.execute(client, label, ["ptb", *ptb], gas=budget, funding=funding)

    def created(result, suffix):
        return [c["objectId"] for c in result["objectChanges"] if c["type"] == "created" and c["objectType"].endswith(suffix)]

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
    process, cleanup, new = None, [], None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-live-seal-") as temporary:
            root = Path(temporary)
            staging = root / "base"
            staging.mkdir()
            archive = subprocess.run(["git", "-C", str(ROOT), "archive", base, "contracts"], check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", str(staging)], input=archive, check=True)
            key_name = gate.fixture_key(gate.contracts_digest(staging / "contracts"), gate.contracts_digest(ROOT / "contracts"))
            state_path = Path(args.state) if args.state else wallet.parent / "live-state.json"
            state = gate.load_state(state_path, chain, operator)
            fixture = state["fixtures"].get(key_name)
            assert fixture and gate.fixture_alive(fixture, operator, client), (
                f"no intact package-upgrade fixture {key_name}: run make live-gate-package-upgrade for this head first")
            new, registry, upgrade_cap = fixture["head_package"], fixture["registry"], fixture["upgrade_cap"]
            device, cap, device_address = fixture["device"], fixture["device_cap"], fixture["device_address"]
            key = root / "device.key"
            key.write_text(fixture["device_key"] + "\n")
            derived = subprocess.check_output([str(DAEMON), "--derive-address", str(key)], text=True).strip()
            assert dm.object_id(derived) == dm.object_id(device_address), "cached device key does not match"
            manifest = dm.resolve(SimpleNamespace(chain_identifier=chain, rpc=rpc_url, package=new, upgrade_cap=upgrade_cap,
                                                  registry=registry, network="testnet", publisher=aggregator, aggregator=aggregator))
            assert all(n in manifest["type_origins"] for n in dm.SEAL_POLICY_TYPES), "head package lacks seal_policy"
            namespace = manifest["type_origins"]["device_registry::DeviceRegistry"]
            evidence.update(fixture=key_name, head_package=new, namespace=namespace, registry=registry, device=device)
            check(f"reused fixture {key_name[:12]}…: head {new[:12]}… includes seal_policy (origin "
                  f"{manifest['type_origins']['seal_policy::AccessPolicy'][:12]}…); Seal namespace is the original package {namespace[:12]}…")

            # The trusted Seal package is whatever defines Mysten's key server objects.
            packages = set()
            for object_id in KEY_SERVERS:
                object_type = client("object", object_id)["objType"]
                assert object_type.endswith("::key_server::KeyServer"), object_type
                packages.add(dm.object_id(object_type.split("::")[0]))
            assert len(packages) == 1, packages
            seal_package = packages.pop()
            evidence.update(seal_package=seal_package, key_servers=KEY_SERVERS)

            device_gas = gate.balance_of(client, device_address)
            ledger.grant(device_gas)
            if device_gas < gate.DEVICE_MINIMUM:
                mutate("top up device gas", "--split-coins", "gas", f"[{gate.DEVICE_FUNDING}]", "--assign", "fee",
                       "--transfer-objects", "[fee]", f"@{device_address}", budget=10_000_000, funding=gate.DEVICE_FUNDING)

            seal = fixture.get("seal")
            if seal and not (gate.owner_of(client, seal["policy"]) == "shared"
                             and dm.object_id(gate.owner_of(client, seal["cap"]) or "0x0") == operator):
                seal = None
            if not seal:
                made = mutate("create access policy", "--move-call", f"{new}::seal_policy::create_policy", '"live releases"',
                              "--assign", "cap", "--transfer-objects", "[cap]", f"@{operator}")
                seal = {"policy": created(made, "::seal_policy::AccessPolicy")[0], "cap": created(made, "::seal_policy::AccessAdminCap")[0]}
                mutate("grant the device", "--move-call", f"{new}::seal_policy::grant_device", f"@{seal['policy']}",
                       f"@{seal['cap']}", f"@{registry}", f"@{device}")
                fixture["seal"] = seal
                gate.save_state(state_path, state)
                check(f"access policy {seal['policy'][:12]}… created and the device granted (cached with the fixture)")
            else:
                check(f"reused access policy {seal['policy'][:12]}… (re-verified on chain)")
            policy = client("object", seal["policy"])
            fields = gate.plain(policy["content"])
            fields = fields.get("fields", fields)
            assert dm.object_id(device) in [dm.object_id(d) for d in fields["devices"]], fields
            key_epoch = str(fields["key_epoch"])
            evidence.update(policy=seal["policy"], key_epoch=key_epoch)

            servers = [{"objectId": object_id, "weight": 1} for object_id in KEY_SERVERS]
            run_id = "live-seal-" + uuid.uuid4().hex[:10]
            version = "9." + str(int(time.time()) % 100000) + ".0"
            artifact = component.build(version, run_id)
            artifact_blob, recipe_blob = f"artifact-{run_id}", f"recipe-{run_id}"
            recipe = component.recipe(artifact, artifact_blob)
            sealed = seal_tool({"op": "encrypt", "manifest": manifest, "servers": servers, "threshold": 2,
                                "policyId": seal["policy"], "keyEpoch": key_epoch,
                                "items": [{"kind": "artifact", "hex": artifact.hex()}, {"kind": "recipe", "hex": recipe.hex()}]})
            blobs[artifact_blob], blobs[recipe_blob] = (bytes.fromhex(e) for e in sealed["envelopes"])
            assert artifact[:64] not in blobs[artifact_blob] and b"componentName" not in blobs[recipe_blob]
            evidence["identity"] = sealed["identity"]
            check("artifact and recipe encrypted by the dapp module for Mysten's testnet key servers (2 of 2, PoP verified)")

            components = root / "state" / "test-components"
            template = root / "device-template.toml"
            servers_toml = ", ".join(f'{{ object_id = "{object_id}" }}' for object_id in KEY_SERVERS)
            template.write_text(
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{device}"\ndevice_cap_id = "{cap}"\n'
                f'[apply]\ncommand = {json.dumps(f"/bin/sh {component.UPDATER} --root {components}")}\ntimeout_seconds = 60\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 3\n'
                f'[seal]\nmode = "required"\nkey_server_package = "{seal_package}"\nkey_servers = [{servers_toml}]\n'
                f'min_threshold = 2\nrequest_timeout_seconds = 20\n')
            dm.generate(manifest, root / "release", template)
            config = root / "release/config.toml"
            log = (root / "daemon.log").open("w")
            journal = root / "state" / "state.json"
            process = subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)
            # Deployments created before enrollment are never applied: start first.
            gate.wait_for(lambda: journal.exists() and json.loads(journal.read_text()).get("initialized"), "daemon did not enroll", process)
            made = mutate("create confidential deployment", "--move-call", f"{new}::deployment_manager::create_deployment_verified",
                          f'"{component.DEFAULT_COMPONENT}"', f'"{version}"', f'"{recipe_blob}"',
                          f'"{hashlib.sha256(artifact).hexdigest()}"', f'"{hashlib.sha256(recipe).hexdigest()}"',
                          "vector[]", f"vector[@{device}]", CLOCK)
            deployment = created(made, "::deployment_manager::DeploymentRecord")[0]
            cleanup.append(deployment)


            def receipts():
                for field in client("dynamic-field", deployment)["dynamicFields"]:
                    value = client("object", field["fieldId"])
                    if "ReceiptsKey" not in str(value.get("objType", "")):
                        continue
                    receipt = gate.plain(value.get("content", {}))["value"]
                    return {dm.object_id(e["key"]): int(e["value"]) for e in receipt["statuses"]["contents"]} if int(receipt["version"]) == 2 else {}
                return {}
            gate.wait_for(lambda: receipts() == {dm.object_id(device): 1}, f"confidential deployment {deployment} was not Applied", process)
            history = (components / component.DEFAULT_COMPONENT / "history.jsonl").read_text()
            assert version in history and run_id in history
            evidence["receipts"] = {deployment: receipts()}
            proof = subprocess.run(["node", str(ROOT / "scripts/testnet_receipt_effects.cjs"), str(root / "release/deployment.json"), deployment],
                                   capture_output=True, text=True, timeout=120, check=True)
            evidence["report_transactions"] = json.loads(proof.stdout)
            check("daemon certified a session with the device key; Mysten's key servers approved seal_approve_device on testnet; "
                  "decrypted, verified, applied and reported Applied (receipt and effects read independently)")
            process.send_signal(signal.SIGINT)
            process.wait(timeout=60)
            process = None
            daemon_log = (root / "daemon.log").read_text()
            assert fixture["device_key"] not in daemon_log and "componentName" not in daemon_log

            stranger = os.urandom(32).hex()
            refused = seal_tool({"op": "decrypt", "manifest": manifest, "servers": servers, "threshold": 2, "secretKey": stranger,
                                 "policyId": seal["policy"], "envelope": blobs[recipe_blob].hex()})
            assert "error" in refused and "NoAccess" in (refused["error"] + refused.get("name", "")), refused
            evidence["unauthorized_account"] = refused
            check("an ungranted account is refused (NoAccess) by Mysten's testnet key servers")

            mutate("delete confidential deployment", "--move-call", f"{new}::deployment_manager::delete_deployment", f"@{deployment}")
            cleanup.clear()
            check("deployment deleted (storage rebate)")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        if cleanup and new:
            try:
                mutate("bounded abort cleanup", *sum((["--move-call", f"{new}::deployment_manager::delete_deployment", f"@{d}"] for d in cleanup), []))
            except Exception as error:
                evidence["cleanup_error"] = str(error)
        walrus.shutdown()
        try:
            end = gate.balance_of(client, operator)
        except Exception:
            end = None
        evidence.update(success=sys.exc_info()[0] is None, allowance_used_mist=ledger.used, max_spend_mist=ledger.limit,
                        end_balance_mist=end, spent_mist=(start_balance - end if end is not None else None),
                        finished=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if args.evidence:
            Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"allowance used {ledger.used} MIST; {len(evidence['transactions'])} transactions; evidence: {args.evidence or '(not written)'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallet", required=True, help="client.yaml of the dedicated testnet wallet")
    parser.add_argument("--base-ref", help="the package-upgrade fixture's base (default: merge base with origin/main)")
    parser.add_argument("--max-spend-mist", type=int, default=100_000_000)
    parser.add_argument("--evidence", help="write evidence JSON here")
    parser.add_argument("--state", help="fixture cache (default: live-state.json beside the wallet)")
    parser.add_argument("--build-sui", default="sui")
    parser.add_argument("--network-sui", default=os.environ.get("SUIFLEET_NETWORK_SUI", toolchain.NETWORK_DEFAULT))
    run(parser.parse_args())
