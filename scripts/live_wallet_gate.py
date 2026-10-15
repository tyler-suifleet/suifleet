#!/usr/bin/env python3
"""Live wallet acceptance (SF-010, the `wallet` live gate): the dApp's real
dApp Kit connect UI, transaction hook, builders, queries and Walrus upload
code in headless Chromium against Sui testnet and Walrus testnet.

A throwaway ed25519 test key, generated here, acts as the browser wallet
(scripted Wallet Standard wallet; the one stand-in for an extension). It is
funded with 0.1 SUI from the supplied wallet and swept back at the end, so a
run costs only gas and unrefunded storage. Until that sweep succeeds its seed
is kept (0600) beside the wallet as live-browser-key.hex, and the next run
sweeps it first (its gas charged to that run's allowance), so a failed run
never strands the funding.

The deployment under test is the package-upgrade gate's cached fixture for
these contracts (no package is published here): run
`make live-gate-package-upgrade` first. Checks: connect UI, wallet rejection,
fresh registration + cap + activation, group membership, Walrus upload and
a verified deployment (listed with its resolved device; blobs available),
an abort refused by simulation before signing, an abort executed on chain
and reported as failure, wrong-chain refusal, and cleanup.

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
import time
from types import SimpleNamespace

import deployment_manifest as dm
from live_common import DAEMON, ROOT, Budget, Live, Spent, WALRUS_TESTNET
from live_package_upgrade import contracts_digest, load_state

FUNDING = 100_000_000  # 0.1 SUI for the test key; swept back afterwards.
SWEEP_GAS = 3_000_000


def sweep_wallet(live, seed, root):
    """A throwaway client.yaml holding the test key; returns (config, address)."""
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
    subprocess.run(["sui", "client", "--client.config", str(wallet / "client.yaml"), "switch", "--address", address],
                   check=True, capture_output=True)
    return wallet / "client.yaml", address


def sweep(live, seed, root, ledger):
    """Send everything the throwaway key holds back to the operator, from
    Python (independent of the browser run). The sweep is a charged mutation
    like any other: its gas is reserved against `ledger` before submission
    (this run's key: its charged funding; a previous run's key: this run's
    own allowance, since that funding was charged to the earlier run), and
    what it returns never replenishes either."""
    config, address = sweep_wallet(live, seed, root)
    time.sleep(3)  # Reads trail the browser's own sweep.
    amount = live.balance(address)
    if amount < 5_000_000:
        return 0
    try:
        live.execute("sweep the test key", ["ptb", "--transfer-objects", "[gas]", f"@{live.operator}"],
                     gas=SWEEP_GAS, sender=(config, ledger))
    except Spent:
        raise
    except RuntimeError as error:
        # Charged (or, with no outcome, fully reserved) all the same.
        time.sleep(5)
        assert live.balance(address) < 5_000_000, f"sweep failed and the test key still holds funds: {error}"
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
            recovered = sweep(live, pending.read_text().strip(), root, live.ledger)
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
        # The funding is charged to the allowance in full: the browser's
        # transactions and the sweep spend only the test key's own coins, and
        # what the sweep returns never replenishes the allowance.
        live.transaction("fund the browser test key", "--split-coins", "gas", f"[{FUNDING}]", "--assign", "fund",
                         "--transfer-objects", "[fund]", f"@{browser_address}", budget=10_000_000, funding=FUNDING)
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
        live.evidence["python_sweep_mist"] = sweep(live, seed, root, Budget(FUNDING, live.evidence["transactions"]))
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
