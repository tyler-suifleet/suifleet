#!/usr/bin/env python3
"""Real fresh publish plus two upgrades, on an explicitly isolated loopback network.
Run inside the pinned Sui dev container with a disposable local Sui node/faucet.
Never reads the normal wallet or connects to a public chain.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlparse
import deployment_manifest as dm


def run(rpc_url, faucet_url):
    for url in (rpc_url, faucet_url):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("acceptance test requires an isolated loopback node/faucet")
    chain_id = dm.rpc(rpc_url, "sui_getChainIdentifier", [])
    with tempfile.TemporaryDirectory(prefix="suifleet-manifest-upgrades-") as tmp:
        root = Path(tmp); wallet = root / "wallet"; wallet.mkdir()
        config = wallet / "client.yaml"
        def client(*args, json_output=False):
            command = ["sui", "client", "--client.config", str(config), *map(str,args)]
            if json_output: command.append("--json")
            result = subprocess.run(command, text=True, capture_output=True, timeout=180)
            # Wallet initialization may print a recovery phrase. Never forward it.
            if result.returncode:
                raise RuntimeError("local Sui command failed: " + " ".join(command[:3]+list(map(str,args[:1]))) + "\n" + result.stderr[-4000:] + (result.stdout[-4000:] if args[0] in ("publish", "upgrade") else ""))
            return json.loads(result.stdout) if json_output else result.stdout
        # -y creates only this disposable wallet. Existing local alias is built in.
        client("-y", "active-address")
        # Give our endpoint a unique alias regardless of default CLI configuration.
        client("new-env", "--alias", "manifest-test", "--rpc", rpc_url)
        client("switch", "--env", "manifest-test")
        client("faucet", "--url", faucet_url)
        for _ in range(30):
            if client("gas", json_output=True): break
            time.sleep(1)
        else: raise RuntimeError("local faucet did not supply gas")

        def write_package(path, stage):
            (path / "sources").mkdir(parents=True, exist_ok=True)
            if not (path / "Move.toml").exists():
                (path / "Move.toml").write_text('[package]\nname="manifest_fixture"\nedition="2024"\n[environments]\nmanifest-test="' + chain_id + '"\n')
            modules = {}
            for name in dm.REQUIRED_TYPES:
                if stage < 2 and name.endswith("RecipeDigestKey"): continue
                if stage < 3 and name.endswith("DeviceAddressKey"): continue
                module, datatype = name.split("::")
                modules.setdefault(module, []).append(datatype)
            for module, types in modules.items():
                body = "module manifest_fixture::" + module + " {\n"
                for datatype in types:
                    if datatype == "DeviceRegistry":
                        body += "public struct DeviceRegistry has key { id: UID }\n"
                        body += "fun init(ctx: &mut TxContext) { sui::transfer::share_object(DeviceRegistry { id: sui::object::new(ctx) }); }\n"
                    else: body += "public struct " + datatype + " has copy, drop, store {}\n"
                if stage >= 4 and module == "deployment_manager":
                    body += "public struct FutureKey has copy, drop, store {}\n"
                (path / "sources" / (module + ".move")).write_text(body + "}\n")
        def transaction(*args):
            result = client(*args, "--gas-budget", "100000000", json_output=True)
            if result["effects"]["status"]["status"] != "success":
                raise AssertionError(result["effects"]["status"])
            return result
        def published(result):
            return next(change["packageId"] for change in result["objectChanges"] if change["type"] == "published")
        def created(result, suffix):
            return next(change["objectId"] for change in result["objectChanges"] if change["type"] == "created" and change["objectType"].endswith(suffix))
        def resolve(package, registry, cap):
            return dm.resolve(SimpleNamespace(chain_identifier=chain_id, rpc=rpc_url, package=package, upgrade_cap=cap, registry=registry, network="localnet",
                publisher="http://localhost:31415", aggregator="http://localhost:31416"))

        fresh = root / "fresh"; write_package(fresh, 3)
        fresh_tx = transaction("publish", fresh)
        fresh_id = published(fresh_tx)
        fresh_manifest = resolve(fresh_id, created(fresh_tx, "::DeviceRegistry"), created(fresh_tx, "::UpgradeCap"))
        assert set(fresh_manifest["type_origins"].values()) == {dm.object_id(fresh_id)}
        print("PASS: fresh publish; manifest resolved from actual package and shared registry")

        package = root / "upgrade"; write_package(package, 1)
        first = transaction("publish", package)
        first_id = published(first)
        registry = created(first, "::DeviceRegistry")
        cap = created(first, "::UpgradeCap")
        write_package(package, 2)
        second = transaction("upgrade", package, "--upgrade-capability", cap)
        second_id = published(second)
        write_package(package, 3)
        third = transaction("upgrade", package, "--upgrade-capability", cap)
        third_id = published(third)
        manifest = resolve(third_id, registry, cap)
        for name, origin in manifest["type_origins"].items():
            expected = second_id if name.endswith("RecipeDigestKey") else third_id if name.endswith("DeviceAddressKey") else first_id
            assert origin == dm.object_id(expected), (name, origin, expected)
        assert manifest["call_package_id"] == dm.object_id(third_id)
        assert manifest["package_version"] == "3"
        assert manifest["shared_objects"]["registry"]["id"] == dm.object_id(registry)
        print("PASS: two executed upgrades retain old origins and separately introduce RecipeDigestKey/DeviceAddressKey")
        template = root / "device-template.toml"
        key = root / "device.key"; key.write_text("07" * 32); key.chmod(0o600)
        template.write_text('[device]\nkeypair_path=' + json.dumps(str(key)) + '\ndevice_object_id="0xde"\ndevice_cap_id="0xca"\n[apply]\ncommand="/bin/true"\n')
        bundle = root / "release"
        dm.generate(manifest, bundle, template)
        daemon = Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet"
        subprocess.run([str(daemon), "--check-config", str(bundle / "config.toml")], check=True, timeout=60)
        subprocess.run(["node", str(Path(__file__).with_name("test_deployment_manifest.cjs")), str(bundle / "deployment.json")], check=True, timeout=60)
        print("PASS: generated device config and frontend SDK independently verify real upgrade metadata")
        # Every required type now exists in v3. A further additive upgrade makes
        # that complete, still-readable manifest stale without changing its types.
        write_package(package, 4)
        fourth = transaction("upgrade", package, "--upgrade-capability", cap)
        try:
            dm.verify(manifest)
        except ValueError as error:
            assert "UpgradeCap" in str(error)
        else:
            raise AssertionError("resolver accepted a stale complete package")
        for command in ([str(daemon), "--check-config", str(bundle / "config.toml")],
                        ["node", str(Path(__file__).with_name("test_deployment_manifest.cjs")), str(bundle / "deployment.json")]):
            rejected = subprocess.run(command, text=True, capture_output=True, timeout=60)
            assert rejected.returncode != 0 and "UpgradeCap" in rejected.stderr, rejected.stderr
        newest = resolve(published(fourth), registry, cap)
        assert newest["type_origins"] == manifest["type_origins"]
        assert newest["package_version"] == "4"
        print("PASS: all three real validators reject a stale callable v3 package after an additive v4 upgrade")
        print(json.dumps({"chain_identifier":chain_id,"packages":[first_id,second_id,third_id],"registry":registry}))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default="http://127.0.0.1:9000")
    parser.add_argument("--faucet", default="http://127.0.0.1:9123/v2/gas")
    args = parser.parse_args()
    run(args.rpc, args.faucet)
