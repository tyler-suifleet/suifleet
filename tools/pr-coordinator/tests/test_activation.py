import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('activation', SOURCE/'activation.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class Activation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'; self.repo.mkdir()
        self.lib = self.root/'lib'; self.lib.mkdir()
        self.state = self.root/'state'; self.state.mkdir()
        self.service = Mock()
        self.service.active.return_value = list(a.SERVICES)
        self.engine = a.Activator(dict(root=str(self.repo), project='p', repository='owner/repo', github_account='user'), self.lib, self.state, self.service)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        runtime = self.repo/a.PREFIX; runtime.mkdir(parents=True)
        for name in a.FILES:
            (runtime/name).write_text('import argparse\nargparse.ArgumentParser().parse_args()\n')
        (runtime/'runtime-compatibility.json').write_text(json.dumps(a.COMPATIBILITY))
        self.commit('base')
        self.base = self.git('rev-parse', 'HEAD')
        self.initial = self.engine.stage(self.base)
        a.switch(self.engine.link, self.initial)
        self.db = sqlite3.connect(self.state/'reviews.sqlite3')
        self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE activation_merges(project,pr,head,base,commit_id,review_id); CREATE TABLE jobs(id,head,status,publication_done,result,kind);')
        self.engine.tip = Mock(side_effect=lambda: self.git('rev-parse', 'HEAD'))
        self.engine.gh = Mock()
        self.engine.validate = Mock()

    def git(self, *args):
        return a.run(['git', *args], self.repo)

    def commit(self, title):
        self.git('add', '.')
        self.git('commit', '-qm', title)

    def update(self, trusted=True, worker_source='# reviewed worker\n'):
        self.git('checkout', '-qb', 'feature')
        (self.repo/a.PREFIX/'worker.py').write_text(worker_source)
        self.commit('feature')
        head = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', 'master')
        self.git('merge', '--no-ff', 'feature', '-qm', 'reviewed merge')
        target = self.git('rev-parse', 'HEAD')
        if trusted:
            self.db.execute('INSERT INTO activation_merges VALUES(?,?,?,?,?,?)', ('p', 1, head, self.base, target, 3))
            self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (3, head, 'done', 1, json.dumps(dict(verdict='approve', acceptance_complete=True)), 'review'))
            self.db.commit()
        self.engine.gh.return_value = dict(merged_at='now', merge_commit_sha=target, base=dict(ref='main', repo=dict(full_name='owner/repo')), head=dict(sha=head, repo=dict(full_name='owner/repo')))
        return target

    def test_unchanged_does_not_restart(self):
        self.assertEqual(self.engine.activate()['installed'], self.base)
        self.service.stop.assert_not_called()

    def test_trusted_merged_activation_and_repeated_event(self):
        target = self.update()
        private = self.state/'private'; private.write_text('preserve')
        result = self.engine.activate()
        self.assertEqual(result['installed'], target)
        self.assertEqual(result['activation']['phase'], 'installed')
        self.assertEqual(private.read_text(), 'preserve')
        self.assertEqual((self.engine.link/'worker.py').read_text(), '# reviewed worker\n')
        self.engine.activate()
        self.assertEqual(self.service.stop.call_count, 1)

    def test_unreviewed_and_remote_mismatch_fail_closed(self):
        self.update(False)
        with self.assertRaisesRegex(RuntimeError, 'receipt'):
            self.engine.activate()
        self.assertEqual(self.engine.link.resolve(), self.initial)
        self.service.stop.assert_not_called()

    def test_invalid_review_or_unmerged_remote(self):
        self.update()
        self.engine.gh.return_value['merged_at'] = None
        with self.assertRaisesRegex(RuntimeError, 'remote merged'):
            self.engine.activate()
        self.service.stop.assert_not_called()

    def test_nonancestor_source(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.engine.verify('a'*40, self.base)

    def test_moved_source_and_partial_stage_failure(self):
        target = self.update()
        self.engine.tip.side_effect = [target, self.base]
        with self.assertRaisesRegex(RuntimeError, 'moved'):
            self.engine.activate()
        self.service.stop.assert_not_called()
        self.assertEqual(self.engine.link.resolve(), self.initial)

    def test_partial_copy_leaves_old_generation(self):
        self.update()
        original = Path.write_bytes
        def fail(path, data):
            if path.name == 'dispatcher.py':
                raise OSError('disk full')
            return original(path, data)
        with patch.object(Path, 'write_bytes', fail), self.assertRaises(OSError):
            self.engine.activate()
        self.assertEqual(self.engine.link.resolve(), self.initial)
        self.assertFalse(list((self.lib/'generations').glob('.stage-*')))
        self.service.stop.assert_not_called()

    def test_restart_failure_rolls_back_and_does_not_loop(self):
        target = self.update()
        self.service.start.side_effect = [RuntimeError('startup failure'), None]
        with self.assertRaises(RuntimeError):
            self.engine.activate()
        self.assertEqual(self.engine.link.resolve(), self.initial)
        self.assertEqual(self.engine.status()['activation']['phase'], 'rolled_back')
        with self.assertRaisesRegex(RuntimeError, 'failed activation'):
            self.engine.activate()
        self.assertEqual(self.service.start.call_count, 2)
        self.assertEqual(self.engine.status()['activation']['target'], target)

    def test_pending_intent_recovers_after_supervisor_crash(self):
        target = self.update()
        candidate = self.engine.stage(target)
        a.atomic(self.engine.journal, dict(phase='pending', previous=str(self.initial), target=target, generation=str(candidate), units=list(a.SERVICES)))
        a.switch(self.engine.link, candidate)
        with self.assertRaisesRegex(RuntimeError, 'failed activation'):
            self.engine.activate()
        self.assertEqual(self.engine.link.resolve(), self.initial)
        self.service.start.assert_called_once_with(list(a.SERVICES), self.initial)

    def test_serialized_activation(self):
        with (self.state/'activation.lock').open('a') as lock:
            a.fcntl.flock(lock, a.fcntl.LOCK_EX | a.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.engine.activate()

    def test_parent_readiness_records_actual_identity(self):
        a.ready(self.state, 'coordinator')
        receipt = json.loads((self.state/'coordinator-ready.json').read_text())
        self.assertEqual(receipt['pid'], os.getpid())
        self.assertEqual(receipt['runtime'], str(SOURCE))
        self.assertTrue(receipt['started'])

    def test_unit_requires_preserving_live_workers(self):
        service = a.Systemd(self.state)
        with patch.object(a, 'run', return_value='ActiveState=active\nKillMode=control-group'):
            with self.assertRaisesRegex(RuntimeError, 'preserve workers'):
                service.active()

    def test_invalid_source_is_never_switched(self):
        self.update(worker_source='def invalid syntax')
        with self.assertRaises(SyntaxError):
            self.engine.activate()
        self.assertEqual(self.engine.link.resolve(), self.initial)
        self.service.stop.assert_not_called()

    def test_validation_failure_is_actionable_and_does_not_stop_services(self):
        self.update()
        self.engine.validate.side_effect = RuntimeError('offline regression suite failed')
        with self.assertRaises(RuntimeError):
            self.engine.activate()
        self.assertIn('regression', self.engine.status()['activation']['detail'])
        self.service.stop.assert_not_called()

    def test_identical_runtime_new_main_skips_restarts(self):
        (self.repo/'documentation').write_text('main advanced')
        self.commit('docs')
        result = self.engine.activate()
        self.assertEqual(result['activation']['phase'], 'unchanged')
        self.assertNotEqual(result['merged'], result['installed'])
        self.engine.activate()
        self.service.stop.assert_not_called()
        self.assertEqual(self.engine.validate.call_count, 1)

    def test_unpublished_review_and_wrong_parents_fail_closed(self):
        target = self.update()
        self.db.execute('UPDATE jobs SET publication_done=0'); self.db.commit()
        with self.assertRaisesRegex(RuntimeError, 'review unavailable'):
            self.engine.verify(self.base, target)
        self.db.execute('UPDATE jobs SET publication_done=1')
        self.db.execute("UPDATE activation_merges SET base='wrong'"); self.db.commit()
        with self.assertRaisesRegex(RuntimeError, 'ancestry'):
            self.engine.verify(self.base, target)

    def test_worker_exit_receipt_survives_activation(self):
        # Real wrapper continues while the parent generations change. The parent
        # service adapter has no permission to stop or relaunch this worker.
        import sys
        import time
        directory = self.state/'worker'; directory.mkdir()
        release = directory/'release'
        command = [sys.executable, '-c',
                   'from pathlib import Path; import time; p=Path('+repr(str(release))+'); '
                   'exec("while not p.exists(): time.sleep(.01)")']
        (directory/'command.json').write_text(json.dumps(command))
        (directory/'prompt.txt').write_text('fixture')
        process = subprocess.Popen([sys.executable, str(SOURCE/'worker.py'), str(directory)], start_new_session=True)
        try:
            deadline = time.monotonic()+5
            while not (directory/'process.json').exists() and time.monotonic()<deadline:
                time.sleep(.01)
            identity = (directory/'process.json').read_bytes()
            self.update()
            self.engine.activate()
            self.assertIsNone(process.poll())
            self.assertEqual((directory/'process.json').read_bytes(), identity)
            self.assertFalse((directory/'exit.json').exists())
            release.touch()
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(json.loads((directory/'exit.json').read_text()), {'returncode': 0})
        finally:
            release.touch()
            process.wait(timeout=5)

    def test_incompatible_store_protocol_requires_explicit_migration(self):
        self.update()
        (self.repo/a.PREFIX/'runtime-compatibility.json').write_text('{"activation_protocol":2}')
        self.commit('incompatible protocol')
        with self.assertRaisesRegex(RuntimeError, 'migration'):
            self.engine.stage(self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.engine.link.resolve(), self.initial)

    def test_bootstrap_rejects_unmerged_checkout_before_mutating_install(self):
        import runpy
        import sys
        home = self.root/'home'; home.mkdir()
        config = home/'.config/pr-coordinator/config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'projects':[{'id':'p', 'root':str(self.repo),
                          'repository':'owner/repo', 'github_account':'user'}]}))
        library = home/'.local/lib/pr-coordinator'; library.mkdir(parents=True)
        sentinel = library/'coordinator.py'; sentinel.write_text('preserved')
        with patch.dict(sys.modules, {'activation':a}), patch.object(Path, 'home', return_value=home), \
             patch.object(sys, 'argv', ['install.py', '--activation-project', 'p']), \
             patch.object(a.Activator, 'tip', return_value='0'*40), \
             patch.object(subprocess, 'check_output', return_value='f'*40), \
             self.assertRaisesRegex(SystemExit, 'fetched main'):
            runpy.run_path(str(SOURCE/'install.py'), run_name='__main__')
        self.assertEqual(sentinel.read_text(), 'preserved')
        self.assertFalse((library/'current').exists())

    def test_bootstrap_requires_published_independent_merge_proof(self):
        target = self.update()
        self.db.executescript('CREATE TABLE integrations(project,pr,head,commit_id); ALTER TABLE jobs ADD COLUMN family; ALTER TABLE jobs ADD COLUMN directory; ALTER TABLE jobs ADD COLUMN project; ALTER TABLE jobs ADD COLUMN pr;')
        head = self.git('rev-parse', 'feature')
        self.db.execute('INSERT INTO integrations VALUES(?,?,?,?)', ('p', 1, head, target))
        folder = self.state/'review'; folder.mkdir()
        (folder/'snapshot.json').write_text(json.dumps(dict(headRefOid=head, baseRefOid=self.base, ticket=dict(agent='Codex'))))
        self.db.execute('UPDATE jobs SET family=?, directory=?, project=?, pr=?', ('Claude', str(folder), 'p', 1))
        self.db.commit()
        self.engine.verify_bootstrap(target)
        self.db.execute("UPDATE jobs SET family='Codex'"); self.db.commit()
        with self.assertRaisesRegex(RuntimeError, 'independent'):
            self.engine.verify_bootstrap(target)

    def test_validation_does_not_inherit_credentials_or_private_home(self):
        with patch.dict(os.environ, {'GH_TOKEN':'fixture-secret', 'PR_COORDINATOR_CLAIM_TOKEN':'fixture-claim'}), \
             patch.object(a, 'run') as execute:
            a.Activator.validate(self.engine, self.base)
        environment = execute.call_args.kwargs['env']
        self.assertNotIn('GH_TOKEN', environment)
        self.assertNotIn('PR_COORDINATOR_CLAIM_TOKEN', environment)
        self.assertNotEqual(environment['HOME'], str(Path.home()))

    def test_dependency_start_cannot_enable_an_inactive_parent(self):
        with patch.object(a, 'run', side_effect=['ActiveState=inactive\nKillMode=process',
                                               'ActiveState=active\nKillMode=process']):
            with self.assertRaisesRegex(RuntimeError, 'dependency'):
                a.Systemd(self.state).active()

    def test_fetch_uses_private_ref_without_clobbering_shared_fetch_head(self):
        with patch.object(a, 'run', side_effect=['fixture-token', '', 'a'*40]) as execute:
            self.assertEqual(a.Activator.tip(self.engine), 'a'*40)
        fetch = execute.call_args_list[1]
        self.assertIn('--no-write-fetch-head', fetch.args[0])
        self.assertIn('+refs/heads/main:refs/pr-coordinator/activation-main', fetch.args[0])
        self.assertNotIn('fixture-token', fetch.args[0])
        self.assertEqual(fetch.kwargs['env']['GH_TOKEN'], 'fixture-token')
        self.assertIn('refs/pr-coordinator/activation-main^{commit}', execute.call_args_list[2].args[0])

    def test_unreviewed_validation_source_cannot_execute(self):
        self.update()
        (self.repo/a.PREFIX/'tests').mkdir()
        (self.repo/a.PREFIX/'tests/test_unreviewed.py').write_text('raise RuntimeError("must not execute")')
        self.commit('direct unreviewed validation change')
        with self.assertRaisesRegex(RuntimeError, 'receipt'):
            self.engine.activate()
        self.engine.validate.assert_not_called()
        self.service.stop.assert_not_called()
