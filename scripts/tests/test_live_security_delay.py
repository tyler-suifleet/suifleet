"""Exact abort expectations survive compaction without accepting arbitrary failures."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_security_delay import policy_failure_function


class PolicyAbortLocationTests(unittest.TestCase):
    def select(self, source):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / 'sources').mkdir()
            (package / 'sources/security_policy.move').write_text(source)
            return policy_failure_function(package)

    def test_current_release_source(self):
        self.assertEqual(policy_failure_function(Path(__file__).resolve().parents[2] / 'contracts'), 'check')

    def test_older_factored_guard(self):
        self.assertEqual(self.select('p0(p.transaction == *ctx.digest()); p0((p.attested & needed) == needed); fun p0(condition: bool) { if (!condition) abort EFactor }'), 'p0')

    def test_older_inline_guard(self):
        self.assertEqual(self.select('assert!(needed == 0 || (p.transaction == *ctx.digest() && (p.attested & needed) == needed), EFactor);'), 'check')

    def test_missing_or_changed_factor_guard_is_rejected(self):
        for source in ['', 'assert!(true, EFactor);',
                       'p0(p.transaction == *ctx.digest()); p0((p.attested & needed) == needed);',
                       'p0(p.transaction == *ctx.digest()); p0((p.attested & needed) == needed); fun p0(condition: bool) { if (!condition) abort EPolicy }']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.select(source)


if __name__ == '__main__':
    unittest.main()
