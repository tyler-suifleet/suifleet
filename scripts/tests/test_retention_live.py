"""Offline integrity checks for live retention fixture reuse."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_fleet_budget import unrelocate_module


class BytecodeReuseTests(unittest.TestCase):
    def test_only_address_identifiers_are_normalized(self):
        package = '0x' + 'ab' * 32
        address = bytes.fromhex(package[2:])
        # Two Move tables: address identifiers then literal constants. The
        # same bytes in a literal must not be hidden by normalization.
        header = bytes.fromhex('a11ceb0b07000000') + bytes([2, 8, 0, 32, 6, 32, 32])
        self.assertEqual(unrelocate_module(header + address + address, package), header + bytes(32) + address)
        with self.assertRaises(AssertionError):
            unrelocate_module(b'not Move bytecode', package)
