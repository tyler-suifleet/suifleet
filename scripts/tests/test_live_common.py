"""Offline spend-accounting and fixture-reuse checks for the SF-010 live suites."""
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_common
from live_common import Live
from live_upgrade_security import fixture_valid
from testnet_budget import Spent

OPERATOR = "0x" + "0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965"
ABORT = ("MoveAbort(MoveLocation { module: ModuleId { address: ab, name: Identifier(\"device_cap\") }, function: 3, "
         "instruction: 9, function_name: Some(\"assert_signer\") }, 0) in command 0")


def effects(status="success", cost=1_000_000, storage=0, rebate=0, error=None, digest="Dig1111111111111111111111111111111111111111"):
    state = {"status": status, **({"error": error} if error else {})}
    return {"digest": digest, "objectChanges": [], "effects": {"status": state, "gasUsed": {
        "computationCost": str(cost), "storageCost": str(storage), "storageRebate": str(rebate)}}}


class Chain:
    """Scripted `sui client` responses; records every command and config."""

    def __init__(self, *responses, tx_blocks=None):
        self.responses, self.tx_blocks, self.calls = list(responses), tx_blocks or {}, []

    def __call__(self, *command, json_output=True, check=True, config=None):
        self.calls.append((command, config))
        if command[0] == "tx-block":
            return self.tx_blocks.get(command[1], CompletedProcess([], 1, "", "not found"))
        return self.responses.pop(0)


def live(chain, limit=100_000_000):
    run = Live("/nonexistent/client.yaml", "testnet", limit)
    run.client = chain
    return run


def submitted_budget(chain):
    command, _ = chain.calls[0]
    return int(command[command.index("--gas-budget") + 1])


class LiveSpendTests(unittest.TestCase):
    def test_reserves_only_the_remaining_allowance(self):
        chain = Chain(effects(cost=1_000_000))
        run = live(chain)
        run.ledger.used = 90_000_000
        run.transaction("near the ceiling", "--move-call", "x")
        self.assertEqual(submitted_budget(chain), 10_000_000)  # Not the default 200,000,000.
        self.assertEqual(run.ledger.used, 91_000_000)

    def test_exhausted_allowance_never_submits(self):
        for used in (99_000_000, 100_000_000):
            chain = Chain()
            run = live(chain)
            run.ledger.used = used
            with self.assertRaises(Spent):
                run.transaction("over the ceiling", "--move-call", "x")
            with self.assertRaises(Spent):
                run.publish("publish", "/tmp/package", "/tmp/pub.toml")
            self.assertEqual(chain.calls, [])

    def test_rebates_never_replenish(self):
        chain = Chain(effects(cost=1_000_000, rebate=500_000_000), effects(cost=1_000_000))
        run = live(chain)
        run.transaction("delete with a large rebate", "--move-call", "x")
        self.assertEqual(run.ledger.used, 0)  # Net cost clamps at zero, never below.
        run.transaction("next", "--move-call", "x")
        self.assertEqual(run.ledger.remaining, 99_000_000)

    def test_failed_execution_is_charged_and_read_back_by_digest(self):
        digest = "Fazz111111111111111111111111111111111111111"
        failed = effects("failure", cost=2_000_000, error="InsufficientGas", digest=digest)
        chain = Chain(CompletedProcess([], 1, "", f"Error executing transaction '{digest}': failure"), tx_blocks={digest: failed})
        run = live(chain)
        with self.assertRaises(RuntimeError):
            run.transaction("fails on chain", "--move-call", "x")
        self.assertEqual(run.ledger.used, 2_000_000)
        self.assertEqual(run.evidence["transactions"][-1]["status"], "failure")

    def test_unknown_outcome_keeps_the_full_reservation(self):
        chain = Chain(CompletedProcess([], 1, "", "connection reset"))
        run = live(chain)
        with self.assertRaises(RuntimeError):
            run.transaction("unknown", "--move-call", "x", budget=30_000_000)
        self.assertEqual(run.ledger.used, 30_000_000)
        self.assertEqual(run.evidence["transactions"][-1]["status"], "unknown")

    def test_attack_charges_gas_and_reads_the_abort_from_effects(self):
        digest = "Abrt111111111111111111111111111111111111111"
        chain = Chain(CompletedProcess([], 1, "", f"transaction '{digest}' failed"),
                      tx_blocks={digest: effects("failure", cost=1_500_000, error=ABORT, digest=digest)})
        run = live(chain)
        entry = run.attack("impersonation", "--move-call", "x", module="device_cap", function="assert_signer", code=0)
        self.assertEqual(entry["abort"], ("device_cap", "assert_signer", "0"))
        self.assertEqual(entry["gas_mist"], 1_500_000)
        self.assertEqual(run.ledger.used, 1_500_000)

    def test_attack_that_succeeds_or_aborts_differently_fails(self):
        run = live(Chain(effects("success")))
        with self.assertRaises(RuntimeError):
            run.attack("should abort", "--move-call", "x", module="device_cap", function="assert_signer", code=0)
        run = live(Chain(effects("failure", error=ABORT.replace("}, 0)", "}, 1)"))))
        with self.assertRaises(AssertionError):
            run.attack("wrong code", "--move-call", "x", module="device_cap", function="assert_signer", code=0)

    def test_child_account_funding_is_charged_and_bounds_its_gas(self):
        chain = Chain(effects(cost=1_000_000), effects(cost=1_000_000))
        run = live(chain)
        run.transaction("fund the device", "--split-coins", "gas", "[40000000]", funding=40_000_000, budget=10_000_000)
        self.assertEqual(run.ledger.used, 41_000_000)
        device = run.child("/tmp/device.yaml", 40_000_000)
        run.transaction("device report", "--move-call", "x", budget=100_000_000, sender=device)
        command, config = chain.calls[1]
        self.assertEqual(config, "/tmp/device.yaml")
        self.assertEqual(int(command[command.index("--gas-budget") + 1]), 40_000_000)  # Capped by the child's funding.
        self.assertEqual(run.ledger.used, 41_000_000)  # Already charged as funding.
        device[1].used = 39_000_000
        with self.assertRaises(Spent):
            run.transaction("device over its funding", "--move-call", "x", sender=device)

    def test_funding_beyond_the_allowance_is_refused(self):
        chain = Chain()
        run = live(chain, limit=50_000_000)
        with self.assertRaises(Spent):
            run.transaction("fund", "--split-coins", "gas", "[100000000]", funding=100_000_000)
        self.assertEqual(chain.calls, [])

    def test_mutations_assert_the_testnet_lock_when_held(self):
        chain = Chain(effects())
        run = live(chain)
        with patch.dict("os.environ", {"SUIFLEET_TESTNET_LOCK_TOKEN": "t"}), \
                patch.object(live_common.subprocess, "run") as lock:
            run.transaction("locked", "--move-call", "x")
        self.assertIn("lock-assert", lock.call_args.args[0])


class SecurityFixtureTests(unittest.TestCase):
    FIXTURE = {"upgrade_cap": "0xc", "registry": "0xr", "head_package": "0x" + "2" * 64}

    def reads(self, owner=OPERATOR, package=None, version="2", registry_owner=None):
        objects = {"0xc": {"owner": {"AddressOwner": owner}, "content": {"package": package or self.FIXTURE["head_package"],
                                                                        "version": version}},
                   "0xr": {"owner": registry_owner or {"Shared": {"initial_shared_version": 3}}},
                   self.FIXTURE["head_package"]: {}}
        run = live(Chain())
        run.operator = OPERATOR

        def read(object_id):
            if object_id not in objects:
                raise RuntimeError("missing")
            return json.loads(json.dumps(objects[object_id]))
        run.object = read
        return run

    def test_valid_lineage_is_reused(self):
        self.assertTrue(fixture_valid(self.reads(), self.FIXTURE))

    def test_changed_lineage_or_ownership_is_not_reused(self):
        self.assertFalse(fixture_valid(self.reads(version="3"), self.FIXTURE))  # Upgraded again on top.
        self.assertFalse(fixture_valid(self.reads(package="0x" + "9" * 64), self.FIXTURE))
        self.assertFalse(fixture_valid(self.reads(owner="0x" + "1" * 64), self.FIXTURE))
        self.assertFalse(fixture_valid(self.reads(registry_owner={"AddressOwner": OPERATOR}), self.FIXTURE))
        self.assertFalse(fixture_valid(self.reads(), {**self.FIXTURE, "head_package": "0x" + "4" * 64}))


if __name__ == "__main__":
    unittest.main()
