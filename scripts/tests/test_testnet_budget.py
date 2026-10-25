import sys
from pathlib import Path
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testnet_budget import Budget, Spent

def effect(status='success', cost=1_000_000, rebate=0):
    return {'digest': 'digest', 'effects': {'status': {'status': status}, 'gasUsed': {
        'computationCost': str(cost), 'storageCost': '0', 'storageRebate': str(rebate)}}}


class BudgetTests(unittest.TestCase):
    def test_tiny_cap_never_invokes_publish_upgrade_or_cleanup(self):
        for command in ('test-publish', 'test-upgrade', 'ptb'):
            client = Mock()
            with self.assertRaises(Spent):
                Budget(1, []).execute(client, command, [command])
            client.assert_not_called()

    def test_bounded_budget_and_funding(self):
        budget = Budget(10_000_000, [])
        client = Mock(return_value=effect())
        budget.execute(client, 'fund', ['ptb'], funding=3_000_000)
        self.assertEqual(client.call_args.args[-1], '7000000')
        self.assertEqual(budget.used, 4_000_000)
        with self.assertRaises(Spent):
            budget.grant(6_000_001)

    def test_failures_and_unknown_outcome_consume_allowance(self):
        rows = []
        budget = Budget(10_000_000, rows)
        with self.assertRaises(RuntimeError):
            budget.execute(Mock(return_value=effect('failure')), 'failed cleanup', ['ptb'])
        self.assertEqual(budget.used, 1_000_000)
        self.assertEqual(rows[0]['status'], 'failure')
        with self.assertRaises(TimeoutError):
            budget.execute(Mock(side_effect=TimeoutError()), 'publish unknown', ['test-publish'])
        self.assertEqual(budget.remaining, 0)
        with self.assertRaises(Spent):
            budget.execute(Mock(), 'cleanup', ['ptb'])

    def test_rebate_or_faucet_cannot_replenish(self):
        budget = Budget(10_000_000, [])
        budget.grant(3_000_000)
        budget.execute(Mock(return_value=effect(rebate=100_000_000)), 'delete', ['ptb'])
        self.assertEqual(budget.remaining, 7_000_000)
        self.assertFalse(hasattr(budget, 'balance'))



    def test_faucet_is_bounded_and_cannot_reset_allowance(self):
        budget = Budget(10_000_000, [])
        budget.grant(3_000_000)
        budget.faucet(Mock(return_value='credited'))
        self.assertEqual(budget.remaining, 7_000_000)
        second = Mock()
        with self.assertRaises(Spent):
            budget.faucet(second)
        second.assert_not_called()
        self.assertEqual(budget.remaining, 7_000_000)

    def test_exhausted_fresh_fixture_never_submits_clipped_deployment_batch(self):
        # Exact allowance state before run 40206962805's InsufficientGas.
        rows = []
        budget = Budget(1_000_000_000, rows)
        budget.grant(979_149_743)
        client = Mock()
        with self.assertRaisesRegex(Spent, 'full gas budget'):
            budget.execute(client, 'create 3 deployments via head', ['ptb'],
                           gas=200_000_000, require_full=True)
        client.assert_not_called()
        self.assertEqual(budget.used, 979_149_743)
        self.assertEqual(rows, [])

    def test_cached_fixture_can_submit_full_deployment_budget_and_settle(self):
        budget = Budget(1_000_000_000, [])
        budget.grant(30_000_000)
        client = Mock(return_value=effect(cost=25_000_000))
        budget.execute(client, 'create deployments', ['ptb'], gas=200_000_000,
                       require_full=True)
        self.assertEqual(client.call_args.args[-1], '200000000')
        self.assertEqual(budget.used, 55_000_000)

    def test_full_budget_reserves_funding_without_replenishment(self):
        client = Mock()
        budget = Budget(200_000_000, [])
        with self.assertRaises(Spent):
            budget.execute(client, 'fund', ['ptb'], gas=200_000_000,
                           funding=1, require_full=True)
        client.assert_not_called()
        self.assertEqual(budget.used, 0)
