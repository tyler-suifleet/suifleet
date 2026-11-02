"""No predecessor is recycled before its authoritative acceptance completes."""
import sys
from pathlib import Path
import unittest
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_package_upgrade as live


class LifecycleGrouping(unittest.TestCase):
    def execute(self, fail=None, compact=False):
        self.cleanup, self.evidence, self.events, self.budgets = [], {}, [], []
        cases = [('base', '3', {}, 1), ('head', '1', {}, 1), ('head', '2', {}, 1), ('head', '4', {}, 2)]
        def transaction(label, *args, **kwargs):
            self.budgets.append(kwargs)
            self.events.append(('submit', args))
            if fail == ('submit', len(self.cleanup)):
                raise RuntimeError('unknown outcome')
            return str(len([e for e in self.events if e[0] == 'submit']))
        def settled(record, status):
            self.events.append(('settled', record, status))
            if fail == ('settled', record):
                raise RuntimeError('missing authoritative receipt')
        def proof(record):
            self.events.append(('proof', record))
            if fail == ('proof', record):
                raise RuntimeError('independent effects unavailable')
            return [{'deployment': record}]
        return live.completed_deployment_cases(cases, head='head',
            call=lambda package, version, **_: ['--move-call', package+'::create', version],
            transaction=transaction, created=lambda made, _: [made], settled=settled,
            receipts=lambda record: {record: 'verified'}, proof=proof,
            cleanup=self.cleanup, evidence=self.evidence,
            compact=compact, read_version=lambda record: "wrong" if fail == ("version", record) else {"1": "3", "2": "1", "3": "2", "4": "4"}[record])

    def test_all_four_receipts_proved_before_recycling_and_no_mixed_package_calls(self):
        self.assertEqual(self.execute(), {'3': '1', '1': '2', '2': '3', '4': '4'})
        self.assertEqual(self.cleanup, ['4'])
        self.assertEqual(len(self.evidence['receipts']), 4)
        self.assertEqual(len(self.evidence['report_transactions']), 4)
        for index, event in enumerate(self.events):
            if event[0] != 'submit':
                continue
            args = event[1]
            if index == 0:
                self.assertEqual(args, ('--move-call', 'base::create', '3'))
            else:
                self.assertEqual(self.events[index-1][0], 'proof')
                self.assertEqual(args[-2], 'head::deployment_manager::delete_deployment')
                self.assertNotIn('base::create', args)
        self.assertIn(('settled', '4', 2), self.events)

    def test_compact_profile_cannot_override_a_lower_configured_cap(self):
        with self.assertRaisesRegex(ValueError, 'at least 15000000'):
            live.run(SimpleNamespace(compact_lifecycle=True, transaction_gas_budget=14_999_999))

    def test_compact_profile_explicitly_reserves_first_and_successor_budgets(self):
        self.execute(compact=True)
        self.assertEqual(self.budgets, [{'budget': 15_000_000}] + [{'budget': 10_000_000}] * 3)
        self.execute()
        self.assertEqual(self.budgets, [{}] * 4)

    def test_missing_receipt_or_effects_preserves_current_object_and_stops(self):
        for phase in ('version', 'settled', 'proof'):
            with self.subTest(phase=phase), self.assertRaises(RuntimeError):
                self.execute((phase, '2'))
            self.assertEqual(self.cleanup, ['2'])
            self.assertEqual(sum(e[0] == 'submit' for e in self.events), 2)

    def test_unknown_successor_keeps_predecessor_for_bounded_cleanup(self):
        with self.assertRaisesRegex(RuntimeError, 'unknown'):
            self.execute(('submit', 1))
        self.assertEqual(self.cleanup, ['1'])
        self.assertEqual(len(self.evidence['report_transactions']), 1)


if __name__ == '__main__':
    unittest.main()
