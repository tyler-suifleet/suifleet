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
        helpers = patch.object(d.Dispatcher, 'claim_helpers', return_value=[])
        helpers.start()
        self.addCleanup(helpers.stop)
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
                               ('session','suifleet','Codex',status,str(folder),json.dumps(self.claim),99999999,'start',None))
        self.worker.ticket = Mock(return_value={**self.claim,'status':owner_status})
        return folder

    def state_of_run(self):
        return self.worker.db.execute('SELECT status FROM runs').fetchone()[0]

    def test_successful_handoff_frees_slot_only_after_wrapper_exit(self):
        self.receipt()
        with patch.object(d,'process_identity',return_value='start'):
            self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'running')
        with patch.object(d,'process_identity',return_value=None),patch.object(d,'worker_stopped',return_value=True):
            self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'finished')
        self.assertTrue(self.worker.available(self.slot,[]))

    def test_stopped_incomplete_handoff_quarantines_without_releasing_claim(self):
        for owner,code,verdict in [('in_progress',0,'review_ready'),('review',1,'review_ready'),('review',0,'blocked')]:
            with self.subTest(owner=owner,code=code,verdict=verdict):
                self.worker.db.execute('DELETE FROM runs')
                self.receipt(owner_status=owner,exitcode=code,verdict=verdict)
                with patch.object(d,'process_identity',return_value=None),patch.object(d,'worker_stopped',return_value=True):
                    self.worker.reconcile(self.project)
                self.assertEqual(self.state_of_run(),'quarantined')
                self.assertFalse(self.worker.available(self.slot,[self.claim]))
                self.assertEqual(json.loads(self.worker.db.execute('SELECT claim FROM runs').fetchone()[0])['token'],'private-token')

    def test_fast_fail_ticks_back_off_and_stop_at_durable_cap(self):
        owners = {}
        def ticket(project, *args):
            if args[0] == 'list': return list(owners.values())
            if args[0] == 'show': return owners[args[1]]
            if args[0] == 'refresh': return {}
            if args[0] == 'assert': return {}
            if args[0] == 'claim':
                key = 'SF-' + str(len(owners)+1)
                claim = {**self.claim, 'key':key, 'session':args[args.index('--session')+1]}
                owners[key] = claim
                return claim
            raise AssertionError(args)
        def stopped_wrapper(argv, **kwargs):
            folder = Path(argv[-1])
            (folder/'exit.json').write_text('{"returncode":1}')
            (folder/'process.json').write_text(json.dumps(dict(pid=99999999,started='start')))
            return Mock(pid=99999999, poll=Mock(return_value=1))
        self.worker.ticket = Mock(side_effect=ticket)
        with patch.object(d,'run',return_value='test-token'), patch.object(d.subprocess,'Popen',side_effect=stopped_wrapper), patch.object(d,'process_identity',return_value=None), patch.object(d,'worker_stopped',return_value=True), patch.object(d.time,'time') as now:
            # Advance past every backoff: even unlimited ticks cannot reserve the backlog.
            for tick in range(20):
                now.return_value = tick*10000
                self.worker.tick(True)
                if tick == 1:
                    self.assertFalse(self.worker.available(self.slot, list(owners.values())))
                    pause = self.worker.db.execute('SELECT attempts FROM backoff').fetchone()
                    self.assertEqual(pause[0], 1)
            self.assertEqual(len(owners), 3)
            self.assertEqual(self.worker.db.execute("SELECT COUNT(*) FROM runs WHERE status='quarantined'").fetchone()[0], 3)
            restarted = d.Dispatcher(self.config, self.state)
            self.assertFalse(restarted.available(self.slot, list(owners.values())))
            self.assertEqual(self.worker.db.execute('SELECT attempts FROM backoff').fetchone()[0], 3)

    def test_parked_jobs_also_have_a_retention_cap(self):
        self.receipt(owner_status='blocked', verdict='blocked')
        with patch.object(d,'process_identity',return_value=None), patch.object(d,'worker_stopped',return_value=True):
            self.worker.reconcile(self.project)
        self.assertFalse(self.worker.available({**self.slot,'max_quarantined':1}, []))

    def test_corrupt_receipt_is_isolated_without_losing_ownership(self):
        folder=self.receipt()
        (folder/'process.json').write_text('{broken')
        self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'running')
        self.assertFalse(self.worker.available(self.slot,[]))

    def test_blocked_worker_parks_and_does_not_count_as_manual_owner(self):
        self.receipt(owner_status='blocked', verdict='blocked')
        with patch.object(d,'process_identity',return_value=None), patch.object(d,'worker_stopped',return_value=True):
            self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'parked')
        self.assertTrue(self.worker.available(self.slot,[{**self.claim,'status':'blocked'}]))
        self.worker.ticket.assert_called_once()

    def test_orphaned_successful_handoff_is_reconciled_after_restart(self):
        self.receipt(status='orphaned')
        with patch.object(d,'process_identity',return_value=None), patch.object(d,'worker_stopped',return_value=True):
            self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'finished')

    def test_live_descendant_keeps_capacity_when_wrapper_disappears(self):
        self.receipt()
        with patch.object(d,'process_identity',return_value=None), patch.object(d,'worker_stopped',return_value=False):
            self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'running')
        self.assertFalse(self.worker.available(self.slot,[]))

    def test_changed_owner_is_never_renewed_and_stopped_run_is_quarantined(self):
        self.receipt(status='orphaned',owner_status='in_progress')
        self.worker.ticket.return_value={**self.claim,'token':'replacement'}
        with patch.object(d,'process_identity',return_value='start'):
            self.worker.reconcile(self.project)
        self.worker.ticket.assert_called_once()
        with patch.object(d,'process_identity',return_value=None),patch.object(d,'worker_stopped',return_value=True):
            self.worker.reconcile(self.project)
        self.assertEqual(self.state_of_run(),'quarantined')

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

    def test_review_repairs_do_not_block_implementation_capacity(self):
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            db.execute("INSERT INTO jobs VALUES('Codex','running')")
        self.assertTrue(self.worker.available(self.slot,[]))
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
        self.assertFalse(self.worker.available(self.slot,[]))
        row=self.worker.db.execute('SELECT * FROM backoff').fetchone()
        self.assertEqual(row['reason'],'idle: no eligible ticket')
        with patch.object(d.time,'time',return_value=row['retry_after']+1):
            self.assertTrue(self.worker.available(self.slot,[]))

    def test_fetch_failure_never_creates_an_orphan_and_can_retry(self):
        self.worker.ticket=Mock()
        with patch.object(d,'run',side_effect=subprocess.CalledProcessError(128,['git','fetch'])):
            self.worker.launch(self.project,self.slot)
        self.worker.ticket.assert_not_called()
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0],0)
        row=self.worker.db.execute('SELECT * FROM backoff').fetchone()
        with patch.object(d.time,'time',return_value=row['retry_after']+1):
            self.assertTrue(self.worker.available(self.slot,[]))

    def test_backoff_is_durable_exponential_and_bounded(self):
        with patch.object(d.time,'time',return_value=1000):
            for expected in [60,120,240,480,900,900]:
                self.worker.defer(self.project,'Codex','idle')
                row=self.worker.db.execute('SELECT retry_after FROM backoff').fetchone()
                self.assertEqual(row[0],1000+expected)
            restarted=d.Dispatcher(self.config,self.state)
            self.assertFalse(restarted.available(self.slot,[]))

    def test_refresh_failure_backs_off_without_claiming(self):
        def ticket(project,*args):
            if args[0]=='list':return []
            if args[0]=='refresh':raise subprocess.CalledProcessError(1,['ticket'])
            raise AssertionError('Unexpected claim')
        self.worker.ticket=Mock(side_effect=ticket)
        self.worker.tick(True)
        self.assertFalse(self.worker.available(self.slot,[]))
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
        self.worker.ticket=Mock(return_value=[dict(key='SF-099',agent='Codex',status='in_progress')])
        self.worker.tick(True)
        self.worker.ticket.assert_called_once_with(self.project,'list')

    def test_capacity_counts_manual_and_managed_claim_once(self):
        self.slot['max_workers'] = 2
        self.receipt()
        owner = {**self.claim, 'expired': True}
        self.assertTrue(self.worker.available(self.slot, [owner]))
        other = {**owner, 'session': 'manual-worker', 'key': 'SF-100'}
        self.assertFalse(self.worker.available(self.slot, [owner, other]))
        self.worker.db.execute("UPDATE runs SET status='orphaned'")
        self.assertTrue(self.worker.available(self.slot, [owner]))
        restarted = d.Dispatcher(self.config, self.state)
        self.assertFalse(restarted.available(self.slot, [owner, other]))
        self.slot['max_workers'] = 1
        self.assertFalse(restarted.available(self.slot, [owner]))
        self.assertEqual(self.state_of_run(), 'orphaned')

    def test_invalid_capacity_rejected(self):
        for limit in [0, -1, True, 1.5, '2', 17, None]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                d.Dispatcher({**self.config, 'implementation_slots':[{**self.slot, 'max_workers':limit}]}, self.state)

    def test_tick_fills_capacity_without_duplicate_claims(self):
        self.worker.reconcile = Mock()  # Reconciliation has separate real-receipt tests.
        self.slot['max_workers'] = 3
        self.worker.ticket = Mock(return_value=[])
        def launch(project, slot):
            session = 'worker-' + str(self.worker.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0])
            self.worker.db.execute('INSERT INTO runs(session,project,family,status) VALUES(?,?,?,?)',
                                   (session, 'suifleet', 'Codex', 'running'))
        self.worker.launch = Mock(side_effect=launch)
        self.worker.tick(True)
        self.assertEqual(self.worker.launch.call_count, 3)
        self.worker.tick(True)
        self.assertEqual(self.worker.launch.call_count, 3)
        restarted = d.Dispatcher(self.config, self.state)
        self.assertFalse(restarted.available(self.slot, []))

    def test_other_family_and_project_do_not_consume_capacity(self):
        self.receipt(status='orphaned')
        self.assertTrue(self.worker.available({**self.slot, 'family':'Claude'}, []))
        self.assertTrue(self.worker.available({**self.slot, 'project':'another'}, []))

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
        with patch.object(d,'process_identity',return_value=None),patch.object(d,'worker_stopped',return_value=True):
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



class ObsoleteReceiptTests(unittest.TestCase):
    setUp = DispatchTests.setUp
    receipt = DispatchTests.receipt
    state_of_run = DispatchTests.state_of_run

    def authority(self, owner):
        self.env = patch.dict(d.os.environ, {'XDG_STATE_HOME': str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        folder = self.state/'atomic-ticketing'
        folder.mkdir()
        db = sqlite3.connect(folder/'coordination.sqlite3', isolation_level=None)
        self.addCleanup(db.close)
        db.execute('''CREATE TABLE tickets(key TEXT, project TEXT, status TEXT, token TEXT,
                   session TEXT, branch TEXT, worktree TEXT, revision INTEGER,
                   published_revision INTEGER, evidence TEXT)''')
        self.owner = {**self.claim, 'status': 'review', 'revision': 4,
                      'published_revision': 4, 'evidence': 'published PR handoff', **owner}
        db.execute('INSERT INTO tickets VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('suifleet::SF-099','suifleet',*(self.owner[k] for k in
                    ('status','token','session','branch','worktree','revision','published_revision','evidence'))))
        db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, ticket TEXT, actor TEXT, kind TEXT, detail TEXT)')
        self.worker.ticket.return_value = self.owner
        return db

    def retire(self):
        with patch.object(d,'process_identity',return_value=None), patch.object(d,'worker_stopped',return_value=True):
            self.worker.retire_obsolete(self.project)

    def test_done_and_review_archive_preserving_original_receipt_and_allow_work(self):
        for owner in [{'status':'done','token':None}, {'status':'review'}]:
            with self.subTest(owner=owner):
                self.receipt(status='quarantined')
                db = self.authority(owner)
                original = dict(self.worker.db.execute('SELECT * FROM runs').fetchone())
                self.retire()
                self.assertEqual(self.state_of_run(),'finished')
                saved = self.worker.db.execute('SELECT * FROM retired_runs').fetchone()
                self.assertEqual(saved['original_status'],'quarantined')
                self.assertEqual(saved['original_message'],original['message'])
                self.assertNotIn('private-token', saved['reason'])
                current = dict(self.worker.db.execute('SELECT * FROM runs').fetchone())
                for field in ('claim','pid','started','directory'):
                    self.assertEqual(current[field],original[field])
                self.assertTrue(self.worker.available({**self.slot,'max_quarantined':1}, []))
                self.assertTrue((Path(original['directory'])/'result.json').exists())
                self.worker.db.execute('DELETE FROM runs')
                self.worker.db.execute('DELETE FROM retired_runs')
                db.close()
                (self.state/'atomic-ticketing/coordination.sqlite3').unlink()
                (self.state/'atomic-ticketing').rmdir()
                self.env.stop()

    def test_recovery_requires_release_after_exact_claim_generation(self):
        self.receipt(status='parked')
        db = self.authority({'token':'new-token','session':'replacement','status':'in_progress'})
        self.retire()
        self.assertEqual(self.state_of_run(),'parked')
        db.execute("INSERT INTO events VALUES(1,'suifleet::SF-099','session','claim','Codex/model; token=private-token; paths=[]')")
        db.execute("INSERT INTO events VALUES(2,'suifleet::SF-099','session','release','Operator recovery: stopped')")
        self.retire()
        self.assertEqual(self.state_of_run(),'finished')

    def test_recovered_review_without_old_result_is_retired(self):
        folder = self.receipt(status='quarantined')
        (folder/'result.json').unlink()
        db = self.authority({'token':'new-token','session':'replacement'})
        db.execute("INSERT INTO events VALUES(1,'suifleet::SF-099','session','claim','Codex/model; token=private-token; paths=[]')")
        db.execute("INSERT INTO events VALUES(2,'suifleet::SF-099','session','release','Operator recovery: stopped')")
        self.retire()
        self.assertEqual(self.state_of_run(),'finished')

    def test_unresolved_pending_mirror_unknown_and_changed_ownership_stay_reserved(self):
        self.receipt(status='quarantined')
        db = self.authority({'status':'blocked'})
        self.retire()
        self.assertEqual(self.state_of_run(),'quarantined')
        db.execute("UPDATE tickets SET status='review',revision=5")
        self.worker.ticket.return_value = {**self.owner,'status':'review','revision':5,'publish_pending':True}
        self.retire()
        self.assertEqual(self.state_of_run(),'quarantined')
        self.worker.ticket.return_value = {**self.owner,'status':'review'}
        self.retire()  # stale CLI observation cannot archive a newer authority row
        self.assertEqual(self.state_of_run(),'quarantined')
        self.worker.ticket.side_effect = subprocess.CalledProcessError(1,['ticket'])
        self.retire()
        self.assertFalse(self.worker.available({**self.slot,'max_quarantined':1}, []))

    def test_live_reused_pid_lingering_and_missing_identity_consume_capacity(self):
        folder = self.receipt(status='parked')
        self.authority({})
        for current, stopped in [('start',False),('reused-start',True),(None,False)]:
            with self.subTest(current=current), patch.object(d,'process_identity',return_value=current), patch.object(d,'worker_stopped',return_value=stopped):
                self.worker.retire_obsolete(self.project)
                self.assertEqual(self.state_of_run(),'parked')
                self.assertFalse(self.worker.available(self.slot, []))
        (folder/'process.json').write_text('{}')
        self.retire()
        self.assertFalse(self.worker.available(self.slot, []))

    def test_process_restart_and_receipt_race_before_commit_are_retained(self):
        self.receipt(status='quarantined')
        self.authority({})
        original = self.worker.stopped_identity
        calls = 0
        def changed(row):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.worker.db.execute("UPDATE runs SET message='concurrent change'")
                return None
            return original(row)
        with patch.object(self.worker,'stopped_identity',side_effect=changed):
            self.retire()
        self.assertEqual(self.state_of_run(),'quarantined')
        self.assertEqual(self.worker.db.execute('SELECT COUNT(*) FROM retired_runs').fetchone()[0],0)

    def test_authority_is_locked_while_receipt_is_compared_and_updated(self):
        self.receipt(status='quarantined')
        self.authority({})
        other = sqlite3.connect(self.state/'atomic-ticketing/coordination.sqlite3', timeout=0)
        self.addCleanup(other.close)
        original = self.worker.stopped_identity
        calls = 0
        def competing_owner(row):
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.assertRaises(sqlite3.OperationalError):
                    other.execute("UPDATE tickets SET token='concurrent'")
                other.rollback()
            return original(row)
        with patch.object(self.worker,'stopped_identity',side_effect=competing_owner):
            self.retire()
        self.assertEqual(self.state_of_run(),'finished')

    def test_failure_isolated_and_independent_eligible_work_resumes(self):
        self.receipt(status='quarantined')
        self.authority({})
        self.worker.db.execute("INSERT INTO runs VALUES('broken','suifleet','Claude','quarantined','/missing','{',NULL,NULL,NULL)")
        self.retire()
        self.assertEqual(self.worker.db.execute("SELECT status FROM runs WHERE session='broken'").fetchone()[0],'quarantined')
        self.assertTrue(self.worker.available({**self.slot,'max_quarantined':1}, []))
        self.worker.ticket = Mock(return_value=[])
        self.worker.launch = Mock()
        self.worker.tick(True)
        self.worker.launch.assert_called_once_with(self.project,self.slot)

    def test_same_claim_repair_requires_fenced_newer_process(self):
        self.receipt(status='quarantined')
        self.worker.db.execute("UPDATE runs SET started='100'")
        self.authority({'status':'in_progress'})
        folder = self.state/'repair'
        folder.mkdir()
        (folder/'process.json').write_text(json.dumps(dict(pid=456,started='200')))
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            db.execute('DROP TABLE jobs')
            db.execute('CREATE TABLE jobs(project TEXT, kind TEXT, repair_claim TEXT, pid INTEGER, started TEXT, directory TEXT, status TEXT, result TEXT)')
            proof = dict(claim=d.hashlib.sha256(self.claim['token'].encode()).hexdigest(),
                         key=self.claim['key'], branch=self.claim['branch'],worktree=self.claim['worktree'],head='head')
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',('suifleet','fix',json.dumps(proof),456,'200',str(folder),'running',None))
        # Neither an unfenced historical job nor a different generation proves repair.
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            db.execute("UPDATE jobs SET repair_claim=NULL")
        self.retire()
        self.assertEqual(self.state_of_run(),'quarantined')
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            db.execute('UPDATE jobs SET repair_claim=?',(json.dumps({**proof,'claim':'other'}),))
        self.retire()
        self.assertEqual(self.state_of_run(),'quarantined')
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            db.execute('UPDATE jobs SET repair_claim=?',(json.dumps(proof),))
        def identity(pid):
            return '200' if pid == 456 else None
        with patch.object(d,'process_identity',side_effect=identity), patch.object(d,'worker_stopped',return_value=True):
            self.worker.retire_obsolete(self.project)
        self.assertEqual(self.state_of_run(),'finished')
        # A collected successful repair also proves replacement after exit.
        self.worker.db.execute("UPDATE runs SET status='quarantined'")
        self.worker.db.execute('DELETE FROM retired_runs')
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            db.execute("UPDATE jobs SET status='done', result=?",
                       (json.dumps(dict(verdict='fixed',evidence='verified repair')),))
        self.retire()
        self.assertEqual(self.state_of_run(),'finished')

    def test_failed_archive_rolls_back_without_changing_authority(self):
        self.receipt(status='quarantined')
        db = self.authority({})
        before = db.execute('SELECT * FROM tickets').fetchall()
        self.worker.db.execute("CREATE TRIGGER reject_archive BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.retire()
        self.assertEqual(self.state_of_run(),'quarantined')
        self.assertEqual(self.worker.db.execute('SELECT COUNT(*) FROM retired_runs').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT * FROM tickets').fetchall(),before)
        self.worker.db.execute('DROP TRIGGER reject_archive')
        self.retire()
        self.assertEqual(self.state_of_run(),'finished')

    def test_other_claim_generation_release_does_not_prove_recovery(self):
        self.receipt(status='quarantined')
        db = self.authority({'token':'replacement','session':'other'})
        db.execute("INSERT INTO events VALUES(1,'suifleet::SF-099','session','claim','token=private-token;')")
        db.execute("INSERT INTO events VALUES(2,'suifleet::SF-099','session','claim','token=unrelated;')")
        db.execute("INSERT INTO events VALUES(3,'suifleet::SF-099','session','release','Operator recovery')")
        self.retire()
        self.assertEqual(self.state_of_run(),'quarantined')


class HandoffAuditTests(unittest.TestCase):
    setUp = DispatchTests.setUp
    receipt = DispatchTests.receipt
    authority = ObsoleteReceiptTests.authority
    def setup_audit(self):
        folder = self.receipt(status='quarantined', owner_status='in_progress')
        (folder/'result.json').unlink()
        (folder/'process.json').write_text(json.dumps(dict(pid=99999999, started='start')))
        self.authority({'status':'in_progress'})
        self.identity = patch.object(d, 'process_identity', return_value=None)
        self.stopped = patch.object(d, 'worker_stopped', return_value=True)
        self.helpers = patch.object(self.worker, 'claim_helpers', return_value=[])
        self.command = patch.object(d, 'run', side_effect=lambda args, **kw: 'branch' if args[:2] == ['git','branch'] else '')
        self.spawn = patch.object(d.subprocess, 'Popen', return_value=Mock(pid=12345))
        for patcher in (self.identity,self.stopped,self.helpers,self.command,self.spawn):
            patcher.start()
            self.addCleanup(patcher.stop)
        return folder

    def attempts(self):
        return self.worker.db.execute('SELECT count(*) FROM handoff_audits').fetchone()[0]

    def test_single_durable_same_claim_attempt_and_restart(self):
        folder = self.setup_audit()
        self.worker.audit_missing_handoff(self.project)
        self.assertEqual(self.attempts(),1)
        audit = self.worker.db.execute("SELECT * FROM runs WHERE session LIKE '%audit'").fetchone()
        self.assertEqual(json.loads(audit['claim']),self.claim)
        self.assertTrue((folder/'exit.json').exists())
        self.assertIn('do not implement or rebuild', (folder/'handoff-audit/prompt.txt').read_text())
        self.worker.audit_missing_handoff(self.project)
        restarted = d.Dispatcher(self.config,self.state)
        restarted.audit_missing_handoff(self.project)
        self.assertEqual(self.attempts(),1)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0],2)

    def test_unsafe_conditions_never_attempt(self):
        folder = self.setup_audit()
        original = (folder/'process.json').read_text()
        for kind in ('result','nonzero','unknown_exit','missing_identity','live','children','helpers','token','branch','dirty','mirror','authority_race'):
            with self.subTest(kind=kind):
                (folder/'exit.json').write_text('{"returncode":0}')
                (folder/'process.json').write_text(original)
                self.worker.ticket.return_value = self.owner
                patches = []
                if kind == 'result': (folder/'result.json').write_text('{}')
                if kind == 'nonzero': (folder/'exit.json').write_text('{"returncode":1}')
                if kind == 'unknown_exit': (folder/'exit.json').unlink()
                if kind == 'missing_identity': (folder/'process.json').unlink()
                if kind == 'live': patches.append(patch.object(d,'process_identity',return_value='reused'))
                if kind == 'children': patches.append(patch.object(d,'worker_stopped',return_value=False))
                if kind == 'helpers': patches.append(patch.object(self.worker,'claim_helpers',return_value=[{'pid':1,'started':'old'}]))
                if kind == 'token': self.worker.ticket.return_value = {**self.owner,'token':'changed'}
                if kind == 'branch': patches.append(patch.object(d,'run',return_value='different'))
                if kind == 'dirty': patches.append(patch.object(d,'run',side_effect=['branch',' M dirty']))
                if kind == 'mirror': self.worker.ticket.return_value = {**self.owner,'publish_pending':True}
                if kind == 'authority_race': self.worker.ticket.return_value = {**self.owner,'revision':9}
                for p in patches: p.start()
                try:
                    self.worker.audit_missing_handoff(self.project)
                    self.assertEqual(self.attempts(),0)
                finally:
                    for p in reversed(patches): p.stop()
                    (folder/'result.json').unlink(missing_ok=True)

    def test_expired_lease_is_renewed_before_assert_and_audited_once(self):
        self.setup_audit()
        expired = True
        calls = []
        def ticket(project, *args):
            nonlocal expired
            calls.append(args[0])
            if args[0] == 'heartbeat':
                expired = False
            if args[0] == 'assert' and expired:
                raise subprocess.CalledProcessError(1, 'ticket', stderr='Lease expired')
            return self.owner
        self.worker.ticket.side_effect = ticket
        self.worker.audit_missing_handoff(self.project)
        self.assertEqual(calls, ['show', 'heartbeat', 'assert'])
        self.assertEqual(self.attempts(), 1)
        self.worker.audit_missing_handoff(self.project)
        self.assertEqual(self.attempts(), 1)

    def test_assert_dependency_or_remote_failure_never_launches(self):
        self.setup_audit()
        def ticket(project, *args):
            if args[0] == 'assert':
                raise subprocess.CalledProcessError(1, 'ticket', stderr='Dependency incomplete or issue closed')
            return self.owner
        self.worker.ticket.side_effect = ticket
        self.worker.audit_missing_handoff(self.project)
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0], 1)

    def test_ownership_changes_during_renewal_prevent_launch(self):
        self.setup_audit()
        def ticket(project, *args):
            if args[0] == 'heartbeat':
                with sqlite3.connect(self.state/'atomic-ticketing/coordination.sqlite3') as db:
                    db.execute("UPDATE tickets SET token='replacement'")
            return self.owner
        self.worker.ticket.side_effect = ticket
        self.worker.audit_missing_handoff(self.project)
        self.assertEqual(self.attempts(), 0)

    def test_source_and_incomplete_or_blocked_audit_use_one_retention_slot(self):
        folder = self.setup_audit()
        self.worker.audit_missing_handoff(self.project)
        (folder/'handoff-audit/process.json').write_text(json.dumps(dict(pid=12345, started='audit-start')))
        self.worker.db.execute("UPDATE runs SET status='quarantined'")
        for status in ('quarantined', 'parked'):
            with self.subTest(status=status):
                self.worker.db.execute("UPDATE runs SET status=? WHERE session LIKE '%audit'", (status,))
                self.assertTrue(self.worker.available({**self.slot, 'max_quarantined':2}, []))
                self.assertFalse(self.worker.available({**self.slot, 'max_quarantined':1}, []))
        other = {**self.claim, 'token':'another-generation'}
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
            ('other', 'suifleet', 'Codex', 'quarantined', str(folder), json.dumps(other), 99999999, 'start', None))
        self.assertFalse(self.worker.available({**self.slot, 'max_quarantined':2}, []))

    def test_launch_failure_is_not_retried(self):
        self.setup_audit()
        with patch.object(d.subprocess,'Popen',side_effect=OSError('unavailable')) as spawn:
            self.worker.audit_missing_handoff(self.project)
            self.worker.audit_missing_handoff(self.project)
        self.assertEqual(spawn.call_count,1)
        self.assertEqual(self.attempts(),1)

    def test_incomplete_audit_is_quarantined_without_recursive_audit(self):
        folder = self.setup_audit()
        self.worker.audit_missing_handoff(self.project)
        audit = folder/'handoff-audit'
        (audit/'process.json').write_text(json.dumps(dict(pid=12345,started='audit-start')))
        (audit/'exit.json').write_text('{"returncode":0}')
        self.worker.reconcile(self.project)
        self.worker.audit_missing_handoff(self.project)
        self.assertEqual(self.attempts(),1)
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM runs WHERE status='quarantined'").fetchone()[0],2)

    def test_completed_audit_uses_normal_review_handoff(self):
        folder = self.setup_audit()
        self.worker.audit_missing_handoff(self.project)
        audit = folder/'handoff-audit'
        (audit/'process.json').write_text(json.dumps(dict(pid=12345,started='audit-start')))
        (audit/'exit.json').write_text('{"returncode":0}')
        (audit/'result.json').write_text(json.dumps({'verdict':'review_ready','evidence':'PR/head/CI verified'}))
        self.worker.ticket.return_value = {**self.owner,'status':'review'}
        self.worker.reconcile(self.project)
        self.assertEqual(self.worker.db.execute("SELECT status FROM runs WHERE session LIKE '%audit'").fetchone()[0], 'finished')


class HelperProcessTests(unittest.TestCase):
    def test_detached_heartbeat_detected_and_stale_helper_not_reused(self):
        token = d.uuid.uuid4().hex
        child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)', 'ticket heartbeat'],
                                 env={**d.os.environ,'PR_COORDINATOR_CLAIM_TOKEN':token}, start_new_session=True)
        try:
            matches = d.Dispatcher.claim_helpers(None, {'token':token})
            self.assertIn(child.pid, [r['pid'] for r in matches])
            self.assertTrue(all(r['started'] for r in matches))
            self.assertEqual(d.Dispatcher.claim_helpers(None, {'token':'changed-claim'}), [])
        finally:
            child.terminate()
            child.wait()
        self.assertEqual(d.Dispatcher.claim_helpers(None, {'token':token}), [])

    def test_audit_wrapper_deadline_persists_actual_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder/'command.json').write_text(json.dumps([sys.executable,'-c','import time; time.sleep(60)']))
            (folder/'prompt.txt').write_text('offline timeout test')
            (folder/'limits.json').write_text('{"timeout_seconds":0.05}')
            result = subprocess.run([sys.executable,str(ROOT/'worker.py'),str(folder)], timeout=5)
            self.assertEqual(result.returncode,124)
            self.assertEqual(json.loads((folder/'exit.json').read_text()), {'returncode':124})
            self.assertTrue((folder/'process.json').exists())


if __name__ == '__main__':
    unittest.main()
