"""Identity reuse must not reuse package acceptance or erase uncertain spend."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_package_upgrade import funded_identity, load_state, DEVICE_FUNDING, DEVICE_MAX_REUSE
from testnet_budget import Budget, Spent


class IdentityReuseTests(unittest.TestCase):
    def state(self):
        return dict(chain='4c78adac', operator='lane-0', fixtures={
            'exact-base..head-one': dict(device_address='device-one', device_key='test-key-one'),
            'exact-base..head-two': dict(device_address='device-two', device_key='test-key-two')},
            publications={'another-pair': {'pending_operation': {'digest': 'retained-unknown'}}},
            quarantine={'historical': {'status': 'unresolved', 'reserved_mist': 400_000_000}})

    def test_reuse_smallest_sufficient_funded_identity_without_changing_pairs_or_fences(self):
        state = self.state()
        before = copy.deepcopy(state)
        balances = {'device-one': 24_000_000, 'device-two': 18_000_000}
        identity = funded_identity(state, balances.__getitem__)
        self.assertEqual(identity, dict(device_address='device-two', device_key='test-key-two'))
        self.assertEqual(state, before)
        ledger = Budget(1_600_000_000, [])
        ledger.grant(balances[identity['device_address']])
        self.assertEqual(ledger.used, 18_000_000)

    def test_unfunded_and_excessive_grants_are_not_selected(self):
        state = self.state()
        self.assertIsNone(funded_identity(state, lambda _: 9_999_999))
        self.assertIsNone(funded_identity(state, lambda _: DEVICE_MAX_REUSE + 1))

    def test_failed_balance_read_is_not_a_fresh_identity_fallback(self):
        balance = Mock(side_effect=RuntimeError('unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            funded_identity(self.state(), balance)

    def test_conflicting_cached_identity_keys_fail_closed(self):
        state = self.state()
        state['fixtures']['exact-base..head-two']['device_address'] = 'device-one'
        with self.assertRaisesRegex(RuntimeError, 'conflicting keys'):
            funded_identity(state, lambda _: 20_000_000)

    def test_other_lane_or_chain_has_no_reusable_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            path.write_text(json.dumps(self.state()))
            for chain, operator in [('4c78adac', 'lane-1'), ('other-chain', 'lane-0')]:
                state = load_state(path, chain, operator)
                self.assertIsNone(funded_identity(state, lambda _: self.fail('cross-lane balance read')))

    def test_exact_target_funding_keeps_existing_grant_and_unknown_full_reservation(self):
        evidence = []
        ledger = Budget(1_600_000_000, evidence)
        existing = 8_000_000
        ledger.grant(existing)
        with self.assertRaisesRegex(RuntimeError, 'unknown'):
            ledger.execute(Mock(side_effect=RuntimeError('unknown')), 'top up device gas', ['ptb'],
                gas=10_000_000, funding=DEVICE_FUNDING - existing, require_full=True)
        self.assertEqual(ledger.used, DEVICE_FUNDING + 10_000_000)
        self.assertEqual(evidence[-1]['status'], 'unknown')
        ledger.faucet(lambda: None)
        self.assertEqual(ledger.used, DEVICE_FUNDING + 10_000_000)

    def test_reuse_is_not_permission_to_exceed_actual_fresh_remainder(self):
        ledger = Budget(1_600_000_000, [])
        ledger.grant(1_575_058_682)
        ledger.grant(18_000_000)
        client = Mock()
        with self.assertRaises(Spent):
            ledger.execute(client, 'required head deployment', ['ptb'], gas=60_000_000, require_full=True)
        client.assert_not_called()
