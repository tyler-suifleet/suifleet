"""SF-304: the manual record-recovery procedure in docs/ANSIBLE.md, offline.

Runs the documented shell block as written, with its fixed paths redirected
to a scratch directory and runuser/suifleet/install replaced by stubs. A
failed or foreign lookup must install nothing and keep the attempt marker.
"""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

DOC = Path(__file__).resolve().parents[2] / "docs/ANSIBLE.md"
ADDRESS = "0x" + "a" * 64
RECEIPT = {
    "device_address": ADDRESS,
    "device_object_id": "0x" + "1" * 64,
    "device_cap_id": "0x" + "2" * 64,
    "owner_cap_id": "0x" + "3" * 64,
    "digest": "4" * 44,
}


def procedure():
    text = DOC.read_text()
    start = text.index("To restore a record by hand")
    block = re.search(r"```sh\n(.*?)```", text[start:], re.S)
    assert block, "manual recovery block not found"
    return "\n".join(line[2:] if line.startswith("  ") else line for line in block.group(1).splitlines()) + "\n"


STUBS = {
    # runuser -u HOLDER -- CMD...: run CMD as the caller.
    "runuser": '#!/bin/sh\nshift 3\nexec "$@"\n',
    # install without the root ownership a test run cannot set.
    "install": '#!/bin/sh\nargs=""\nwhile [ $# -gt 2 ]; do case "$1" in -o|-g) shift 2;; *) args="$args $1"; shift;; esac; done\n'
               'exec /usr/bin/install $args "$1" "$2"\n',
}
SUIFLEET = ('#!/bin/sh\ncase "$1" in\n--derive-address) echo "$FAKE_ADDRESS";;\n'
            '--find-enrollment) printf %s "$FAKE_RECEIPT"; exit "$FAKE_RC";;\n*) exit 64;;\nesac\n')


class ManualRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sf304-recovery-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.etc, self.lookup, self.bin = self.tmp / "etc", self.tmp / "lookup", self.tmp / "bin"
        for d in (self.etc, self.lookup, self.bin):
            d.mkdir()
        (self.etc / "device.key").write_text("k")
        (self.etc / ".enrollment.json.partial").write_text("")
        (self.lookup / "enroll.toml").write_text("")
        for name, body in {**STUBS, "suifleet": SUIFLEET}.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def run_procedure(self, receipt, rc=0):
        script = (procedure().replace("/etc/suifleet", str(self.etc)).replace("/run/lookup", str(self.lookup))
                  .replace("/usr/bin/suifleet", str(self.bin / "suifleet")))
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_ADDRESS": ADDRESS,
               "FAKE_RECEIPT": receipt, "FAKE_RC": str(rc)}
        return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)

    def assert_untouched(self, result):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.etc / "enrollment.json").exists())
        self.assertEqual((self.etc / ".enrollment.json.partial").read_text(), "")

    def test_failed_lookup_installs_nothing_and_keeps_the_marker(self):
        self.assert_untouched(self.run_procedure("", rc=3))

    def test_lookup_error_with_output_installs_nothing(self):
        self.assert_untouched(self.run_procedure(json.dumps(RECEIPT, separators=(",", ":")) + "\n", rc=1))

    def test_receipt_for_another_key_is_refused(self):
        other = {**RECEIPT, "device_address": "0x" + "b" * 64}
        self.assert_untouched(self.run_procedure(json.dumps(other, separators=(",", ":")) + "\n"))

    def test_truncated_receipt_is_refused(self):
        self.assert_untouched(self.run_procedure(json.dumps(RECEIPT, separators=(",", ":"))[:80]))

    def test_existing_record_is_never_replaced(self):
        (self.etc / "enrollment.json").write_text("kept")
        result = self.run_procedure(json.dumps(RECEIPT, separators=(",", ":")) + "\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.etc / "enrollment.json").read_text(), "kept")
        self.assertTrue((self.etc / ".enrollment.json.partial").exists())

    def test_complete_receipt_installs_the_record_then_removes_the_marker(self):
        line = json.dumps(RECEIPT, separators=(",", ":")) + "\n"
        result = self.run_procedure(line)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.etc / "enrollment.json"
        self.assertEqual(record.read_text(), line)
        self.assertEqual(record.stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.etc / ".enrollment.json.partial").exists())
        self.assertFalse((self.etc / ".enrollment.json.restore").exists())
        self.assertFalse(self.lookup.exists())


if __name__ == "__main__":
    unittest.main()
