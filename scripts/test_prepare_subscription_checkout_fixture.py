"""No mutation/replay may happen before accepting the retained fixture ledger."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import prepare_subscription_checkout_fixture as fixture


class IsolatedFixtureRefusal(unittest.TestCase):
    def run_refusal(self, state):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'; path.write_text(json.dumps(state))
            before = path.read_bytes()
            argv = ['prepare', '--wallet', 'unused', '--state', str(path), '--manifest', 'unused', '--private-dir', str(Path(d)/'identity')]
            with patch('sys.argv', argv), patch.dict('os.environ', {'SUIFLEET_TESTNET_LOCK_TOKEN': 'offline'}), patch.object(fixture, 'Live') as live:
                with self.assertRaises(AssertionError): fixture.main()
                live.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((Path(d)/'identity').exists())

    def test_existing_or_interrupted_fixture_never_replayed(self):
        self.run_refusal(dict(limit_mist=100000000, isolated_fixture={'pending_digest': 'unknown'}))

    def test_unknown_prior_digest_never_replayed(self):
        self.run_refusal(dict(limit_mist=100000000, pending_digest='unknown', transactions=[]))

    def test_changed_allowance_refused_before_network_or_key_creation(self):
        self.run_refusal(dict(limit_mist=200000000, transactions=[]))

if __name__ == '__main__': unittest.main()
