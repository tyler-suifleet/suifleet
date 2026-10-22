"""Reject malformed/truncated partition tables before any filesystem inspection."""
import importlib.util
from pathlib import Path
import struct
import unittest

spec = importlib.util.spec_from_file_location('inspect_pi_image', Path(__file__).parents[1]/'inspect_pi_image.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def mbr():
    data=bytearray(512)
    data[510:]=b'\x55\xaa'
    for offset, kind, start, sectors in [(446,0xc,8192,204800),(462,0x83,212992,1048576)]:
        data[offset+4]=kind
        struct.pack_into('<II',data,offset+8,start,sectors)
    return data


class PartitionTests(unittest.TestCase):
    def test_pi_layout(self):
        parts=module.partitions(mbr())
        self.assertEqual(parts[1]['start'],212992*512)
        self.assertEqual(parts[0]['size'],100*1024*1024)

    def test_bad_signature(self):
        data=mbr();data[510]=0
        with self.assertRaises(ValueError):module.partitions(data)

    def test_truncated_header(self):
        with self.assertRaises(ValueError):module.partitions(mbr()[:-1])

    def test_wrong_root_type(self):
        data=mbr();data[466]=0xc
        with self.assertRaises(ValueError):module.partitions(data)

    def test_overlapping_partitions(self):
        data=mbr();struct.pack_into('<I',data,470,10000)
        with self.assertRaises(ValueError):module.partitions(data)


RECIPE = 'SRCREV = "abc123"\nSUIFLEET_LOCK_SHA256 = "def456"\n'


class BuildInfoTests(unittest.TestCase):
    def test_recipe_pins(self):
        self.assertEqual(module.recipe_pins(RECIPE), dict(source_revision='abc123', lock_sha256='def456'))

    def test_recipe_without_lock_pin(self):
        with self.assertRaises(ValueError):module.recipe_pins('SRCREV = "abc123"\n')

    def test_matching_build_info(self):
        info = 'source_revision=abc123\nlock_sha256=def456\ncrate_version=0.2.1\n'
        self.assertEqual(module.check_build_info(info, module.recipe_pins(RECIPE))['crate_version'], '0.2.1')

    def test_stale_source_revision(self):
        info = 'source_revision=0ld\nlock_sha256=def456\ncrate_version=0.2.1\n'
        with self.assertRaises(ValueError):module.check_build_info(info, module.recipe_pins(RECIPE))

    def test_stale_lock_digest(self):
        info = 'source_revision=abc123\nlock_sha256=0ld\ncrate_version=0.2.1\n'
        with self.assertRaises(ValueError):module.check_build_info(info, module.recipe_pins(RECIPE))

    def test_incomplete_build_info(self):
        with self.assertRaises(ValueError):module.check_build_info('source_revision=abc123\n', None)
