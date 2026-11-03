#!/usr/bin/env python3
"""SF-196: effects-checked existing-customer upgrade and policy boundaries.

Uses the shared verified package/PTB submitters and strict non-replenishing
ledger. Cache state is private, bytecode-keyed and reverified before reuse.
A pending/uncertain publication is never retried as a fresh publication.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

import deployment_manifest as dm
import sui_toolchain as tc
from live_common import Live, CLOCK
from live_package_upgrade import contracts_digest


def run(args):
    if args.network == "testnet" and not os.environ.get("SUIFLEET_TESTNET_LOCK_TOKEN"):
        raise RuntimeError("machine testnet acceptance requires network:suifleet-testnet lock")
    live = Live(args.wallet, args.network, args.max_spend_mist)
    live.preflight(auto_fund=args.auto_fund)
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Compilation is offline; publication compares these bytes before signing.
    base_digest = contracts_digest(Path(args.base))
    head_digest = contracts_digest(Path("contracts"))
    key = hashlib.sha256(json.dumps([live.chain, live.operator, base_digest, head_digest], sort_keys=True).encode()).hexdigest()
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    def save():
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        state_path.chmod(0o600)

    def checkpoint(identity):
        state["pending"]["verified_transaction"] = identity
        save()

    def transaction(label, *ptb, funding=0):
        return live.transaction(label, *ptb, budget=min(20_000_000, live.ledger.remaining - funding), funding=funding)

    live.evidence["success"] = False
    try:
        if state.get("pending"):
            raise RuntimeError("fixture has an uncertain mutation; reconcile its exact digest before retrying")
        fixture = state.setdefault("fixtures", {}).get(key)
        if fixture is None and args.repair_fixture:
            candidates = [f for f in state.get("fixtures", {}).values()
                          if f["base_digest"] == base_digest and not f.get("superseded_by")]
            assert len(candidates) == 1, "repair needs one exact original-base fixture"
            prior = candidates[0]
            cap = live.object(prior["cap"])
            assert dm.object_id(cap["owner"]["AddressOwner"]) == live.operator
            assert dm.object_id(cap["content"]["package"]) == prior["head"]
            assert int(cap["content"]["version"]) == prior.get("version", 2)
            facts = dm.chain_facts(live.rpc_url, prior["head"])
            assert dm.origins(facts["objects"][prior["head"]])["customer::Customer"] == prior["base"]
            state["pending"] = {"key": key, "operation": "repair latest fixture", "previous_head": prior["head"]}; save()
            tc.write_pubfile(state_path.with_suffix(".toml"), live.chain, Path("contracts"), prior["head"], prior["cap"],
                             version=prior.get("version", 2), original=prior["base"])
            result = live.publish("repair latest security fixture", Path("contracts"), state_path.with_suffix(".toml"),
                                  {"cap": prior["cap"], "package": prior["head"]}, persist=checkpoint)
            new_head = live.published(result)
            fixture = {**prior, "head": new_head, "head_digest": head_digest, "version": prior.get("version", 2) + 1,
                       "security_origin": prior.get("security_origin", prior["head"]),
                       "upgrade_path": prior.get("upgrade_path", [prior["base"], prior["head"]]) + [new_head]}
            prior["superseded_by"] = new_head
            state["fixtures"][key] = fixture; del state["pending"]; save()
            live.evidence["repair_previous_head"] = prior["head"]
        if fixture:
            cap = live.object(fixture["cap"])
            assert dm.object_id(cap["owner"]["AddressOwner"]) == live.operator
            assert dm.object_id(cap["content"]["package"]) == fixture["head"]
            assert int(cap["content"]["version"]) == fixture.get("version", 2)
            facts = dm.chain_facts(live.rpc_url, fixture["head"], fixture["customer"], fixture["registry"])
            origins = dm.origins(facts["objects"][fixture["head"]])
            assert origins["customer::Customer"] == fixture["base"]
            assert origins["security_policy::Policy"] == fixture.get("security_origin", fixture["head"])
            assert "Shared" in live.object(fixture["registry"])["owner"]
            live.check("cached package lineage, customer/registry and split origins reverified")
        else:
            # Each checkpoint remains reserved on uncertainty. Do not silently
            # republish identical bytecode or overwrite the ambiguous receipt.
            state["pending"] = {"key": key, "operation": "publish base"}; save()
            result = live.publish("security publish base", Path(args.base), state_path.with_suffix(".toml"), persist=checkpoint)
            base = live.published(result)
            registry = live.created(result, "::device_registry::DeviceRegistry")[0]
            cap = live.created(result, "::package::UpgradeCap")[0]
            state["pending"].update(base=base, registry=registry, cap=cap, digest=result["digest"]); save()
            result = transaction("create customer before upgrade", "--move-call", f"{base}::customer::create", f"@{registry}", '"SF196 isolated fixture"')
            customer = live.created(result, "::customer::Customer")[0]
            fleet = live.created(result, "::customer::Fleet")[0]
            budget = live.created(result, "::fleet_budget::FleetBudget")[0]
            state["pending"].update(customer=customer, fleet=fleet, budget=budget, operation="upgrade head"); save()
            tc.write_pubfile(state_path.with_suffix(".toml"), live.chain, Path("contracts"), base, cap)
            result = live.publish("security upgrade head", Path("contracts"), state_path.with_suffix(".toml"), {"cap": cap, "package": base}, persist=checkpoint)
            head = live.published(result)
            fixture = dict(base=base, head=head, registry=registry, cap=cap, customer=customer, fleet=fleet, budget=budget,
                           base_digest=base_digest, head_digest=head_digest)
            state["fixtures"][key] = fixture; del state["pending"]; save()
        base, head = fixture["base"], fixture["head"]
        c, f = fixture["customer"], fixture["fleet"]
        # Verify the lineage and type origins on every path before tenant writes.
        cap_info = live.object(fixture["cap"])
        assert dm.object_id(cap_info["owner"]["AddressOwner"]) == live.operator
        assert dm.object_id(cap_info["content"]["package"]) == head
        assert int(cap_info["content"]["version"]) == fixture.get("version", 2)
        facts = dm.chain_facts(live.rpc_url, head)
        origins = dm.origins(facts["objects"][head])
        assert origins["customer::Customer"] == base
        assert origins["security_policy::Action"] == fixture.get("security_origin", head)
        live.evidence["type_origins"] = origins
        # Idempotent, so cached migrated state remains usable.
        transaction("initialize existing customer policy", "--move-call", f"{head}::customer::initialize_security", f"@{c}")
        transaction("seal reconciled fleet", "--move-call", f"{head}::customer::seal_security_fleet", f"@{c}", f"@{f}")
        for function, arguments in [
            ("set_credential", [f"@{c}", f"@{live.operator}", "true", "true"]),
            ("rename_fleet", [f"@{c}", f"@{f}", '"old-package bypass"']),
            ("borrow_budget_cap", [f"@{c}", f"@{f}"]),
        ]:
            ptb = ["--move-call", f"{base}::customer::{function}", *arguments]
            if function == "borrow_budget_cap":
                ptb += ["--assign", "loan", "--move-call", f"{base}::customer::return_budget_cap", f"@{f}", "loan.0", "loan.1"]
            live.attack("old package " + function, *ptb, module="customer", function="assert_member", code=0, budget=min(20_000_000, live.ledger.remaining))
        for previous in fixture.get("upgrade_path", [])[1:-1]:
            live.attack("intermediate package cannot alter administrators", "--move-call", f"{previous}::customer::set_credential",
                        f"@{c}", f"@{live.operator}", "true", "true", module="customer", function="assert_member", code=0,
                        budget=min(20_000_000, live.ledger.remaining))
        live.attack("unimplemented factor cannot activate", "--move-call", f"{head}::customer::set_security", f"@{c}", "vector[4u8,0u8,0u8,0u8,0u8,0u8,0u8]",
                    module="security_policy", function="update", code=2, budget=min(20_000_000, live.ledger.remaining))
        transaction("current all-off administration", "--move-call", f"{head}::customer::rename_fleet", f"@{c}", f"@{f}", '"SF196 verified fleet"')
        result = transaction("create disposable catalog", "--move-call", f"{head}::component_catalog::create_catalog")
        catalog = live.created(result, "::component_catalog::ComponentCatalog")[0]
        transaction("customer takes publisher custody", "--move-call", f"{head}::customer::adopt_catalog", f"@{c}", f"@{catalog}")
        live.attack("old catalog publisher excluded", "--move-call", f"{base}::component_catalog::publish_version", f"@{catalog}", '"app"', '"1.0.0"',
                    'vector["linux"]', 'vector["x86_64"]', 'vector["blob"]', 'vector["' + "ab" * 32 + '"]', 'vector["' + "cd" * 32 + '"]', CLOCK,
                    module="component_catalog", function="publish_version", code=0, budget=min(20_000_000, live.ledger.remaining))
        transaction("customer publishes authenticated version", "--move-call", f"{head}::customer::publish_version", f"@{c}", f"@{catalog}", '"app"', '"1.0.0"',
                    'vector["linux"]', 'vector["x86_64"]', 'vector["blob"]', 'vector["' + "ab" * 32 + '"]', 'vector["' + "cd" * 32 + '"]', CLOCK)
        transaction("fund isolated activity budget", "--split-coins", "gas", "[1000000]", "--assign", "funds",
                    "--move-call", f"{head}::customer::borrow_budget_cap", f"@{c}", f"@{f}", "--assign", "loan",
                    "--move-call", f"{head}::fleet_budget::deposit", f"@{fixture['budget']}", "loan.0", "funds.0",
                    "--move-call", f"{head}::customer::return_budget_cap", f"@{f}", "loan.0", "loan.1", funding=1_000_000)
        transaction("scoped activity withdrawal", "--move-call", f"{head}::customer::withdraw_budget", f"@{c}", f"@{f}", f"@{fixture['budget']}", "1000000", f"@{live.operator}")
        live.check("existing-object migration, three old-customer routes, publisher exclusion and current publication effects")
        live.evidence["fixture"] = fixture
        live.evidence["success"] = True
    finally:
        live.finish(Path(args.evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--network", choices=["testnet", "localnet"], required=True)
    parser.add_argument("--base", required=True, help="exported pre-framework contracts directory")
    parser.add_argument("--state", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--repair-fixture", action="store_true", help="upgrade only the verified latest fixture; record the full original-base lineage")
    parser.add_argument("--auto-fund", action="store_true", help="at most one protected testnet faucet request through the shared ledger")
    parser.add_argument("--max-spend-mist", type=int, default=1_600_000_000)
    run(parser.parse_args())
