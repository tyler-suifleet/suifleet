import base64
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_package_size import check_size, package_size


class PackageSizeTests(unittest.TestCase):
    def test_accounting_includes_datatype_origins_and_dependency_linkage(self):
        modules = [base64.b64encode(b'bytecode').decode()]
        summaries = {'mod': {'structs': {'Thing': {}}, 'enums': {'Choice': {}}}}
        self.assertEqual(package_size(modules, summaries, ['dep']),
                         8 + 8 + 3 + (3 + 5 + 32) + (3 + 6 + 32) + 72)

    def test_boundary_and_observed_testnet_failure(self):
        check_size(102400)
        for size in (102401, 103941):
            with self.assertRaisesRegex(ValueError, str(size)):
                check_size(size)
