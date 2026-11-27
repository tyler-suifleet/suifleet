"""SF-304: root stages the transferred claim from a pipe, never sharing the staged file, offline.

The SSH account owns the transfer directory, so a reader running as that
account (runuser) reads claim.key and root writes the staged copy. The
reader must get only the write end of a pipe: if it inherited a handle to
the staged file it could rewrite the root-owned 0400 copy after the chmod,
between the address derivation and the enrollment that both read it. This
runs the role's own writer and reader scripts, taken from stage_claim.yml,
with a stand-in runuser on PATH that records the reader's descriptors.
(scripts/test_ansible.py stages real claims on disposable hosts.)
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TASKS = Path(__file__).resolve().parents[2] / "examples/ansible/roles/suifleet/tasks"
CLAIM = b"suiprivkey1qexampleclaimbytes\n"

# Records the reader's descriptors, then runs it as the caller (the test is
# not root, so there is no account to switch to).
FAKE_RUNUSER = r"""#!/usr/bin/env python3
import json, os, stat, sys
assert sys.argv[1] == "-u" and sys.argv[3] == "--", sys.argv
fds = {}
for name in os.listdir("/proc/self/fd"):
    try:
        fds[name] = os.readlink(f"/proc/self/fd/{name}")
    except OSError:
        pass
probe = {"stdout_fifo": stat.S_ISFIFO(os.fstat(1).st_mode), "fds": fds,
         "dest_exists": os.path.lexists(os.environ["SF304_DEST"])}
with open(os.environ["SF304_PROBE"], "w") as handle:
    json.dump(probe, handle)
mode = os.environ.get("SF304_MODE", "")
if mode == "fail-after-output":
    os.write(1, b"partial claim bytes")
    sys.exit(1)
if mode == "oversized":
    os.write(1, b"x" * 5000)
    sys.exit(0)
os.execvp(sys.argv[4], sys.argv[4:])
"""


def stage_task():
    import yaml  # PyYAML: Ubuntu python3-yaml or the pinned controller

    tasks = yaml.safe_load((TASKS / "stage_claim.yml").read_text())
    block = next(t for t in tasks if t.get("name") == "Transfer and stage the claim")["block"]
    return next(t for t in block if t.get("name") == "Stage the single-use claim key in memory-backed storage")


class ClaimStaging(unittest.TestCase):
    def setUp(self):
        argv = stage_task()["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[:3], ["python3", "-I", "-c"])
        writer, self.reader = argv[3], argv[7]
        self.assertEqual(writer.count("os.fchown(fd, 0, 0)"), 1)
        # Not root here: the only test-side change gives the staged file to the caller instead of root.
        self.writer = writer.replace("os.fchown(fd, 0, 0)", "os.fchown(fd, os.getuid(), os.getgid())")
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.transfer, self.staging, bin_dir = root / "transfer", root / "staging", root / "bin"
        for path in (self.transfer, self.staging, bin_dir):
            path.mkdir(mode=0o700)
        (bin_dir / "runuser").write_text(FAKE_RUNUSER)
        (bin_dir / "runuser").chmod(0o755)
        self.dest, self.probe = self.staging / "claim.key", root / "probe.json"
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", SF304_DEST=str(self.dest),
                        SF304_PROBE=str(self.probe))

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, mode=""):
        return subprocess.run(["python3", "-I", "-c", self.writer, str(self.transfer), "deploy", str(self.dest),
                               self.reader], env=dict(self.env, SF304_MODE=mode), capture_output=True, text=True,
                              timeout=60)

    def write_claim(self, data=CLAIM):
        path = self.transfer / "claim.key"
        path.write_bytes(data)
        path.chmod(0o600)

    def probe_result(self):
        import json

        return json.loads(self.probe.read_text())

    def test_reader_gets_only_a_pipe_and_root_writes_the_staged_copy_after_it_exits(self):
        self.write_claim()
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = self.probe_result()
        self.assertTrue(probe["stdout_fifo"], probe)
        self.assertFalse(probe["dest_exists"], "the staged file existed while the reader ran")
        self.assertFalse([fd for fd, target in probe["fds"].items() if str(self.staging) in target], probe)
        self.assertEqual(self.dest.read_bytes(), CLAIM)
        st = self.dest.lstat()
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o400)

    def test_reader_refuses_output_that_is_not_a_pipe(self):
        self.write_claim()
        with open(self.staging / "redirected", "wb") as out:
            result = subprocess.run(["python3", "-I", "-c", self.reader, str(self.transfer / "claim.key")],
                                    stdout=out, stderr=subprocess.PIPE, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing the transferred claim: the reader's output is not a pipe", result.stderr)
        self.assertEqual((self.staging / "redirected").read_bytes(), b"")

    def test_refused_read_leaves_no_staged_file(self):
        (self.transfer / "claim.key").symlink_to(self.transfer / "elsewhere")
        (self.transfer / "elsewhere").write_bytes(CLAIM)
        result = self.stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without following a link", result.stderr)
        self.assertFalse(os.path.lexists(self.dest))

    def test_reader_failing_after_output_leaves_no_staged_file(self):
        self.write_claim()
        result = self.stage("fail-after-output")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the reader exited with status 1", result.stderr)
        self.assertFalse(os.path.lexists(self.dest))

    def test_oversized_reader_output_leaves_no_staged_file(self):
        self.write_claim()
        result = self.stage("oversized")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("more than 4096 bytes", result.stderr)
        self.assertFalse(os.path.lexists(self.dest))

    def test_existing_staged_claim_is_never_replaced(self):
        self.write_claim()
        self.dest.write_bytes(b"earlier")
        result = self.stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("a staged claim already exists", result.stderr)
        self.assertEqual(self.dest.read_bytes(), b"earlier")
        self.assertFalse(self.probe.exists(), "the reader ran although a staged claim existed")


if __name__ == "__main__":
    unittest.main()
