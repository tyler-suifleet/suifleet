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
import time
import unittest
from pathlib import Path

TASKS = Path(__file__).resolve().parents[2] / "examples/ansible/roles/suifleet/tasks"
CLAIM = b"suiprivkey1qexampleclaimbytes\n"

# Records the reader's descriptors, then runs it as the caller (the test is
# not root, so there is no account to switch to).
FAKE_RUNUSER = r"""#!/usr/bin/env python3
import json, os, stat, sys, time
assert sys.argv[1] == "-u" and sys.argv[3] == "--", sys.argv
fds = {}
for name in os.listdir("/proc/self/fd"):
    try:
        fds[name] = os.readlink(f"/proc/self/fd/{name}")
    except OSError:
        pass
probe = {"stdout_fifo": stat.S_ISFIFO(os.fstat(1).st_mode), "fds": fds, "pid": os.getpid(),
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
if mode == "endless":
    # Never stops writing: root must refuse once more than 4 KiB arrived.
    while True:
        os.write(1, b"x" * 1024)
if mode == "endless-stderr":
    # Never stops writing diagnostics: they must never reach Ansible.
    while True:
        os.write(2, b"e" * 1024)
if mode == "leaky-refusal":
    # Writes a synthetic claim marker as diagnostics, then fails: the marker
    # must never appear in root's output.
    os.write(2, os.environ["SF304_MARKER"].encode() + b"\n")
    sys.exit(1)
if mode == "leaky-stall":
    os.write(2, os.environ["SF304_MARKER"].encode() + b"\n")
    time.sleep(3600)
if mode == "noisy":
    # Plenty of diagnostics, then a normal read: root drains them without stalling.
    os.write(2, b"n" * 200000)
if mode == "stall":
    # Writes part of a claim, then never finishes.
    os.write(1, b"partial")
    time.sleep(3600)
    sys.exit(0)
if mode == "orphan-holder":
    # Exits successfully but leaves a descendant holding the pipe open.
    if os.fork() == 0:
        with open(os.environ["SF304_PROBE"] + ".child", "w") as handle:
            handle.write(str(os.getpid()))
        time.sleep(3600)
        os._exit(0)
    os.write(1, b"partial")
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
        # The stall tests shorten root's overall wait from 60 to 2 seconds.
        self.assertEqual(self.writer.count("LIMIT, DEADLINE = 4096, 60"), 1)
        self.writer = self.writer.replace("LIMIT, DEADLINE = 4096, 60", "LIMIT, DEADLINE = 4096, 2")
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.transfer, self.staging, bin_dir = root / "transfer", root / "staging", root / "bin"
        for path in (self.transfer, self.staging, bin_dir):
            path.mkdir(mode=0o700)
        (bin_dir / "runuser").write_text(FAKE_RUNUSER)
        (bin_dir / "runuser").chmod(0o755)
        self.dest, self.probe = self.staging / "claim.key", root / "probe.json"
        self.marker = "suiprivkey1qsf304syntheticmarker" + os.urandom(8).hex()
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", SF304_DEST=str(self.dest),
                        SF304_PROBE=str(self.probe), SF304_MARKER=self.marker)

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
        self.assertEqual(result.returncode, 10, "the reader reports refusals by fixed exit status")
        self.assertEqual(result.stderr, "", "the reader prints no diagnostics")
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

    def assert_gone(self, pid):
        # Killed and reaped (a zombie of an unrelated parent would still be listed, so allow a moment).
        for _ in range(50):
            try:
                with open(f"/proc/{pid}/stat") as handle:
                    if handle.read().rsplit(")", 1)[1].split()[0] == "Z":
                        return
            except FileNotFoundError:
                return
            time.sleep(0.1)
        self.fail(f"reader process {pid} is still running")

    def test_continued_oversized_output_is_refused_promptly(self):
        self.write_claim()
        started = time.monotonic()
        result = self.stage("endless")
        self.assertLess(time.monotonic() - started, 15, "root kept reading an endless reader")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to stage the claim: the reader returned more than 4096 bytes", result.stderr)
        self.assertFalse(os.path.lexists(self.dest))
        self.assert_gone(self.probe_result()["pid"])

    def test_stalled_reader_times_out_and_is_killed(self):
        self.write_claim()
        started = time.monotonic()
        result = self.stage("stall")
        self.assertLess(time.monotonic() - started, 15, "root waited past its deadline")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to stage the claim: the reader did not finish within 2 seconds", result.stderr)
        self.assertFalse(os.path.lexists(self.dest))
        self.assert_gone(self.probe_result()["pid"])

    def test_descendant_holding_the_pipe_times_out_and_is_killed(self):
        self.write_claim()
        started = time.monotonic()
        result = self.stage("orphan-holder")
        self.assertLess(time.monotonic() - started, 15, "root waited past its deadline")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the reader did not finish within 2 seconds", result.stderr)
        self.assertFalse(os.path.lexists(self.dest))
        child = Path(str(self.probe) + ".child")
        for _ in range(50):
            if child.exists() and child.read_text():
                break
            time.sleep(0.1)
        self.assert_gone(int(child.read_text()))

    def test_endless_reader_diagnostics_are_bounded_and_the_reader_is_killed(self):
        self.write_claim()
        started = time.monotonic()
        result = self.stage("endless-stderr")
        self.assertLess(time.monotonic() - started, 15, "root waited past its deadline")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), "refusing to stage the claim: the reader did not finish within 2 seconds",
                         "the reader's diagnostics reached the caller")
        self.assertEqual(result.stdout, "")
        self.assertFalse(os.path.lexists(self.dest))
        self.assert_gone(self.probe_result()["pid"])

    def test_reader_diagnostics_are_drained_and_never_passed_through(self):
        self.write_claim()
        result = self.stage("noisy")
        self.assertEqual(result.returncode, 0, result.stderr[:2000])
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.dest.read_bytes(), CLAIM)

    def test_reader_refusal_is_reported_as_roots_fixed_message(self):
        self.write_claim(b"x" * 5000)
        result = self.stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), "refusing to stage the claim: the reader exited with status 12 (refusing "
                         "the transferred claim: not a regular single-link file of 1 to 4096 bytes owned by the SSH account)")
        self.assertFalse(os.path.lexists(self.dest))

    def test_reader_diagnostics_never_reach_roots_output(self):
        # A synthetic claim marker the reader writes as diagnostics before it
        # fails, or before it stalls, must not appear in anything root prints.
        self.write_claim()
        for mode, expected in (("leaky-refusal", "refusing to stage the claim: the reader exited with status 1"),
                               ("leaky-stall", "refusing to stage the claim: the reader did not finish within 2 seconds")):
            with self.subTest(mode=mode):
                result = self.stage(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.strip(), expected)
                self.assertNotIn(self.marker, result.stderr + result.stdout)
                self.assertFalse(os.path.lexists(self.dest))
                self.assert_gone(self.probe_result()["pid"])

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
