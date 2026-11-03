"""Refusals select one exact site for inline and compacted package versions."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from abort_site import abort_site

ROOT = Path(__file__).resolve().parents[2]
SITES = [
    ('customer', 'assert_member', 0, 'pd'),
    ('customer', 'retire_pending_device', 1, 'pe'),
    ('customer', 'assert_reporter', 1, 'pe'),
    ('customer', 'borrow_budget_cap', 1, 'pe'),
    ('customer', 'adopt_group_members', 1, 'pe'),
    ('customer', 'advance_rollout_onchain', 1, 'pe'),
    ('customer', 'assert_group', 1, 'pe'),
    ('customer', 'enroll_checked', 2, 'pf'),
    ('security_policy', 'begin_scoped_bytes', 0, 'p1'),
    ('fleet_budget', 'draw_gas', 8, 'g2'),
    ('deployment_manager', 'receipts_mut', 5, 'pl'),
    ('component_catalog', 'publish_version', 0, 'g0'),
    ('security_policy', 'begin_scoped_bytes', 3, 'g0'),
    ('customer', 'approve_action', 9, 'g1'),
    ('multisig_admin', 'approve', 1, 'g1'),
]


class AbortSiteTests(unittest.TestCase):
    def test_current_and_inline_guard_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / 'sources').mkdir()
            for module, function, code, guard in SITES:
                with self.subTest(module=module, function=function):
                    # Historical inline form, independent of clone depth. Real
                    # older packages are also exercised by the localnet suites.
                    old = f'const ERefused: u64 = {code}; fun {function}() {{ assert!(false, ERefused); }}'
                    (package / 'sources' / (module + '.move')).write_text(old)
                    self.assertEqual(abort_site(package, module, function, code), function)
                    self.assertEqual(abort_site(ROOT / 'contracts', module, function, code), guard)

    def test_forwarded_and_renamed_receipt_sites(self):
        for function, code, expected in [
            ('handover', 5, 'pl'), ('delete_as', 5, 'pl'),
            ('receipt_status', 5, 'pl'), ('finish_receipt_migration', 15, 'g2'),
            ('record_sharded', 14, 'po'), ('finish_receipt_cleanup', 17, 'pg'),
            ('set_control', 2, 'pj'),
        ]:
            with self.subTest(function=function):
                self.assertEqual(abort_site(ROOT / 'contracts', 'deployment_manager', function, code), expected)

    def test_handover_and_group_authority_sites(self):
        self.assertEqual(abort_site(ROOT / 'contracts', 'fleet_handover', 'confirm', 0), 'p2')
        self.assertEqual(abort_site(ROOT / 'contracts', 'device_group', 'remove_device', 0), 'p0')

    def test_unrecognized_or_ambiguous_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / 'sources').mkdir()
            for source in [
                'const EBad: u8 = 1; fun action() { g(false) } fun g(condition: bool) { if (!condition) abort EBad }',
                'const EBad: u8 = 0; fun action() {}',
                'const EBad: u8 = 0; fun action() { other::g(false) } fun g(condition: bool) { if (!condition) abort EBad }',
                'const EBad: u8 = 0; fun action() { assert!(false, EBad); g(false) } fun g(condition: bool) { if (!condition) abort EBad }',
            ]:
                (package / 'sources/m.move').write_text(source)
                with self.subTest(source=source), self.assertRaises(ValueError):
                    abort_site(package, 'm', 'action', 0)

    def test_javascript_uses_the_same_selector(self):
        result = subprocess.check_output(['node', '-e', "console.log(require('./scripts/abort_site.cjs').abortSite('contracts','deployment_manager','receipts_mut',5))"], cwd=ROOT, text=True)
        self.assertEqual(result.strip(), 'pl')
