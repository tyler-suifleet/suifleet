"""scripts/claims.py roster mode (SF-100), offline with a stand-in daemon."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "claims.py"
# `suifleet --provision KEY`: writes a 0600 key and prints an address derived from it.
FAKE_DAEMON = """#!/usr/bin/env python3
import hashlib, os, sys
assert sys.argv[1] == "--provision"
seed = os.urandom(32).hex()
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, seed.encode() + b"\\n")
print("0x" + hashlib.sha256(seed.encode()).hexdigest())
"""


class ClaimsRoster(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.daemon = self.dir / "suifleet"
        self.daemon.write_text(FAKE_DAEMON)
        self.daemon.chmod(0o700)
        self.manifest = self.dir / "deployment.json"
        self.manifest.write_text('{"network": "localnet"}\n')

    def run_claims(self, *args, check=True):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, check=check)

    def roster(self, text):
        path = self.dir / "devices.csv"
        path.write_text(text)
        return path

    def test_roster_writes_public_reservations_and_private_media(self):
        out = self.dir / "out"
        policy = "0x" + "ab" * 32
        self.run_claims("generate", "--roster", self.roster("serial,alias,group\nSN1,,Lab\nSN2,kitchen,\n\nSN3,,\n"),
                        out, "--template", "pi-{n:3}", "--start", "7", "--daemon", self.daemon,
                        "--policy", policy, "--manifest", self.manifest)
        roster = json.loads((out / "roster.json").read_text())
        self.assertEqual([(e["serial"], e["alias"], e.get("group")) for e in roster["entries"]],
                         [("SN1", "pi-007", "Lab"), ("SN2", "kitchen", None), ("SN3", "pi-009", None)])
        self.assertEqual(roster["claimFundMist"], "20000000")
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for entry in roster["entries"]:
            folder = out / "enrollment" / entry["serial"]
            key = (folder / "claim.key").read_text().strip()
            self.assertEqual(stat.S_IMODE((folder / "claim.key").stat().st_mode), 0o600)
            self.assertNotIn(key, (out / "roster.json").read_text(), "roster.json carries key material")
            toml = (folder / "enroll.toml").read_text()
            self.assertIn(f'policy_id = "{policy}"', toml)
            self.assertIn(f'name = "{entry["alias"]}"', toml)
            self.assertIn('claim_key_path = "claim.key"', toml)
            self.assertEqual((folder / "deployment.json").read_text(), self.manifest.read_text())
        ptb = self.run_claims("ptb", out, "--package", "0x1", "--policy", policy).stdout.strip().splitlines()
        self.assertEqual(len(ptb), 1)
        self.assertTrue(all(e["claim"] in ptb[0] for e in roster["entries"]))

    def test_rejects_conflicts_before_writing(self):
        out = self.dir / "out"
        result = self.run_claims("generate", "--roster", self.roster("serial,alias\nSN1,a\nsn1,b\nSN2,A\nSN3,bad name\n../x,c\nSN4,\n"),
                                 out, "--daemon", self.daemon, check=False)
        self.assertNotEqual(result.returncode, 0)
        for line in (3, 4, 5, 6, 7):
            self.assertIn(f"line {line}:", result.stderr)
        self.assertFalse(out.exists(), "a rejected roster wrote credentials")

    def test_ptb_chunks_large_batches(self):
        out = self.dir / "out"
        out.mkdir()
        (out / "claims.json").write_text(json.dumps({f"k{i}": "0x" + f"{i:064x}" for i in range(600)}))
        lines = self.run_claims("ptb", out, "--package", "0x1", "--policy", "0x2").stdout.strip().splitlines()
        self.assertEqual([line.count("--transfer-objects") for line in lines], [250, 250, 100])


if __name__ == "__main__":
    unittest.main()
