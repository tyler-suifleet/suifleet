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
    ('security_policy', 'check', 1, 'k6'),
    ('customer', 'assert_member', 0, 'z1'),
    ('customer', 'retire_pending_device', 1, 'z0'),
    ('customer', 'assert_reporter', 1, 'z0'),
    ('customer', 'borrow_budget_cap', 1, 'z0'),
    ('customer', 'adopt_group_members', 1, 'z0'),
    ('customer', 'advance_rollout_onchain', 1, 'z0'),
    ('customer', 'assert_group', 1, 'z0'),
    ('customer', 'enroll_checked', 2, 'z3'),
    ('security_policy', 'begin_scoped_bytes', 0, 'z0'),
    ('fleet_budget', 'draw_gas', 8, 'q'),
    ('deployment_manager', 'receipts_mut', 5, 'q'),
    ('component_catalog', 'publish_version', 0, 'z2'),
    ('security_policy', 'begin_scoped_bytes', 3, 'z1'),
    ('customer', 'approve_action', 9, 'z2'),
    ('multisig_admin', 'approve', 1, 'g0'),
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
            ('handover', 5, 'q'), ('delete_as', 5, 'q'),
            ('receipt_status', 5, 'q'), ('finish_receipt_migration', 15, 'q'),
            ('record_sharded', 14, 'q'), ('finish_receipt_cleanup', 17, 'q'),
            ('set_control', 2, 'q'),
        ]:
            with self.subTest(function=function):
                self.assertEqual(abort_site(ROOT / 'contracts', 'deployment_manager', function, code), expected)

    def test_handover_and_group_authority_sites(self):
        self.assertEqual(abort_site(ROOT / 'contracts', 'fleet_handover', 'confirm', 0), 'g0')
        self.assertEqual(abort_site(ROOT / 'contracts', 'device_group', 'remove_device', 0), 'q')

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

    def test_selector_guard_requires_the_matching_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / 'sources').mkdir()
            source = """const EBad: u64 = 0; const BBad: u8 = 0;
            const EOther: u64 = 1; const BOther: u8 = 1;
            fun action() { q(nested(false), BBad) }
            fun q(condition: bool, code: u8) {
                if (!condition) { if (code == BBad) abort EBad; abort EOther }
            }"""
            (package / 'sources/m.move').write_text(source)
            self.assertEqual(abort_site(package, 'm', 'action', 0), 'q')
            with self.assertRaises(ValueError):
                abort_site(package, 'm', 'action', 1)
            (package / 'sources/m.move').write_text(source.replace('nested(false), BBad', 'nested(false), BOther'))
            self.assertEqual(abort_site(package, 'm', 'action', 1), 'q')
            with self.assertRaises(ValueError):
                abort_site(package, 'm', 'action', 0)

    def test_javascript_uses_the_same_selector(self):
        result = subprocess.check_output(['node', '-e', "console.log(require('./scripts/abort_site.cjs').abortSite('contracts','deployment_manager','receipts_mut',5))"], cwd=ROOT, text=True)
        self.assertEqual(result.strip(), 'q')
