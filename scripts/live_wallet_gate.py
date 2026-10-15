#!/usr/bin/env python3
"""Live wallet acceptance (SF-010, the `wallet` live gate): the dApp's real
dApp Kit connect UI, transaction hook, builders, queries and Walrus upload
code in headless Chromium against Sui testnet and Walrus testnet.

A throwaway ed25519 test key, generated here and never written outside the
run's temp directory, acts as the browser wallet (scripted Wallet Standard
wallet; the one stand-in for an extension). It is funded with 0.1 SUI from
the supplied wallet and swept back at the end, so a run costs only gas and
unrefunded storage.

The deployment under test is the package-upgrade gate's cached fixture for
these contracts (no package is published here): run
`make live-gate-package-upgrade` first. Checks: connect UI, wallet rejection,
fresh registration + cap + activation, group membership, Walrus upload and
a verified deployment (listed with its resolved device; blobs available),
an on-chain abort reported as failure, wrong-chain refusal, and cleanup.

A real browser-extension wallet (Slush etc.) still needs a person:
docs/TESTNET.md, "Manual extension-wallet check".
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import deployment_manifest as dm
from live_common import DAEMON, ROOT, Live, Spent, WALRUS_TESTNET
from live_package_upgrade import contracts_digest, load_state

FUNDING = 100_000_000  # 0.1 SUI for the test key; swept back afterwards.


def sweep(live, seed, root):
    """Send everything the throwaway key holds back to the operator, from
    Python (independent of the browser run)."""
    from live_upgrade_security import bech32_suiprivkey
    wallet = root / "sweep-wallet"
    wallet.mkdir(exist_ok=True)
    (wallet / "sui.keystore").write_text("[]\n")
    (wallet / "client.yaml").write_text(
        f"keystore:\n  File: {wallet / 'sui.keystore'}\nenvs:\n- alias: live\n  rpc: {live.rpc_url}\n  ws: null\n  basic_auth: null\n"
        "active_env: live\nactive_address: null\n")
    env = {**os.environ, "SUI_CONFIG_DIR": str(wallet)}
    subprocess.run(["sui", "keytool", "--keystore-path", str(wallet / "sui.keystore"), "import",
                    bech32_suiprivkey(bytes.fromhex(seed)), "ed25519"], check=True, capture_output=True, env=env)
    key = root / "sweep.key"
    key.write_text(seed + "\n")
    address = subprocess.check_output([str(DAEMON), "--derive-address", str(key)], text=True).strip()
    client = ["sui", "client", "--client.config", str(wallet / "client.yaml")]
    subprocess.run([*client, "switch", "--address", address], check=True, capture_output=True)
    def held():
        gas = json.loads(subprocess.run([*client, "gas", "--json"], capture_output=True, text=True).stdout)
        return sum(int(c["mistBalance"]) for c in gas.get("gasCoins", [])) + int(gas.get("addressMistBalance") or 0)
    import time
    time.sleep(3)  # Reads trail the browser's own sweep.
    amount = held()
    if amount < 5_000_000:
        return 0
    result = subprocess.run([*client, "ptb", "--transfer-objects", "[gas]", f"@{live.operator}", "--gas-budget", "3000000", "--json"],
                            capture_output=True, text=True)
    if result.returncode:
        time.sleep(5)
        assert held() < 5_000_000, f"sweep failed and the test key still holds funds: {(result.stderr + result.stdout)[-500:]}"
        return 0  # Already swept (the read was stale).
    return amount


def pending_key_path(live):
    return live.wallet.parent / "live-browser-key.hex"



def run(args):
    live = Live(args.wallet, args.network, args.max_spend_mist)
    live.preflight()
    state = load_state(Path(args.state) if args.state else live.wallet.parent / "live-state.json", live.chain, live.operator)
    tree = contracts_digest(ROOT / "contracts")
    fixture = state["fixtures"].get(tree) or next((f for k, f in state["fixtures"].items() if k.endswith(".." + tree)), None)
    if not fixture or not fixture.get("owner_cap"):
        sys.exit("no activated package-upgrade fixture for these contracts; run make live-gate-package-upgrade first")
    publisher, aggregator = WALRUS_TESTNET
    manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url, package=fixture["head_package"],
                                          upgrade_cap=fixture["upgrade_cap"], registry=fixture["registry"], network=args.network,
                                          publisher=publisher, aggregator=aggregator))
    live.evidence.update(package=fixture["head_package"], registry=fixture["registry"])
    with tempfile.TemporaryDirectory(prefix="suifleet-live-wallet-") as temporary:
        root = Path(temporary)
        (root / "deployment.json").write_text(json.dumps(manifest))
        pending = pending_key_path(live)
        if pending.exists():
            recovered = sweep(live, pending.read_text().strip(), root)
            pending.unlink()
            live.evidence["recovered_from_previous_run_mist"] = recovered
            print(f"recovered {recovered / 1e9:.4f} SUI from a previous run's test key")
        seed = os.urandom(32).hex()
        # Kept (0600, beside the wallet) until the sweep succeeds, so a failed
        # run never strands the funding.
        pending.write_text(seed + "\n")
        pending.chmod(0o600)
        key = root / "browser.key"
        key.write_text(seed + "\n")
        key.chmod(0o600)
        browser_address = subprocess.check_output([str(DAEMON), "--derive-address", str(key)], text=True).strip()
        device_address = subprocess.check_output([str(DAEMON), "--provision", str(root / "device.key")], text=True).strip()
        live.transaction("fund the browser test key", "--split-coins", "gas", f"[{FUNDING}]", "--assign", "fund",
                         "--transfer-objects", "[fund]", f"@{browser_address}", budget=10_000_000)
        before_sweep = live.balance()
        evidence_file = root / "browser-evidence.json"
        env = {**os.environ, "LIVE_MANIFEST_FILE": str(root / "deployment.json"), "LIVE_SEED": seed,
               "LIVE_DEVICE_ADDRESS": device_address, "LIVE_RETURN_ADDRESS": live.operator, "LIVE_EVIDENCE": str(evidence_file)}
        result = subprocess.run(["node", str(ROOT / "scripts/live_wallet_browser.cjs")], env=env, text=True,
                                capture_output=True, timeout=1800)
        browser = json.loads(evidence_file.read_text()) if evidence_file.exists() else {}
        for line in result.stdout.splitlines():
            print(line)
        live.evidence["browser"] = browser
        if result.returncode:
            print(result.stderr[-4000:])
        # The browser sweeps on success; sweep any remainder (or everything,
        # after a failure) from here as well.
        live.evidence["python_sweep_mist"] = sweep(live, seed, root)
        pending.unlink()
        assert result.returncode == 0, "live browser acceptance failed"
        returned = live.balance() - before_sweep
        live.evidence["returned_mist"] = returned
        assert returned > FUNDING // 2, f"sweep returned only {returned} MIST"
        live.check(f"browser run complete; {returned / 1e9:.4f} SUI swept back from the test key")
    live.finish(args.evidence)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallet", required=True, help="client.yaml of the funded wallet to spend from")
    parser.add_argument("--network", default="testnet", choices=["testnet"])
    parser.add_argument("--state", help="package-upgrade fixture cache (default: live-state.json beside the wallet)")
    parser.add_argument("--max-spend-mist", type=int, default=200_000_000)
    parser.add_argument("--evidence", help="write evidence JSON here")
    try:
        run(parser.parse_args())
    except Spent as error:
        sys.exit(f"stopped: {error}")
