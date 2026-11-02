"""Replay executed SF-221 cold/max-grant effects with the production ledger."""
import argparse
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testnet_budget import Budget, Spent
from test_fresh_cost_localnet import retained_grant
import live_package_upgrade as upgrade


class OrderedGuardCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.receipt = json.loads((Path(__file__).resolve().parents[2] /
            'docs/validation/sf221-ordered-guard-costs.json').read_text())

    def runs(self, case):
        return {run['phase']: run for run in self.receipt['cases'][case]['runs']}

    def replay(self, run):
        ledger = Budget(run['max_spend_mist'], [])
        grant_at = run['preexisting_device_grant_before_transaction_index']
        for index, tx in enumerate(run['transactions']):
            if index == grant_at:
                ledger.grant(run['preexisting_device_grant_mist'])
            effects = dict(status=dict(status=tx['status']), gasUsed=tx['gas_used'])
            ledger.execute(None, tx['label'], lambda _, tx=tx, effects=effects:
                dict(digest=tx['digest'], effects=effects), gas=tx['gas_budget_mist'],
                funding=tx['funding_mist'], require_full=True)
            self.assertEqual(ledger.used, tx['allowance_used_mist'], tx['label'])
        self.assertEqual(ledger.used, run['allowance_used_mist'])
        return ledger

    def test_cold_and_largest_retained_grant_complete_full_reservations(self):
        self.assertTrue(self.receipt['local_acceptance_complete'])
        self.assertFalse(self.receipt['acceptance_complete'])
        self.assertEqual(self.receipt['source']['bytes'], 85448)
        for case in ('cold', 'max_retained'):
            evidence = self.receipt['cases'][case]
            self.assertEqual(evidence['harness_exit_code'], 0)
            self.assertTrue(all(x['success'] for x in evidence['outcomes']))
            runs = self.runs(case)
            for phase in ('pair-0-fresh', 'pair-0-retained'):
                run = runs[phase]
                self.assertTrue(run['success'])
                ledger = self.replay(run)
                self.assertEqual(ledger.limit, 1_600_000_000)
                for tx in run['transactions']:
                    if tx['label'].startswith('create deployment '):
                        self.assertEqual(tx['gas_budget_mist'],
                            15_000_000 if 'via base' in tx['label'] else 10_000_000)
                    if tx['label'].startswith('create and admit'):
                        self.assertEqual(tx['gas_budget_mist'], 11_000_000)
            self.assertEqual(runs['pair-0-fresh']['preexisting_device_grant_mist'],
                             30_000_000 if case == 'max_retained' else 0)

    def test_actual_near_miss_still_refuses_full_audit_reservation(self):
        case = self.receipt['cases']['failed_max_retained']
        self.assertEqual(case['harness_exit_code'], 1)
        runs = self.runs('failed_max_retained')
        fresh = runs['pair-0-fresh']
        self.assertFalse(fresh['success'])
        self.assertTrue(runs['pair-0-retained']['success'])
        ledger = self.replay(fresh)
        self.assertEqual(ledger.used, 1_589_409_011)
        submit = Mock()
        with self.assertRaises(Spent):
            ledger.execute(None, 'create and admit audit', submit,
                           gas=11_000_000, require_full=True)
        submit.assert_not_called()
        self.assertEqual(ledger.remaining, 10_590_989)
        # The successful cached result never credits or resets the failed ledger.
        self.replay(runs['pair-0-retained'])
        self.assertEqual(ledger.used, 1_589_409_011)

    def test_authenticated_maximum_identity_is_fully_charged_and_pair_reused(self):
        runs = self.runs('max_retained')
        prep, fresh, cached = (runs[p] for p in
            ('prior-identity', 'pair-0-fresh', 'pair-0-retained'))
        self.assertEqual(prep['retained_grant_mist'], upgrade.DEVICE_MAX_REUSE)
        self.assertEqual(len(prep['ciphertext_sha256']), 64)
        self.assertEqual(fresh['preexisting_device_grant_mist'], prep['retained_grant_mist'])
        self.assertTrue(any('authenticated same-lane' in x for x in fresh['checks']))
        self.assertEqual(fresh['fixture'], cached['fixture'])
        self.assertEqual(fresh['type_origins'], cached['type_origins'])
        self.assertFalse(any(t['label'] == 'top up device gas' for t in fresh['transactions']))
        self.assertFalse(any('publish' in t['label'] or 'upgrade' in t['label']
                             for t in cached['transactions']))
        self.assertEqual(retained_grant(str(upgrade.DEVICE_MAX_REUSE)), 30_000_000)
        for amount in (upgrade.DEVICE_MINIMUM - 1, upgrade.DEVICE_MAX_REUSE + 1):
            with self.assertRaises(argparse.ArgumentTypeError):
                retained_grant(str(amount))

    def test_actual_pruning_and_retirement_cannot_replenish_ordinary(self):
        runs = self.runs('max_retained')
        temporal = runs['temporal']
        retained, = temporal['retained_audits']
        self.assertTrue(temporal['success'])
        self.assertGreater(int(retained['proof']['lowest']), int(retained['checkpoint']))
        self.assertTrue(retained['proof']['audit']['complete'])
        tx, = temporal['transactions']
        self.assertEqual(tx['digest'], retained['cleanup_digest'])
        self.assertEqual(tx['gas_budget_mist'], 20_000_000)
        self.assertEqual(tx['gas_mist'], 0)
        ledger = self.replay(runs['pair-0-fresh'])
        before = ledger.used
        with self.assertRaises(Spent):
            ledger.execute(None, 'retire audit', Mock(), gas=20_000_000, require_full=True)
        self.assertEqual(ledger.used, before)


if __name__ == '__main__':
    unittest.main()
