"""Setup batching must not reinterpret a retained transaction after recovery."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment_manifest as dm
import live_package_upgrade as live
import testnet_reconcile as rc


class ActivationRecovery(unittest.TestCase):
    def plan(self, origins=None, pending=None):
        return live.activation_plan('0x1', '0x2', '0x3', '0x4', '0x5',
                                    dict.fromkeys(dm.INBOX_TYPES, '0x1') if origins is None else origins, pending)

    def test_legacy_pending_keeps_original_single_call_even_when_head_supports_batch(self):
        legacy = 'activate identity in registry custody'
        for pending in (legacy, {'label': legacy, 'tx': None}):
            label, calls, ptb, batched = self.plan(pending=pending)
            self.assertEqual(label, legacy)
            self.assertFalse(batched)
            self.assertEqual(calls, ['0x1::device_registry::activate_device'])
            self.assertEqual(ptb.count('--move-call'), 1)

    def test_new_pending_cannot_fall_back_to_legacy_when_api_is_missing(self):
        label, calls, ptb, batched = self.plan()
        self.assertTrue(batched)
        self.assertEqual(self.plan(pending={'label': label, 'tx': None}), (label, calls, ptb, batched))
        for missing in dm.INBOX_TYPES:
            origins = dict.fromkeys((name for name in dm.INBOX_TYPES if name != missing), '0x1')
            with self.assertRaises(rc.Uncertain):
                self.plan(origins=origins, pending={'label': label, 'tx': None})
            self.assertFalse(self.plan(origins=origins)[3])

    def test_unrelated_pending_operation_is_never_replaced(self):
        with self.assertRaises(rc.Uncertain):
            self.plan(pending={'label': 'upgrade to head', 'tx': None})


if __name__ == '__main__':
    unittest.main()
