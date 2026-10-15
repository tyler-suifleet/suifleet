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


