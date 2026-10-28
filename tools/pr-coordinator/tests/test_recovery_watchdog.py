import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import recovery_watchdog as w
import install_watchdog as installer


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.config = dict(projects=[dict(id='suifleet', root=str(self.base))], workers={'Codex':['codex','exec','-']},
                           recovery_watchdog=dict(enabled=True, projects=['suifleet'], stall_seconds=60,
                                                  cooldown_seconds=300, timeout_seconds=120))
        self.model = dict(inventory={'suifleet':dict(state='ok', breakdown={'review':3})}, workers=[], attention=[])

    def monitor(self, now=1000):
        return w.Watchdog(self.config, self.base, now)

    def pending(self):
        monitor = self.monitor()
        folder = monitor.root/'run-test'
        folder.mkdir()
        monitor.state['run'] = dict(phase='running', nonce='test-nonce', directory=str(folder), pid=123, started='100')
        return monitor, folder

    def test_requires_observed_stall_and_offline_only_default(self):
        self.assertIn('observation grace', self.monitor().tick(self.model)['status'])
        self.assertIn('would launch', self.monitor(1061).tick(self.model)['status'])

    def test_real_progress_restarts_stall_clock(self):
        self.monitor().tick(self.model)
        self.model['inventory']['suifleet']['breakdown'].update(review=2, done=1)
        self.assertIn('observation grace', self.monitor(1061).tick(self.model)['status'])
        self.assertIn('would launch', self.monitor(1122).tick(self.model)['status'])

    def test_worker_notice_and_nonterminal_churn_cannot_starve_recovery(self):
        self.monitor().tick(self.model)
        self.model['workers'] = [dict(project='suifleet', id='retry1', role='implementation', ticket='SF-1')]
        self.model['attention'] = [dict(project='suifleet', state='ci-failed', ticket='SF-1', pr=1)]
        self.model['inventory']['suifleet']['breakdown'].update(review=2, in_progress=1)
        self.monitor(1020).tick(self.model)
        self.model['workers'] = []
        self.model['attention'] = []
        self.model['inventory']['suifleet']['breakdown'].update(review=3, in_progress=0)
        self.monitor(1050).tick(self.model)
        self.model['attention'] = [dict(project='suifleet', state='ci-failed', ticket='SF-2', pr=2)]
        self.assertIn('would launch', self.monitor(1061).tick(self.model)['status'])
        observation = w.read(self.monitor().path)['observations']['suifleet']
        self.assertEqual(observation['progress_at'], 1000)
        self.assertEqual(observation['activity_at'], 1061)

    def test_long_active_worker_exit_does_not_grant_another_idle_grace(self):
        self.model['workers'] = [dict(project='suifleet', id='worker1', role='implementation', ticket='SF-1')]
        self.monitor().tick(self.model)
        self.assertIn('workers active', self.monitor(5000).tick(self.model)['status'])
        self.model['workers'] = []
        self.assertIn('would launch', self.monitor(5001).tick(self.model)['status'])

    def test_legacy_state_migration_keeps_original_recovery_deadline(self):
        monitor = self.monitor()
        monitor.tick(self.model)
        old = w.read(monitor.path)
        observation = old['observations']['suifleet']
        for key in ('done', 'inventory_available', 'activity_at'):
            observation.pop(key)
        w.atomic(monitor.path, old)
        self.assertIn('would launch', self.monitor(1061).tick(self.model)['status'])
        self.assertEqual(w.read(monitor.path)['observations']['suifleet']['progress_at'],1000)

    def test_inventory_loss_and_return_do_not_manufacture_completion(self):
        self.model['inventory']['suifleet']['breakdown']['done'] = 5
        self.monitor().tick(self.model)
        self.monitor(1050).tick({})
        self.assertIn('would launch',self.monitor(1061).tick(self.model)['status'])
        self.assertEqual(w.read(self.monitor().path)['observations']['suifleet']['progress_at'],1000)

    def test_grace_has_an_explicit_deadline_and_boundary_is_due(self):
        report = self.monitor().tick(self.model)
        self.assertEqual(report['next_check_at'],1060)
        report = self.monitor(1059).tick(self.model)
        self.assertIn('observation grace',report['status'])
        self.assertEqual(report['projects'][0]['recovery_due_at'],1060)
        self.assertIn('would launch',self.monitor(1060).tick(self.model)['status'])

    def test_normal_active_work_does_not_launch_an_idle_recovery_agent(self):
        self.model['workers'] = [dict(project='suifleet', id='job1', role='implementation', ticket='SF-1')]
        self.monitor().tick(self.model)
        self.assertIn('workers active', self.monitor(5000).tick(self.model, execute=True)['status'])

    def test_closed_or_only_external_acceptance_does_not_launch(self):
        self.model['inventory']['suifleet']['breakdown'] = {'blocked':4}
        self.monitor().tick(self.model)
        self.assertIn('no actionable', self.monitor(5000).tick(self.model, execute=True)['status'])

    def test_deferred_acceptance_backlog_does_not_spawn_recovery(self):
        self.config['projects'][0]['acceptance_policy'] = {'development_defaults':{'acceptance_only_tickets':['SF-031']}}
        self.model['inventory']['suifleet']['breakdown'] = {'backlog':1}
        self.model['_watchdog_tickets'] = {'suifleet':[dict(key='SF-031',status='backlog')]}
        self.monitor().tick(self.model)
        self.assertIn('no actionable',self.monitor(5000).tick(self.model,execute=True)['status'])

    def test_timestamp_only_and_obsolete_retry_noise_are_not_progress(self):
        self.monitor().tick(self.model)
        self.model['generated_at'] = 1061
        self.model['attention'] = [dict(project='suifleet', state='retry', since=1061, pr=2)]
        self.assertIn('would launch', self.monitor(1061).tick(self.model)['status'])

    def test_unrelated_project_never_triggers_this_watchdog(self):
        self.model['inventory']['suifleet']['breakdown'] = {}
        self.model['attention'] = [dict(project='other', state='ci-failed')]
        self.monitor().tick(self.model)
        self.assertIn('no actionable', self.monitor(5000).tick(self.model)['status'])

    def test_missing_observability_gets_a_bounded_diagnosis(self):
        self.monitor().tick({})
        self.assertIn('would launch', self.monitor(1061).tick({})['status'])

    def test_unknown_project_boolean_limits_and_command_are_rejected(self):
        for option, value in [('projects',['other']), ('enabled','yes'), ('timeout_seconds',False),
                              ('cooldown_seconds',1), ('stall_seconds',-1)]:
            config = copy.deepcopy(self.config)
            config['recovery_watchdog'][option] = value
            with self.subTest(option=option), self.assertRaises(ValueError):
                w.settings(config)
        self.config['workers']['Codex'] = []
        with self.assertRaises(ValueError): w.settings(self.config)

    def test_durable_launch_intent_precedes_fork_and_strips_parent_secrets(self):
        monitor = self.monitor()
        child = Mock(pid=123)
        def launch(*args, **kwargs):
            state = w.read(monitor.path)
            self.assertEqual(state['run']['phase'], 'launching')
            self.assertEqual((Path(state['run']['directory'])/'prompt.txt').stat().st_mode & 0o777, 0o600)
            self.assertNotIn('GH_TOKEN', kwargs['env'])
            self.assertNotIn('PR_COORDINATOR_CLAIM_TOKEN', kwargs['env'])
            self.assertNotIn('PR_COORDINATOR_ACTIVE', kwargs['env'])
            prompt = (Path(state['run']['directory'])/'prompt.txt').read_text()
            self.assertIn('hand it\nto normal PR review', prompt)
            self.assertIn('only through the reviewed merged-only path', prompt)
            self.assertIn('never install unreviewed\nworking-tree changes', prompt)
            self.assertNotIn('test and install the contained fix', prompt)
            self.assertTrue(kwargs['start_new_session'])
            return child
        with patch.dict(os.environ, GH_TOKEN='secret', PR_COORDINATOR_CLAIM_TOKEN='claim', PR_COORDINATOR_ACTIVE='1'), \
                patch.object(w.subprocess, 'Popen', side_effect=launch), \
                patch.object(w, 'identity', return_value=dict(pid=123, started='200', session=123)):
            monitor.tick(self.model, execute=True, kick=True)
        report = monitor.path.read_text()
        self.assertNotIn('secret', report)
        self.assertNotIn('"claim"', report)
        self.assertEqual(w.read(monitor.path)['run']['phase'], 'running')

    def test_uncertain_fork_never_replays_after_restart(self):
        with patch.object(w.subprocess, 'Popen', side_effect=OSError('launch failed')):
            with self.assertRaises(OSError): self.monitor().tick(self.model, execute=True, kick=True)
        with patch.object(w.subprocess, 'Popen') as launch:
            self.assertIn('uncertain launch', self.monitor(5000).tick(self.model, execute=True, kick=True)['status'])
            launch.assert_not_called()

    def test_restart_adopts_process_receipt_and_fences_surviving_descendants(self):
        monitor, folder = self.pending()
        monitor.state['run'] = dict(phase='launching', nonce='test-nonce', directory=str(folder))
        monitor.save('intent')
        w.atomic(folder/'process.json', dict(pid=123, started='100', session=123))
        w.atomic(folder/'exit.json', dict(returncode=0))
        with patch.object(w, 'survivors', return_value=[456]), patch.object(w.subprocess, 'Popen') as launch:
            self.assertIn('descendant', self.monitor(5000).tick(self.model, execute=True, kick=True)['status'])
            launch.assert_not_called()

    def test_missing_exit_is_uncertain_and_preserves_reservation(self):
        monitor, folder = self.pending()
        with patch.object(w, 'survivors', return_value=[]):
            self.assertTrue(monitor.reconcile())
        self.assertIn('run', w.read(monitor.path))

    def test_reported_wait_and_cooldown_survive_restart_and_kick(self):
        monitor, folder = self.pending()
        w.atomic(folder/'exit.json', dict(returncode=0))
        w.atomic(folder/'result.json', dict(outcome='waiting', actions=[], verification=['CI running'], blockers=[], next_check_at=3000))
        with patch.object(w, 'survivors', return_value=[]): monitor.tick(self.model)
        with patch.object(w.subprocess, 'Popen') as launch:
            self.assertIn('provider wait', self.monitor(2000).tick(self.model, execute=True, kick=True)['status'])
            launch.assert_not_called()
        self.assertEqual(w.read(monitor.path)['last_result']['verification'], ['CI running'])

    def test_nonzero_timeout_and_invalid_result_back_off(self):
        for code in [1,124,0]:
            with self.subTest(code=code):
                if self.monitor().path.exists(): self.monitor().path.unlink()
                folder = self.monitor().root/'run-test'
                if folder.exists():
                    import shutil
                    shutil.rmtree(folder)
                monitor, folder = self.pending()
                w.atomic(folder/'exit.json', dict(returncode=code))
                with patch.object(w, 'survivors', return_value=[]): monitor.tick(self.model)
                self.assertGreaterEqual(w.read(monitor.path)['not_before'], 1300)
                self.assertEqual(w.read(monitor.path)['last_result']['outcome'], 'blocked')

    def test_activation_lock_and_disabled_file_prevent_launch(self):
        monitor = self.monitor()
        with (self.base/'activation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertIn('maintenance', monitor.tick(self.model, execute=True, kick=True)['status'])
        (monitor.root/'disabled').touch()
        self.assertEqual(monitor.tick(self.model, execute=True, kick=True)['status'], 'disabled')

    def test_singleton_tick_lock_does_not_run_dashboard_or_model(self):
        config_path = self.base/'config.json'
        config_path.write_text(json.dumps(self.config))
        with (self.base/'recovery-watchdog.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            proc = subprocess.run([sys.executable, str(ROOT/'recovery_watchdog.py'), '--config', str(config_path),
                                   '--state', str(self.base), '--execute'], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('already running', proc.stdout)

    def test_recursive_worker_cannot_invoke_watchdog(self):
        env = {**os.environ, 'SUIFLEET_RECOVERY_ACTIVE':'1'}
        proc = subprocess.run([sys.executable, str(ROOT/'recovery_watchdog.py'), '--config', '/nonexistent'],
                              env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('Recursive', proc.stderr)

    def test_additive_install_preserves_existing_runtime_and_private_settings(self):
        home = self.base/'home'
        config = home/'.config/pr-coordinator/config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(self.config))
        sentinel = home/'.local/lib/pr-coordinator/current'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text('existing reviewed runtime')
        with patch.object(installer.shutil, 'which', return_value='/usr/bin/node'), \
                patch.object(installer.subprocess, 'run') as systemd:
            generation = installer.install(home, ['suifleet'])
        systemd.assert_not_called()
        self.assertEqual(sentinel.read_text(), 'existing reviewed runtime')
        self.assertEqual(w.read(config)['workers'], self.config['workers'])
        self.assertFalse(w.read(config)['recovery_watchdog']['enabled'])
        self.assertEqual((generation/'recovery_watchdog.py').read_bytes(), (ROOT/'recovery_watchdog.py').read_bytes())
        self.assertTrue((home/'.config/systemd/user/recovery-watchdog.timer').exists())
        service = (home/'.config/systemd/user/recovery-watchdog.service').read_text()
        self.assertIn('TimeoutStartSec=165', service)
        self.assertNotIn('@OBSERVATION_TIMEOUT@', service)

    def test_escaped_helper_fences_a_successfully_exited_wrapper(self):
        folder = self.base/'escape'
        folder.mkdir()
        helper_file = folder/'helper.pid'
        code = ('import subprocess,sys; from pathlib import Path; '
                'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],start_new_session=True); '
                'Path(sys.argv[1]).write_text(str(p.pid))')
        w.atomic(folder/'command.json',[sys.executable,'-c',code,str(helper_file)])
        w.atomic(folder/'limits.json',dict(timeout_seconds=5))
        (folder/'prompt.txt').write_text('test')
        proc = subprocess.run([sys.executable,str(ROOT/'recovery_worker.py'),str(folder)],
            env={**os.environ,'SUIFLEET_RECOVERY_RUN':'escaped-test'}, start_new_session=True,timeout=10)
        helper = int(helper_file.read_text())
        try:
            self.assertEqual(proc.returncode,0)
            receipt=w.read(folder/'process.json')
            run=dict(nonce='escaped-test',directory=str(folder),pid=receipt['pid'],started=receipt['started'])
            self.assertIn(helper,w.survivors(run))
        finally:
            try: os.kill(helper,9)
            except ProcessLookupError: pass

    def test_reported_recovery_is_not_marked_verified_without_snapshot_progress(self):
        monitor,folder=self.pending()
        w.atomic(folder/'exit.json',dict(returncode=0))
        w.atomic(folder/'result.json',dict(outcome='recovered',actions=['restarted'],verification=[],blockers=[],next_check_at=1300))
        with patch.object(w,'survivors',return_value=[]): monitor.tick(self.model)
        self.assertEqual(w.read(monitor.path)['snapshot_verification']['changed_projects'],[])
        self.model['inventory']['suifleet']['breakdown'].update(review=2, done=1)
        self.monitor(1100).tick(self.model)
        self.assertEqual(w.read(monitor.path)['snapshot_verification']['changed_projects'],['suifleet'])

    def test_actual_wrapper_timeout_stops_child_group_and_records_exit(self):
        folder = self.base/'wrapper'
        folder.mkdir()
        w.atomic(folder/'command.json', [sys.executable, '-c',
                 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); time.sleep(60)'])
        w.atomic(folder/'limits.json', dict(timeout_seconds=0.3))
        (folder/'prompt.txt').write_text('test')
        env = {**os.environ,'SUIFLEET_RECOVERY_RUN':'wrapper-test'}
        proc = subprocess.run([sys.executable,str(ROOT/'recovery_worker.py'),str(folder)], env=env,
                              start_new_session=True, timeout=10, capture_output=True)
        self.assertEqual(proc.returncode,124,proc.stderr)
        self.assertEqual(w.read(folder/'exit.json')['returncode'],124)
        self.assertEqual((folder/'worker.log').stat().st_mode & 0o777, 0o600)
        receipt = w.read(folder/'process.json')
        run = dict(nonce='wrapper-test', directory=str(folder), pid=receipt['pid'], started=receipt['started'])
        self.assertEqual(w.survivors(run),[])


if __name__ == '__main__':
    unittest.main()
