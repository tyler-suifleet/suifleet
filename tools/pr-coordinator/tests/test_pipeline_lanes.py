"""Offline process isolation and routing tests; no GitHub/model commands."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coordinator as c


class PipelineLanes(unittest.TestCase):
    def engine(self, directory, lane):
        engine = c.Coordinator({'workers': {'Codex': ['true'], 'Claude': ['true']}, 'projects': []}, Path(directory))
        engine.lane = lane
        for name in ('owners', 'collect', 'retire_closed_review_retries', 'discover_merged', 'prs', 'tick_ci'):
            setattr(engine, name, Mock(return_value=[]))
        engine.batches.tick = Mock(return_value=set())
        return engine

    def test_completion_never_waits_for_snapshots_ci_or_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine(directory, 'completion')
            engine.tick_project(dict(id='p'), True)
            engine.collect.assert_called_once()
            engine.prs.assert_not_called()
            engine.discover_merged.assert_not_called()
            engine.batches.tick.assert_not_called()

    def test_ci_never_waits_for_ownership_or_source_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine(directory, 'ci')
            project = dict(id='p')
            engine.tick_project(project, True)
            engine.tick_ci.assert_called_once_with(project)
            engine.owners.assert_not_called()
            engine.collect.assert_not_called()
            engine.batches.tick.assert_not_called()

    def test_source_cannot_collect_repair_or_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine(directory, 'source')
            engine.tick_project(dict(id='p', integration_batch={'enabled': True}), True)
            engine.prs.assert_called_once()
            engine.collect.assert_not_called()
            engine.discover_merged.assert_not_called()
            engine.batches.tick.assert_not_called()

    def test_integration_does_not_collect_or_run_ci_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine(directory, 'integration')
            engine.hosted_runner_recovery.scan = Mock()
            engine.tick_project(dict(id='p', integration_batch={'enabled': True}), True)
            engine.batches.tick.assert_called_once()
            engine.collect.assert_not_called()
            engine.hosted_runner_recovery.scan.assert_not_called()
            self.assertEqual(len(engine.prs.call_args.args), 3)

    def test_candidate_advances_before_unrelated_source_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine(directory, 'integration')
            project = dict(id='p', repository='a/b', review_github_account='reviewer', integration_batch={'enabled': True})
            engine.store.db.execute('INSERT INTO integration_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('p','batch','awaiting_ci','b'*40,'a'*40,'integration/batch',99,'[]',directory,None,0,0,None,0,None))
            engine.gh = Mock(return_value=json.dumps([[dict(number=1),dict(number=99)]]))
            events = []
            engine.pr = Mock(side_effect=lambda p,n,f: events.append(('snapshot',n)) or dict(number=n))
            engine.batches.advance = Mock(side_effect=lambda *args: events.append(('advance',args[1]['pr'])))
            # Use actual discovery/sorting with a deliberately earlier source PR.
            c.Coordinator.prs(engine, project, lambda pr: engine.advance_candidate_snapshot(project,pr))
            self.assertEqual(events, [('snapshot',99),('advance',99),('snapshot',1)])

    def test_source_failure_is_discovered_before_any_completed_review(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine(directory, 'integration')
            project = dict(id='p', repository='a/b')
            owner = dict(branch='work', status='review', spec={'area': 'edge'})
            row = dict(number=7, head=dict(ref='work', sha='a'*40))
            pr = dict(number=7, headRefOid='a'*40, baseRefOid='b'*40,
                      _review_mode='source', threads=[], comments=[], reviews=[],
                      statusCheckRollup=[dict(__typename='CheckRun', name='Security CodeQL',
                                              status='COMPLETED', conclusion='FAILURE',
                                              detailsUrl='https://example.org/check/7')])
            self.assertFalse(engine.discovery_needed(project, row, [owner]))
            engine.observe_pr(project, pr)
            self.assertTrue(engine.discovery_needed(project, row, [owner]))
            self.assertFalse(engine.discovery_needed(project, row, [dict(owner, expired=True)]))
            row['head']['sha'] = 'c'*40
            self.assertFalse(engine.discovery_needed(project, row, [owner]))
            row['head']['sha'] = pr['headRefOid']
            pr['statusCheckRollup'][0].update(status='IN_PROGRESS', conclusion=None)
            engine.observe_pr(project, pr)
            self.assertFalse(engine.discovery_needed(project, row, [owner]))

    def test_lane_lock_fences_duplicates_but_not_other_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with c.lane_lock(root, 'p', 'integration'):
                with self.assertRaises(BlockingIOError):
                    with c.lane_lock(root, 'p', 'integration'):
                        self.fail('duplicate publisher acquired lock')
                with c.lane_lock(root, 'p', 'source'):
                    pass
                with c.lane_lock(root, 'other', 'integration'):
                    pass
            with c.lane_lock(root, 'p', 'integration'):
                pass

    def test_invalid_lane_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                with c.lane_lock(Path(directory), '../outside', 'source'):
                    self.fail('unsafe project name')

    def test_real_processes_progress_while_source_blocks_and_stop_with_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root/'driver.py'
            driver.write_text('''import sys,time
from pathlib import Path
sys.path.insert(0, ''' + repr(str(Path(c.__file__).parent)) + ''')
import coordinator as c
class Fake:
    def __init__(self, config, state): self.state=state
    def tick(self, execute):
        (self.state/(self.lane+'.tick')).write_text(str(time.time()))
        if self.lane=='source':
            while not (self.state/'release').exists(): time.sleep(.05)
c.Coordinator=Fake
c.__file__=__file__
c.main()
''')
            config = root/'config.json'
            config.write_text(json.dumps(dict(parallel_pipeline=True, projects=[dict(id='p', integration_batch={'enabled': True})])))
            state = root/'state'
            log = (root/'log').open('w')
            parent = subprocess.Popen([sys.executable, str(driver), '--config', str(config), '--state', str(state), '--execute', '--watch'], stdout=log, stderr=log)
            pids = []
            try:
                deadline = time.monotonic()+10
                while time.monotonic()<deadline:
                    if all((state/(lane+'.tick')).exists() for lane in c.LANES):
                        break
                    if parent.poll() is not None:
                        self.fail((root/'log').read_text())
                    time.sleep(.05)
                self.assertTrue(all((state/(lane+'.tick')).exists() for lane in c.LANES), (root/'log').read_text())
                for lane in c.LANES:
                    receipt = json.loads((state/'p'/'lanes'/(lane+'.json')).read_text())
                    pids.append(receipt['pid'])
                self.assertEqual(len(set(pids)), 4)
                # Restart only completion; the other three lanes keep their PID.
                os.kill(pids[1], 15)
                # Parent shutdown reaps its scheduling children, including the
                # blocked source. No process group or model worker is signalled.
                parent.terminate()
                self.assertEqual(parent.wait(timeout=15), 0)
                self.assertTrue(all(not Path('/proc/'+str(pid)).exists() for pid in pids))
            finally:
                if parent.poll() is None:
                    parent.terminate()
                    parent.wait(timeout=15)
                log.close()

    def test_expensive_poll_cadence_keeps_completion_independent_and_accounts_for_work(self):
        from types import SimpleNamespace
        import ctypes
        for lane, expected_wait in [('integration',50),('completion',5),('ci',110),('source',290)]:
            with self.subTest(lane=lane), tempfile.TemporaryDirectory() as directory:
                engine=Mock()
                args=SimpleNamespace(state=Path(directory),project_id='p',parent_pid=123,
                                     lane=lane,interval=300)
                with patch.object(ctypes,'CDLL',return_value=SimpleNamespace(prctl=lambda *args:0)), \
                     patch.object(c.os,'getppid',side_effect=[123,123,0]), \
                     patch.object(c,'Coordinator',return_value=engine), \
                     patch.object(c.time,'monotonic',side_effect=[0,10]), \
                     patch.object(c.time,'sleep') as sleep:
                    c.run_lane({'projects':[{'id':'p'}]},args)
                engine.tick.assert_called_once_with(True)
                sleep.assert_called_once_with(expected_wait)
