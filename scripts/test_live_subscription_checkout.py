"""Offline refusal regressions: no wallet, network or tracker mutations."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import live_subscription_checkout as checkout


class CheckoutPreflight(unittest.TestCase):
    def run_case(self, *, state=None, inspect=False, reason='Renewal requires 30-day notice'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger, evidence, manifest = (root / n for n in ('state', 'evidence', 'manifest'))
            if state is not None:
                ledger.write_text(json.dumps(state))
            m = json.loads((checkout.ROOT / 'docs/validation/sf181-testnet-current-manifest.json').read_text())
            manifest.write_text(json.dumps(m))
            live = MagicMock(chain='4c78adac', operator=checkout.AGENT)
            live.owner.return_value = checkout.AGENT
            live.object.return_value = {'content': {'admin': checkout.AGENT}}
            discovery = dict(customer='existing', existing={'account': {}}, action='renew',
                             reason=reason, eligible_at_utc='2026-12-04T01:30:04.331Z')
            argv = ['checkout', '--wallet', 'unused', '--manifest', str(manifest), '--state', str(ledger),
                    '--evidence', str(evidence), '--offer', 'explicit-offer'] + (['--inspect-only'] if inspect else [])
            with patch('sys.argv', argv), patch.dict('os.environ', {'SUIFLEET_TESTNET_LOCK_TOKEN': 'offline'}), \
                 patch.object(checkout, 'Live', return_value=live), patch.object(checkout.dm, 'verify'), \
                 patch.object(checkout.subprocess, 'check_output', return_value=json.dumps(discovery)):
                if inspect:
                    checkout.main()
                else:
                    with self.assertRaises(RuntimeError):
                        checkout.main()
            live.transaction.assert_not_called()
            live.execute.assert_not_called()
            live.signer.assert_not_called()
            if state is not None:
                self.assertEqual(json.loads(ledger.read_text()), state)
            return json.loads(evidence.read_text()) if evidence.exists() else None

    def test_explicit_customer_requires_recorded_ready_isolated_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(json.dumps({'transactions': [], 'allowance_used_mist': 5952761}))
            argv = ['checkout', '--wallet', 'unused', '--manifest', 'unused',
                    '--state', str(state), '--evidence', 'unused',
                    '--customer', '0x1', '--offer', '0x2']
            with patch('sys.argv', argv), patch.dict('os.environ', {'SUIFLEET_TESTNET_LOCK_TOKEN': 'offline'}), patch.object(checkout, 'Live') as live:
                with self.assertRaisesRegex(RuntimeError, 'reconciled isolated fixture'):
                    checkout.main()
                live.assert_not_called()

    def test_bound_customer_stops_without_setup_or_payment(self):
        result = self.run_case()
        self.assertEqual(result['transactions_submitted'], 0)
        self.assertEqual(result['fixture_preflight']['eligible_at_utc'], '2026-12-04T01:30:04.331Z')

    def test_inspection_preserves_prior_charges(self):
        result = self.run_case(inspect=True, state={'transactions': [], 'allowance_used_mist': 5952761})
        self.assertEqual(result['prior_allowance_used_mist'], 5952761)

    def test_unknown_digest_requires_reconciliation(self):
        self.assertIsNone(self.run_case(state={'transactions': [], 'pending_digest': 'unknown'}))

    def test_started_checkout_cannot_repeat_payment(self):
        self.assertIsNone(self.run_case(state={'transactions': [], 'prepay_started': True}))

    def test_unknown_transaction_cannot_reset_allowance(self):
        self.assertIsNone(self.run_case(state={'transactions': [{'status': 'unknown'}]}))


if __name__ == '__main__':
    unittest.main()
