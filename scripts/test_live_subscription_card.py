"""Offline no-replay checks: no wallet or network tool may run on refusal."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import live_subscription_card as card


class RecoveryRefusals(unittest.TestCase):
    def refuse(self, state=None, flag=None):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'purchase'
            directory.mkdir()
            if state is not None:
                state.setdefault('limit_mist', 100_000_000)
                (directory / 'state.json').write_text(json.dumps(state))
            args = ['live_subscription_card.py', '--wallet', '/unused/client.yaml', '--manifest', '/unused/manifest.json', '--offer', 'unused', '--private-dir', str(directory), '--evidence', str(Path(tmp)/'evidence.json')]
            if flag:
                args.append(flag)
            with patch.dict(os.environ, {'SUIFLEET_TESTNET_LOCK_TOKEN': 'offline-fixture'}), patch('sys.argv', args), patch.object(card, 'Live') as live:
                with self.assertRaises((AssertionError, KeyError, IndexError)):
                    card.main()
                live.assert_not_called()
            self.assertFalse((Path(tmp)/'evidence.json').exists())

    def test_existing_directory_never_replays(self):
        self.refuse()

    def test_unknown_funding_cannot_resume(self):
        self.refuse({'allowance_used_mist': 55_000_000, 'child_transactions': [], 'transactions': [{'status': 'unknown'}]}, '--resume-unfunded')

    def test_submitted_prepay_cannot_resume(self):
        self.refuse({'customer': 'customer', 'purchase': {'saved': True}, 'child_transactions': [{'status': 'success', 'label': 'SF-183 frontend prepay after simulated arrival'}]}, '--resume-ready')

    def test_completed_plan_cannot_reopen(self):
        self.refuse({'customer': 'customer', 'purchase': {'started': True}, 'account': {'id': 'paid-account'}, 'completed': True}, '--resume-ready')

    def test_successful_creation_cannot_repeat(self):
        self.refuse({'customer': 'already-created', 'child_transactions': [{'status': 'success'}]}, '--resume-failed-create')


if __name__ == '__main__':
    unittest.main()
