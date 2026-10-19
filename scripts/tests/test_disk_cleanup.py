"""Offline tests for scripts/disk_cleanup.py (SF-080): real Git worktrees and
files in a temporary directory; claims, locks, processes and containers come
from a fake system. Nothing outside the temporary directory is touched."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_cleanup as dc  # noqa: E402

DAY = 86400


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})


def age(path, days):
    """Backdate a tree and the worktree's Git metadata."""
    stamp = time.time() - days * DAY
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(os.path.join(root, name), (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp))


class FakeSystem:
    def __init__(self, claims, locks=(), processes=(), mounts=(), docker_ok=True, ticket_ok=True, uninspectable=()):
        self.claims_list, self.locks_list = list(claims), list(locks)
        self.uninspectable = list(uninspectable)
        self.process_paths, self.mount_list = list(processes), list(mounts)
        self.docker_ok, self.ticket_ok = docker_ok, ticket_ok
        self.on_evaluate = None
        self.session = "cleanup-session"

    def lock(self, project, resource, ticket, purpose, session=None):
        """The coordinator's exclusive lease: a live lock of another session is refused."""
        session = session or self.session
        if not self.ticket_ok:
            raise dc.Unverifiable("coordinator unavailable")
        for held in self.locks_list:
            if held["resource"] == resource and held.get("session") != session and held.get("expires", 0) > time.time():
                raise dc.Unverifiable(f"{resource} is locked by session {held.get('session')}")
        self.locks_list = [l for l in self.locks_list if l["resource"] != resource]
        self.locks_list.append({"resource": resource, "ticket": ticket, "purpose": purpose, "session": session,
                                "expires": time.time() + 900, "token": f"tok-{resource}"})
        return f"tok-{resource}"

    def renew(self, project, resource, token):
        held = next((l for l in self.locks_list if l.get("token") == token and l["resource"] == resource), None)
        if not held or held["expires"] < time.time():
            raise dc.Unverifiable(f"{resource} lease lost")
        held["expires"] = time.time() + 900

    def unlock(self, project, resource, token):
        self.locks_list = [l for l in self.locks_list if l.get("token") != token]

    def claims(self, project):
        if not self.ticket_ok:
            raise dc.Unverifiable("coordinator unavailable")
        if self.on_evaluate:
            self.on_evaluate(self)
        return [dict(c) for c in self.claims_list]

    def locks(self, project):
        return list(self.locks_list)

    def processes(self):
        return list(self.process_paths), list(self.uninspectable)

    def container_mounts(self):
        if not self.docker_ok:
            raise dc.Unverifiable("docker unavailable")
        return list(self.mount_list)

    def docker_usage(self):
        return {"available": False}

    def now(self):
        return time.time()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sf080-")).resolve()
        self.root = self.tmp / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        (self.root / ".gitignore").write_text("target/\nnode_modules/\nbuild/tmp*/\n")
        (self.root / "README").write_text("source\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "init")
        self.worktrees = self.tmp / "worktrees"
        self.claims = []

    def tearDown(self):
        subprocess.run(["chmod", "-R", "u+rwx", str(self.tmp)], capture_output=True)
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def worktree(self, key, status="done", days=10, output=True):
        path = self.worktrees / key.lower()
        git(self.root, "worktree", "add", "-q", "--detach", str(path))
        if output:
            (path / "target/debug").mkdir(parents=True)
            (path / "target/debug/app").write_bytes(b"x" * 100_000)
        age(path, days)
        gitdir = self.root / ".git/worktrees" / path.name
        age(gitdir, days)
        self.claims.append({"key": key, "status": status, "worktree": str(path), "publish_pending": False})
        return path

    def config(self, **extra):
        return {"schema_version": 1, "projects": [{"id": "p", "root": str(self.root)}], "retain_days": 3,
                "worktree_outputs": ["target", "node_modules"], **extra}

    def plan(self, system=None, **extra):
        return dc.plan(self.config(**extra), system or FakeSystem(self.claims))

    def reasons(self, result):
        return {(Path(e["path"]).parent.name, e["relative"]): e["reason"] for e in result["excluded"]}

    def snapshot(self):
        return sorted((str(p), p.lstat().st_mtime) for p in self.tmp.rglob("*"))


class HelperTests(unittest.TestCase):
    def test_only_named_helpers_under_the_configured_parent_are_accepted(self):
        proc = Path(tempfile.mkdtemp(prefix="sf080-proc-"))
        try:
            (proc / "10").mkdir()
            os.symlink("/usr/lib/systemd/systemd", proc / "10/exe")
            (proc / "11").mkdir()
            (proc / "11/comm").write_text("(sd-pam)\n")
            (proc / "11/stat").write_text("11 ((sd-pam)) S 10 11 11 0 -1\n")
            (proc / "12").mkdir()
            (proc / "12/comm").write_text("cargo\n")
            (proc / "12/stat").write_text("12 (cargo) S 10 12 12 0 -1\n")
            helper = [{"comm": "(sd-pam)", "parent_exe": "/usr/lib/systemd/systemd"}]
            self.assertTrue(dc.accepted_helper(str(proc), 11, helper))
            self.assertFalse(dc.accepted_helper(str(proc), 12, helper), "another name under the same parent")
            self.assertFalse(dc.accepted_helper(str(proc), 11, [{"comm": "(sd-pam)", "parent_exe": "/usr/bin/other"}]))
            self.assertFalse(dc.accepted_helper(str(proc), 11, []), "nothing accepted by default")
            os.unlink(proc / "10/exe")
            self.assertFalse(dc.accepted_helper(str(proc), 11, helper), "an uninspectable parent fails closed")
        finally:
            subprocess.run(["rm", "-rf", str(proc)])


class PlanTests(Fixture):
    def test_dry_run_selects_only_done_idle_outputs_and_mutates_nothing(self):
        done = self.worktree("SF-1")
        self.worktree("SF-2", status="review")
        self.worktree("SF-3", status="in_progress")
        self.worktree("SF-4", days=1)
        before = self.snapshot()
        result = self.plan()
        self.assertEqual(self.snapshot(), before, "plan must not mutate")
        self.assertEqual([c["path"] for c in result["candidates"]], [str(done / "target")])
        self.assertGreaterEqual(result["candidate_bytes"], 100_000)
        reasons = self.reasons(result)
        self.assertEqual(reasons[("sf-2", "target")], "ticket status review")
        self.assertEqual(reasons[("sf-3", "target")], "ticket status in_progress")
        self.assertIn("worktree active", reasons[("sf-4", "target")])
        self.assertTrue(result["dry_run"])

    def test_locks_processes_and_containers_block(self):
        a, b, c, d = (self.worktree(k) for k in ("SF-10", "SF-11", "SF-12", "SF-13"))
        system = FakeSystem(self.claims, locks=[{"resource": "build:x", "purpose": "SF-10 rebuild", "expires": time.time() - 10}],
                            processes=[str(b / "target/debug/app"), str(c)], mounts=[str(d)])
        reasons = self.reasons(self.plan(system))
        self.assertIn("build:x (expired; break-lock", reasons[("sf-10", "target")])
        self.assertIn("in use by a process", reasons[("sf-11", "target")])
        self.assertIn("in use by a process", reasons[("sf-12", "target")], "a process inside the worktree blocks its outputs")
        self.assertIn("mounted by a running container", reasons[("sf-13", "target")])

    def test_unverifiable_checks_fail_closed(self):
        self.worktree("SF-20")
        self.assertEqual(self.plan(FakeSystem(self.claims, docker_ok=False))["candidates"], [])
        self.assertIn("unverifiable: docker unavailable", self.reasons(self.plan(FakeSystem(self.claims, docker_ok=False)))[("sf-20", "target")])
        result = self.plan(FakeSystem(self.claims, ticket_ok=False))
        self.assertEqual(result["candidates"], [])
        self.assertIn("coordinator unavailable", result["unverifiable"][0])

    def test_symlinks_and_escapes_are_refused(self):
        outside = self.tmp / "precious"
        outside.mkdir()
        (outside / "data").write_text("keep")
        a = self.worktree("SF-30", output=False)
        os.symlink(outside, a / "target")
        b = self.worktree("SF-31", output=False)
        (b / "real").mkdir()
        os.symlink(b / "real", b / "node_modules")
        age(a, 10), age(b, 10)
        reasons = self.reasons(self.plan())
        self.assertIn("symlink on path", reasons[("sf-30", "target")])
        self.assertIn("symlink on path", reasons[("sf-31", "node_modules")])
        with self.assertRaises(SystemExit):
            path = self.tmp / "bad.json"
            path.write_text(json.dumps(self.config(worktree_outputs=["../repo"])))
            dc.load_config(path)
        for bad in ("/etc", "a/../b", "", "./target"):
            self.assertFalse(dc.plain_relative(bad), bad)

    def test_missing_or_malformed_config_exits_with_a_message(self):
        malformed = self.tmp / "malformed.json"
        malformed.write_text("{not json")
        listed = self.tmp / "list.json"
        listed.write_text("[]")
        for path, expected in ((self.tmp / "absent.json", "no configuration"),
                               (malformed, "cannot read configuration"), (listed, "must be a JSON object")):
            with self.assertRaises(SystemExit, msg=path) as raised:
                dc.load_config(path)
            self.assertIn(expected, str(raised.exception.code))

    def test_tracked_unignored_and_protected_content_is_refused(self):
        a = self.worktree("SF-40")
        (a / "target/device.key").write_text("secret")
        b = self.worktree("SF-41", output=False)
        (b / "keep").mkdir()
        (b / "keep/file").write_text("x")
        c = self.worktree("SF-42", output=False)
        (c / "target").mkdir()
        (c / "target/tracked").write_text("x")
        git(c, "add", "-f", "target/tracked")
        for p in (a, b, c, self.root / ".git/worktrees/sf-42"):
            age(p, 10)
        reasons = self.reasons(self.plan(worktree_outputs=["target", "keep"]))
        self.assertIn("protected files inside", reasons[("sf-40", "target")])
        self.assertEqual(reasons[("sf-41", "keep")], "not ignored by Git")
        self.assertEqual(reasons[("sf-42", "target")], "contains files tracked by Git")

    def test_run_budget(self):
        self.worktree("SF-50")
        self.worktree("SF-51")
        result = self.plan(max_bytes_per_run=150_000)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertIn("max_bytes_per_run", " ".join(e["reason"] for e in result["excluded"]))


class ApplyTests(Fixture):
    def test_apply_removes_rechecked_candidates_and_records_bytes(self):
        a = self.worktree("SF-60")
        b = self.worktree("SF-61")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        self.assertEqual(len(plan["candidates"]), 2)
        # Between plan and apply, SF-61 is reopened and SF-60 gains a process.
        system.claims_list[1]["status"] = "in_progress"
        result = dc.apply(self.config(), system, plan)
        by_ticket = {r["ticket"]: r for r in result["results"]}
        self.assertEqual(by_ticket["SF-60"]["result"], "removed")
        self.assertGreaterEqual(by_ticket["SF-60"]["freed_bytes"], 100_000)
        self.assertEqual(by_ticket["SF-61"]["result"], "skipped")
        self.assertFalse((a / "target").exists())
        self.assertTrue((b / "target/debug/app").exists())
        self.assertTrue((a / "README").exists(), "source untouched")
        self.assertEqual(list(a.glob(".target.retiring-*")), [])
        self.assertIn("filesystem_free_delta_bytes", result)

    def test_race_detected_immediately_before_each_removal(self):
        a = self.worktree("SF-70")
        b = self.worktree("SF-71")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        calls = {"n": 0}

        def race(s):
            # A worker starts building in SF-71 after SF-70's removal began.
            calls["n"] += 1
            if calls["n"] == 2:
                s.process_paths.append(str(b / "target/debug/app"))
        system.on_evaluate = race
        result = dc.apply(self.config(), system, plan)
        by_ticket = {r["ticket"]: r for r in result["results"]}
        self.assertEqual(by_ticket["SF-70"]["result"], "removed")
        self.assertEqual(by_ticket["SF-71"]["result"], "skipped")
        self.assertIn("in use by a process", by_ticket["SF-71"]["reason"])
        self.assertTrue((b / "target").exists())

    def test_partial_failure_is_reported_and_others_continue(self):
        a = self.worktree("SF-80")
        b = self.worktree("SF-81")
        locked = a / "target/debug/locked"
        locked.mkdir()
        (locked / "f").write_bytes(b"y" * 10_000)
        age(a, 10)
        age(self.root / ".git/worktrees/sf-80", 10)
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        locked.chmod(0o500)
        result = dc.apply(self.config(), system, plan)
        by_ticket = {r["ticket"]: r for r in result["results"]}
        if os.geteuid() != 0:
            self.assertEqual(by_ticket["SF-80"]["result"], "partial")
            self.assertGreater(by_ticket["SF-80"]["error_count"], 0)
            self.assertTrue(by_ticket["SF-80"]["left_aside"])
        self.assertEqual(by_ticket["SF-81"]["result"], "removed")
        self.assertFalse((b / "target").exists())

    def test_apply_refuses_unconfigured_or_forged_entries(self):
        a = self.worktree("SF-90")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        forged = dict(plan["candidates"][0], relative="..", path=str(self.root))
        plan["candidates"].append(forged)
        result = dc.apply(self.config(worktree_outputs=["node_modules"]), system, plan)
        self.assertTrue(all(r["result"] == "skipped" for r in result["results"]))
        self.assertTrue((a / "target").exists())
        self.assertTrue((self.root / "README").exists())
        with self.assertRaises(SystemExit):
            dc.apply(self.config(), system, {"kind": "other"})

    def during_apply_scan(self, action):
        """Run `action` inside apply's in-place scan of each candidate."""
        real = dc.measure

        def measure(path, *args, **kwargs):
            if args or kwargs:
                action(Path(path))
            return real(path, *args, **kwargs)
        return mock.patch.object(dc, "measure", measure)

    def before_rename(self, action):
        """Run `action` immediately before apply renames a candidate aside."""
        real = os.rename

        def rename(src, dst, *args, **kwargs):
            if ".retiring-" in str(dst):
                action(Path(src))
            return real(src, dst, *args, **kwargs)
        return mock.patch.object(dc.os, "rename", rename)

    def test_lock_or_process_appearing_during_scan_aborts_without_moving(self):
        a = self.worktree("SF-110")
        b = self.worktree("SF-111")
        c = self.worktree("SF-112")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        self.assertEqual(len(plan["candidates"]), 3)

        def race(path):
            # Each output must still be in place and usable during the scan.
            self.assertEqual(len((path / "debug/app").read_bytes()), 100_000)
            name = path.parent.name
            if name == "sf-110":
                system.locks_list.append({"resource": "build:sf-110", "purpose": "SF-110 rebuild", "expires": time.time() + 600})
            elif name == "sf-111":
                system.process_paths.append(str(path / "debug"))
            else:
                system.claims_list[2]["status"] = "in_progress"
        renamed = []
        with self.during_apply_scan(race), self.before_rename(renamed.append):
            result = dc.apply(self.config(), system, plan)
        self.assertEqual(renamed, [], "a refused output is never moved")
        by_ticket = {r["ticket"]: r for r in result["results"]}
        self.assertIn("build:sf-110", by_ticket["SF-110"]["reason"])
        self.assertIn("in use by a process", by_ticket["SF-111"]["reason"])
        self.assertIn("ticket status in_progress", by_ticket["SF-112"]["reason"])
        for path in (a, b, c):
            self.assertTrue((path / "target/debug/app").exists(), "aborted candidates stay intact")
            self.assertEqual(list(path.glob(".target.retiring-*")), [])
        self.assertTrue(all(r["result"] == "skipped" for r in result["results"]))
        self.assertEqual(result["measured_freed_bytes"], 0)

    def test_worker_is_excluded_by_the_cleanup_lock_until_deletion_is_decided(self):
        a = self.worktree("SF-200")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        refused = []

        def worker(path):
            # A cooperative worker locks its build resource just before the rename.
            with self.assertRaises(dc.Unverifiable) as error:
                system.lock(None, "build:SF-200", "SF-200", "SF-200 rebuild", session="worker")
            refused.append(str(error.exception))
            self.assertTrue((path / "debug/app").exists(), "output is in place until the rename")
        with self.before_rename(worker):
            result = dc.apply(self.config(), system, plan)
        self.assertEqual(len(refused), 1)
        self.assertIn("cleanup-session", refused[0])
        self.assertEqual(result["results"][0]["result"], "removed")
        self.assertEqual(system.locks_list, [], "the exclusion lock is released")
        # Once released, the worker gets the lock and a fresh output.
        system.lock(None, "build:SF-200", "SF-200", "SF-200 rebuild", session="worker")

    def test_cleanup_holds_the_lock_through_its_scan(self):
        a = self.worktree("SF-201")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        refused = []

        def worker(path):
            with self.assertRaises(dc.Unverifiable) as error:
                system.lock(None, "build:SF-201", "SF-201", "rebuild", session="worker")
            refused.append(str(error.exception))
        with self.during_apply_scan(worker):
            result = dc.apply(self.config(), system, plan)
        self.assertEqual(len(refused), 1, "a cooperative writer cannot start while the cleanup scans")
        self.assertEqual(result["results"][0]["result"], "removed")
        # A worker already holding the lock blocks the cleanup before any scan.
        a = self.worktree("SF-205")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        system.lock(None, "build:SF-205", "SF-205", "rebuild", session="worker")
        scanned = []
        with self.during_apply_scan(scanned.append):
            result = dc.apply(self.config(), system, plan)
        self.assertEqual(result["results"][0]["result"], "skipped")
        self.assertIn("exclusion lock build:SF-205 not available", result["results"][0]["reason"])
        self.assertEqual(scanned, [])
        self.assertTrue((a / "target/debug/app").exists())
        # The coordinator being unavailable for the lock also fails closed.
        system = FakeSystem(self.claims)
        system.lock = mock.Mock(side_effect=dc.Unverifiable("coordinator unavailable"))
        result = dc.apply(self.config(), system, self.plan(FakeSystem(self.claims)))
        self.assertIn("exclusion lock build:SF-205 not available", result["results"][0]["reason"])
        self.assertTrue((a / "target/debug/app").exists())

    def writer_before_cleanup_lock(self, system, ticket, write):
        """A cooperative worker that locks, writes and releases just before
        the cleanup asks for its exclusion lock."""
        real = system.lock

        def lock(project, resource, ticket_key, purpose, session=None):
            if session is None and resource == f"build:{ticket}" and not getattr(lock, "done", False):
                lock.done = True
                token = real(project, resource, ticket, "rebuild", session="worker")
                write()
                system.unlock(project, resource, token)
            return real(project, resource, ticket_key, purpose, session)
        system.lock = lock

    def test_writer_that_finished_before_the_cleanup_lock_is_seen_by_the_scan(self):
        a = self.worktree("SF-206")
        system = FakeSystem(self.claims)
        plan = self.plan(system)

        def write():
            (a / "target/device.key").write_text("k")
            (a / "target/debug/new-output").write_bytes(b"n" * 200_000)
        self.writer_before_cleanup_lock(system, "SF-206", write)
        result = dc.apply(self.config(max_bytes_per_run=150_000), system, plan)
        record = result["results"][0]
        self.assertEqual(record["result"], "skipped")
        self.assertRegex(record["reason"], "protected files inside|recently modified")
        self.assertTrue((a / "target/device.key").exists() and (a / "target/debug/new-output").exists())
        self.assertEqual(system.locks_list, [])
        # Backdated (no recency signal) and unprotected: the fresh size still meets the budget.
        b = self.worktree("SF-207")
        system = FakeSystem(self.claims)
        plan = self.plan(system, max_bytes_per_run=150_000)
        self.assertEqual(len(plan["candidates"]), 1)

        def grow():
            (b / "target/debug/new-output").write_bytes(b"n" * 200_000)
            age(b / "target", 10)
        self.writer_before_cleanup_lock(system, "SF-207", grow)
        result = dc.apply(self.config(max_bytes_per_run=150_000), system, plan)
        record = result["results"][0]
        self.assertEqual(record["result"], "skipped")
        self.assertIn("max_bytes_per_run", record["reason"])
        self.assertGreaterEqual(record["bytes"], 300_000, "the budget is checked on the size measured under the lock")
        self.assertTrue((b / "target/debug/new-output").exists())

    def test_lease_lost_during_the_scan_keeps_the_output(self):
        a = self.worktree("SF-208")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        renamed = []
        with self.during_apply_scan(lambda path: system.locks_list.clear()), self.before_rename(renamed.append):
            result = dc.apply(self.config(), system, plan)
        self.assertEqual(result["results"][0]["result"], "skipped")
        self.assertIn("lost during the scan", result["results"][0]["reason"])
        self.assertEqual(renamed, [])
        self.assertTrue((a / "target/debug/app").exists())

    def test_uncooperative_lock_or_process_before_rename_is_caught_and_restored(self):
        a = self.worktree("SF-202")
        b = self.worktree("SF-203")
        system = FakeSystem(self.claims)
        plan = self.plan(system)

        def race(path):
            if path.parent.name == "sf-202":
                system.locks_list.append({"resource": "build:pr-other", "purpose": "SF-202 rebuild", "session": "w",
                                          "expires": time.time() + 600})
            else:
                system.process_paths.append(str(path.with_name(".target.retiring-%d" % os.getpid()) / "debug/app"))
        with self.before_rename(race):
            result = dc.apply(self.config(), system, plan)
        by_ticket = {r["ticket"]: r for r in result["results"]}
        self.assertIn("build:pr-other", by_ticket["SF-202"]["reason"])
        self.assertIn("in use by a process", by_ticket["SF-203"]["reason"])
        for path in (a, b):
            self.assertTrue((path / "target/debug/app").exists(), "renamed back intact")
            self.assertEqual(list(path.glob(".target.retiring-*")), [])
        self.assertEqual([l["resource"] for l in system.locks_list], ["build:pr-other"])

    def test_external_hard_links_are_not_counted_as_freed(self):
        a = self.worktree("SF-204")
        outside = self.tmp / "kept-link"
        os.link(a / "target/debug/app", outside)
        age(a, 10)
        age(self.root / ".git/worktrees/sf-204", 10)
        whole = dc.measure(self.tmp)
        self.assertEqual(whole["exclusive_bytes"], whole["bytes"], "links that are all inside a tree are freed with it")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        self.assertLess(plan["candidate_exclusive_bytes"], 100_000)
        result = dc.apply(self.config(), system, plan)
        record = result["results"][0]
        self.assertEqual(record["result"], "removed")
        self.assertGreaterEqual(record["removed_allocated_bytes"], 100_000)
        self.assertLess(record["freed_bytes"], 100_000, "blocks still linked outside are not reclaimed")
        self.assertEqual(len(outside.read_bytes()), 100_000)

    def test_recreated_output_after_rename_keeps_old_tree_aside(self):
        a = self.worktree("SF-115")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        real = os.rename

        def rename(src, dst):
            real(src, dst)
            if ".retiring-" in str(dst):
                system.process_paths.append(str(dst))
                Path(src).mkdir()
        with mock.patch.object(dc.os, "rename", rename):
            result = dc.apply(self.config(), system, plan)
        record = result["results"][0]
        self.assertEqual(record["result"], "skipped")
        self.assertIn("left aside, not deleted", record["reason"])
        self.assertTrue((Path(record["left_aside"]) / "debug/app").exists())

    def test_apply_enforces_cumulative_budget_on_fresh_sizes(self):
        a = self.worktree("SF-120")
        b = self.worktree("SF-121")
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        self.assertEqual(len(plan["candidates"]), 2)
        # The configured ceiling shrank after planning: nothing fits.
        result = dc.apply(self.config(max_bytes_per_run=1), system, plan)
        self.assertTrue(all(r["result"] == "skipped" and "max_bytes_per_run" in r["reason"] for r in result["results"]))
        self.assertTrue((a / "target/debug/app").exists() and (b / "target/debug/app").exists())
        # Room for one: the second is refused on the cumulative total.
        result = dc.apply(self.config(max_bytes_per_run=150_000), system, plan)
        self.assertEqual(sorted(r["result"] for r in result["results"]), ["removed", "skipped"])
        self.assertLessEqual(result["measured_freed_bytes"], 150_000)

    def test_apply_measures_grown_candidate_against_budget(self):
        c = self.worktree("SF-122")
        system = FakeSystem(self.claims)
        plan = self.plan(system, max_bytes_per_run=150_000)
        self.assertEqual(len(plan["candidates"]), 1)
        (c / "target/debug/grown").write_bytes(b"g" * 100_000)
        age(c, 10)
        result = dc.apply(self.config(max_bytes_per_run=150_000), system, plan)
        self.assertEqual(result["results"][0]["result"], "skipped")
        self.assertIn("max_bytes_per_run", result["results"][0]["reason"])
        self.assertTrue((c / "target/debug/grown").exists())

    def test_uninspectable_processes_fail_closed(self):
        a = self.worktree("SF-130")
        result = self.plan(FakeSystem(self.claims, uninspectable=[4242]))
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["unreadable_processes"], 1)
        self.assertIn("could not be inspected", self.reasons(result)[("sf-130", "target")])
        # An explicitly accepted foreign uid does not block...
        accepted = self.plan(FakeSystem(self.claims, uninspectable=[4242]), uninspectable_uids_accepted=[4242])
        self.assertEqual(len(accepted["candidates"]), 1)
        # ...but acceptance never covers the candidate owner's own processes.
        owner = (a / "target").lstat().st_uid
        result = self.plan(FakeSystem(self.claims, uninspectable=[owner]), uninspectable_uids_accepted=[owner])
        self.assertIn(f"owner uid {owner} could not be inspected", self.reasons(result)[("sf-130", "target")])
        # Becoming uninspectable during apply also aborts.
        system = FakeSystem(self.claims)
        plan = self.plan(system)
        with self.during_apply_scan(lambda path: system.uninspectable.append(4242)):
            result = dc.apply(self.config(), system, plan)
        self.assertEqual(result["results"][0]["result"], "skipped")
        self.assertTrue((a / "target/debug/app").exists())

    def test_system_processes_counts_partial_inspection_as_uninspectable(self):
        real_readlink = os.readlink

        def readlink(path, *args, **kwargs):
            if "/fd/" in str(path):
                raise PermissionError(13, "denied")
            return real_readlink(path, *args, **kwargs)
        with mock.patch.object(dc.os, "readlink", readlink):
            paths, uninspectable = dc.System({}).processes()
        self.assertGreater(len(uninspectable), 0, "a denied descriptor is not silently skipped")


class YoctoTests(Fixture):
    def tree(self, days=10):
        build = self.tmp / "yocto/build"
        for sub in ("tmp/work/x", "tmp/deploy/images", "tmp/log", "conf", "downloads"):
            (build / sub).mkdir(parents=True, exist_ok=True)
        (build / "tmp/work/x/obj").write_bytes(b"z" * 50_000)
        (build / "tmp/deploy/images/image.wic").write_bytes(b"i" * 1000)
        (build / "conf/local.conf").write_text("x")
        age(build.parent, days)
        return build

    def test_only_configured_work_is_retired_and_preserved_outputs_stay(self):
        build = self.tree()
        self.assertEqual([c for c in self.plan()["candidates"] if c["kind"] == "yocto-work"], [], "unconfigured trees are never touched")
        config = self.config(yocto=[{"path": str(build), "retire": ["tmp/work"]}])
        system = FakeSystem(self.claims)
        plan = dc.plan(config, system)
        self.assertEqual([c["path"] for c in plan["candidates"]], [str(build / "tmp/work")])
        result = dc.apply(config, system, plan)
        self.assertEqual(result["results"][0]["result"], "removed")
        self.assertFalse((build / "tmp/work").exists())
        for kept in ("tmp/deploy/images/image.wic", "conf/local.conf", "downloads"):
            self.assertTrue((build / kept).exists(), kept)
        bad = self.tmp / "bad.json"
        for entry in ("tmp/deploy", "downloads", "tmp/work/../deploy", "conf"):
            bad.write_text(json.dumps(self.config(yocto=[{"path": str(build), "retire": [entry]}])))
            with self.assertRaises(SystemExit, msg=entry):
                dc.load_config(bad)

    def test_active_locked_or_running_trees_are_kept(self):
        build = self.tree()
        config = self.config(yocto=[{"path": str(build), "retire": ["tmp/work"]}])
        (build / "bitbake.lock").write_text("")
        age(build.parent, 10)
        self.assertIn("bitbake.lock", self.reasons(dc.plan(config, FakeSystem(self.claims)))[("tmp", "tmp/work")])
        (build / "bitbake.lock").unlink()
        locks = [{"resource": "build:sf053-yocto", "purpose": "BitBake rebuild", "expires": time.time() + 600}]
        self.assertIn("build:sf053-yocto", self.reasons(dc.plan(config, FakeSystem(self.claims, locks=locks)))[("tmp", "tmp/work")])
        self.assertIn("in use", self.reasons(dc.plan(config, FakeSystem(self.claims, processes=[str(build)])))[("tmp", "tmp/work")])
        fresh = self.tree(days=0)
        self.assertIn("tree active", self.reasons(dc.plan(config, FakeSystem(self.claims)))[("tmp", "tmp/work")])
        self.assertTrue((fresh / "tmp/work/x/obj").exists())

    def test_unreadable_subtree_blocks_plan_and_apply(self):
        build = self.tree()
        hidden = build / "tmp/work/x/secret"
        hidden.mkdir()
        (hidden / "device.key").write_text("k")
        age(build.parent, 10)
        config = self.config(yocto=[{"path": str(build), "retire": ["tmp/work"]}])
        system = FakeSystem(self.claims)
        plan = dc.plan(config, system)
        self.assertEqual(len(plan["candidates"]), 0, "protected material is visible while readable")
        (hidden / "device.key").unlink()
        age(build.parent, 10)
        plan = dc.plan(config, system)
        self.assertEqual(len(plan["candidates"]), 1)
        (hidden / "device.key").write_text("k")
        hidden.chmod(0)
        age(build.parent, 10)
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        self.assertIn("unreadable entries", self.reasons(dc.plan(config, system))[("tmp", "tmp/work")])
        result = dc.apply(config, system, plan)
        self.assertEqual(result["results"][0]["result"], "skipped")
        self.assertIn("unreadable entries", result["results"][0]["reason"])
        hidden.chmod(0o700)
        self.assertTrue((hidden / "device.key").exists(), "unverifiable tree restored intact")
        hidden.chmod(0)
        report = dc.audit(config, system)
        self.assertEqual(report["yocto"][0]["unreadable"], {"tmp/work": 1})

    def test_unreadable_tmp_or_build_root_is_reported_and_blocks(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        build = self.tree()
        config = self.config(yocto=[{"path": str(build), "retire": ["tmp/work"]}])
        system = FakeSystem(self.claims)
        stale = dc.plan(config, system)
        self.assertEqual(len(stale["candidates"]), 1)
        for closed in (build / "tmp", build):
            closed.chmod(0)
            try:
                before = self.snapshot() if closed != build else None
                report = dc.audit(config, system)
                tree = report["yocto"][0]
                self.assertIn(str(closed), tree["unscannable"], "an unlistable directory is reported, not an exception")
                self.assertFalse(tree["complete"])
                self.assertIsNone(tree["idle_days"], "activity cannot be verified, so it is not reported as idle")
                self.assertIn(str(closed), report["unscannable"])
                self.assertIn("filesystems", report, "other categories are still produced")
                self.assertTrue(all(not k.startswith("tmp/") for k in tree["parts"]))
                if closed != build:
                    self.assertIn("conf", tree["parts"], "readable parts are still sized")
                    self.assertEqual(self.snapshot(), before)
                reason = self.reasons(dc.plan(config, system))[("tmp", "tmp/work")]
                self.assertIn("cannot inspect path", reason)
                result = dc.apply(config, system, stale)
                self.assertEqual(result["results"][0]["result"], "skipped")
                self.assertRegex(result["results"][0]["reason"], "cannot inspect path|unverifiable")
            finally:
                closed.chmod(0o755)
            self.assertTrue((build / "tmp/work/x/obj").exists(), "nothing was moved or deleted")
        # Readable candidate, unreadable activity marker: the tree is not assumed idle.
        real_lstat = dc.os.lstat

        def lstat(path, *args, **kwargs):
            if str(path).endswith("tmp/log"):
                raise PermissionError(13, "denied", str(path))
            return real_lstat(path, *args, **kwargs)
        with mock.patch.object(dc.os, "lstat", lstat):
            self.assertIn("tree activity unverifiable", self.reasons(dc.plan(config, system))[("tmp", "tmp/work")])
            result = dc.apply(config, system, stale)
        self.assertIn("tree activity unverifiable", result["results"][0]["reason"])
        self.assertTrue((build / "tmp/work/x/obj").exists())

    def test_unreadable_worktree_and_report_path_do_not_abort_audit(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        self.worktree("SF-100")
        closed = self.tmp / "worktrees"
        closed.chmod(0)
        try:
            report = dc.audit(self.config(report_paths=[str(closed / "sf-100")]), FakeSystem(self.claims))
            self.assertIn(str(closed / "sf-100"), report["unscannable"])
            self.assertEqual(report["report_paths"], [])
            plan = dc.plan(self.config(), FakeSystem(self.claims))
            self.assertEqual(plan["candidates"], [])
            self.assertTrue(any("cannot inspect path" in e["reason"] for e in plan["excluded"]))
        finally:
            closed.chmod(0o755)


class AuditTests(Fixture):
    def test_audit_reports_categories_without_mutation(self):
        self.worktree("SF-100")
        self.worktree("SF-101", status="review")
        before = self.snapshot()
        report = dc.audit(self.config(report_paths=[str(self.tmp / "worktrees")]), FakeSystem(self.claims))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(report["worktrees"]["p"]["done"]["worktrees"], 1)
        self.assertGreaterEqual(report["worktrees"]["p"]["review"]["regenerable_bytes"], 100_000)
        self.assertIn(report["filesystems"][0]["pressure"], ("ok", "warn", "critical"))
        self.assertIn("never removed", report["docker"]["note"])
        unreadable = self.tmp / "worktrees/sf-100/target/debug/closed"
        unreadable.mkdir()
        unreadable.chmod(0)
        if os.geteuid() != 0:
            self.assertEqual(dc.measure(self.tmp / "worktrees/sf-100")["unreadable"], 1, "unreadable entries are reported, not hidden")


if __name__ == "__main__":
    unittest.main()
