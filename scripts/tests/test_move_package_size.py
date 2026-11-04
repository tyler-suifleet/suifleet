"""Golden release-compiler fixture and publication-limit regressions."""
import base64
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_move_package_size import LIMIT, check_package_size, module_identity

# Real pinned sui 1.81.1 build, edition 2024.beta: size_fixture::Item and
# Generic<phantom T>, both copy/drop/store, and public value(): u64 { 1 }.
MODULE = bytes.fromhex(
    'a11ceb0b070000050801000202020a030c0505110307142c0840200a600a0c6a110003'
    '0001070000000701000100040001000001030747656e65726963044974656d0b64756d'
    '6d795f6669656c640c73697a655f666978747572650576616c75650000000000000000'
    '0000000000000000000000000000000000000000000000000002010403010201020100'
    '0100000002060100000000000000020000')


class PackageSize(unittest.TestCase):
    def built(self, module=MODULE):
        return {'modules': [base64.b64encode(module).decode()], 'dependencies': ['0x1', '0x2']}

    def test_counts_names_origins_and_linkage_not_only_bytecode(self):
        self.assertEqual(module_identity(MODULE), (b'size_fixture', [b'Item', b'Generic']))
        # Sui MovePackage::size: version + modules + origins + two linkage entries.
        expected = 8 + len(MODULE) + 12 + (12 + 7 + 32) + (12 + 4 + 32) + 2 * 72
        self.assertEqual(check_package_size(self.built()), expected)

    def test_accepts_exact_limit_and_rejects_one_byte_over_before_signing(self):
        size = check_package_size(self.built())
        self.assertEqual(check_package_size(self.built(), limit=size), size)
        with self.assertRaisesRegex(ValueError, 'exceeds.*before signing'):
            check_package_size(self.built(), limit=size - 1)
        self.assertEqual(LIMIT, 102400)

    def test_rejects_truncation_unknown_format_duplicate_and_empty_modules(self):
        for module in (b'garbage', MODULE[:20], MODULE[:4] + bytes(4) + MODULE[8:]):
            with self.subTest(module=module[:8]), self.assertRaises((ValueError, IndexError, KeyError)):
                check_package_size(self.built(module))
        built = self.built()
        built['modules'] *= 2
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            check_package_size(built)
        with self.assertRaisesRegex(ValueError, 'empty'):
            check_package_size({'modules': [], 'dependencies': []})


if __name__ == '__main__':
    unittest.main()
