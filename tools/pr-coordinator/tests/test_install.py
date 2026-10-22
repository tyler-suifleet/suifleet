import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ScopedInstall(unittest.TestCase):
    def test_coordinator_only_preserves_running_dependencies_and_config(self):
        source=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)
            binary=home/'bin';binary.mkdir()
            command=binary/'systemctl';command.write_text('#!/bin/sh\nexit 0\n');command.chmod(0o755)
            sentinels=['.local/lib/pr-coordinator/worker.py','.local/lib/pr-coordinator/dispatcher.py','.config/pr-coordinator/config.json','.config/systemd/user/pr-coordinator.service','.config/systemd/user/ticket-dispatcher.service']
            for name in sentinels:
                path=home/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('preserved '+name)
            result=subprocess.run([sys.executable,str(source/'install.py'),'--coordinator-only'],env={**os.environ,'HOME':directory,'PATH':str(binary)+os.pathsep+os.environ['PATH']},capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            for name in sentinels:self.assertEqual((home/name).read_text(),'preserved '+name)
            self.assertEqual((home/'.local/lib/pr-coordinator/coordinator.py').read_bytes(),(source/'coordinator.py').read_bytes())

    def test_dashboard_only_touches_no_engine_service_or_config(self):
        source=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            binary = home/'bin'; binary.mkdir()
            marker = home/'systemctl-called'
            command = binary/'systemctl'; command.write_text(f'#!/bin/sh\ntouch {marker}\n'); command.chmod(0o755)
            sentinels = ['.local/lib/pr-coordinator/coordinator.py', '.local/lib/pr-coordinator/dispatcher.py',
                         '.local/lib/pr-coordinator/worker.py', '.config/pr-coordinator/config.json',
                         '.config/systemd/user/pr-coordinator.service', '.config/systemd/user/ticket-dispatcher.service']
            for name in sentinels:
                path = home/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('preserved '+name)
            prototype = home/'old-prototype.py'
            prototype.write_text('original prototype')
            alias = home/'.local/bin/fleet-status'
            alias.parent.mkdir(parents=True)
            alias.symlink_to(prototype)
            env = {**os.environ, 'HOME': directory, 'PATH': str(binary)+os.pathsep+os.environ['PATH']}
            result = subprocess.run([sys.executable, str(source/'install.py'), '--dashboard-only'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in sentinels:
                self.assertEqual((home/name).read_text(), 'preserved '+name)
            self.assertFalse(marker.exists(), 'dashboard install must not call systemctl')
            self.assertEqual(prototype.read_text(), 'original prototype')
            self.assertFalse(alias.is_symlink())
            self.assertEqual((home/'.local/lib/pr-coordinator/dashboard.py').read_bytes(), (source/'dashboard.py').read_bytes())
            self.assertEqual((home/'.local/bin/fleet-status').read_bytes(), (home/'.local/bin/agent-dashboard').read_bytes())
            launcher = home/'.local/bin/agent-dashboard'
            run = subprocess.run([str(launcher), '--json'], env={**env, 'XDG_STATE_HOME': str(home/'state'), 'XDG_CONFIG_HOME': str(home/'cfg')},
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(run.returncode, 3, run.stderr)
            self.assertEqual({s['state'] for s in json.loads(run.stdout)['sources'].values()}, {'unavailable'})
