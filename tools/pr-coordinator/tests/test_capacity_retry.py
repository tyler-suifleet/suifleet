"""Bounded same-claim recovery of stopped provider capacity failures (offline)."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from test_ci_redirects import Fixture, ROOT, git, d, REAL_RUN

ERROR = 'ERROR: Selected model is at capacity. Please try a different model.'


class Classification(unittest.TestCase):
    def test_terminal_only_bounded_nonzero_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            cases = [
                (ERROR, 1, True),
                (ERROR+'\n2026-10-23T02:10:26.940344Z ERROR codex_core::session: failed to record rollout items: thread 01a165c1-ca39-7480-9c0f-84d8a0a1d9e9 not found\ntokens used\n1,420,245', 1, True),
                (json.dumps({'type':'error','message': ERROR[7:]}), 1, True),
                (json.dumps({'type':'result','is_error':True,'result': ERROR[7:]}), 1, True),
                (ERROR, 0, False), (ERROR, -15, False), (ERROR, 124, False),
                ('source: '+ERROR, 1, False),
                (json.dumps({'type':'assistant','message':ERROR}), 1, False),
                (ERROR+'\nERROR: permission denied', 1, False),
                (ERROR+'\nERROR: usage limit reached', 1, False),
                ('ERROR: API capacity exceeded', 1, False),
                (ERROR+'\nunknown terminal output', 1, False),
                (ERROR+'\n'+'x'*40000, 1, False),
            ]
            for text, code, expected in cases:
                with self.subTest(text=text[:90], code=code):
                    (path/'worker.log').write_text(text)
                    (path/'exit.json').write_text(json.dumps({'returncode':code}))
                    self.assertEqual(d.capacity_failure(path), expected)
            (path/'worker.log').write_text(ERROR)
            (path/'exit.json').write_text('{"returncode":1}')
            (path/'result.json').write_text('{broken')
            self.assertFalse(d.capacity_failure(path))


class Recovery(Fixture):
    def setUp(self):
        super().setUp()
        # Process scans are global to the host. Independent/parallel test runs
        # must not look like a second live editor holding this fixture's claim.
        self.claim['token'] = uuid.uuid4().hex
        self.owner['token'] = self.claim['token']

    def ticket(self, project, *args):
        if args[0] in ('heartbeat', 'assert'):
            self.commands.append(args[0])
            if args[2:] != ('--token', self.claim['token']):
                raise AssertionError('wrong claim token')
            return dict(self.owner)
        return super().ticket(project, *args)

    def dispatcher(self):
        if hasattr(self, 'worker'):
            self.old_children = getattr(self, 'old_children', []) + list(self.worker.children.values())
        return super().dispatcher()

    def kill_processes(self):
        super().kill_processes()
        for child in [*self.worker.children.values(), *getattr(self, 'old_children', [])]:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)

    def stopped(self, source='dispatch-owner', code=1):
        directory = self.state/'implementations'/source
        directory.mkdir(parents=True)
        (directory/'command.json').write_text(json.dumps(self.config['workers']['Codex']))
        (directory/'worker.log').write_text(ERROR)
        (directory/'exit.json').write_text(json.dumps({'returncode':code}))
        (directory/'process.json').write_text(json.dumps({'pid':99999999,'started':d.process_identity(os.getpid())}))
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                               (source,'suifleet','Codex','running',str(directory),json.dumps(self.claim),99999999,d.process_identity(os.getpid()),None))
        return self.worker.db.execute('SELECT * FROM runs WHERE session=?',(source,)).fetchone()

    def recorded(self):
        self.worker.reconcile(self.project)
        return self.worker.db.execute('SELECT * FROM capacity_retries').fetchone()

    def due(self):
        self.worker.db.execute('UPDATE capacity_retries SET retry_after=0')

    def run_retry(self):
        def run(argv, **kwargs):
            return 'private-gh-token' if argv[:3] == ['gh','auth','token'] else REAL_RUN(argv, **kwargs)
        with patch.object(d, 'run', side_effect=run):
            self.worker.retry_capacity(self.project, [self.slot], [self.owner])

    def test_restart_backoff_heartbeat_and_dirty_work_preserved(self):
        self.stopped()
        before = self.recorded()
        self.assertEqual(before['attempt'],1)
        self.assertEqual(self.worker.db.execute('SELECT status FROM runs').fetchone()[0],'capacity_wait')
        self.worker = self.dispatcher()  # restart does not reset budget or timer
        self.worker.retry_capacity(self.project,[self.slot],[self.owner])
        after = self.worker.db.execute('SELECT * FROM capacity_retries').fetchone()
        self.assertEqual(before['retry_after'],after['retry_after'])
        self.assertIn('heartbeat',self.commands)
        (self.worktree/'feature.txt').write_text('staged work\n')
        git('add','feature.txt',cwd=self.worktree)
        (self.worktree/'feature.txt').write_text('unstaged work\n')
        (self.worktree/'untracked.txt').write_text('private unfinished work')
        snapshot = git('status','--porcelain=v1',cwd=self.worktree)
        self.due(); self.run_retry()
        result = self.worker.db.execute('SELECT * FROM capacity_retries').fetchone()
        self.assertEqual(result['state'],'launched')
        self.assertEqual(git('status','--porcelain=v1',cwd=self.worktree),snapshot)
        self.assertEqual((self.worktree/'feature.txt').read_text(),'unstaged work\n')
        self.assertEqual(git('show',':feature.txt',cwd=self.worktree),'staged work')
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0],2)
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0],1)
        self.assertFalse(self.worker.available(self.slot,[self.owner]))

    def test_refuses_changed_owner_model_git_operation_resource_and_helper(self):
        self.stopped(); self.recorded(); self.due()
        for mutate, restore in [
            (lambda:self.owner.update(publish_pending=True),lambda:self.owner.pop('publish_pending')),
            (lambda:self.slot.update(model='different'),lambda:self.slot.update(model='actual-test-model')),
            (lambda:self.locks.append(dict(resource='network:test',session=self.claim['session'],expires=time.time()+500)),lambda:self.locks.clear()),
            (lambda:self.worker.git_path(self.worktree,'MERGE_HEAD').write_text(self.head),lambda:self.worker.git_path(self.worktree,'MERGE_HEAD').unlink()),
        ]:
            mutate()
            try:
                self.run_retry()
                self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'waiting')
            finally:
                restore()
        with patch.object(self.worker,'capacity_helpers',return_value=[{'pid':123,'started':'4'}]):
            self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0],1)

    def test_waiting_resource_fence_heartbeats_but_expired_claim_does_not(self):
        self.stopped();self.recorded();self.due()
        self.locks.append(dict(resource='network:test',session=self.claim['session'],expires=time.time()+500))
        self.commands.clear(); self.run_retry()
        self.assertIn('heartbeat',self.commands)
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'waiting')
        original=self.worker.ticket
        def expired(project,*args):
            if args[0]=='assert':
                raise subprocess.CalledProcessError(1,['ticket','assert'])
            return original(project,*args)
        self.worker.ticket=expired
        self.commands.clear();self.run_retry()
        self.assertNotIn('heartbeat',self.commands)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0],1)

    def test_authoritative_handoff_cancels_wait_without_restart(self):
        self.stopped(); self.recorded(); self.due()
        self.owner.update(status='review')
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'cancelled')
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0],1)

    def test_due_recovery_bypasses_successor_idle_backoff(self):
        self.stopped(); self.recorded(); self.due()
        self.worker.db.execute('INSERT INTO backoff VALUES(?,?,?,?,?)',
                               ('suifleet','Codex',4,time.time()+900,'idle: no eligible ticket'))
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'launched')

    def test_same_family_replacements_stagger_in_persistent_state(self):
        self.slot['max_workers']=4
        for n in range(2):
            row=self.stopped('source-'+str(n)); self.worker.record_capacity(self.project,row,self.claim)
        self.due(); self.run_retry()
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM capacity_retries WHERE state='launched'").fetchone()[0],1)
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM capacity_retries WHERE state='waiting'").fetchone()[0],1)
        self.worker=self.dispatcher(); self.run_retry()
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM capacity_retries WHERE state='launched'").fetchone()[0],1)

    def test_original_process_identity_cannot_change(self):
        self.stopped();self.recorded();self.due()
        source=self.worker.db.execute('SELECT * FROM runs').fetchone()
        (Path(source['directory'])/'process.json').write_text('{"pid":99999998,"started":"1"}')
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'waiting')

    def test_live_descendant_claim_helper_and_ci_redirect_refuse(self):
        self.stopped();self.recorded();self.due()
        with patch.object(d,'worker_stopped',return_value=False):
            self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'waiting')
        with patch.object(self.worker,'claim_helpers',return_value=[{'pid':123,'started':'4'}]):
            self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'waiting')
        digest=hashlib.sha256(self.claim['token'].encode()).hexdigest()
        self.worker.db.execute("INSERT INTO ci_redirects(claim,fingerprint,state) VALUES(?,?,?)",(digest,'failure','pending'))
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'waiting')

    def test_parallel_fixture_token_does_not_block_unrelated_recovery(self):
        self.stopped();self.recorded();self.due()
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)'],
                               env={**os.environ,'PR_COORDINATOR_CLAIM_TOKEN':uuid.uuid4().hex},start_new_session=True)
        self.addCleanup(lambda: (child.kill(),child.wait()))
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'launched')

    def test_live_detached_process_with_claim_token_is_discovered(self):
        self.stopped();self.recorded();self.due()
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)'],
                               env={**os.environ,'PR_COORDINATOR_CLAIM_TOKEN':self.claim['token']},start_new_session=True)
        self.addCleanup(lambda: (child.kill(),child.wait()))
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'waiting')

    def test_stable_claim_budget_exhaustion(self):
        for number in range(4):
            source = 'owner-'+str(number)
            row = self.stopped(source)
            self.worker.record_capacity(self.project,row,self.claim)
            self.worker.db.execute("UPDATE runs SET status='finished' WHERE session=?",(source,))
        values = list(self.worker.db.execute('SELECT attempt,state FROM capacity_retries ORDER BY attempt'))
        self.assertEqual([tuple(r) for r in values],[(1,'waiting'),(2,'waiting'),(3,'waiting'),(4,'exhausted')])
        self.worker = self.dispatcher()
        self.assertEqual(self.worker.db.execute('SELECT max(attempt) FROM capacity_retries').fetchone()[0],4)

    def test_launch_intent_is_never_replayed_after_restart(self):
        self.stopped(); self.recorded(); self.due()
        original = subprocess.Popen
        def popen(argv, **kwargs):
            if str(ROOT/'worker.py') in argv:
                raise OSError('ambiguous spawn')
            return original(argv, **kwargs)
        with patch.object(d.subprocess,'Popen',side_effect=popen):
            self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'uncertain')
        self.worker = self.dispatcher()
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0],2)
        self.assertFalse(self.worker.available(self.slot,[self.owner]))

    def test_configured_slot_capacity_prevents_replacement(self):
        self.stopped(); self.recorded(); self.due()
        other = {**self.claim,'session':'manual-other','key':'SF-098','token':'other'}
        self.worker.retry_capacity(self.project,[self.slot],[self.owner,other])
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'waiting')

    def test_dependency_resume_and_review_repair_fence(self):
        self.stopped();self.recorded();self.due()
        digest=hashlib.sha256(self.claim['token'].encode()).hexdigest()
        self.worker.db.execute('INSERT INTO dependency_resumes VALUES(?,?,?,?,?,?,?,?,?,?)',
                               ('old-wait','suifleet','Codex',digest,'resuming',None,0,0,'test',time.time()))
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'waiting')
        self.worker.db.execute('DELETE FROM dependency_resumes')
        with patch.object(self.worker,'replacement_repair',return_value=True):
            self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0],'waiting')

    def test_real_repeated_capacity_exits_exhaust_one_stable_claim_budget(self):
        script=Path(self.temp.name)/'always_capacity.py'
        script.write_text('import sys\nprint('+repr(ERROR)+',flush=True)\nsys.exit(1)\n')
        self.config['workers']['Codex']=[sys.executable,str(script)]
        directory=self.state/'implementations'/'initial'
        directory.mkdir(parents=True)
        (directory/'command.json').write_text(json.dumps(self.config['workers']['Codex']))
        (directory/'prompt.txt').write_text('original work')
        child=subprocess.Popen([sys.executable,str(ROOT/'worker.py'),str(directory)],cwd=self.worktree,
                               env={**os.environ,'PR_COORDINATOR_CLAIM_TOKEN':self.claim['token']},start_new_session=True)
        child.wait(timeout=10)
        identity=json.loads((directory/'process.json').read_text())
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                               ('initial','suifleet','Codex','running',str(directory),json.dumps(self.claim),child.pid,identity['started'],None))
        for attempt in range(4):
            self.worker.reconcile(self.project)
            if attempt==3:
                break
            self.due(); self.worker.db.execute('DELETE FROM backoff'); self.run_retry()
            latest=self.worker.db.execute("SELECT session FROM runs WHERE status='running'").fetchone()[0]
            self.worker.children[latest].wait(timeout=10)
            self.worker=self.dispatcher()  # budget survives every service restart
        rows=list(self.worker.db.execute('SELECT attempt,state FROM capacity_retries ORDER BY attempt'))
        self.assertEqual([tuple(r) for r in rows],[(1,'launched'),(2,'launched'),(3,'launched'),(4,'exhausted')])
        self.assertEqual(self.worker.db.execute('SELECT count(DISTINCT claim) FROM capacity_retries').fetchone()[0],1)
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM runs WHERE status='quarantined'").fetchone()[0],1)

    def test_real_original_session_descendant_blocks_recovery(self):
        marker=Path(self.temp.name)/'descendant-pid'
        script=Path(self.temp.name)/'orphan_cli.py'
        script.write_text('import subprocess,sys,pathlib\np=subprocess.Popen([sys.executable,"-c","import time;time.sleep(300)"])\n'
                          'pathlib.Path('+repr(str(marker))+').write_text(str(p.pid))\nprint('+repr(ERROR)+',flush=True)\nsys.exit(1)\n')
        self.config['workers']['Codex']=[sys.executable,str(script)]
        directory=self.state/'implementations'/'with-descendant';directory.mkdir(parents=True)
        (directory/'command.json').write_text(json.dumps(self.config['workers']['Codex']))
        (directory/'prompt.txt').write_text('original')
        child=subprocess.Popen([sys.executable,str(ROOT/'worker.py'),str(directory)],cwd=self.worktree,start_new_session=True)
        child.wait(timeout=10)
        descendant=int(marker.read_text()); self.processes.append(child.pid)
        self.addCleanup(lambda: os.kill(descendant,signal.SIGKILL) if d.runnable_identity(descendant) else None)
        identity=json.loads((directory/'process.json').read_text())
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                               ('with-descendant','suifleet','Codex','running',str(directory),json.dumps(self.claim),child.pid,identity['started'],None))
        self.worker.reconcile(self.project)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM capacity_retries').fetchone()[0],0)
        self.assertEqual(self.worker.db.execute('SELECT status FROM runs').fetchone()[0],'running')

    def test_real_wrapper_capacity_exit_to_single_replacement(self):
        script = Path(self.temp.name)/'capacity_cli.py'
        marker = Path(self.temp.name)/'first-exit'
        script.write_text('import pathlib,sys,time\np=pathlib.Path('+repr(str(marker))+')\n'
                          'if not p.exists():\n p.write_text("once")\n print('+repr(ERROR)+',flush=True)\n sys.exit(1)\n'
                          'time.sleep(300)\n')
        self.config['workers']['Codex'] = [sys.executable,str(script)]
        directory = self.state/'implementations'/'real-owner'
        directory.mkdir(parents=True)
        (directory/'command.json').write_text(json.dumps(self.config['workers']['Codex']))
        (directory/'prompt.txt').write_text('implement original claim')
        child = subprocess.Popen([sys.executable,str(ROOT/'worker.py'),str(directory)],cwd=self.worktree,
                                 env={**os.environ,'PR_COORDINATOR_CLAIM_TOKEN':self.claim['token']},start_new_session=True)
        self.processes.append(child.pid)
        self.worker.children['real-owner']=child
        child.wait(timeout=10)
        identity=json.loads((directory/'process.json').read_text())
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                               ('real-owner','suifleet','Codex','running',str(directory),json.dumps(self.claim),child.pid,identity['started'],None))
        self.recorded(); self.due(); self.run_retry()
        record=self.worker.db.execute('SELECT * FROM capacity_retries').fetchone()
        self.assertEqual(record['state'],'launched')
        replacement=self.worker.db.execute('SELECT * FROM runs WHERE session=?',(record['replacement'],)).fetchone()
        deadline=time.time()+10
        while not (Path(replacement['directory'])/'process.json').exists():
            self.assertLess(time.time(),deadline);time.sleep(.02)
        self.assertEqual(json.loads(replacement['claim']),self.claim)
        self.assertEqual(json.loads((Path(replacement['directory'])/'command.json').read_text()),self.config['workers']['Codex'])
        self.run_retry()
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0],1)


if __name__ == '__main__':
    unittest.main()
