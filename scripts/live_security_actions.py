#!/usr/bin/env python3
"""Positive/tampered/cancelled production Action acceptance on the cached fixture."""
import argparse
import json
import os
from pathlib import Path
import subprocess

import deployment_manifest as dm
from live_common import Live, CLOCK


def run(args):
    if not os.environ.get("SUIFLEET_TESTNET_LOCK_TOKEN"):
        raise RuntimeError("requires network:suifleet-testnet lock")
    live = Live(args.wallet, "testnet", 100_000_000)
    live.preflight(auto_fund=False)
    receipt = json.loads(Path(args.fixture_receipt).read_text())
    assert receipt["success"] and receipt["chain"] == live.chain and receipt["operator"] == live.operator
    f = receipt["fixture"]
    cap = live.object(f["cap"])
    assert dm.object_id(cap["owner"]["AddressOwner"]) == live.operator
    assert dm.object_id(cap["content"]["package"]) == f["head"]
    assert int(cap["content"]["version"]) == f["version"]
    c, fleet, head = f["customer"], f["fleet"], f["head"]
    live.evidence.update(success=False, fixture=f)

    def transaction(label, *ptb):
        return live.transaction(label, *ptb, budget=20_000_000)

    def prepare(name):
        raw = subprocess.check_output(["node", "scripts/security_action_fixture.cjs", live.rpc_url,
                                       c, fleet, receipt["type_origins"]["customer::Customer"], name], text=True)
        arguments = json.loads(raw)
        vector = "vector[" + ",".join(str(b) + "u8" for b in arguments) + "]"
        expiry = int(live.object("0x6")["content"]["timestamp_ms"]) + 600_000
        result = transaction("prepare canonical action: " + name, "--move-call", f"{head}::security_policy::action_intent",
                             f"@{c}", vector, "--assign", "intent", "--move-call", f"{head}::customer::prepare_security_action",
                             f"@{c}", "intent", str(expiry) + "u64", CLOCK)
        return live.created(result, "::security_policy::Action")[0]

    def deleted(result, action):
        assert any(change["type"] == "deleted" and change["objectId"] == action for change in result["objectChanges"])

    try:
        name = "SF196 intent-bound fleet"
        action = prepare(name)
        result = transaction("execute exact single-use action", "--move-call", f"{head}::customer::rename_fleet_secured",
                             f"@{c}", f"@{fleet}", json.dumps(name), f"@{action}", CLOCK)
        deleted(result, action)
        assert live.object(fleet)["content"]["name"] == name
        live.check("canonical SDK object BCS accepted; executed Action nonce deleted")
        pending = prepare("SF196 approved name")
        live.attack("substituted arguments rejected on chain", "--move-call", f"{head}::customer::rename_fleet_secured",
                    f"@{c}", f"@{fleet}", json.dumps("unauthorized replacement"), f"@{pending}", CLOCK,
                    module="security_policy", function="begin_scoped_bytes", code=0, budget=20_000_000)
        assert live.object(fleet)["content"]["name"] == name
        result = transaction("cancel retained action after failed substitution", "--move-call",
                             f"{head}::customer::cancel_security_action", f"@{c}", f"@{pending}")
        deleted(result, pending)
        live.check("argument substitution leaves fleet unchanged; rollback retains Action for authorized cancellation; nonce deleted")
        live.evidence["success"] = True
    finally:
        live.finish(Path(args.evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--fixture-receipt", required=True)
    parser.add_argument("--evidence", required=True)
    run(parser.parse_args())
