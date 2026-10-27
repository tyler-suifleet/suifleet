"""SF-173 evaluation-fleet deployment: run each step at most once, reuse
completed steps only after re-verification, and stop on unknown outcomes."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_evaluation_fleet as fleet

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = "0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965"


def effects(digest, status="success"):
    return {"digest": digest, "effects": {"status": {"status": status},
            "gasUsed": {"computationCost": "1000", "storageCost": "5000", "storageRebate": "2000"}}}


class FakeLive:
    def __init__(self, wallet, operator=OPERATOR, executed=None):
        self.wallet, self.operator, self.chain = wallet, operator, "4c78adac"
        self.evidence, self.checks, self._executed = {}, [], executed or {}

    def executed(self, digest):
        return self._executed.get(digest, {})

    def check(self, label):
        self.checks.append(label)


class EvaluationFleetSteps(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        self.profile = self.temp / "profile"
        self.profile.mkdir()
        # The base manifest and Seal settings of the real evaluation profile;
        # the tool never writes the profile before every step succeeds.
        base = json.loads((ROOT / "profiles/seal-testnet/deployment.json").read_text())
        (self.profile / "deployment.json").write_text(json.dumps(base))
        shutil.copy(ROOT / "profiles/seal-testnet/seal.json", self.profile / "seal.json")
        self.registry = base["shared_objects"]["registry"]["id"]
        (self.temp / "wallet").mkdir()
        self.wallet = self.temp / "wallet/client.yaml"

    def deployment(self, live=None, **args):
        live = live or FakeLive(self.wallet)
        options = SimpleNamespace(profile=self.profile, dry_run=False, adopt={})
        vars(options).update(args)
        return fleet.Deployment(live, options), live

    def state(self):
        return json.loads((self.wallet.parent / fleet.STATE).read_text())

    def test_a_step_runs_once_and_is_reused_after_verification(self):
        calls, verified = [], []
        d, live = self.deployment()
        run = lambda: calls.append(1) or effects("D1")
        done = d.step("budget:x", run, lambda r: {"budget": "0xb"}, verified.append)
        self.assertEqual(done, {"budget": "0xb", "digest": "D1", "net_gas_mist": 4000})
        self.assertEqual(calls, [1])
        saved = next(iter(self.state().values()))
        self.assertEqual(saved["pending"], {})
        self.assertEqual(saved["steps"]["budget:x"]["digest"], "D1")
        again, _ = self.deployment()
        self.assertEqual(again.step("budget:x", run, lambda r: {}, verified.append), done)
        self.assertEqual(calls, [1], "a completed step must never run again")
        self.assertEqual(len(verified), 2, "reuse must re-verify on chain first")

    def test_a_failed_reverification_refuses_reuse(self):
        d, _ = self.deployment()
        d.step("storage:x", lambda: effects("D2"), lambda r: {"storage": "0xs"}, lambda s: None)
        again, _ = self.deployment()

        def gone(step):
            raise AssertionError("0xs is not shared")
        with self.assertRaises(AssertionError):
            again.step("storage:x", lambda: self.fail("must not rerun"), lambda r: {}, gone)

    def test_an_interrupted_step_stays_pending_and_is_never_resubmitted(self):
        d, _ = self.deployment()

        def lost():
            raise RuntimeError("DeadlineExceeded after submission")
        with self.assertRaises(RuntimeError):
            d.step("core:k", lost, lambda r: {}, lambda s: None)
        self.assertIn("core:k", next(iter(self.state().values()))["pending"])
        again, _ = self.deployment()
        with self.assertRaises(fleet.Reconcile):
            again.step("core:k", lambda: self.fail("an unknown outcome must not be resubmitted"), lambda r: {}, lambda s: None)

    def test_adopt_reads_executed_effects_instead_of_resubmitting(self):
        d, _ = self.deployment()
        with self.assertRaises(RuntimeError):
            d.step("core:k", lambda: (_ for _ in ()).throw(RuntimeError("timeout")), lambda r: {}, lambda s: None)
        d.fleet["pending"]["core:k"]["digest"] = "GOOD"
        d.save()
        live = FakeLive(self.wallet, executed={"GOOD": effects("GOOD"), "BAD": effects("BAD", "failure")})
        failed, _ = self.deployment(live, adopt={"core:k": "BAD"})
        with self.assertRaises(fleet.Reconcile):
            failed.step("core:k", lambda: self.fail("no resubmission"), lambda r: {"package": "0xp"}, lambda s: None)
        adopted, live = self.deployment(live, adopt={"core:k": "GOOD"})
        done = adopted.step("core:k", lambda: self.fail("no resubmission"), lambda r: {"package": "0xp"}, lambda s: None)
        self.assertEqual(done["digest"], "GOOD")
        self.assertEqual(live.evidence["adopted"], {"core:k": "GOOD"})
        self.assertEqual(next(iter(self.state().values()))["pending"], {})

    def test_dry_run_plans_without_running_or_writing_state(self):
        d, live = self.deployment(dry_run=True)
        self.assertIsNone(d.step("budget:x", lambda: self.fail("dry run must not execute"), lambda r: {}, lambda s: None))
        self.assertEqual(live.evidence["planned"], ["budget:x"])
        self.assertFalse((self.wallet.parent / fleet.STATE).exists())

    def test_pre_sign_refusal_can_retry_without_adopting_an_unrelated_transaction(self):
        d, _ = self.deployment()
        def refused():
            raise fleet.toolchain.NotSubmitted("compiler refused before signing")
        with self.assertRaises(fleet.toolchain.NotSubmitted):
            d.step("storage:x", refused, lambda r: {}, lambda s: None)
        self.assertEqual(next(iter(self.state().values()))["pending"], {})
        again, _ = self.deployment()
        done = again.step("storage:x", lambda: effects("D3"), lambda r: {}, lambda s: None)
        self.assertEqual(done["digest"], "D3")

    def test_submission_identity_is_durable_and_cannot_be_replaced(self):
        d, _ = self.deployment()
        def lost():
            d.record_submission("EXPECTED")
            with self.assertRaises(fleet.Reconcile):
                d.record_submission("UNRELATED")
            raise RuntimeError("connection lost after submission")
        with self.assertRaises(RuntimeError):
            d.step("core:k", lost, lambda r: {}, lambda s: None)
        pending = next(iter(self.state().values()))["pending"]["core:k"]
        self.assertEqual(pending["digest"], "EXPECTED")
        self.assertIsNone(d.active_step)
        again, _ = self.deployment(FakeLive(self.wallet, executed={"UNRELATED": effects("UNRELATED")}),
                                  adopt={"core:k": "UNRELATED"})
        with self.assertRaises(fleet.Reconcile):
            again.step("core:k", lambda: self.fail("never replay"), lambda r: {}, lambda s: None)

    def test_failed_checkpoint_replace_preserves_previous_intent(self):
        d, _ = self.deployment()
        with self.assertRaises(RuntimeError):
            d.step("core:k", lambda: (_ for _ in ()).throw(RuntimeError("unknown")), lambda r: {}, lambda s: None)
        before = (self.wallet.parent / fleet.STATE).read_bytes()
        d.fleet["pending"].clear()
        with patch.object(fleet.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                d.save()
        self.assertEqual((self.wallet.parent / fleet.STATE).read_bytes(), before)

    def test_public_record_restores_cache_only_for_the_same_fleet(self):
        step = {"package": "0xp", "version": "2", "digest": "D"}
        record = {"chain": "4c78adac", "operator": OPERATOR, "registry": self.registry, "steps": {"core:k": step}}
        (self.profile / "evaluation-fleet.json").write_text(json.dumps(record))
        d, _ = self.deployment()
        self.assertEqual(d.fleet["steps"], {"core:k": step})
        other, _ = self.deployment(FakeLive(self.wallet, operator="0x" + "1" * 64))
        self.assertEqual(other.fleet["steps"], {}, "another wallet's record must not be adopted")

    def test_state_is_private_and_namespaced_by_chain_operator_and_registry(self):
        d, _ = self.deployment()
        d.step("budget:x", lambda: effects("D1"), lambda r: {}, lambda s: None)
        path = self.wallet.parent / fleet.STATE
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(json.loads(path.read_text())), [f"4c78adac:{OPERATOR}:{self.registry}"])

    def test_core_key_binds_sources_and_compiler(self):
        self.assertNotEqual(fleet.core_key("1.81.1-a"), fleet.core_key("1.82.1-b"))
        self.assertTrue(fleet.core_key("1.81.1-a").endswith("@1.81.1-a"))

    def test_mutations_refuse_before_any_wallet_access_without_the_lock(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(fleet, "EvaluationLive") as wallet:
            os.environ.pop("SUIFLEET_TESTNET_LOCK_TOKEN", None)
            with self.assertRaises(SystemExit):
                fleet.main(["--wallet", str(self.wallet)])
            wallet.assert_not_called()


if __name__ == "__main__":
    unittest.main()
