#!/usr/bin/env python3
"""Live upgrade-security acceptance (SF-010): existing objects, legacy caps
and old-package entry points across a real package upgrade, on Sui testnet.

Spends testnet gas from an explicitly supplied wallet (docs/TESTNET.md).

1. Publishes the BASE contracts (default: main before SF-008's identities,
   `f43d531^1`) and creates live state with that package: a device whose key
   the daemon provisions, its signer-bound cap, a group, and a deployment the
   device reports Applied through the base package.
2. Upgrades in place to this tree.
3. Attacks, each of which must abort on chain with the intended Move abort:
   - the OLD package's report_status on a record created by the new package
     (its receipts are a newer authority generation);
   - the new package's superseded report_status signature;
   - a verified report from the legacy device before it is activated;
   - the operator reporting with a cap it minted for the device;
   - a verified report after the operator revokes the device.
4. Activates the legacy device through the new package and runs the real
   daemon: it applies a new deployment and its receipt reads Applied; after
   revocation the next deployment gets no receipt.
5. Records evidence (versions, packages, every digest, aborts, receipts,
   spend). Recipes and artifacts go to real Walrus testnet storage
   (--walrus testnet) or a local stand-in.

Cost: package storage is never refunded, so the two packages (base and its
upgrade to head) are cached in --state, keyed by the content of the base and
head contracts, and re-verified on chain before reuse: the UpgradeCap is still
the operator's, at version 2 and pointing at the cached head (so nothing else
was upgraded on top), the registry is shared, and the manifest's type origins
split between base and head. A reused pair recreates the legacy state through
the base package's entry points, which old packages keep after an upgrade;
--fresh republishes (~0.18 SUI), for release candidates that must also show
state created before the upgrade. Every transaction, the device's funding and
the device's own transactions are charged to --max-spend-mist.

Cleanup runs after a failure too: the run deletes its deployments and group
and deregisters the device (an activated one leaves a revoked identity
tombstone; DeviceCaps have no delete function and remain), then sweeps the
device key's unspent funding back to the operator. Until that sweep succeeds
the key's seed is kept (0600) beside the wallet as live-security-device.key,
and the next run sweeps it first, charged to its own allowance.
"""
import argparse
import json
from pathlib import Path
import signal
import sys
import tempfile
import uuid
from types import SimpleNamespace

import deployment_manifest as dm
from live_common import (CLOCK, DAEMON, Live, Spent, Walrus, contracts_at, deployment_args, history, ids, key_wallet,
                         pending_key, recover_key, stage, start_daemon, stop_daemon, sweep, wait_for)
from live_package_upgrade import contracts_digest, fixture_key, load_state, save_state
import subprocess

DEFAULT_BASE = "f43d531^1"  # main immediately before SF-008 (#50): receipts, no identities.


DEVICE_FUNDING = 40_000_000  # The legacy device's own report/attack gas, charged up front.
CLEANUP_GAS = 20_000_000  # Deletions are cheap; an unknown outcome must not starve the remaining cleanup.


def fixture_valid(live, fixture):
    """Free reads: the cached packages are still this operator's lineage."""
    try:
        cap = live.object(fixture["upgrade_cap"])
        registry = live.object(fixture["registry"])
        live.object(fixture["head_package"])
    except RuntimeError:
        return False
    content = cap.get("content") or {}
    return (dm.object_id((cap.get("owner") or {}).get("AddressOwner") or "0x0") == live.operator
            and dm.object_id(content.get("package") or "0x0") == dm.object_id(fixture["head_package"])
            and int(content.get("version") or 0) == 2 and "Shared" in (registry.get("owner") or {}))


def run(args):
    live = Live(args.wallet, args.network, args.max_spend_mist)
    live.preflight()
    repository = Path(__file__).resolve().parents[1]
    base = subprocess.check_output(["git", "-C", str(repository), "rev-parse", args.base_ref], text=True).strip()
    live.evidence.update(base=base, walrus=args.walrus)
    state_path = Path(args.state) if args.state else live.wallet.parent / "live-state.json"
    walrus = Walrus(args.walrus)
    daemon, passed, failure = None, False, None
    made = {"base_records": [], "head_records": []}
    with tempfile.TemporaryDirectory(prefix="suifleet-live-security-") as temporary:
        root = Path(temporary)
        try:
            package_dir, pubfile = root / "package", root / "Pub.live.toml"
            base_contracts = contracts_at(base, root / "base")
            # The (base, head) pair and the release compiler that built its bytecode.
            key_name = fixture_key(contracts_digest(base_contracts), contracts_digest(repository / "contracts"))
            state = load_state(state_path, live.chain, live.operator)
            fixtures = state.setdefault("security_fixtures", {})
            fixture = None if args.fresh else fixtures.get(key_name)
            if fixture and not fixture_valid(live, fixture):
                print("Cached security fixture is no longer intact on chain; publishing a new one")
                fixture = None
            run_id = "sec-" + uuid.uuid4().hex[:8]
            pending = pending_key_path(live)
            recover_key(live, pending, root)
            key = root / "device.key"
            subprocess.check_output([str(DAEMON), "--provision", str(key)], text=True)
            seed = key.read_text().strip()
            pending_key(pending, seed)
            device_config, device_address = key_wallet(live, seed, root, "device-wallet")
            device_wallet = live.child(device_config, DEVICE_FUNDING)
            made.update(pending=pending, seed=seed, root=root, device_ledger=device_wallet[1])

            def legacy_state(old, registry):
                """A legacy device, its signer-bound cap (plus one the operator
                keeps), a group and a receipted deployment, all through `old`."""
                registered = live.transaction("register legacy device + bound cap + group (base)",
                    "--move-call", f"{old}::device_registry::register_device", f"@{registry}", '"legacy-pi"',
                    f"@{device_address}", '"aarch64"', CLOCK, "--assign", "device",
                    "--move-call", f"{old}::device_cap::issue_cap", "device",
                    "--move-call", f"{old}::device_cap::issue", "device", "--assign", "minted",
                    "--transfer-objects", "[device, minted]", f"@{live.operator}",
                    "--move-call", f"{old}::device_group::create_group", '"legacy"', '"sf010"', CLOCK,
                    "--split-coins", "gas", f"[{DEVICE_FUNDING}]", "--assign", "fee",
                    "--transfer-objects", "[fee]", f"@{device_address}", funding=DEVICE_FUNDING)
                device = live.created(registered, "::device_registry::Device")[0]
                caps = live.created(registered, "::device_cap::DeviceCap")
                group = live.created(registered, "::device_group::DeviceGroup")[0]
                made.update(base_package=old, registry=registry, device=device, group=group)
                made.setdefault("package", old)  # Until the upgrade (a reused fixture already set head).
                for cap_id in caps:
                    live.readable(cap_id)
                cap = next(c for c in caps if dm.object_id(live.owner(c)) == dm.object_id(device_address))
                minted = next(c for c in caps if c != cap)
                live.transaction("add legacy device to group (base)", "--move-call", f"{old}::device_group::add_device", f"@{group}", f"@{device}")
                d0 = live.created(live.transaction("deployment via base", "--move-call", f"{old}::deployment_manager::create_deployment_verified",
                                                   *deployment_args(walrus, "0.9.0", run_id), "vector[]", ids([device]), CLOCK),
                                  "::deployment_manager::DeploymentRecord")[0]
                made["base_records"].append(d0)
                live.transaction("legacy device reports Applied via base", "--move-call", f"{old}::deployment_manager::report_status",
                                 f"@{d0}", f"@{cap}", "1u8", budget=10_000_000, sender=device_wallet)
                wait_for(lambda: live.receipts(d0) == {dm.object_id(device): 1}, "base receipt not readable")
                return device, cap, minted, group, d0

            if fixture:
                old, new, registry, upgrade_cap = (fixture[k] for k in ("base_package", "head_package", "registry", "upgrade_cap"))
                made["package"] = new
                live.check(f"reused fixture {key_name[:12]}…: base {old[:12]}…, head {new[:12]}… (UpgradeCap version 2 still "
                           "points at head; re-verified on chain; no package storage spent)")
                device, cap, minted, group, d0 = legacy_state(old, registry)
                live.check(f"base {old[:12]}…'s entry points, after the upgrade: legacy device, bound cap, group and a "
                           "receipted deployment created with them")
                live.evidence["legacy_state"] = "created through the base package after the cached upgrade"
            else:
                # 1. Base package and legacy state.
                stage(base_contracts, package_dir)
                first = live.publish("publish base (pre-identity)", package_dir, pubfile)
                old = live.published(first)
                registry = live.created(first, "::device_registry::DeviceRegistry")[0]
                upgrade_cap = live.created(first, "::package::UpgradeCap")[0]
                device, cap, minted, group, d0 = legacy_state(old, registry)
                live.check(f"base {old[:12]}…: legacy device, bound cap, group and a receipted deployment created with it")
                live.evidence["legacy_state"] = "created before the upgrade"

                # 2. Upgrade in place.
                stage(repository / "contracts", package_dir)
                new = live.published(live.publish("upgrade to head", package_dir, pubfile,
                                                        upgrade={"cap": upgrade_cap, "package": old}))
                made["package"] = new
                fixtures[key_name] = dict(base_package=old, head_package=new, registry=registry, upgrade_cap=upgrade_cap,
                                          base=base, created=live.evidence["started"])
                save_state(state_path, state)
            manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url, package=new, upgrade_cap=upgrade_cap,
                                                  registry=registry, network=args.network, publisher=walrus.publisher,
                                                  aggregator=walrus.aggregator))
            live.evidence.update(base_package=old, head_package=new, registry=registry, upgrade_cap=upgrade_cap, fixture=key_name,
                                 type_origins=manifest["type_origins"])
            assert manifest["type_origins"]["device_registry::IdentityKey"] == dm.object_id(new), "identity types should originate in the upgrade"
            assert manifest["type_origins"]["device_registry::Device"] == dm.object_id(old), "existing types should originate in the base"
            live.check(f"upgraded in place to {new[:12]}…; identity types originate in the upgrade, existing types in the base")

            # 3. Attacks (each must abort exactly as intended).
            d1 = live.created(live.transaction("deployment via head", "--move-call", f"{new}::deployment_manager::create_deployment_verified",
                                               *deployment_args(walrus, "1.0.0", run_id), "vector[]", ids([device]), CLOCK),
                              "::deployment_manager::DeploymentRecord")[0]
            made["head_records"].append(d1)
            # The device holds DEVICE_FUNDING: its budgets stay well below that.
            attack = lambda label, *ptb, **expected: live.attack(label, *ptb, budget=10_000_000, sender=device_wallet, **expected)
            attack("old package report on a new record", "--move-call", f"{old}::deployment_manager::report_status", f"@{d1}", f"@{cap}", "1u8",
                   module="deployment_manager", function="receipts_mut", code=5)
            attack("superseded report signature in the new package", "--move-call", f"{new}::deployment_manager::report_status", f"@{d1}",
                   f"@{cap}", "1u8", module="deployment_manager", function="report_status", code=7)
            attack("verified report before the legacy device is activated", "--move-call", f"{new}::deployment_manager::report_verified_status",
                   f"@{d1}", f"@{registry}", f"@{cap}", "1u8", module="device_cap", function="assert_active", code=2)
            live.attack("operator reports with a cap it minted for the device", "--move-call",
                        f"{new}::deployment_manager::report_verified_status", f"@{d1}", f"@{registry}", f"@{minted}", "1u8",
                        module="device_cap", function="assert_signer", code=0)
            assert live.receipts(d1) == {}, "an attack wrote a receipt"
            live.check("old-package, superseded, pre-activation and impersonation reports all abort on chain; no receipt written")

            # 4. Activation, the real daemon (paying from the device's charged funding), revocation.
            activated = live.transaction("activate the legacy device (head)", "--move-call", f"{new}::device_registry::activate_device",
                                         f"@{registry}", f"@{device}", "--assign", "owner", "--transfer-objects", "[owner]", f"@{live.operator}")
            owner_cap = live.created(activated, "::device_registry::DeviceOwnerCap")[0]
            made["owner_cap"] = owner_cap
            live.readable(owner_cap)
            daemon, components = start_daemon(root, "legacy", manifest, f'[device]\nkeypair_path = "{key}"\n'
                                              f'device_object_id = "{device}"\ndevice_cap_id = "{cap}"\n')
            d2 = live.created(live.transaction("deployment via head (after activation)",
                                               "--move-call", f"{new}::deployment_manager::create_deployment_verified",
                                               *deployment_args(walrus, "2.0.0", run_id), "vector[]", ids([device]), CLOCK),
                              "::deployment_manager::DeploymentRecord")[0]
            made["head_records"].append(d2)
            wait_for(lambda: live.receipts(d2) == {dm.object_id(device): 1}, "activated legacy device did not report Applied", daemon)
            assert history(components) == ["2.0.0"], history(components)
            live.check("activated legacy device: the real daemon applied the new deployment (artifact from "
                       f"{'Walrus testnet' if args.walrus == 'testnet' else 'local stand-in'}); its existing bound cap reported Applied")
            live.transaction("revoke the device (head)", "--move-call", f"{new}::device_registry::revoke_device", f"@{registry}", f"@{owner_cap}")
            attack("verified report after revocation", "--move-call", f"{new}::deployment_manager::report_verified_status",
                   f"@{d2}", f"@{registry}", f"@{cap}", "2u8", module="device_cap", function="assert_active", code=2)
            assert live.receipts(d2) == {dm.object_id(device): 1}, "revoked device changed its receipt"
            live.check("after revocation the device's reports abort and its receipt is unchanged")
            daemon.send_signal(signal.SIGINT)
            daemon.wait(timeout=60)
            daemon = None
            live.evidence["receipts"] = {"d0": {dm.object_id(device): 1}, "d2": {dm.object_id(device): 1}}
            passed = True
        finally:
            if daemon:
                stop_daemon(daemon)
            # 5. Cleanup, after a failure too: storage rebates on the run's
            # objects, then the device's unspent funding back to the operator.
            failure = teardown(live, made)
            walrus.close()
            live.finish(args.evidence)
    if failure and passed:
        raise failure


def pending_key_path(live):
    return live.wallet.parent / "live-security-device.key"


def teardown(live, made):
    """Delete what the run created and sweep the device key, attempting
    every step even after a failure (each is charged like any mutation).
    The device key stays pending beside the wallet unless its sweep
    succeeds. Returns the first failure, or None."""
    steps, package, registry = [], made.get("package"), made.get("registry")
    if made["head_records"]:
        steps.append(("delete head deployments", lambda: live.transaction("delete head deployments", *(
            arg for record in made["head_records"]
            for arg in ("--move-call", f"{package}::deployment_manager::delete_deployment", f"@{record}")), budget=CLEANUP_GAS)))
    for record in made["base_records"]:
        base = made["base_package"]
        steps.append(("delete base deployment", lambda record=record: live.transaction(
            "delete base deployment", "--move-call", f"{base}::deployment_manager::delete_deployment", f"@{record}",
            budget=CLEANUP_GAS)))
    if made.get("group"):
        # An activated device leaves a revoked identity tombstone; its caps
        # (no delete function) remain with the operator and the device key.
        deregister = (["--move-call", f"{package}::device_registry::deregister_activated_device", f"@{registry}",
                       f"@{made['owner_cap']}"] if made.get("owner_cap") else
                      ["--move-call", f"{package}::device_registry::deregister_device", f"@{registry}", f"@{made['device']}"])
        steps.append(("delete group, deregister device", lambda: live.transaction(
            "delete the legacy group and deregister the device", "--move-call", f"{package}::device_group::delete_group",
            f"@{made['group']}", *deregister, budget=CLEANUP_GAS)))
    if made.get("seed"):
        def sweep_device():
            # Checked whether or not the funding transfer reported effects.
            live.evidence["device_sweep_mist"] = sweep(live, made["seed"], made["root"], made["device_ledger"],
                                                       label="sweep the device key")
            made["pending"].unlink()
        steps.append(("sweep the device key", sweep_device))
    failure = None
    for label, step in steps:
        try:
            step()
        except Exception as error:  # Spent included: the remaining steps may still fit.
            live.evidence.setdefault("cleanup_failures", []).append(f"{label}: {error}")
            print(f"cleanup failed: {label}: {error}")
            failure = failure or error
    return failure


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallet", required=True, help="client.yaml of the funded wallet to spend from")
    parser.add_argument("--network", default="testnet", choices=["testnet", "localnet"])
    parser.add_argument("--base-ref", default=DEFAULT_BASE, help=f"pre-upgrade contracts (default {DEFAULT_BASE})")
    parser.add_argument("--walrus", default="testnet", choices=["testnet", "local"])
    parser.add_argument("--max-spend-mist", type=int, default=600_000_000, help="strict run allowance, including device funding")
    parser.add_argument("--evidence", help="write evidence JSON here")
    parser.add_argument("--state", help="fixture cache (default: live-state.json beside the wallet)")
    parser.add_argument("--fresh", action="store_true", help="ignore the cache: publish and upgrade a new fixture")
    try:
        run(parser.parse_args())
    except Spent as error:
        sys.exit(f"stopped: {error}")
