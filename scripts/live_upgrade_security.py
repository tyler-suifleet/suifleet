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

Cost: two package publications (~0.18 SUI) plus about a dozen transactions;
run it per release candidate, not per commit.
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
from live_common import (CLOCK, DAEMON, Live, Spent, Walrus, contracts_at, deployment_args, history, ids, stage,
                         start_daemon, wait_for)
import subprocess

DEFAULT_BASE = "f43d531^1"  # main immediately before SF-008 (#50): receipts, no identities.


def run(args):
    live = Live(args.wallet, args.network, args.max_spend_mist)
    live.preflight()
    base = subprocess.check_output(["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", args.base_ref],
                                   text=True).strip()
    live.evidence.update(base=base, walrus=args.walrus)
    walrus = Walrus(args.walrus)
    daemon = None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-live-security-") as temporary:
            root = Path(temporary)
            package_dir, pubfile = root / "package", root / "Pub.live.toml"
            stage(contracts_at(base, root / "base"), package_dir)
            run_id = "sec-" + uuid.uuid4().hex[:8]

            # 1. Base package and legacy state.
            first = live.publish("publish base (pre-identity)", package_dir, pubfile)
            old = live.published(first)
            registry = live.created(first, "::device_registry::DeviceRegistry")[0]
            upgrade_cap = live.created(first, "::package::UpgradeCap")[0]
            key = root / "device.key"
            device_address = subprocess.check_output([str(DAEMON), "--provision", str(key)], text=True).strip()
            registered = live.transaction("register legacy device + bound cap + group (base)",
                "--move-call", f"{old}::device_registry::register_device", f"@{registry}", '"legacy-pi"',
                f"@{device_address}", '"aarch64"', CLOCK, "--assign", "device",
                "--move-call", f"{old}::device_cap::issue_cap", "device",
                "--move-call", f"{old}::device_cap::issue", "device", "--assign", "minted",
                "--transfer-objects", "[device, minted]", f"@{live.operator}",
                "--move-call", f"{old}::device_group::create_group", '"legacy"', '"sf010"', CLOCK,
                "--split-coins", "gas", "[40000000]", "--assign", "fee", "--transfer-objects", "[fee]", f"@{device_address}")
            device = live.created(registered, "::device_registry::Device")[0]
            caps = live.created(registered, "::device_cap::DeviceCap")
            group = live.created(registered, "::device_group::DeviceGroup")[0]
            for cap_id in caps:
                live.readable(cap_id)
            cap = next(c for c in caps if dm.object_id(live.owner(c)) == dm.object_id(device_address))
            minted = next(c for c in caps if c != cap)
            live.transaction("add legacy device to group (base)", "--move-call", f"{old}::device_group::add_device", f"@{group}", f"@{device}")
            d0 = live.created(live.transaction("deployment via base", "--move-call", f"{old}::deployment_manager::create_deployment_verified",
                                               *deployment_args(walrus, "0.9.0", run_id), "vector[]", ids([device]), CLOCK),
                              "::deployment_manager::DeploymentRecord")[0]
            device_wallet = device_client(root, key, live)
            report = device_wallet("ptb", "--move-call", f"{old}::deployment_manager::report_status", f"@{d0}", f"@{cap}", "1u8",
                                   "--gas-budget", "10000000")
            assert isinstance(report, dict), f"legacy report failed:\n{report.stderr[-2000:]}\n{report.stdout[-1000:]}"
            live.record("legacy device reports Applied via base", report)
            wait_for(lambda: live.receipts(d0) == {dm.object_id(device): 1}, "base receipt not readable")
            live.check(f"base {old[:12]}…: legacy device, bound cap, group and a receipted deployment created with it")

            # 2. Upgrade in place.
            stage(Path(__file__).resolve().parents[1] / "contracts", package_dir)
            upgraded = live.publish("upgrade to head", package_dir, pubfile, upgrade_cap=upgrade_cap)
            new = live.published(upgraded)
            manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url, package=new, upgrade_cap=upgrade_cap,
                                                  registry=registry, network=args.network, publisher=walrus.publisher,
                                                  aggregator=walrus.aggregator))
            live.evidence.update(base_package=old, head_package=new, registry=registry, type_origins=manifest["type_origins"])
            assert manifest["type_origins"]["device_registry::IdentityKey"] == dm.object_id(new), "identity types should originate in the upgrade"
            live.check(f"upgraded in place to {new[:12]}…; identity types originate in the upgrade, existing types in the base")

            # 3. Attacks (each must abort exactly as intended).
            d1 = live.created(live.transaction("deployment via head", "--move-call", f"{new}::deployment_manager::create_deployment_verified",
                                               *deployment_args(walrus, "1.0.0", run_id), "vector[]", ids([device]), CLOCK),
                              "::deployment_manager::DeploymentRecord")[0]
            attack = lambda label, *ptb, **expected: device_attack(live, device_wallet, label, *ptb, **expected)
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

            # 4. Activation, the real daemon, revocation.
            activated = live.transaction("activate the legacy device (head)", "--move-call", f"{new}::device_registry::activate_device",
                                         f"@{registry}", f"@{device}", "--assign", "owner", "--transfer-objects", "[owner]", f"@{live.operator}")
            owner_cap = live.created(activated, "::device_registry::DeviceOwnerCap")[0]
            live.readable(owner_cap)
            daemon, components = start_daemon(root, "legacy", manifest, f'[device]\nkeypair_path = "{key}"\n'
                                              f'device_object_id = "{device}"\ndevice_cap_id = "{cap}"\n')
            d2 = live.created(live.transaction("deployment via head (after activation)",
                                               "--move-call", f"{new}::deployment_manager::create_deployment_verified",
                                               *deployment_args(walrus, "2.0.0", run_id), "vector[]", ids([device]), CLOCK),
                              "::deployment_manager::DeploymentRecord")[0]
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

            # 5. Cleanup: storage rebate on the records.
            deletes = []
            for record in (d1, d2):
                deletes += ["--move-call", f"{new}::deployment_manager::delete_deployment", f"@{record}"]
            live.transaction("delete head deployments", *deletes)
            live.transaction("delete base deployment", "--move-call", f"{old}::deployment_manager::delete_deployment", f"@{d0}")
            live.evidence["receipts"] = {"d0": {dm.object_id(device): 1}, "d2": {dm.object_id(device): 1}}
    finally:
        if daemon and daemon.poll() is None:
            daemon.kill()
        walrus.close()
        live.finish(args.evidence)


def device_client(root, key, live):
    """A `sui client` for the device's own key (a throwaway testnet key that
    the daemon provisioned), in a separate config under the run's temp dir."""
    wallet = root / "device-wallet"
    wallet.mkdir()
    (wallet / "sui.keystore").write_text("[]\n")
    (wallet / "client.yaml").write_text(
        f"keystore:\n  File: {wallet / 'sui.keystore'}\nenvs:\n- alias: live\n  rpc: {live.rpc_url}\n  ws: null\n  basic_auth: null\n"
        "active_env: live\nactive_address: null\n")
    seed = bytes.fromhex(key.read_text().strip())
    # sui keytool takes a Bech32 "suiprivkey" (flag 0x00 = ed25519 + seed).
    import os
    # keytool also loads a client config; point it at this throwaway wallet.
    imported = subprocess.run(["sui", "keytool", "--keystore-path", str(wallet / "sui.keystore"), "import",
                               bech32_suiprivkey(seed), "ed25519"], capture_output=True, text=True,
                              env={**os.environ, "SUI_CONFIG_DIR": str(wallet)})
    assert imported.returncode == 0, "could not import the device's throwaway key"
    address = subprocess.check_output([str(DAEMON), "--derive-address", str(key)], text=True).strip()
    subprocess.run(["sui", "client", "--client.config", str(wallet / "client.yaml"), "switch", "--address", address],
                   check=True, capture_output=True)

    def call(*command):
        result = subprocess.run(["sui", "client", "--client.config", str(wallet / "client.yaml"), *command, "--json"],
                                text=True, capture_output=True, timeout=600)
        if result.returncode:
            return result
        out = result.stdout
        return json.loads(out[out.find("{"):])
    return call


def device_attack(live, device_wallet, label, *ptb, module, function, code):
    """`Live.attack`, but sent by the device's own key."""
    # The device holds ~0.04 SUI: budgets must stay below its balance.
    result = device_wallet("ptb", *ptb, "--gas-budget", "10000000")
    text = (result.stderr + result.stdout) if hasattr(result, "stderr") else json.dumps(result.get("effects", {}).get("status"))
    import re
    from live_common import ABORT
    digest = re.search(r"transaction '([1-9A-HJ-NP-Za-km-z]{32,44})'", text)
    found = ABORT.search(text)
    observed = found.groups() if found else None
    live.evidence["transactions"].append({"label": label, "digest": digest.group(1) if digest else None,
                                          "status": "aborted" if found else "unexpected", "abort": observed, "sender": "device"})
    assert observed == (module, function, str(code)), f"{label}: expected abort {module}::{function} {code}, got {observed}\n{text[-1500:]}"


def bech32_suiprivkey(seed):
    """Bech32 encoding (HRP "suiprivkey") of the ed25519 flag and seed."""
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    data = bytes([0]) + seed
    bits, acc, values = 0, 0, []
    for byte in data:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            values.append((acc >> bits) & 31)
    if bits:
        values.append((acc << (5 - bits)) & 31)
    hrp = "suiprivkey"

    def polymod(items):
        generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
        check = 1
        for item in items:
            top = check >> 25
            check = (check & 0x1ffffff) << 5 ^ item
            for i in range(5):
                check ^= generator[i] if (top >> i) & 1 else 0
        return check
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    mod = polymod(expanded + values + [0] * 6) ^ 1
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(charset[v] for v in values + checksum)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallet", required=True, help="client.yaml of the funded wallet to spend from")
    parser.add_argument("--network", default="testnet", choices=["testnet", "localnet"])
    parser.add_argument("--base-ref", default=DEFAULT_BASE, help=f"pre-upgrade contracts (default {DEFAULT_BASE})")
    parser.add_argument("--walrus", default="testnet", choices=["testnet", "local"])
    parser.add_argument("--max-spend-mist", type=int, default=600_000_000)
    parser.add_argument("--evidence", help="write evidence JSON here")
    try:
        run(parser.parse_args())
    except Spent as error:
        sys.exit(f"stopped: {error}")
