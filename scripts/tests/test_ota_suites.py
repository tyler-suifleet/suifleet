import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('ota_suites', ROOT/'scripts/run_ota_suites.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_overlap_respects_bound_and_keeps_all_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            commands = [(f'suite-{i}', [sys.executable, '-c',
                'import time,json,pathlib; a=time.monotonic(); time.sleep(.3); '
                f'pathlib.Path({str(root/str(i))!r}).write_text(json.dumps([a,time.monotonic()])); print("suite output")']) for i in range(5)]
            self.assertEqual(runner.run_suites(commands, 2, root/'logs'), 0)
            spans = [json.loads((root/str(i)).read_text()) for i in range(5)]
            events = sorted([(a, 1) for a,b in spans] + [(b,-1) for a,b in spans])
            current = maximum = 0
            for _, delta in events:
                current += delta
                maximum = max(maximum, current)
            self.assertEqual(maximum, 2)
            directory = next((root/'logs').iterdir())
            self.assertEqual(len(list(directory.glob('*.log'))), 5)
            self.assertTrue(all('suite output' in f.read_text() for f in directory.glob('*.log')))
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            summary = json.loads((directory/'summary.json').read_text())
            self.assertTrue(summary['complete'])
            self.assertEqual(len(summary['results']), 5)

    def test_sequential_fallback_propagates_failure_and_runs_later_suite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code = runner.run_suites([
                ('failure', [sys.executable, '-c', 'print("failure diagnostics"); raise SystemExit(17)']),
                ('later', [sys.executable, '-c', f'from pathlib import Path; Path({str(root/"ran")!r}).touch()'])], 1, root/'logs')
            self.assertEqual(code, 1)
            self.assertTrue((root/'ran').exists())
            summary = json.loads(next((root/'logs').glob('*/summary.json')).read_text())
            self.assertEqual(summary['results'][0]['returncode'], 17)
            self.assertTrue(summary['complete'])

    def test_timeout_is_failure_even_if_suite_handles_interrupt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            command = [sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGINT,lambda *_:exit(0)); time.sleep(30)']
            self.assertEqual(runner.run_suites([('timeout', command)], 1, root, timeout=.2), 1)
            summary = json.loads(next(root.glob('*/summary.json')).read_text())
            self.assertTrue(summary['results'][0]['timed_out'])

    def test_invalid_configuration_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            for jobs in [0,9]:
                with self.assertRaises(ValueError):runner.run_suites([],jobs,Path(temp))
            with self.assertRaises(ValueError):runner.run_suites([('../escape',[])],1,Path(temp))
            with self.assertRaises(ValueError):runner.run_suites([('same',[]),('same',[])],1,Path(temp))

    def test_termination_cleans_child_process_group_and_stops_launches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child = ('import subprocess,time,pathlib; '
                     'p=subprocess.Popen(["sleep","30"]); '
                     f'pathlib.Path({str(root/"pid")!r}).write_text(str(p.pid)); time.sleep(30)')
            commands = [('child',[sys.executable,'-c',child]),('must-not-start',[sys.executable,'-c',f'from pathlib import Path;Path({str(root/"bad")!r}).touch()'])]
            harness = ('import sys,signal;sys.path.insert(0,'+repr(str(ROOT/'scripts'))+');'
                       'from run_ota_suites import run_suites;'
                       'signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()));'
                       f'run_suites({commands!r},1,__import__("pathlib").Path({str(root/"logs")!r}))')
            process = subprocess.Popen([sys.executable,'-c',harness], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pid = None
            try:
                deadline = time.monotonic()+10
                while not (root/'pid').exists() and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue((root/'pid').exists())
                pid = int((root/'pid').read_text())
                process.terminate();process.wait(timeout=10)
                self.assertNotEqual(process.returncode,0)
                status = Path(f'/proc/{pid}/stat')
                self.assertTrue(not status.exists() or status.read_text().split()[2]=='Z')
                self.assertFalse((root/'bad').exists())
                summary = json.loads(next((root/'logs').glob('*/summary.json')).read_text())
                self.assertFalse(summary['complete'])
                self.assertTrue(summary['results'][0]['interrupted'])
            finally:
                if process.poll() is None:process.kill();process.wait()
                if pid:
                    try:os.kill(pid,signal.SIGKILL)
                    except ProcessLookupError:pass

    def test_manifest_retains_every_ota_mode(self):
        self.assertEqual(len(runner.SUITES),29)
        self.assertEqual(len({s[0] for s in runner.SUITES}),29)
        ota = [s[2:] for s in runner.SUITES if s[1]=='test_ota.py']
        self.assertEqual(set(ota),{(),('--poll-reads',),('--platform-installer',),('--firmware-helper',)})
        fleet = [s for s in runner.SUITES if s[1]=='test_fleet_data_ota.py']
        self.assertEqual(len(fleet),5)
        self.assertTrue(any('--stream-retention' in s and '--quilt' in s for s in fleet))
        self.assertTrue(all((ROOT/'scripts'/s[1]).is_file() for s in runner.SUITES))


if __name__ == '__main__':unittest.main()
