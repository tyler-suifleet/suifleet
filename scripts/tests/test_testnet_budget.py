import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
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


class LivePublicationBudgetTests(unittest.TestCase):
    def test_large_upgrade_uses_remaining_ceiling_and_settles_actual_effects(self):
        from live_common import Live
        live = Live.__new__(Live)
        live.ledger = Budget(700_000_000, [])
        live.ledger.grant(135_436_148)
        live.evidence = {}
        live.operator, live.build_sui, live.network_sui, live.wallet = "0xa", "sui", "sui", Path("client.yaml")
        live.assert_lock = Mock()
        live.executor = Mock(return_value=Mock())
        # Run 40212571323 exhausted the old 400M transaction budget. A
        # successful head upgrade costs 533M, within the unchanged suite cap.
        observed = []
        def submit(gas):
            observed.append(gas)
            return effect(cost=533_318_963)
        with patch("live_common.toolchain.package_submitter", return_value=submit):
            live.publish("upgrade to head", Path("package"), Path("pubfile"),
                         upgrade={"cap": "0xb", "package": "0xc"})
        self.assertEqual(observed, [564_563_852])
        self.assertEqual(live.ledger.used, 668_755_111)
        self.assertEqual(live.ledger.remaining, 31_244_889)
        self.assertEqual(live.ledger.limit, 700_000_000)
        live.assert_lock.assert_called_once()

    def test_upgrade_cannot_submit_when_allowance_exhausted(self):
        from live_common import Live
        live = Live.__new__(Live)
        live.ledger = Budget(700_000_000, [])
        live.ledger.grant(699_000_000)
        live.evidence = {}
        live.operator, live.build_sui, live.network_sui, live.wallet = "0xa", "sui", "sui", Path("client.yaml")
        live.assert_lock = Mock()
        live.executor = Mock(return_value=Mock())
        submit = Mock()
        with patch("live_common.toolchain.package_submitter", return_value=submit):
            with self.assertRaises(Spent):
                live.publish("upgrade to head", Path("package"), Path("pubfile"),
                             upgrade={"cap": "0xb", "package": "0xc"})
        submit.assert_not_called()
        self.assertEqual(live.ledger.used, 699_000_000)


class LiveTransactionBudgetTests(unittest.TestCase):
    def live(self, used):
        from live_common import Live
        live = Live.__new__(Live)
        live.ledger = Budget(700_000_000, [])
        live.ledger.grant(used)
        live.assert_lock = Mock()
        live.executor = Mock(return_value=Mock(return_value=effect(cost=25_000_000)))
        return live

    def test_security_deployment_refuses_clipped_gas_before_submission(self):
        # Run 40220107253 had already spent at least 668,755,111 MIST
        # after its large upgrade, before the post-activation deployment.
        live = self.live(668_755_111)
        with self.assertRaisesRegex(Spent, 'full gas budget'):
            live.transaction('deployment via head (after activation)', '--move-call', 'deployment')
        live.executor.return_value.assert_not_called()
        self.assertEqual(live.ledger.used, 668_755_111)
        self.assertEqual(live.ledger.evidence, [])

    def test_cached_security_fixture_submits_and_settles_full_budget(self):
        live = self.live(60_000_000)
        live.transaction('deployment via head (after activation)', '--move-call', 'deployment')
        self.assertEqual(live.executor.return_value.call_args.args[-2:], ('--gas-budget', '200000000'))
        self.assertEqual(live.ledger.used, 85_000_000)

    def test_device_budget_cannot_be_clipped_or_charged_to_operator(self):
        live = self.live(60_000_000)
        child = Budget(40_000_000, [])
        child.grant(31_000_000)
        with self.assertRaises(Spent):
            live.transaction('device report', '--move-call', 'report', budget=10_000_000,
                             sender=('device-config', child))
        live.executor.return_value.assert_not_called()
        self.assertEqual(child.used, 31_000_000)
        self.assertEqual(live.ledger.used, 60_000_000)
