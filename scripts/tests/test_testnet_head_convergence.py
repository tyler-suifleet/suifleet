"""Deterministic journal/receipt ordering regressions; no chain writes or sleeps."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_package_upgrade as live


class HeadConvergence(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = Path(self.directory.name) / 'state.json'
        self.expected = {'0x1': {'0x4'}, '0x2': {'0x3'}}

    def write(self, inbox='0x4', group='0x3', extra=False):
        components = {'app': inbox}
        if extra:
            components['unexpected'] = '0x9'
        self.journal.write_text(json.dumps({'heads': {'seen': {
            '0x1': {'seq': 2, 'components': components},
            '0x2': {'seq': 1, 'components': {'app': group}},
        }}}))

    def test_receipt_can_precede_persisted_head(self):
        # The failed run had the first direct deployment still in the journal
        # even though the fourth deployment's Failed receipt was on chain.
        self.write(inbox='0x5')
        with self.assertRaisesRegex(AssertionError, 'did not converge'):
            with patch.object(live.time, 'monotonic', side_effect=[0, 0, 2]), patch.object(live.time, 'sleep'):
                live.wait_for_announced_heads(self.journal, self.expected, timeout=1)
        with patch.object(live.time, 'sleep', side_effect=lambda _: self.write()) as sleep:
            live.wait_for_announced_heads(self.journal, self.expected)
        sleep.assert_called_once_with(1)

    def test_exact_heads_pass_without_waiting(self):
        self.write()
        with patch.object(live.time, 'sleep') as sleep:
            live.wait_for_announced_heads(self.journal, self.expected)
        sleep.assert_not_called()

    def test_stale_inbox_group_and_extra_component_fail_with_diagnostics(self):
        for options in ({'inbox': '0x5'}, {'group': '0x6'}, {'extra': True}):
            with self.subTest(options=options):
                self.write(**options)
                with patch.object(live.time, 'monotonic', side_effect=[0, 0, 2]), patch.object(live.time, 'sleep'):
                    with self.assertRaisesRegex(AssertionError, 'expected .*observed'):
                        live.wait_for_announced_heads(self.journal, self.expected, timeout=1)

    def test_daemon_exit_fails_promptly(self):
        self.write(inbox='0x5')
        process = Mock(returncode=7)
        process.poll.return_value = 7
        with patch.object(live.time, 'sleep') as sleep:
            with self.assertRaisesRegex(AssertionError, r'daemon exited \(7\)'):
                live.wait_for_announced_heads(self.journal, self.expected, process)
        sleep.assert_not_called()

    def test_malformed_journal_is_not_hidden_as_eventual_consistency(self):
        self.journal.write_text('{}')
        with self.assertRaises(KeyError):
            live.wait_for_announced_heads(self.journal, self.expected)


if __name__ == '__main__':
    unittest.main()
