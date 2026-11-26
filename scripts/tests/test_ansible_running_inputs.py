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

    def test_reload_precedes_every_start_or_restart(self):
        reload = self.task("Load unit changes")
        for index, task in enumerate(self.tasks):
            state = task.get("ansible.builtin.systemd_service", {}).get("state")
            if state in ("started", "restarted"):
                self.assertGreater(index, reload, task["name"])


if __name__ == "__main__":
    unittest.main()
