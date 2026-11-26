"""SF-304: the Ansible role's running-inputs reporter and its reload decision, offline.

systemctl is replaced by a scripted sequence of samples; /proc reads are real
(a reaped child gives the process-exit window the reporter must survive).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROLE = Path(__file__).resolve().parents[2] / "examples/ansible/roles/suifleet"
SPEC = importlib.util.spec_from_file_location("running_inputs", ROLE / "files/running_inputs.py")
running_inputs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(running_inputs)


def props(pid=0, invocation="", active="inactive", reload="no"):
    return {"ActiveState": active, "MainPID": str(pid), "InvocationID": invocation, "NeedDaemonReload": reload,
            "FragmentPath": "", "DropInPaths": ""}


def exited_pid():
    child = subprocess.Popen(["true"])
    child.wait()  # reaped: /proc/<pid> is gone
    assert not os.path.exists(f"/proc/{child.pid}")
    return child.pid


class Scripted:
    """Stands in for show(): returns the given samples in order, then repeats the last."""

    def __init__(self, *samples):
        self.samples, self.calls = list(samples), 0

    def __call__(self, unit):
        self.calls += 1
        return self.samples.pop(0) if len(self.samples) > 1 else self.samples[0]


class Reporter(unittest.TestCase):
    def setUp(self):
        self.saved = running_inputs.show
        sleep = mock.patch("time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def tearDown(self):
        running_inputs.show = self.saved

    def check(self, *samples, unit="suifleet.service", applied=None):
        running_inputs.show = show = Scripted(*samples)
        return running_inputs.check(unit, [], applied or {}), show

    def test_stopped_unit_with_unloaded_changes_reports_a_pending_reload(self):
        for unit in ("suifleet.service", "suifleet-signer.socket", "suifleet-enrollment.path"):
            report, _ = self.check(props(reload="yes"), unit=unit)
            self.assertEqual((report["active"], report["stale"], report["reload_needed"]), (False, False, True), unit)

    def test_main_process_exiting_before_its_executable_is_read_is_resampled(self):
        report, show = self.check(props(exited_pid(), "inv-1", "active"), props(0, "", "inactive"))
        self.assertEqual((report["active"], report["exe_replaced"], report["stale"]), (False, False, False))
        self.assertEqual(show.calls, 2)

    def test_restart_during_inspection_uses_the_new_invocation(self):
        mine = os.getpid()
        report, _ = self.check(props(exited_pid(), "old", "active"), props(mine, "new", "active"),
                               props(mine, "new", "active"), applied={"suifleet.service": "x"})
        self.assertEqual(report["invocation"], "new")
        self.assertTrue(report["active"])
        self.assertFalse(report["exe_replaced"])

    def test_sample_whose_invocation_changed_after_the_read_is_not_used(self):
        mine = os.getpid()
        report, show = self.check(props(mine, "a", "active"), props(mine, "b", "active"), props(mine, "b", "active"),
                                  props(mine, "b", "active"))
        self.assertEqual(report["invocation"], "b")
        self.assertEqual(show.calls, 4)

    def test_unstable_unit_fails_clearly_after_bounded_attempts(self):
        pids = [props(exited_pid(), f"inv-{i}", "active") for i in range(3)]
        running_inputs.show = show = Scripted(*pids)
        with self.assertRaises(SystemExit) as raised:
            running_inputs.check("suifleet.service", [], {})
        self.assertIn("kept changing", str(raised.exception.code))
        self.assertEqual(show.calls, getattr(running_inputs, "SAMPLE_ATTEMPTS", None))

    def test_active_unit_with_unloaded_changes_is_stale(self):
        report, _ = self.check(props(os.getpid(), "i", "active", reload="yes"))
        self.assertTrue(report["stale"] and report["reload_needed"])

    def test_cli_reports_every_unit_as_json(self):
        bindir = Path(tempfile.mkdtemp())
        (bindir / "systemctl").write_text("#!/bin/sh\nprintf 'ActiveState=inactive\\nMainPID=0\\nInvocationID=\\n"
                                          "NeedDaemonReload=yes\\nFragmentPath=\\nDropInPaths=\\n'\n")
        (bindir / "systemctl").chmod(0o700)
        out = subprocess.run([sys.executable, "-I", ROLE / "files/running_inputs.py", bindir / "absent.json",
                              "suifleet-signer.socket=", "suifleet.service=/nonexistent"],
                             env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"),
                             check=True, capture_output=True, text=True).stdout
        report = json.loads(out)
        self.assertEqual(sorted(report), ["suifleet-signer.socket", "suifleet.service"])
        self.assertTrue(all(r["reload_needed"] and not r["stale"] for r in report.values()))

    def cli(self, *arguments, samples, stdin=None):
        """Run the reporter with systemctl replaced by a script printing one unit's properties."""
        bindir = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", bindir])
        lines = "".join(f"{k}={v}\\n" for k, v in samples.items())
        (bindir / "systemctl").write_text(f"#!/bin/sh\nprintf '{lines}'\n")
        (bindir / "systemctl").chmod(0o700)
        out = subprocess.run([sys.executable, "-I", ROLE / "files/running_inputs.py", *arguments,
                              bindir / "absent.json", "suifleet-signer.socket="],
                             env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"), input=stdin,
                             check=True, capture_output=True, text=True).stdout
        return json.loads(out)["suifleet-signer.socket"]

    def test_drop_in_loaded_by_the_reload_is_verified_against_the_loaded_baseline(self):
        """A drop-in added before the run appears in DropInPaths only after the reload."""
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", folder])
        fragment, drop_in = folder / "suifleet-signer.socket", folder / "sf304.conf"
        fragment.write_text("[Socket]\n")
        drop_in.write_text("[Socket]\nSocketMode=0620\n")
        cached = dict(ActiveState="active", MainPID="0", InvocationID="old", NeedDaemonReload="yes",
                      FragmentPath=fragment, DropInPaths="")
        loaded = dict(cached, NeedDaemonReload="no", DropInPaths=drop_in)
        restarted = dict(loaded, InvocationID="new")
        before = self.cli(samples=cached)
        self.assertTrue(before["stale"] and before["reload_needed"])
        report = json.dumps({"suifleet-signer.socket": before})
        # Without the loaded baseline the path list alone reads as an input change.
        self.assertTrue(self.cli("--since", samples=restarted, stdin=report)["changed_since"])
        baseline = self.cli("--rebase", samples=loaded, stdin=report)
        self.assertEqual({k: v for k, v in baseline.items() if k != "digest"},
                         {k: v for k, v in before.items() if k != "digest"}, "restart decision changed")
        after = self.cli("--since", samples=restarted, stdin=json.dumps({"suifleet-signer.socket": baseline}))
        self.assertEqual((after["changed_since"], after["not_restarted"], after["reload_needed"]), (False, False, False))
        self.assertEqual(after["digest"], baseline["digest"])
        # The same for a drop-in removed before the run.
        removed = self.cli("--rebase", samples=dict(cached, DropInPaths=""),
                           stdin=json.dumps({"suifleet-signer.socket": self.cli(samples=dict(cached, DropInPaths=drop_in))}))
        self.assertFalse(self.cli("--since", samples=dict(cached, NeedDaemonReload="no", InvocationID="new"),
                                  stdin=json.dumps({"suifleet-signer.socket": removed}))["changed_since"])

    def test_rebase_keeps_the_earlier_digest_of_units_that_did_not_need_the_reload(self):
        """A change after the first sample of a loaded unit is still detected."""
        previous = {"a": dict(digest="d1", reload_needed=False, stale=False), "b": dict(digest="d2", reload_needed=True, stale=True)}
        current = {"a": dict(digest="x1"), "b": dict(digest="x2")}
        self.assertEqual(running_inputs.rebase(previous, current),
                         {"a": dict(digest="d1", reload_needed=False, stale=False),
                          "b": dict(digest="x2", reload_needed=True, stale=True)})


class IdentityTasks(unittest.TestCase):
    """A pending provisioning-unit definition is loaded before the oneshot starts."""

    def test_pending_provisioning_reload_precedes_its_start(self):
        import yaml

        tasks = yaml.safe_load((ROLE / "tasks/identity.yml").read_text())
        names = [t["name"] for t in tasks]
        read = tasks[names.index("Read whether the provisioning unit has unloaded changes")]
        self.assertEqual(read["ansible.builtin.command"]["argv"][-1], "suifleet-provision.service")
        self.assertIn("NeedDaemonReload", read["ansible.builtin.command"]["argv"])
        self.assertIs(read["check_mode"], False)
        reload = tasks[names.index("Load the installed provisioning unit before starting it")]
        self.assertTrue(reload["ansible.builtin.systemd_service"]["daemon_reload"])
        self.assertEqual(reload["when"], "suifleet_provision_reload.stdout == 'yes'")
        start = names.index("Provision the on-device identity")
        self.assertEqual(tasks[start]["ansible.builtin.systemd_service"]["state"], "started")
        self.assertLess(names.index("Load the installed provisioning unit before starting it"), start)


class ServiceTasks(unittest.TestCase):
    """The role reloads systemd for a pending reload on any unit, before it starts one."""

    def setUp(self):
        import yaml  # PyYAML: Ubuntu python3-yaml or the pinned controller

        self.tasks = yaml.safe_load((ROLE / "tasks/service.yml").read_text())[0]["block"]

    def task(self, prefix):
        return next(i for i, t in enumerate(self.tasks) if t["name"].startswith(prefix))

    def test_pending_reload_on_any_unit_triggers_the_reload(self):
        decide = self.tasks[self.task("Decide which units to restart")]["ansible.builtin.set_fact"]
        self.assertIn("selectattr('value.reload_needed')", decide["suifleet_reload_pending"])
        self.assertNotIn("active", decide["suifleet_reload_pending"])
        reload = self.tasks[self.task("Load unit changes")]
        self.assertTrue(reload["ansible.builtin.systemd_service"]["daemon_reload"])
        self.assertIn("suifleet_reload_pending | bool", reload["when"])

    def test_loaded_baseline_is_sampled_after_the_reload_and_used_by_verification(self):
        import yaml

        rebase = self.task("Record the loaded inputs")
        self.assertGreater(rebase, self.task("Load unit changes"))
        argv = self.tasks[rebase]["ansible.builtin.command"]["argv"]
        self.assertIn("--rebase", argv)
        verify = yaml.safe_load((ROLE / "tasks/verify.yml").read_text())[1]["block"]
        check = next(t for t in verify if t["name"].startswith("Check that the active units use the installed inputs"))
        self.assertIn("suifleet_running_loaded.stdout", check["ansible.builtin.command"]["stdin"])
        record = next(t for t in verify if t["name"].startswith("Record the inputs the active units were verified"))
        self.assertIn("suifleet_running_after.stdout", record["vars"]["verified"])

    def test_reload_precedes_every_start_or_restart(self):
        reload = self.task("Load unit changes")
        for index, task in enumerate(self.tasks):
            state = task.get("ansible.builtin.systemd_service", {}).get("state")
            if state in ("started", "restarted"):
                self.assertGreater(index, reload, task["name"])


if __name__ == "__main__":
    unittest.main()
