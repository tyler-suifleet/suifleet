#!/usr/bin/env python3
"""SF-187 cached-fixture acceptance. No publication or existing-customer edits.

Caller must hold network:suifleet-testnet and supply SUIFLEET_TESTNET_LOCK_TOKEN.
A fresh run-owned customer exercises zero/grant/boundary/raise/removal. Its empty
customer/default fleet remain (there is no customer deletion API); device objects
are removed and remaining child gas is returned. Failed runs retain their private
runtime directory for explicit recovery, never print key material.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import deployment_manifest as dm
from live_common import Live, plain


def run(args):
    os.umask(0o077)
    assert os.environ.get("SUIFLEET_TESTNET_LOCK_TOKEN"), "a held mutation lock is required"
    live = Live(args.wallet, "testnet", 80_000_000)
    live.preflight()
    package, registry, cap = map(dm.object_id, (args.package, args.registry, args.upgrade_cap))
    facts = dm.chain_facts(live.rpc_url, package, cap, registry)
    assert dm.upgrade_cap(facts["objects"][cap])[:2] == (package, str(facts["objects"][package]["version"]))
    assert live.owner(cap) == live.operator
    assert plain(live.object(registry)["content"])["admin"] == live.operator
    origins = dm.origins(facts["objects"][package])
    assert "customer::Customer" in origins and "customer::Fleet" in origins
    manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url, package=package,
        upgrade_cap=cap, registry=registry, network="testnet", publisher="https://publisher.walrus-testnet.walrus.space",
        aggregator="https://aggregator.walrus-testnet.walrus.space"))
    live.evidence.update(package=package, registry=registry, customer_origin=origins["customer::Customer"])
    root = Path(tempfile.mkdtemp(prefix="sf187-customer-"))
    success = False
    try:
        config = root / "client.yaml"
        (root / "sui.keystore").write_text("[]")
        config.write_text(f"keystore:\n  File: {root}/sui.keystore\nenvs:\n- alias: testnet\n  rpc: {live.rpc_url}\nactive_env: testnet\nactive_address: null\n")
        generated = subprocess.run([live.network_sui, "client", "--client.config", str(config), "new-address", "ed25519", "--json"], capture_output=True, text=True, timeout=60)
        assert generated.returncode == 0, "private test identity creation failed (output suppressed)"
        address = json.loads(generated.stdout[generated.stdout.index("{"):])["address"]
        live.client("switch", "--address", address, config=config)
        live.transaction("fund run-owned customer", "--split-coins", "gas", "[70000000]", "--assign", "fund", "--transfer-objects", "[fund]", f"@{address}", budget=5_000_000, funding=70_000_000)
        child = live.child(config, 70_000_000)
        call = lambda fn: f"{package}::customer::{fn}"
        result = live.transaction("create fresh zero-limit customer", "--move-call", call("create"), f"@{registry}", '"SF-187 acceptance"', budget=40_000_000, sender=child)
        customer = live.created(result, "::customer::Customer")[0]
        fleet = live.created(result, "::customer::Fleet")[0]
        budget = live.created(result, "::fleet_budget::FleetBudget")[0]
        live.evidence.update(customer=customer, fleet=fleet, budget=budget)
        def console_capacity(full):
            subprocess.run(["node", "scripts/check_customer_capacity.cjs"], input=json.dumps(dict(manifest=manifest, customerId=customer, expectFull=full)), text=True, check=True, timeout=120)
        console_capacity(True)
        def register(device_address):
            return ["--move-call", call("register_device"), f"@{customer}", f"@{fleet}", f"@{budget}", f"@{registry}", '"SF-187 test device"', f"@{device_address}", '"aarch64"', "@0x6"]
        live.attack("zero limit refuses first registration", *register(address), module="customer", function="enroll_checked", code=2, budget=5_000_000, sender=child)
        live.transaction("registry grants one device", "--move-call", call("set_entitlement"), f"@{customer}", f"@{registry}", "1", "0", "0", budget=5_000_000)
        console_capacity(False)
        result = live.transaction("first device at granted limit", *register(address), budget=min(40_000_000, child[1].remaining), sender=child)
        devices = [(live.created(result, "::device_registry::Device")[0], live.created(result, "::fleet_budget::GasAccount")[0])]
        console_capacity(True)
        other = "0x" + os.urandom(32).hex()
        live.attack("limit boundary refuses second registration", *register(other), module="customer", function="enroll_checked", code=2, budget=5_000_000, sender=child)
        live.transaction("registry raises device limit", "--move-call", call("set_entitlement"), f"@{customer}", f"@{registry}", "2", "0", "0", budget=5_000_000)
        console_capacity(False)
        state = plain(live.object(customer)["content"])
        assert (int(state["active_devices"]), int(state["device_limit"])) == (1, 2)
        live.check("real testnet zero-limit refusal, registry grant, boundary refusal and raised-grant console capacity")
        for device, account in devices:
            live.transaction("remove run-owned test device", "--move-call", call("deregister_device"), f"@{customer}", f"@{fleet}", f"@{budget}", f"@{account}", f"@{registry}", f"@{device}", budget=5_000_000, sender=child)
        assert int(plain(live.object(customer)["content"])["active_devices"]) == 0
        live.execute("return run-owned customer gas", ["ptb", "--transfer-objects", "[gas]", f"@{live.operator}"], gas=5_000_000, sender=child)
        live.check("test devices removed; zero active devices; child gas returned; empty customer/fleet retained")
        success = True
    finally:
        live.finish(args.evidence)
        if success:
            shutil.rmtree(root)
        else:
            print(f"Acceptance incomplete; private run identity retained for explicit recovery at {root}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("wallet", "package", "registry", "upgrade-cap", "evidence"):
        parser.add_argument("--" + name, required=True)
    run(parser.parse_args())
