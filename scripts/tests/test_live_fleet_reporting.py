"""Fail-closed bounded Testnet resumption; no network or wallet mutations."""
import copy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_fleet_reporting as target
from testnet_budget import Budget

class ReportingRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base/'sf247-reporting-fixture'; self.root.mkdir()
        self.prior = dict(chain='4c78adac', operator='agent', max_spend_mist=400,
            allowance_used_mist=184, start_balance_mist=1000, completed=False,
            runtime=str(self.root), transactions=[dict(label='setup',status='success',digest='known'),
                dict(label='register device in first managed fleet',status='failure',digest='failed')])
        self.live = SimpleNamespace(chain='4c78adac',operator='agent', evidence={'transactions':[]},
            executed=Mock(return_value={'effects':{'status':{'status':'failure','error':'InsufficientGas'}}}),
            created=Mock(return_value=[]))
        self.live.ledger = Budget(400,self.live.evidence['transactions'])

    def test_preserves_spend_and_adopts_existing_identity(self):
        root=target.restore_run(self.live,self.prior,400,self.base)
        self.assertEqual(root,self.root)
        self.assertEqual(self.live.ledger.used,184)
        self.assertEqual(self.live.ledger.remaining,216)
        self.assertEqual(self.live.start_balance,1000)
        self.assertEqual(self.live.evidence['transactions'],self.prior['transactions'])
        self.live.executed.assert_called_once_with('failed')

    def test_rejects_changed_authority_ceiling_and_completed_run(self):
        for changes in [dict(chain='mainnet'),dict(operator='other'),dict(max_spend_mist=401),dict(completed=True)]:
            with self.subTest(changes=changes),self.assertRaises(AssertionError):
                target.restore_run(self.live,{**self.prior,**changes},400,self.base)
        self.assertEqual(self.live.ledger.used,0)
        self.live.executed.assert_not_called()

    def test_unknown_outcome_and_unrelated_failure_never_replay(self):
        for change in [dict(status='unknown'),dict(label='publish a different package')]:
            prior=copy.deepcopy(self.prior);prior['transactions'][-1].update(change)
            with self.subTest(change=change),self.assertRaises(AssertionError):
                target.restore_run(self.live,prior,400,self.base)
        self.assertEqual(self.live.ledger.used,0)

    def test_rechecks_failed_effect_and_rejects_created_device(self):
        self.live.executed.return_value={'effects':{'status':{'status':'success'}}}
        with self.assertRaises(AssertionError):target.restore_run(self.live,self.prior,400,self.base)
        self.live.executed.return_value={'effects':{'status':{'status':'failure','error':'InsufficientGas'}}}
        self.live.created.return_value=['device']
        with self.assertRaises(AssertionError):target.restore_run(self.live,self.prior,400,self.base)
        self.assertEqual(self.live.ledger.used,0)

    def test_rejects_runtime_outside_private_root(self):
        with self.assertRaises(AssertionError):target.restore_run(self.live,self.prior,400,self.base/'other')
        self.assertEqual(self.live.ledger.used,0)

    def test_invalid_lock_stops_before_wallet_preflight(self):
        with patch.dict(os.environ,{'SUIFLEET_TESTNET_LOCK_TOKEN':'fixture-only'}),patch.object(target,'Live',autospec=True) as factory:
            factory.return_value.assert_lock.side_effect=RuntimeError('invalid lease')
            with self.assertRaises(RuntimeError):target.run(SimpleNamespace(wallet=Path('/unused'),max_spend_mist=400,resume_evidence=None))
            factory.return_value.preflight.assert_not_called()
