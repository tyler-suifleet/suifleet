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

A real browser-extension wallet (Slush etc.) is checked separately, per
release candidate: docs/TESTNET.md, "Extension-wallet check".
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
from live_common import DAEMON, ROOT, Budget, Live, Spent, Walrus, pending_key, recover_key, sweep
from live_package_upgrade import contracts_digest, fixture_key, load_state

FUNDING = 100_000_000  # 0.1 SUI for the test key; swept back afterwards.


def pending_key_path(live):
    return live.wallet.parent / "live-browser-key.hex"


def head_fixture(state, tree):
    """The package-upgrade fixture whose head is `tree`, built by the pinned
    release compiler (live_package_upgrade.fixture_key), or None."""
    head = fixture_key(tree, tree)
    return state["fixtures"].get(head) or next((f for k, f in state["fixtures"].items() if k.endswith(".." + head)), None)


def run(args):
    live = Live(args.wallet, args.network, args.max_spend_mist)
    live.preflight()
    state = load_state(Path(args.state) if args.state else live.wallet.parent / "live-state.json", live.chain, live.operator)
    fixture = head_fixture(state, contracts_digest(ROOT / "contracts"))
    if not fixture or not fixture.get("owner_cap"):
        sys.exit("no activated package-upgrade fixture for these contracts; run make live-gate-package-upgrade first")
    # The dApp uploads through the manifest's one publisher, so pick one that
    # is storing blobs now: Mysten's can run dry (CI run 40156238429).
    walrus = Walrus("testnet")
    publisher, aggregator = walrus.working_publisher(), walrus.aggregator
    manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url, package=fixture["head_package"],
                                          upgrade_cap=fixture["upgrade_cap"], registry=fixture["registry"], network=args.network,
                                          publisher=publisher, aggregator=aggregator))
    live.evidence.update(package=fixture["head_package"], registry=fixture["registry"])
    with tempfile.TemporaryDirectory(prefix="suifleet-live-wallet-") as temporary:
        root = Path(temporary)
        (root / "deployment.json").write_text(json.dumps(manifest))
        pending = pending_key_path(live)
        recover_key(live, pending, root)
        seed = os.urandom(32).hex()
        pending_key(pending, seed)
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
