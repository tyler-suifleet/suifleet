#!/usr/bin/env python3
"""Record the suifleet.io guide screencasts on Sui testnet (SF-172).

Drives the real console static export through every step of
site/guide/steps.json with site/guide/record_guide.cjs, against the
package-upgrade live gate's cached fixture for these contracts (no package is
published here; run `make live-gate-package-upgrade BASE_REF=HEAD` first if
`--check` reports none). Run inside the pinned dev image, which has the
transaction CLI, Node and the Chrome headless shell, holding the
`network:suifleet-testnet` lock (docs/TESTNET.md):

    python3 site/guide/record_guide.py \
      --wallet ~/.config/suifleet/testnet/agent/client.yaml \
      --runtime-dir /private/guide-run --evidence /private/guide-run/evidence.json

then encode on the host with site/guide/encode_guide.py. Follow
site/guide/RECORDING.md.

Money: a throwaway browser key is funded from the wallet (charged to the
run's allowance in full) and swept back at the end; its seed is kept 0600
beside the wallet as guide-recorder-key.hex until the sweep succeeds, and the
next run sweeps a leftover first. As the registry administrator, the wallet
also grants the new customer a small device/upload entitlement, as SuiFleet
does for a subscriber. Keys never reach the browser, the frames or the logs.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import deployment_manifest as dm  # noqa: E402
from live_common import DAEMON, Budget, Live, Spent, Walrus, pending_key, plain, recover_key, sweep  # noqa: E402
from live_package_upgrade import contracts_digest, load_state  # noqa: E402
from live_wallet_gate import head_fixture  # noqa: E402

FUNDING = 150_000_000  # 0.15 SUI for the browser key; swept back afterwards.
ENTITLEMENT = ("5", "50000000", "1")  # devices, upload bytes, period


def node(runtime, phase, manifest_file, seed_file, env_extra):
    env = {**os.environ, "GUIDE_MANIFEST_FILE": str(manifest_file), "GUIDE_SEED_FILE": str(seed_file),
           "GUIDE_EXPORT": str(ROOT / "dapp" / "out"), "GUIDE_OUT": str(runtime), "GUIDE_PHASE": phase,
           "GUIDE_STATE": str(runtime / "browser-state.json"), **env_extra}
    result = subprocess.run(["node", str(ROOT / "site/guide/record_guide.cjs")], env=env, text=True, timeout=3600)
    evidence = json.loads((runtime / f"evidence-{phase}.json").read_text())
    return result.returncode, evidence


def created(live, digest, suffix):
    found = live.read_back(digest)
    assert found, f"transaction {digest} is not readable"
    return [c["objectId"] for c in found.get("objectChanges", []) if c["type"] == "created" and c["objectType"].endswith(suffix)]


def build_console(manifest):
    """The console export the screencasts show, built for this manifest."""
    env = {**os.environ, "NEXT_TELEMETRY_DISABLED": "1", "NEXT_PUBLIC_DEPLOYMENT_MANIFEST": json.dumps(manifest)}
    for name in [k for k in env if k.startswith("NEXT_PUBLIC_") and k != "NEXT_PUBLIC_DEPLOYMENT_MANIFEST"]:
        del env[name]  # Only the manifest: no Google, Seal or fleet-data overrides from the host.
    subprocess.run([str(ROOT / "dapp/node_modules/.bin/next"), "build"], cwd=ROOT / "dapp", env=env, check=True,
                   stdout=subprocess.DEVNULL)


def run(args):
    runtime = Path(args.runtime_dir).resolve()
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    live = Live(args.wallet, "testnet", args.max_spend_mist)
    live.preflight(auto_fund=args.faucet)
    state = load_state(Path(args.state) if args.state else live.wallet.parent / "live-state.json", live.chain, live.operator)
    fixture = head_fixture(state, contracts_digest(ROOT / "contracts"))
    if not fixture or not fixture.get("owner_cap"):
        sys.exit("no activated package-upgrade fixture for these contracts; run make live-gate-package-upgrade first")
    walrus = Walrus("testnet")
    manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url, package=fixture["head_package"],
                                          upgrade_cap=fixture["upgrade_cap"], registry=fixture["registry"], network="testnet",
                                          publisher=walrus.working_publisher(), aggregator=walrus.aggregator))
    for group in ("customer", "fleet budget", "catalog"):
        assert all(t in manifest["type_origins"] for t in dm.OPTIONAL_GROUPS[group]), f"fixture lacks {group} types"
    manifest_file = runtime / "deployment.json"
    manifest_file.write_text(json.dumps(manifest))
    commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    live.evidence.update(package=fixture["head_package"], registry=fixture["registry"], console_commit=commit,
                         walrus_publisher=manifest["walrus"]["publisher_url"])
    if not args.skip_build:
        build_console(manifest)
    package, registry = fixture["head_package"], fixture["registry"]

    pending = live.wallet.parent / "guide-recorder-key.hex"
    recover_key(live, pending, runtime)
    seed = os.urandom(32).hex()
    pending_key(pending, seed)
    seed_file = runtime / "browser.key"
    seed_file.touch(mode=0o600)
    seed_file.write_text(seed + "\n")
    browser_address = subprocess.check_output([str(DAEMON), "--derive-address", str(seed_file)], text=True).strip()
    device_address = subprocess.check_output([str(DAEMON), "--provision", str(runtime / "device.key")], text=True).strip()
    live.transaction("fund the guide browser key", "--split-coins", "gas", f"[{FUNDING}]", "--assign", "fund",
                     "--transfer-objects", "[fund]", f"@{browser_address}", budget=10_000_000, funding=FUNDING)
    browser = {"address": browser_address, "phases": []}
    live.evidence["browser"] = browser
    failure = None
    try:
        code, sign_in = node(runtime, "sign-in", manifest_file, seed_file, {})
        browser["phases"].append(sign_in)
        assert code == 0, "sign-in phase failed"
        customers = created(live, sign_in["transactions"][-1]["digest"], "::customer::Customer")
        assert len(customers) == 1, f"expected one new Customer, found {customers}"
        live.transaction("grant the guide customer its entitlement", "--move-call", f"{package}::customer::set_entitlement",
                         f"@{customers[0]}", f"@{registry}", *ENTITLEMENT, budget=20_000_000)
        live.check(f"customer {customers[0]} created in the browser and granted an entitlement")

        code, rest = node(runtime, "rest", manifest_file, seed_file, {"GUIDE_DEVICES": json.dumps([device_address])})
        browser["phases"].append(rest)
        assert code == 0, "recording phase failed"
        by_step = {}
        for tx in rest["transactions"]:
            by_step.setdefault(tx["step"], []).append(tx["digest"])
        devices = created(live, by_step["register-device"][-1], "::device_registry::Device")
        versions = created(live, by_step["publish-version"][-1], "::component_catalog::ComponentVersion")
        assert len(devices) == 1 and len(versions) == 1, (devices, versions)
        fields = plain(live.object(versions[0])["content"])
        extra = {"recipe": fields["recipe_blob_ids"][0], "recipeSha256": fields["recipe_sha256s"][0],
                 "artifactSha256": fields["artifact_sha256s"][0], "cohorts": [devices[0]]}
        code, rollout = node(runtime, "rollout", manifest_file, seed_file, {"GUIDE_EXTRA": json.dumps(extra)})
        browser["phases"].append(rollout)
        assert code == 0, "rollout phase failed"
    except (AssertionError, KeyError, subprocess.TimeoutExpired) as error:
        failure = error
    finally:
        # Independent effects check of everything the browser key executed.
        for phase in browser["phases"]:
            for tx in phase["transactions"]:
                found = live.read_back(tx["digest"])
                status = (found.get("effects") or {}).get("status", {}).get("status")
                tx["effects_status"] = status
                gas = (found.get("effects") or {}).get("gasUsed") or {}
                tx["net_gas_mist"] = (int(gas.get("computationCost", 0)) + int(gas.get("storageCost", 0))
                                      - int(gas.get("storageRebate", 0))) if gas else None
        browser["swept_mist"] = sweep(live, seed, runtime, Budget(FUNDING, live.evidence["transactions"]))
        pending.unlink()
        seed_file.unlink()
        (runtime / "device.key").unlink(missing_ok=True)
        live.finish(args.evidence)
    if failure:
        raise SystemExit(f"recording failed: {failure}")
    bad = [tx["digest"] for p in browser["phases"] for tx in p["transactions"] if tx["effects_status"] != "success"]
    assert not bad, f"transactions without successful effects: {bad}"
    print(f"recorded; frames in {runtime / 'frames'}; encode with site/guide/encode_guide.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallet", required=True, help="client.yaml of the dedicated testnet wallet")
    parser.add_argument("--runtime-dir", required=True, help="private directory for the manifest, frames and evidence")
    parser.add_argument("--state", help="package-upgrade fixture cache (default: live-state.json beside the wallet)")
    parser.add_argument("--max-spend-mist", type=int, default=250_000_000)
    parser.add_argument("--faucet", action="store_true", help="request testnet SUI once if the balance is below the allowance")
    parser.add_argument("--skip-build", action="store_true", help="reuse dapp/out already built for this manifest")
    parser.add_argument("--evidence", help="write evidence JSON here")
    try:
        run(parser.parse_args())
    except Spent as error:
        sys.exit(f"stopped: {error}")
