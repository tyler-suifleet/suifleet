#!/usr/bin/env python3
"""SF-018 compatible upgrade and existing-record controls on disposable local Sui.
Run in a network-none dev container with an indexed node/faucet. No user wallet.
"""
import argparse
import json
import hashlib
import os
import sys
from types import SimpleNamespace
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from test_desired_state_localnet import base_contracts, wait_for
import deployment_manifest as dm
from test_ota import ARTIFACT, Simulator
from test_desired_state import serve

ROOT = Path(__file__).resolve().parents[1]

def run(rpc, faucet):
    for url in (rpc, faucet):
        if urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("disposable loopback network only")
    with tempfile.TemporaryDirectory(prefix="suifleet-control-chain-") as tmp:
        root = Path(tmp); wallet = root / "wallet"; wallet.mkdir()
        (wallet / "sui.keystore").write_text("[]")
        (wallet / "client.yaml").write_text(f"keystore:\n  File: {wallet / 'sui.keystore'}\nenvs:\n- alias: control-test\n  rpc: {rpc}\nactive_env: control-test\nactive_address: null\n")
        def client(*args):
            result = subprocess.run(["sui", "client", "--client.config", str(wallet / "client.yaml"), *map(str,args), "--json"], text=True, capture_output=True, timeout=300)
            # Never print wallet initialization output.
            if result.returncode: raise RuntimeError(f"local {args[0]} failed: {result.stderr[-1500:]}")
            output = result.stdout; return json.loads(output[min(i for i in (output.find("{"),output.find("[")) if i>=0):])
        operator = client("new-address", "ed25519")["address"]
        client("switch", "--address", operator)
        request = Request(faucet + "/v2/gas", json.dumps({"FixedAmountRequest":{"recipient":operator}}).encode(), {"Content-Type":"application/json"})
        with urlopen(request, timeout=30) as response:
            assert 200 <= response.status < 300, "local faucet request rejected"
        wait_for(lambda: bool(client("gas")), "faucet empty")
        def created(result,suffix):
            return next(c["objectId"] for c in result["objectChanges"] if c["type"]=="created" and c["objectType"].endswith(suffix))
        def published(result): return next(c["packageId"] for c in result["objectChanges"] if c["type"]=="published")
        digests = []
        def success(result):
            assert result["effects"]["status"]["status"] == "success", result["effects"]["status"]
            digests.append(result["digest"]); return result
        def tx(*args): return success(client("ptb", *args, "--gas-budget", "100000000"))
        staging = root / "base"; staging.mkdir(); base = base_contracts(staging)
        package = root / "package"; shutil.copytree(staging / "contracts",package,ignore=shutil.ignore_patterns("build","Published.toml"))
        common = ["--build-env","testnet","--pubfile-path",root / "Pub.toml","--gas-budget","400000000","--silence-warnings"]
        first = success(client("test-publish",package,*common)); old=published(first); cap=created(first,"::package::UpgradeCap")
        registry = created(first, "::device_registry::DeviceRegistry")
        key = root / "device.key"; key.write_text("07" * 32); key.chmod(0o600)
        daemon = ROOT / "suifleet-edge/target/debug/suifleet"
        address = subprocess.check_output([str(daemon), "--derive-address", str(key)], text=True).strip()
        request = Request(faucet + "/v2/gas", json.dumps({"FixedAmountRequest":{"recipient":address}}).encode(), {"Content-Type":"application/json"})
        with urlopen(request, timeout=30) as response: assert 200 <= response.status < 300
        registered = tx("--move-call", f"{old}::device_registry::register_device", f"@{registry}", '\"device\"', f"@{address}", '\"x86_64\"', "@0x6", "--assign", "device",
            "--move-call", f"{old}::device_cap::issue_cap", "device",
            "--move-call", f"{old}::device_registry::activate_device", f"@{registry}", "device", "--assign", "owner",
            "--transfer-objects", "[owner]", f"@{operator}")
        device, device_cap = created(registered,"::device_registry::Device"), created(registered,"::device_cap::DeviceCap")
        sim = Simulator(); sim.add("0x999")
        recipe = sim.recipes.pop("recipe-0x999"); recipe["componentName"] = "app"
        sim.recipes["recipe-control"] = recipe
        digest = json.dumps(hashlib.sha256(ARTIFACT).hexdigest())
        recipe_digest = json.dumps(hashlib.sha256(json.dumps(recipe).encode()).hexdigest())
        record = created(tx("--move-call",f"{old}::deployment_manager::create_deployment_verified",'"app"','"1.0.0"','"recipe-control"',digest,recipe_digest,"vector[]",f"vector[@{device}]","@0x6"),"::deployment_manager::DeploymentRecord")
        shutil.rmtree(package); shutil.copytree(ROOT / "contracts",package,ignore=shutil.ignore_patterns("build","Published.toml"))
        upgraded=success(client("test-upgrade",package,"--upgrade-capability",cap,*common)); new=published(upgraded)
        facts=dm.chain_facts(rpc,new,cap); origins=dm.origins(facts["objects"][dm.object_id(new)])
        assert origins["deployment_manager::ControlKey"] == dm.object_id(new)
        assert origins["deployment_manager::DeploymentRecord"] == dm.object_id(old)
        def read():
            result=dm.rpc(rpc,"suix_getDynamicFieldObject",[record,{"type":f"{new}::deployment_manager::ControlKey","value":{"dummy_field":False}}])
            return result["data"]["content"]["fields"]["value"]["fields"]
        def set_control(state):
            return tx("--move-call",f"{new}::deployment_manager::set_control",f"@{record}",str(state),"0","9999999999999","1000","9999999999998","5000")
        set_control(1); assert int(read()["state"]) == 1
        # The actual daemon must parse the new-origin control over gRPC, retain
        # the one-shot event while paused, then apply/report after resume.
        server = serve(sim); aggregator = f"http://127.0.0.1:{server.server_port}"
        manifest = dm.resolve(SimpleNamespace(chain_identifier=facts["chain_identifier"], rpc=rpc, package=new,
            upgrade_cap=cap, registry=registry, network="localnet", publisher=aggregator, aggregator=aggregator))
        (root / "deployment.json").write_text(json.dumps(manifest))
        updater = root / "apply.py"; applied = root / "applied"
        updater.write_text(f"from pathlib import Path\nPath({str(applied)!r}).touch()\n")
        config = root / "config.toml"
        config.write_text('[sui]\nmanifest_path="deployment.json"\n'
            f'[device]\nkeypair_path={json.dumps(str(key))}\ndevice_object_id="{device}"\ndevice_cap_id="{device_cap}"\n'
            f'[apply]\ncommand={json.dumps(f"{sys.executable} {updater}")}\ntimeout_seconds=30\n'
            f'[runtime]\nstate_dir={json.dumps(str(root / "state"))}\npoll_seconds=1\nevent_scan_seconds=0\nreplay_history=true\n')
        def state():
            path = root / "state/state.json"
            return json.loads(path.read_text()) if path.exists() else {}
        with (root / "daemon.log").open("w") as log:
            process = subprocess.Popen([str(daemon)], env={**os.environ,"EDGE_CONFIG":str(config)}, stdout=log, stderr=log)
            try:
                wait_for(lambda: state().get("deferred",{}).get(record,{}).get("reason") == "paused", "real-chain pause not observed", process)
                assert not applied.exists()
                set_control(0); assert int(read()["state"]) == 0 and int(read()["revision"]) == 2
                wait_for(lambda: state().get("deployments",{}).get(record,{}).get("reported"), "real-chain resumed apply/report did not finish", process)
                assert applied.exists() and state()["deployments"][record]["status"] == 1
                def receipt_state():
                    receipt = dm.rpc(rpc, "suix_getDynamicFieldObject", [record, {"type":f"{origins['deployment_manager::ReceiptsKey']}::deployment_manager::ReceiptsKey", "value":{"dummy_field":False}}])
                    fields = receipt["data"]["content"]["fields"]["value"]["fields"]
                    values = fields["statuses"]["fields"]["contents"]
                    statuses = {dm.object_id(v["fields"]["key"]):int(v["fields"]["value"]) for v in values}
                    return receipt["data"]["objectId"], int(fields["version"]), statuses
                wait_for(lambda: receipt_state()[2] == {dm.object_id(device):1}, "missing authoritative on-chain Applied receipt", process)
                receipt_id, authority, statuses = receipt_state()
                assert authority >= 2
                reported = dm.rpc(rpc, "sui_getObject", [receipt_id, {"showPreviousTransaction":True}])["data"]["previousTransaction"]
                effects = dm.rpc(rpc, "sui_getTransactionBlock", [reported, {"showEffects":True}])["effects"]
                assert effects["status"]["status"] == "success", effects["status"]
                digests.append(reported)
            finally:
                process.terminate(); process.wait(timeout=10); server.shutdown()

        # An old entry point may still report statuses; it cannot alter new-origin controls.
        tx("--move-call",f"{old}::deployment_manager::migrate",f"@{record}")
        assert int(read()["revision"]) == 2
        set_control(2); assert int(read()["state"]) == 2
        tx("--move-call",f"{new}::deployment_manager::delete_deployment",f"@{record}")
        print(json.dumps({"result":"PASS", "base":base,"old_package":old,"new_package":new,"record":record,"digests":digests,
            "checks":["compatible upgrade","existing record pause/resume/cancel","actual gRPC daemon pause and resumed apply/report","per-type origins","old package preserves controls","controlled record cleanup"]},indent=2))

if __name__ == "__main__":
    p=argparse.ArgumentParser();p.add_argument("--rpc",default="http://127.0.0.1:9000");p.add_argument("--faucet",default="http://127.0.0.1:9123");a=p.parse_args();run(a.rpc,a.faucet)
