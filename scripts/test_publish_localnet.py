#!/usr/bin/env python3
"""SF-006 acceptance on a disposable localnet: the real contracts published and
upgraded by scripts/redeploy.sh (scripts/publish.py) through the real Sui CLI,
with both generated client configs verified by the daemon and the frontend.

Start the node first (as for test-manifest-live), then: make test-publish-localnet
Refuses non-loopback endpoints; uses its own wallet, package copy and records.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlparse
import deployment_manifest as dm

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
FRONTEND = ROOT / "scripts/test_deployment_manifest.cjs"


def run(rpc_url, faucet_url):
    for url in (rpc_url, faucet_url):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("acceptance test requires an isolated loopback node/faucet")
    chain = dm.chain_facts(rpc_url)["chain_identifier"]
    with tempfile.TemporaryDirectory(prefix="suifleet-publish-") as tmp:
        root = Path(tmp)
        config = root / "wallet" / "client.yaml"; config.parent.mkdir()

        def client(*args):
            result = subprocess.run(["sui", "client", "--client.config", str(config), *map(str, args)],
                                    text=True, capture_output=True, timeout=300)
            # Wallet creation prints a recovery phrase; never forward output.
            if result.returncode:
                raise RuntimeError(f"sui client {args[0]} failed:\n{result.stderr[-3000:]}")
            return result.stdout

        client("-y", "active-address")
        client("new-env", "--alias", "sf-accept", "--rpc", rpc_url)
        client("switch", "--env", "sf-accept")
        address = client("active-address").strip()

        def funded():
            gas = json.loads(client("gas", "--json"))
            if isinstance(gas, dict):
                return bool(gas.get("gasCoins")) or int(gas.get("addressMistBalance") or 0) > 0
            return bool(gas)
        for attempt in range(30):
            if funded():
                break
            try:
                client("faucet", "--url", faucet_url)
            except RuntimeError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("local faucet did not supply gas")

        # The real contracts, unpublished on this chain, with its environment declared.
        package = root / "contracts"
        shutil.copytree(ROOT / "contracts", package, ignore=shutil.ignore_patterns("build"))
        with (package / "Move.toml").open("a") as manifest:
            manifest.write(f'sf-accept = "{chain}"\n')
        # Building pins dependencies for the new environment in Move.lock (as
        # testnet already is in the repository); do it before any snapshot.
        subprocess.run(["sui", "move", "build", "--path", str(package), "-e", "sf-accept", "--silence-warnings"],
                       check=True, capture_output=True, timeout=600)
        records, env_file = root / "deployments", root / ".env.local"
        env_file.write_text("NEXT_PUBLIC_WALRUS_EPOCHS=3\nNEXT_PUBLIC_PACKAGE_ID=0xlegacy\n")
        key = root / "device.key"; key.write_text("07" * 32); key.chmod(0o600)
        template = root / "device-template.toml"
        template.write_text(f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "0xde"\n'
                            'device_cap_id = "0xca"\n[apply]\ncommand = "/bin/true"\n')

        def publish(*extra, expect=address):
            command = ["bash", str(ROOT / "scripts/redeploy.sh"), "--expect-address", expect, "--network", "localnet",
                       "--client-config", str(config), "--package", str(package), "--records", str(records),
                       "--frontend-env", str(env_file), "--device-template", str(template),
                       "--publisher", "http://127.0.0.1:31415", "--aggregator", "http://127.0.0.1:31416", *extra]
            return subprocess.run(command, text=True, capture_output=True, timeout=900)

        def state():
            files = {}
            for path in sorted(root.rglob("*")):
                if "wallet" in path.parts or "build" in path.parts:
                    continue
                files[str(path.relative_to(root))] = (str(path.readlink()) if path.is_symlink()
                                                      else path.read_bytes() if path.is_file() else None)
            return files

        def verified_by_both_clients(release):
            subprocess.run([str(DAEMON), "--check-config", str(release / "config.toml")], check=True,
                           capture_output=True, text=True, timeout=120)
            subprocess.run(["node", str(FRONTEND), str(release / "deployment.json")], check=True,
                           capture_output=True, text=True, timeout=120)

        def balance():
            gas = json.loads(client("gas", "--json"))
            coins = gas.get("gasCoins", []) if isinstance(gas, dict) else gas
            return sum(int(c.get("mistBalance", 0)) for c in coins)

        # 1. Wrong account: refused before any transaction.
        before, funds = state(), balance()
        refused = publish(expect="0x" + "ab" * 32)
        assert refused.returncode == 1 and "is not the expected" in refused.stderr, refused.stderr
        assert state() == before and balance() == funds
        print("PASS: wrong account refused before any transaction; nothing written, no gas spent")

        # 2. Dry run of the fresh publish: simulated only.
        dry = publish("--dry-run")
        assert dry.returncode == 0 and "dry run succeeded" in dry.stdout, dry.stderr
        after = state()
        assert balance() == funds
        assert after == before, {n for n in set(before) | set(after) if before.get(n) != after.get(n)}
        print("PASS: dry run simulates the publish and writes nothing")

        # 3. Fresh publish.
        fresh = publish()
        assert fresh.returncode == 0, fresh.stderr
        first = json.loads((records / "sf-accept/current/deployment.json").read_text())
        assert first["chain_identifier"] == chain and first["package_version"] == "1"
        assert set(first["type_origins"].values()) == {first["call_package_id"]}
        section = (package / "Published.toml").read_text()
        assert f'published-at = "{first["call_package_id"]}"' in section, section
        frontend = env_file.read_text()
        assert "NEXT_PUBLIC_WALRUS_EPOCHS=3\n" in frontend and "0xlegacy" not in frontend
        assert json.loads(frontend.split("NEXT_PUBLIC_DEPLOYMENT_MANIFEST='")[1].split("'")[0]) == first
        first_release = (records / "sf-accept/current").resolve()
        verified_by_both_clients(first_release)
        assert not list((records / "sf-accept").glob("pending-*"))
        print(f"PASS: fresh publish {first['call_package_id']} recorded; daemon and frontend verify its configs")

        # 4. Upgrade: this environment's UpgradeCap, origins and registry kept.
        upgraded = publish()
        assert upgraded.returncode == 0, upgraded.stderr
        second = json.loads((records / "sf-accept/current/deployment.json").read_text())
        assert second["package_version"] == "2" and second["call_package_id"] != first["call_package_id"]
        assert second["type_origins"] == first["type_origins"] and second["shared_objects"] == first["shared_objects"]
        assert second["upgrade_cap_id"] == first["upgrade_cap_id"]
        verified_by_both_clients((records / "sf-accept/current").resolve())
        stale = subprocess.run([str(DAEMON), "--check-config", str(first_release / "config.toml")],
                               capture_output=True, text=True, timeout=120)
        assert stale.returncode != 0 and "UpgradeCap" in stale.stderr, stale.stderr
        print(f"PASS: upgrade to {second['call_package_id']} (v2) recorded; the v1 release is kept and now rejected as stale")

        # 5. An incompatible upgrade fails; the previous configuration stays.
        source = package / "sources/device_registry.move"
        original = source.read_text()
        assert "public struct DeviceRegistry has key {" in original
        source.write_text(original.replace("public struct DeviceRegistry has key {",
                                           "public struct DeviceRegistry has key {\n    sf006_layout_break: u64,", 1))
        before, funds = state(), balance()
        failed = publish()
        assert failed.returncode == 1 and "error:" in failed.stderr, failed.stderr
        after = state()
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        assert not changed, changed
        source.write_text(original)
        print("PASS: incompatible upgrade rejected; records, Published.toml and .env.local unchanged"
              + ("" if balance() == funds else " (gas was charged for the aborted transaction)"))

        # 6. A reset network: Published.toml names another chain; refused before any transaction.
        text = (package / "Published.toml").read_text()
        (package / "Published.toml").write_text(text.replace(f'chain-id = "{chain}"', 'chain-id = "0badc0de"'))
        before = state()
        reset = publish()
        assert reset.returncode == 1 and "may have reset" in reset.stderr, reset.stderr
        assert state() == before
        print("PASS: Published.toml for another chain refused before any transaction")
        print(json.dumps({"chain": chain, "v1": first["call_package_id"], "v2": second["call_package_id"],
                          "registry": second["shared_objects"]["registry"]["id"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default="http://127.0.0.1:9000")
    parser.add_argument("--faucet", default="http://127.0.0.1:9123/v2/gas")
    args = parser.parse_args()
    run(args.rpc, args.faucet)
