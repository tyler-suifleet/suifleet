#!/usr/bin/env python3
"""32-bit x86 catalog manifests across a real package upgrade (SF-076).

Run in a disposable dev container with --network none (a pre-warmed Move cache
is needed). It starts its own loopback localnet with a temporary wallet:

  1. publishes the BASE contracts (the previous release, exported by the caller)
     and creates a shared catalog with them;
  2. the base package refuses an `x86` manifest (EManifest), judged from the
     transaction failure, while an x86_64 manifest registers;
  3. upgrades in place to the current contracts with the same UpgradeCap;
  4. the upgraded package registers an `x86` manifest on the existing catalog,
     and the immutable ComponentVersion stores it;
  5. the base package stays callable and still refuses `x86`: an old entry point
     cannot be relied on to accept it, and nothing assumes it is disabled.

    git archive origin/main contracts | tar -x -C /tmp/base   # on the host
    python3 scripts/test_x86_catalog_localnet.py --base-contracts /tmp/base/contracts
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CLOCK = "@0x6"
RPC, FAUCET = "http://127.0.0.1:9000", "http://127.0.0.1:9123/v2/gas"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-contracts", type=Path, required=True)
    args = parser.parse_args()
    if not Path("/.dockerenv").exists() or {p.name for p in Path("/sys/class/net").iterdir()} != {"lo"}:
        raise SystemExit("Use a disposable Docker container with --network none; existing local/public chains are not permitted")
    for port in (9000, 9123):
        with socket.socket() as connection:
            connection.settimeout(0.2)
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"Refusing existing loopback service on port {port}")
    with tempfile.TemporaryDirectory(prefix="sf076-localnet-") as directory:
        root = Path(directory)
        node_dir, wallet = root / "node", root / "wallet"
        node_dir.mkdir()
        wallet.mkdir()
        with open(root / "node.log", "w") as log:
            node = subprocess.Popen(["sui", "start", "--with-faucet", "--force-regenesis"],
                                    env=dict(os.environ, SUI_CONFIG_DIR=str(node_dir)), stdout=log, stderr=subprocess.STDOUT)
            try:
                wait_for_node(node)
                run(root, wallet, args.base_contracts.resolve())
            finally:
                node.terminate()
                try:
                    node.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    node.kill()
                    node.wait()


def wait_for_node(node):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sui_getChainIdentifier", "params": []}).encode()
    for _ in range(240):
        if node.poll() is not None:
            raise RuntimeError("disposable localnet exited; temporary node logs may contain secrets and are not printed")
        try:
            with urlopen(Request(RPC, body, {"Content-Type": "application/json"}), timeout=1) as response:
                if json.load(response).get("result"):
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("localnet startup timeout")


def run(root, wallet, base):
    def client(*arguments, json_output=True, check=True):
        command = ["sui", "client", "--client.config", str(wallet / "client.yaml"), *map(str, arguments)]
        if json_output:
            command.append("--json")
        result = subprocess.run(command, text=True, capture_output=True, timeout=300)
        # Wallet initialization may print a recovery phrase: never forward it.
        if result.returncode and check:
            raise RuntimeError(f"sui client {arguments[0]} failed:\n{result.stderr[-3000:]}\n{result.stdout[-3000:]}")
        if not check:
            return result
        if not json_output:
            return result.stdout
        out = result.stdout
        return json.loads(out[min(i for i in (out.find("{"), out.find("[")) if i >= 0):])

    (wallet / "sui.keystore").write_text("[]")
    (wallet / "client.yaml").write_text(f"keystore:\n  File: {wallet / 'sui.keystore'}\nenvs:\n- alias: sf076\n  rpc: {RPC}\n"
                                        "active_env: sf076\nactive_address: null\n")
    address = client("new-address", "ed25519")["address"]
    client("switch", "--address", address, json_output=False)
    for _ in range(120):  # The faucet may start after the RPC.
        if client("faucet", "--url", FAUCET, json_output=False, check=False).returncode == 0:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("local faucet did not start")
    for _ in range(120):
        gas = client("gas")
        if (gas.get("gasCoins") or int(gas.get("addressMistBalance") or 0)) if isinstance(gas, dict) else gas:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("local faucet did not supply gas")

    def created(result, suffix):
        return [c["objectId"] for c in result["objectChanges"] if c["type"] == "created" and c["objectType"].endswith(suffix)]

    def ptb(*commands, expect_success=True):
        result = client("ptb", *commands, "--gas-budget", "200000000", check=expect_success)
        if expect_success:
            assert result["effects"]["status"]["status"] == "success", result["effects"]["status"]
        return result

    strings = lambda values: "vector[" + ",".join(json.dumps(v) for v in values) + "]"

    def publish_version(package, catalog, version, arch, expect_success=True):
        return ptb("--move-call", f"{package}::component_catalog::publish_version", f"@{catalog}", '"app"', json.dumps(version),
                   strings(["linux"]), strings([arch]), strings(["recipe-" + version.replace(".", "-")]),
                   strings(["a" * 64]), strings(["b" * 64]), CLOCK, expect_success=expect_success)

    def refused(result):
        """A failed `x86` registration: the executed transaction aborted with EManifest (3)."""
        text = (result.stdout + result.stderr)
        assert result.returncode != 0, "x86 registration unexpectedly succeeded"
        assert "component_catalog::publish_version" in text and "with code 3" in text, text[-2000:]

    contracts = root / "contracts"
    shutil.copytree(base, contracts, ignore=shutil.ignore_patterns("build", "Published.toml"))
    published = client("test-publish", contracts, "--build-env", "testnet", "--pubfile-path", root / "Pub.local.toml",
                       "--gas-budget", "500000000", "--silence-warnings")
    assert published["effects"]["status"]["status"] == "success", published["effects"]["status"]
    base_package = next(c["packageId"] for c in published["objectChanges"] if c["type"] == "published")
    upgrade_cap = created(published, "::package::UpgradeCap")[0]
    catalog = created(ptb("--move-call", f"{base_package}::component_catalog::create_catalog"), "::component_catalog::ComponentCatalog")[0]
    refused(publish_version(base_package, catalog, "1.0.0", "x86", expect_success=False))
    publish_version(base_package, catalog, "1.0.0", "x86_64")
    print("PASS: base package refuses an x86 manifest (EManifest) and registers x86_64 on its catalog")

    shutil.rmtree(contracts / "sources")
    shutil.copytree(ROOT / "contracts/sources", contracts / "sources")
    upgraded = client("test-upgrade", contracts, "--build-env", "testnet", "--pubfile-path", root / "Pub.local.toml",
                      "--upgrade-capability", upgrade_cap, "--gas-budget", "500000000", "--silence-warnings")
    assert upgraded["effects"]["status"]["status"] == "success", upgraded["effects"]["status"]
    package = next(c["packageId"] for c in upgraded["objectChanges"] if c["type"] == "published")
    assert package != base_package
    print("PASS: compatible in-place upgrade to the current contracts with the same UpgradeCap")

    registration = publish_version(package, catalog, "2.0.0", "x86")
    version = created(registration, "::component_catalog::ComponentVersion")[0]
    # The type keeps its introduction (base package) origin across the upgrade.
    assert {c["objectType"] for c in registration["objectChanges"] if c.get("objectId") == version} == \
        {f"{base_package}::component_catalog::ComponentVersion"}, registration["objectChanges"]
    stored = client("object", version)
    fields = stored["content"]
    assert fields["platform_arch"] == ["x86"] and fields["platform_os"] == ["linux"], fields
    assert fields["catalog_id"] == catalog and stored["owner"] == "Immutable", stored["owner"]
    print("PASS: upgraded package registers an x86 manifest on the existing catalog (immutable ComponentVersion)")

    refused(publish_version(base_package, catalog, "3.0.0", "x86", expect_success=False))
    publish_version(base_package, catalog, "3.0.0", "x86_64")
    print("PASS: the old package stays callable and still refuses x86; its x86_64 path is unchanged")


if __name__ == "__main__":
    main()
