"""Replay public SF-221 effects without keys, network calls or testnet writes."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testnet_budget import Budget, Spent


class FreshCostTests(unittest.TestCase):
    def test_sf198_public_failure_uses_exact_distinct_compiler_fixture(self):
        evidence = Path(__file__).resolve().parents[2] / 'docs/validation'
        runs = json.loads((evidence / 'sf221-cost-investigation.json').read_text())['runs']
        run = next(r for r in runs if r['url'].endswith('/41392636950'))
        inventory = json.loads((evidence / 'sf221-bytecode-inventory.json').read_text())
        revisions = {r['ref']: r for r in inventory['revisions']}
        base, head = (revisions[run[k]] for k in ('base', 'head'))
        self.assertNotEqual(base['digest'], head['digest'])
        self.assertEqual(run['fixture'], 'bytecode-v1:' + base['digest'] +
                         '..bytecode-v1:' + head['digest'] + '@1.81.1-e04cf9aa915c')
        self.assertEqual(run['remaining_mist'], 24_090_518)
        self.assertEqual(run['allowance_used_mist'], 1_575_909_482)
        for revision in (base, head):
            self.assertEqual(revision['root_bytes'], sum(m['bytes'] for m in revision['modules']))
            # Metadata removal cannot explain a saving: table 0x10 is absent
            # in the pinned compiler's actual root modules for this pair.
            self.assertTrue(all('16' not in m['table_bytes'] for m in revision['modules']))

    def test_real_local_head_batch_cannot_fit_even_with_free_identity_and_net_only_reservation(self):
        receipt = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-local-costs.json'
        local = json.loads(receipt.read_text())
        retained = next(r for r in local['runs'] if r['phase'] == 'pair-0-retained')
        batch = next(t for t in retained['transactions'] if t['label'] == 'create 3 deployments via head')
        self.assertEqual(batch['status'], 'success')
        self.assertEqual(batch['gas_mist'], 42_002_663)
        for run, ledger in self.ledgers():
            client = Mock()
            # This deliberately optimistic lower bound charges no identity gas
            # and no inbox. Even reserving only the measured net fee is too much.
            with self.assertRaises(Spent):
                ledger.execute(client, batch['label'], ['ptb'], gas=batch['gas_mist'], require_full=True)
            client.assert_not_called()
            self.assertEqual(ledger.used, run['allowance_used_mist'])

    def test_actual_cross_pair_reuse_saves_funding_without_erasing_the_grant(self):
        receipt = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-local-costs.json'
        runs = {r['phase']: r for r in json.loads(receipt.read_text())['runs']}
        fresh = runs['pair-1-fresh']
        self.assertEqual(fresh['preexisting_device_grant_mist'], 24_993_732)
        self.assertFalse(any(t['label'] == 'top up device gas' for t in fresh['transactions']))
        funding = next(t for t in runs['pair-0-retained']['transactions'] if t['label'] == 'top up device gas')
        self.assertEqual(funding['funding_mist'] + funding['gas_mist'] - fresh['preexisting_device_grant_mist'], 6_994_367)
        for phase in ('pair-0-retained', 'pair-1-fresh', 'pair-1-retained'):
            run = runs[phase]
            ledger = Budget(1_600_000_000, [])
            ledger.grant(run['preexisting_device_grant_mist'])
            for tx in run['transactions']:
                ledger.execute(None, tx['label'], lambda _, tx=tx: dict(digest=tx['digest'], effects={
                    'status': {'status': tx['status']}, 'gasUsed': tx['gas_used']}),
                    gas=tx['gas_budget_mist'], funding=tx['funding_mist'], require_full=True)
            self.assertEqual(ledger.used, run['allowance_used_mist'])

    def test_combined_fresh_and_retained_effects_do_not_establish_fresh_acceptance(self):
        path = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-combined-batch-costs.json'
        runs = {r['phase']: r for r in json.loads(path.read_text())['runs']}
        fresh, retained = runs['pair-0-fresh'], runs['pair-0-retained']
        self.assertFalse(fresh['success'])
        self.assertTrue(retained['success'])
        self.assertEqual((fresh['base'], fresh['head']), (retained['base'], retained['head']))
        # Both ledgers must replay their own actual reservations and effects.
        # Separate cached success must never certify the failed fresh ledger.
        for run in (fresh, retained):
            ledger = Budget(run['max_spend_mist'], [])
            for tx in run['transactions']:
                ledger.execute(None, tx['label'], lambda _, tx=tx: dict(
                    digest=tx['digest'], effects=dict(status=dict(status=tx['status']), gasUsed=tx['gas_used'])),
                    gas=tx['gas_budget_mist'], funding=tx['funding_mist'], require_full=True)
            self.assertEqual(ledger.used, run['allowance_used_mist'])
        total = fresh['allowance_used_mist'] + retained['allowance_used_mist']
        self.assertEqual(total - fresh['max_spend_mist'], 93_121_037)
        # A deliberately optimistic fee-only replay cannot repair this pair:
        # erase *all* computation and funding, but retain the existing grouping
        # and non-replenishing refund rule. This is not a bound on regrouping
        # cleanup with creation or on different compatible source code.
        storage = sum(max(0, int(t['gas_used']['storageCost']) - int(t['gas_used']['storageRebate']))
                      for r in (fresh, retained) for t in r['transactions'])
        self.assertEqual(storage, 1_640_251_037)
        self.assertGreater(storage, fresh['max_spend_mist'])

    def ledgers(self):
        receipt = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-cost-investigation.json'
        for run in json.loads(receipt.read_text())['runs']:
            ledger = Budget(run['max_spend_mist'], [])
            for tx in run['transactions']:
                effects = dict(status=dict(status=tx['status']), gasUsed=dict(
                    computationCost=str(tx['gas_mist']), storageCost='0', storageRebate='0'))
                ledger.execute(None, tx['label'], lambda reserved, tx=tx, effects=effects:
                               dict(digest=tx['digest'], effects=effects), gas=tx['gas_budget_mist'])
            self.assertEqual(ledger.used, run['allowance_used_mist'])
            self.assertEqual(ledger.remaining, run['remaining_mist'])
            yield run, ledger

    def test_measured_fresh_failure_is_allowance_not_wallet_balance(self):
        for run, ledger in self.ledgers():
            client = Mock()
            with self.assertRaisesRegex(Spent, 'before top up device gas'):
                ledger.execute(client, 'top up device gas', ['ptb'], gas=10_000_000,
                               funding=30_000_000, require_full=True)
            client.assert_not_called()
            self.assertEqual(ledger.used, run['allowance_used_mist'])
            if run['end_balance_mist'] is not None:
                self.assertGreater(run['end_balance_mist'], ledger.remaining)
                self.assertEqual(run['start_balance_mist'] - run['end_balance_mist'], run['spent_mist'])

    def test_reusing_funded_identity_does_not_remove_grant_or_full_budget_gate(self):
        for run, ledger in self.ledgers():
            # Synthetic reused balances bracket the measured remaining allowance.
            # No live funded identity or successful deployment is asserted here.
            with self.assertRaises(Spent):
                ledger.grant(30_000_000)
            ledger.grant(10_000_000)
            client = Mock()
            with self.assertRaisesRegex(Spent, 'full gas budget'):
                ledger.execute(client, 'required deployment batch', ['ptb'],
                               gas=200_000_000, require_full=True)
            client.assert_not_called()
            self.assertEqual(ledger.used, run['allowance_used_mist'] + 10_000_000)

    def test_cached_identity_gas_and_unknown_funding_remain_charged(self):
        ledger = Budget(1_600_000_000, [])
        ledger.grant(20_000_000)  # Synthetic cached account, no publication fees.
        client = Mock(side_effect=RuntimeError('unknown submission outcome'))
        with self.assertRaisesRegex(RuntimeError, 'unknown'):
            ledger.execute(client, 'top up', ['ptb'], gas=10_000_000, funding=30_000_000)
        self.assertEqual(ledger.used, 60_000_000)
        ledger.faucet(lambda: None)
        self.assertEqual(ledger.used, 60_000_000)
        self.assertEqual(ledger.evidence[-1]['status'], 'unknown')
