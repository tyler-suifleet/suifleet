import json
import fcntl
import os
import signal
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery_control as control


class RetainedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.claim = dict(key='SF-1', token='private', session='original', branch='work/original',
                          worktree=str(self.path), agent='Claude', model='original-model', status='blocked',
                          spec={}, remote={}, publish_pending=False)
        self.d = Mock()
        self.d.state = self.path
        self.d.children = {}
        self.d.config = {'workers': {'Claude': ['original-command']}}
        self.d.db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.d.db.close)
        self.d.db.row_factory = sqlite3.Row
        self.d.db.executescript('''
          CREATE TABLE runs(session TEXT PRIMARY KEY,project TEXT,family TEXT,status TEXT,directory TEXT,claim TEXT,pid INTEGER,started TEXT,message TEXT);
          CREATE TABLE retired_runs(session TEXT PRIMARY KEY,status TEXT,message TEXT,reason TEXT,at REAL);
          CREATE TABLE dependency_resumes(claim TEXT,state TEXT);
          CREATE TABLE ci_redirects(claim TEXT,state TEXT);
          CREATE TABLE capacity_retries(claim TEXT,state TEXT);
        ''')
        (self.path/'command.json').write_text(json.dumps(['original-command']))
        self.d.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)', ('original','project','Claude','parked',str(self.path),json.dumps(self.claim),123,'100','blocked'))
        self.project = dict(id='project', github_account='account')
        self.slot = dict(project='project',family='Claude',model='original-model')
        self.d.stopped_identity.return_value = dict(pid=123,started='100')
        self.d.capacity_helpers.return_value = []
        self.d.owner_locks.return_value = []
        self.d.replacement_repair.return_value = False
        self.d.git_path.side_effect = lambda tree,name: self.path/name
        self.d.available.return_value = True
        self.d.prompt.return_value = 'Implement the retained ticket with tests.'
        def ticket(project, command, *args):
            if command == 'list': return [dict(self.claim)]
            if command == 'resume': self.claim['status'] = 'in_progress'
            return dict(self.claim)
        self.d.ticket.side_effect = ticket
        self.run = patch('coordinator.run', side_effect=lambda cmd,**kw: 'work/original' if cmd[0]=='git' else 'secret')
        self.run.start()
        self.addCleanup(self.run.stop)
        self.spawn = patch.object(control.subprocess,'Popen',return_value=Mock(pid=999))
        self.child = self.spawn.start()
        self.addCleanup(self.spawn.stop)
        self.identity = patch('coordinator.process_identity',return_value='200')
        self.identity.start()
        self.addCleanup(self.identity.stop)

    def resume(self):
        return control.resume(self.d,self.project,self.slot,'SF-1','Repair the confirmed filesystem race, run security checks and required acceptance.',self.path/'worker.py')

    def test_resumes_same_claude_claim_and_registers_supervised_worker(self):
        result = self.resume()
        self.assertEqual(result['family'],'Claude')
        rows = self.d.db.execute('SELECT * FROM runs ORDER BY session').fetchall()
        original = next(r for r in rows if r['session']=='original')
        replacement = next(r for r in rows if r['session']!='original')
        self.assertEqual(original['status'],'finished')
        self.assertEqual(replacement['status'],'running')
        self.assertEqual(json.loads(replacement['claim'])['token'],'private')
        env = self.child.call_args.kwargs['env']
        self.assertEqual(env['PR_COORDINATOR_CLAIM_TOKEN'],'private')
        self.assertNotIn('SUIFLEET_RECOVERY_RUN',env)
        directory = Path(result['directory'])
        self.assertNotIn('private',(directory/'prompt.txt').read_text())
        self.assertEqual((directory/'prompt.txt').stat().st_mode & 0o777,0o600)
        self.assertEqual(json.loads((directory/'command.json').read_text()),['original-command'])
        # Repeating cannot launch the same source twice.
        with self.assertRaises(RuntimeError): self.resume()
        self.assertEqual(self.child.call_count,1)

    def test_codex_uses_its_original_model_too(self):
        self.claim['agent'] = 'Codex'
        self.slot['family'] = 'Codex'
        self.d.config['workers'] = {'Codex': ['original-command']}
        self.d.db.execute("UPDATE runs SET family='Codex',claim=?", (json.dumps(self.claim),))
        self.assertEqual(self.resume()['family'], 'Codex')

    def test_normal_model_worker_cannot_dispatch(self):
        with patch.dict(control.os.environ, {'PR_COORDINATOR_ACTIVE':'1'}), patch.object(sys, 'argv',
                ['recovery_control.py','--project','project','--ticket','SF-1','--reason','concrete repair instruction']):
            with self.assertRaisesRegex(RuntimeError, 'normal implementation/review'):
                control.main()
        self.child.assert_not_called()

    def test_live_worker_helpers_resources_repair_or_capacity_prevent_launch(self):
        for method,value in [('stopped_identity',None),('capacity_helpers',[{'pid':4}]),('owner_locks',['network:test']),('replacement_repair',True),('available',False)]:
            with self.subTest(method=method):
                mock = getattr(self.d,method)
                previous = mock.return_value
                mock.return_value = value
                with self.assertRaises(RuntimeError): self.resume()
                mock.return_value = previous
                self.child.assert_not_called()
                self.assertEqual(self.d.db.execute('SELECT status FROM runs').fetchone()[0],'parked')

    def test_changed_owner_external_dependency_and_git_operation_are_fenced(self):
        for change in [dict(model='different'),dict(remote={'external_blockers':['decision']}),dict(status='review'),dict(publish_pending=True)]:
            with self.subTest(change=change):
                original = dict(self.claim)
                self.claim.update(change)
                with self.assertRaises(RuntimeError): self.resume()
                self.claim.clear(); self.claim.update(original)
        (self.path/'MERGE_HEAD').touch()
        with self.assertRaises(RuntimeError): self.resume()
        self.child.assert_not_called()

    def test_crash_after_intent_is_retained_and_never_replayed(self):
        self.child.side_effect = OSError('fork uncertain')
        with self.assertRaises(OSError): self.resume()
        self.d.update.assert_called_once()
        self.assertIn('orphaned',self.d.update.call_args.args)
        with self.assertRaises(RuntimeError): self.resume()
        self.assertEqual(self.child.call_count,1)

    def test_new_helper_after_resume_prevents_spawn(self):
        self.d.capacity_helpers.side_effect = [[],[dict(pid=9)]]
        with self.assertRaises(RuntimeError): self.resume()
        self.child.assert_not_called()
        self.d.update.assert_called_once()

    def test_pending_retry_or_dependency_resume_cannot_be_stolen(self):
        import hashlib
        digest = hashlib.sha256(b'private').hexdigest()
        for table in ['dependency_resumes','ci_redirects','capacity_retries']:
            self.d.db.execute('INSERT INTO '+table+' VALUES(?,?)',(digest,'waiting'))
            with self.assertRaises(RuntimeError): self.resume()
            self.d.db.execute('DELETE FROM '+table)
        self.child.assert_not_called()


class ControlLifecycleTests(unittest.TestCase):
    def test_main_restarts_after_success_failure_and_each_termination_signal(self):
        for failure, stage in [(None, 'resume'), (RuntimeError('resume failed'), 'resume')] + [
                (number, stage) for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
                for stage in ('stop', 'resume', 'close', 'start')]:
            with self.subTest(failure=failure, stage=stage), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                config = state/'config.json'
                config.write_text(json.dumps(dict(projects=[dict(id='project')],
                    implementation_slots=[dict(project='project', family='Codex')],
                    workers={'Codex':['codex']}, recovery_watchdog=dict(
                        enabled=True, projects=['project'], stall_seconds=60,
                        cooldown_seconds=300, timeout_seconds=120))))
                events = []
                previous = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
                real_flock = fcntl.flock
                def flock(stream, operation):
                    events.append(Path(stream.name).name)
                    return real_flock(stream, operation)
                def run(command, **kwargs):
                    if 'stop' in command:
                        events.append('stop')
                        if isinstance(failure, int) and stage == 'stop':
                            os.kill(os.getpid(), failure)
                    if 'start' in command:
                        # All authority locks must be released before restarting.
                        for name in ('activation.lock','dispatcher.lock','daemon.lock'):
                            with (state/name).open('a') as lock:
                                real_flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        if isinstance(failure, int) and stage == 'start':
                            os.kill(os.getpid(), failure)
                        events.append('start')
                    return Mock(returncode=0)
                dispatcher = Mock()
                dispatcher.ticket.return_value = dict(agent='Codex')
                def close():
                    if isinstance(failure, int) and stage == 'close':
                        os.kill(os.getpid(), failure)
                dispatcher.db.close.side_effect = close
                def resume(*args):
                    events.append('resume')
                    if isinstance(failure, int) and stage == 'resume':
                        os.kill(os.getpid(), failure)
                    if isinstance(failure, Exception):
                        raise failure
                    return dict(ok=True)
                argv = ['recovery_control.py','--config',str(config),'--state',str(state),
                        '--runtime',str(state),'--project','project','--ticket','SF-1',
                        '--reason','Repair the confirmed code defect']
                with patch.dict(os.environ, {'PR_COORDINATOR_ACTIVE':''}), patch.object(sys,'argv',argv), \
                     patch.object(control.subprocess,'run',side_effect=run), \
                     patch.object(control.subprocess,'check_output',return_value='process'), \
                     patch.object(control.fcntl,'flock',side_effect=flock), \
                     patch('dispatcher.Dispatcher',return_value=dispatcher), \
                     patch.object(control,'resume',side_effect=resume), patch('builtins.print'):
                    try:
                        if failure is None or stage in ('close', 'start'):
                            control.main()
                        else:
                            with self.assertRaises(InterruptedError if isinstance(failure,int) else RuntimeError):
                                control.main()
                    finally:
                        sys.path.remove(str(state.resolve()))
                expected = ['activation.lock','stop']
                if not isinstance(failure,int) or stage != 'stop':
                    expected += ['dispatcher.lock','daemon.lock','resume']
                    dispatcher.db.close.assert_called_once()
                expected += ['start']
                self.assertEqual(events, expected)
                for number, handler in previous.items():
                    self.assertEqual(signal.getsignal(number), handler)


if __name__ == '__main__': unittest.main()
