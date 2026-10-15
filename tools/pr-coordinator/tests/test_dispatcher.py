import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('dispatcher', ROOT/'dispatcher.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.project = dict(id='suifleet', root=str(self.state), github_account='project-account')
        self.slot = dict(project='suifleet', family='Codex', model='actual-test-model', enabled=True)
        self.config = dict(projects=[self.project], implementation_slots=[self.slot], workers={'Codex':['test-cli']})
        self.worker = d.Dispatcher(self.config, self.state)
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            db.execute('CREATE TABLE jobs(family TEXT,status TEXT)')
        self.claim = dict(key='SF-099', token='private-token', session='session', status='in_progress',
                          worktree=str(self.state), branch='branch', agent='Codex', model='actual-test-model', spec={})

    def receipt(self, status='running', owner_status='review', exitcode=0, verdict='review_ready'):
        folder = self.state/'job'
        folder.mkdir(exist_ok=True)
        (folder/'exit.json').write_text(json.dumps({'returncode':exitcode}))
        (folder/'result.json').write_text(json.dumps({'verdict':verdict,'evidence':'tests passed; PR ready'}))
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                               ('session','suifleet','Codex',status,str(folder),json.dumps(self.claim),123,'start',None))
        self.worker.ticket = Mock(return_value={**self.claim,'status':owner_status})
        return folder

    def state_of_run(self):
        return self.worker.db.execute('SELECT status FROM runs').fetchone()[0]

    def test_successful_handoff_frees_slot_only_after_wrapper_exit(self):
        self.receipt()
        with patch.object(d,'process_identity',return_value='start'):
            self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'running')
        with patch.object(d,'process_identity',return_value=None):
            self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'finished')
        self.assertTrue(self.worker.available(self.slot,[]))

    def test_failed_or_incomplete_handoff_retains_slot(self):
        for owner,code,verdict in [('in_progress',0,'review_ready'),('review',1,'review_ready'),('review',0,'blocked')]:
            with self.subTest(owner=owner,code=code,verdict=verdict):
                self.worker.db.execute('DELETE FROM runs')
                self.receipt(owner_status=owner,exitcode=code,verdict=verdict)
                with patch.object(d,'process_identity',return_value=None):
                    self.worker.reconcile(self.project)
                self.assertEqual(self.state_of_run(),'orphaned')
                self.assertFalse(self.worker.available(self.slot,[]))

    def test_interrupted_claim_never_reclaims_or_launches(self):
        self.receipt(status='claiming')
        self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'orphaned')
        self.worker.ticket.assert_not_called()

    def test_restart_adopts_process_identity_and_renews_only_live_owner(self):
        folder = self.receipt(owner_status='in_progress')
        (folder/'exit.json').unlink()
        (folder/'process.json').write_text(json.dumps(dict(pid=456,started='new-start')))
        restarted = d.Dispatcher(self.config,self.state)
        restarted.ticket = self.worker.ticket
        with patch.object(d,'process_identity',return_value='new-start') as identity:
            restarted.reconcile(self.project)
        identity.assert_called_once_with(456)
        restarted.ticket.assert_any_call(self.project,'heartbeat','SF-099','--token','private-token')
        self.assertEqual(self.state_of_run(),'running')

    def test_replaced_token_is_never_renewed(self):
        self.receipt(owner_status='in_progress')
        self.worker.ticket.return_value={**self.claim,'token':'replacement'}
        self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'orphaned')
        self.assertEqual(self.worker.ticket.call_count,1)

    def test_expired_manual_claim_and_blocked_work_occupy_slot(self):
        for status in ['in_progress','blocked']:
            self.assertFalse(self.worker.available(self.slot,[dict(agent='Codex',status=status,expired=True)]))
        self.assertTrue(self.worker.available(self.slot,[dict(agent='Codex',status='review')]))

    def test_review_repairs_have_priority_across_projects(self):
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            db.execute("INSERT INTO jobs VALUES('Codex','running')")
        self.assertFalse(self.worker.available(self.slot,[]))
        self.assertTrue(self.worker.available({**self.slot,'family':'Claude'},[]))

    def test_missing_review_database_fails_closed(self):
        (self.state/'reviews.sqlite3').unlink()
        self.assertFalse(self.worker.available(self.slot,[]))

    def test_launch_uses_account_environment_and_persists_private_claim(self):
        self.worker.ticket = Mock(return_value=self.claim)
        process = Mock(pid=456)
        with patch.object(d,'run',return_value='github-secret'), patch.object(d.subprocess,'Popen',return_value=process) as popen, patch.object(d,'process_identity',return_value='started'):
            self.worker.launch(self.project,self.slot)
        self.assertEqual(self.state_of_run(),'running')
        self.worker.ticket.assert_any_call(self.project,'assert','SF-099','--token','private-token')
        kwargs=popen.call_args.kwargs
        self.assertEqual(kwargs['cwd'],self.claim['worktree'])
        self.assertEqual(kwargs['env']['GH_TOKEN'],'github-secret')
        self.assertEqual(kwargs['env']['PR_COORDINATOR_CLAIM_TOKEN'],'private-token')
        folder=Path(self.worker.db.execute('SELECT directory FROM runs').fetchone()[0])
        prompt=(folder/'prompt.txt').read_text()
        self.assertNotIn('github-secret',prompt)
        self.assertNotIn('private-token',prompt)
        self.assertIn('Then EXIT',prompt)

    def test_launch_failure_retains_claim_and_stops_slot(self):
        self.worker.ticket=Mock(return_value=self.claim)
        with patch.object(d,'run',return_value='secret'),patch.object(d.subprocess,'Popen',side_effect=OSError('failure')):
            self.worker.launch(self.project,self.slot)
        self.assertEqual(self.state_of_run(),'orphaned')
        self.assertFalse(self.worker.available(self.slot,[]))
        self.assertIn('private-token',self.worker.db.execute('SELECT claim FROM runs').fetchone()[0])

    def test_no_eligible_work_is_idle_not_failed(self):
        self.worker.ticket=Mock(side_effect=subprocess.CalledProcessError(1,['ticket'],stderr='Coordination stopped: No eligible ticket: {}'))
        with patch.object(d,'run',return_value=''):
            self.worker.launch(self.project,self.slot)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0],0)

    def test_dry_run_does_not_refresh_or_claim(self):
        self.worker.ticket=Mock()
        self.worker.tick(False)
        self.worker.ticket.assert_not_called()

    def test_disabled_slot_does_not_claim(self):
        self.slot['enabled']=False
        self.worker.ticket=Mock(return_value={})
        self.worker.tick(True)
        self.worker.ticket.assert_not_called()

    def test_occupied_slots_do_not_spend_remote_refresh_calls(self):
        self.worker.ticket=Mock(return_value=[dict(agent='Codex',status='in_progress')])
        self.worker.tick(True)
        self.worker.ticket.assert_called_once_with(self.project,'list')

    def test_duplicate_slots_rejected(self):
        self.config['implementation_slots'].append(dict(self.slot))
        with self.assertRaises(ValueError):
            d.Dispatcher(self.config,self.state)

    def test_real_worker_wrapper_exit_receipt_allows_successor(self):
        script = '''import json,sys
from pathlib import Path
sys.stdin.read()
p=next(Path(sys.argv[1]).glob('implementations/*'))
(p/'result.json').write_text(json.dumps({'verdict':'review_ready','model':'offline-fixture','evidence':'fixture implementation ready'}))
'''
        self.config['workers']['Codex']=[sys.executable,'-c',script,str(self.state)]
        self.worker.ticket=Mock(return_value=self.claim)
        with patch.object(d,'run',return_value='fixture-token'):
            self.worker.launch(self.project,self.slot)
        child=next(iter(self.worker.children.values()))
        self.assertEqual(child.wait(timeout=10),0)
        self.worker.ticket.return_value={**self.claim,'status':'review'}
        self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'finished')
        self.assertTrue(self.worker.available(self.slot,[]))

    def test_state_survives_restart_and_blocks_second_launch(self):
        self.receipt(status='orphaned')
        restarted=d.Dispatcher(self.config,self.state)
        self.assertFalse(restarted.available(self.slot,[]))

    def test_merged_completion_clears_token_but_preserves_claim_identity(self):
        self.receipt(owner_status='done')
        self.worker.ticket.return_value={**self.claim,'status':'done','token':None}
        with patch.object(d,'process_identity',return_value=None):
            self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'finished')

    def test_different_completed_session_does_not_release_slot(self):
        self.receipt(owner_status='done')
        self.worker.ticket.return_value={**self.claim,'status':'done','token':None,'session':'replacement'}
        self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'orphaned')

    def test_second_dispatcher_process_cannot_scan_same_state(self):
        import fcntl
        config=self.state/'config.json'
        config.write_text(json.dumps(self.config))
        with open(self.state/'dispatcher.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=subprocess.run([sys.executable,str(ROOT/'dispatcher.py'),'--config',str(config),'--state',str(self.state)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('BlockingIOError',result.stderr)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()
