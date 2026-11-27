"""SF-304: the manual record-recovery procedure in docs/ANSIBLE.md, offline.

Runs the documented shell block as written, with its fixed paths redirected
to a scratch directory and runuser/suifleet/install replaced by stubs. A
failed or foreign lookup must install nothing and keep the attempt marker, and
a record is restored only beside the deployment the lookup ran under. The
inputs are validated and looked up only as copies in a fresh workspace that
the key holder can read but not write, and the receipt is captured there by
root after the lookup exits.

The procedure runs on the host, which needs Python 3.11+ (tomllib). The CI
runner's python3 may be older, so a python3 stub runs the procedure's Python
under a 3.11+ interpreter: this one, one on PATH, or one in the hosted
runner's tool cache. Otherwise every refusal would pass only because tomllib
is missing. Without one, the tests fail in CI and are skipped elsewhere.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
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
FLEET = {"schema_version": 1, "network": "testnet", "chain_identifier": "4c78adac",
         "rpc_url": "https://fleet.example", "rpc_urls": [], "call_package_id": "0x" + "5" * 64}
OTHER = {**FLEET, "call_package_id": "0x" + "6" * 64}
ENROLL = ('manifest_path = "deployment.json"\npolicy_id = "0x7"\nclaim_key_path = "claim.key"\n'
          'name = "pi-1"\nkeypair_path = "/etc/suifleet/device.key"\n')
LINE = json.dumps(RECEIPT, separators=(",", ":")) + "\n"


def modern_python():
    if sys.version_info >= (3, 11):
        return sys.executable
    candidates = [shutil.which(f"python3.{minor}") for minor in range(14, 10, -1)]
    candidates += sorted(glob.glob("/opt/hostedtoolcache/Python/3.1[1-9]*/x64/bin/python3"), reverse=True)
    return next((c for c in candidates if c), None)


PYTHON = modern_python()
if PYTHON is None and os.environ.get("CI"):
    raise RuntimeError("no Python 3.11+ (tomllib) for the manual recovery procedure tests")


def digest(manifest):
    return hashlib.sha256(json.dumps(manifest).encode()).hexdigest()


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
# The lookup records the workspace it ran in and its permissions, and can tamper with its
# inputs afterwards (FAKE_TAMPER) to show that root never reads them again.
SUIFLEET = ('#!/bin/sh\ncase "$1" in\n--derive-address) echo "$FAKE_ADDRESS";;\n'
            '--find-enrollment) d=$(dirname "$2"); { echo "$2"; stat -c "%a %n" "$d" "$d"/*;'
            ' test -e "$d/receipt.json" && echo receipt-exists; } > "$FAKE_LOOKED_UP";'
            ' if [ -n "$FAKE_TAMPER" ]; then cp "$FAKE_TAMPER" "$FAKE_SRC/deployment.json"; fi;'
            ' printf %s "$FAKE_RECEIPT"; exit "$FAKE_RC";;\n*) exit 64;;\nesac\n')


@unittest.skipIf(PYTHON is None, "no Python 3.11+ (tomllib) outside CI")
class ManualRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sf304-recovery-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.etc, self.lookup, self.bin = self.tmp / "etc", self.tmp / "lookup", self.tmp / "bin"
        self.run_dir = self.tmp / "run"
        for d in (self.etc, self.lookup, self.bin, self.run_dir):
            d.mkdir()
        (self.etc / "device.key").write_text("k")
        (self.etc / ".enrollment.json.partial").write_text("")
        self.write_enroll(ENROLL)
        (self.lookup / "fleet.json").write_text(json.dumps(FLEET))
        (self.lookup / "deployment.json").write_text(json.dumps(FLEET))
        self.binding = self.etc / ".enrollment.json.partial.deployment"
        self.looked_up = self.tmp / "looked-up"
        python = f'#!/bin/sh\nexec "{PYTHON}" "$@"\n'
        for name, body in {**STUBS, "suifleet": SUIFLEET, "python3": python}.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def write_enroll(self, text):
        # The procedure's /etc/suifleet paths, including the key path it requires, are redirected.
        (self.lookup / "enroll.toml").write_text(text.replace("/etc/suifleet", str(self.etc)))

    def run_procedure(self, receipt, rc=0, tamper=None):
        script = procedure()
        self.assertIn("/root/lookup", script)
        self.assertIn("/run/suifleet-lookup.", script)
        script = (script.replace("/etc/suifleet", str(self.etc)).replace("/root/lookup", str(self.lookup))
                  .replace("/run/suifleet-lookup.", f"{self.run_dir}/suifleet-lookup.")
                  .replace("/usr/bin/suifleet", str(self.bin / "suifleet")))
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_ADDRESS": ADDRESS,
               "FAKE_RECEIPT": receipt, "FAKE_RC": str(rc),
               "FAKE_LOOKED_UP": str(self.looked_up), "FAKE_SRC": str(self.lookup),
               "FAKE_TAMPER": str(tamper or "")}
        return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)

    def assert_untouched(self, result):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.run_dir.iterdir()), [], "the workspace was left behind")
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
        self.assertEqual(list(self.run_dir.iterdir()), [], "the workspace was left behind")

    def test_lookup_reads_root_owned_copies_in_a_fresh_workspace(self):
        result = self.run_procedure(LINE)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.looked_up.read_text().splitlines()
        workspace = Path(lines[0]).parent
        self.assertEqual(workspace.parent, self.run_dir)
        self.assertTrue(workspace.name.startswith("suifleet-lookup."))
        modes = dict(reversed(line.split(" ", 1)) for line in lines[1:] if line != "receipt-exists")
        # Readable and traversable by the key holder's group, writable only by root.
        self.assertEqual(modes[str(workspace)], "750")
        for name in ("enroll.toml", "deployment.json", "fleet.json"):
            self.assertEqual(modes[str(workspace / name)], "640", name)
        self.assertEqual(len(modes), 4, modes)
        # Root writes the receipt only after the lookup exited.
        self.assertNotIn("receipt-exists", lines)
        self.assertFalse(workspace.exists())

    def test_inputs_changed_during_the_lookup_are_not_used(self):
        # The lookup and validation used the workspace copies; a later change to the
        # input folder has no effect on the restored record or the binding.
        other = self.tmp / "other.json"
        other.write_text(json.dumps(OTHER))
        result = self.run_procedure(LINE, tamper=other)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.binding.read_text(), digest(FLEET) + "\n")

    def test_symlinked_or_special_inputs_are_refused(self):
        secret = self.tmp / "secret"
        secret.write_text(json.dumps(FLEET))
        (self.lookup / "deployment.json").unlink()
        (self.lookup / "deployment.json").symlink_to(secret)
        result = self.run_procedure(LINE)
        self.assert_untouched(result)
        self.assertFalse(self.looked_up.exists(), "the lookup ran")
        (self.lookup / "deployment.json").unlink()
        os.mkfifo(self.lookup / "deployment.json")
        result = self.run_procedure(LINE)
        self.assert_untouched(result)
        self.assertIn("not a regular file", result.stderr)
        self.assertFalse(self.looked_up.exists(), "the lookup ran")

    def test_matching_attempt_binding_is_kept(self):
        # The next run configures the host only under the deployment the attempt enrolled under.
        self.binding.write_text(digest(FLEET) + "\n")
        result = self.run_procedure(LINE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.etc / "enrollment.json").read_text(), LINE)
        self.assertEqual(self.binding.read_text(), digest(FLEET) + "\n")

    def test_binding_for_another_deployment_is_refused_before_the_lookup(self):
        # The attempt enrolled under A; restoring with B's lookup and manifest would pair
        # B's Device and DeviceCap with A's binding.
        self.binding.write_text(digest(OTHER) + "\n")
        result = self.run_procedure(LINE)
        self.assert_untouched(result)
        self.assertIn("another deployment", result.stderr)
        self.assertFalse(self.looked_up.exists(), "the lookup ran")
        self.assertEqual(self.binding.read_text(), digest(OTHER) + "\n")

    def test_unreadable_binding_is_refused(self):
        self.binding.write_text("")
        self.assert_untouched(self.run_procedure(LINE))
        self.assertEqual(self.binding.read_text(), "")

    def test_unconfigured_host_without_binding_records_the_lookup_deployment(self):
        result = self.run_procedure(LINE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.binding.read_text(), digest(FLEET) + "\n")
        self.assertEqual(self.binding.stat().st_mode & 0o777, 0o600)
        self.assertFalse(Path(str(self.binding) + ".new").exists())

    def test_configured_host_without_binding_gets_none(self):
        (self.etc / "config.toml").write_text("")
        result = self.run_procedure(LINE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.binding.exists())

    def test_failed_lookup_records_no_binding(self):
        self.assert_untouched(self.run_procedure("", rc=3))
        self.assertFalse(self.binding.exists())

    def test_lookup_manifest_may_differ_only_in_node_urls(self):
        (self.lookup / "deployment.json").write_text(
            json.dumps({**FLEET, "rpc_url": "https://archive.example", "rpc_urls": ["https://archive.example"]}))
        result = self.run_procedure(LINE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.binding.read_text(), digest(FLEET) + "\n")

    def test_lookup_under_another_deployment_is_refused(self):
        (self.lookup / "deployment.json").write_text(json.dumps(OTHER))
        self.assert_untouched(self.run_procedure(LINE))
        self.assertFalse(self.looked_up.exists(), "the lookup ran")
        self.assertFalse(self.binding.exists())

    def test_duplicate_manifest_key_is_refused(self):
        text = json.dumps(FLEET)[:-1] + ', "call_package_id": "0x' + "6" * 64 + '"}'
        (self.lookup / "deployment.json").write_text(text)
        self.assert_untouched(self.run_procedure(LINE))
        self.assertFalse(self.looked_up.exists(), "the lookup ran")

    def test_enroll_config_for_another_manifest_or_key_is_refused(self):
        for enroll in (ENROLL.replace('"deployment.json"', '"other.json"'),
                       ENROLL.replace("/etc/suifleet/device.key", "/tmp/key"),
                       ENROLL + '\n[signer]\nsocket = "/run/s"\n'):
            with self.subTest(enroll=enroll):
                self.write_enroll(enroll)
                self.assert_untouched(self.run_procedure(LINE))
                self.assertFalse(self.looked_up.exists(), "the lookup ran")
                self.assertFalse(self.binding.exists())

if __name__ == "__main__":
    unittest.main()
