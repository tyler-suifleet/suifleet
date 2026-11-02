"""Replay public SF-221 effects without keys, network calls or testnet writes."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testnet_budget import Budget, Spent


class FreshCostTests(unittest.TestCase):
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
