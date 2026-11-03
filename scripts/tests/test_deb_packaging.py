"""Offline builder safeguards; lifecycle acceptance uses scripts/test_deb.py."""
import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("build_deb", ROOT / "scripts/build_deb.py")
deb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deb)


class PackagingTests(unittest.TestCase):
    def test_rejects_mislabeled_and_non_elf_binaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "binary"
            for data in (b"not an executable", b"\x7fELF\x02\x01" + b"\0" * 12 + struct.pack("<H", 183)):
                binary.write_bytes(data)
                with self.assertRaises(ValueError):
                    deb.build(binary, root / "out", "0.2.2-1", "amd64", 1)
            self.assertFalse((root / "out").exists())

    def test_reproducible_payload_and_no_runtime_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "binary"
            binary.write_bytes(b"\x7fELF\x02\x01" + b"\0" * 12 + struct.pack("<H", 62))
            first = deb.build(binary, root / "first", "0.2.2-1", "amd64", 1793606400)
            second = deb.build(binary, root / "second", "0.2.2-1", "amd64", 1793606400)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            subprocess.run(["dpkg-deb", "-x", str(first), str(root / "extract")], check=True)
            etc = root / "extract/etc/suifleet"
            self.assertEqual([p.name for p in etc.iterdir()], ["release-trust.toml"])
            self.assertFalse((root / "extract/var/lib/suifleet").exists())
            control = subprocess.check_output(["dpkg-deb", "-f", str(first)], text=True)
            self.assertIn("Architecture: amd64", control)
            self.assertIn("libc6 (>= 2.35)", control)


if __name__ == "__main__":
    unittest.main()
