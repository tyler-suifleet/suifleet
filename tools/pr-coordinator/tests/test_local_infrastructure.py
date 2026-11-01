"""Real Git publication and sandbox execution; no hosted services or wallets."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from batch_integration import LocalInfrastructure, infrastructure_scope, LOCAL_COMMANDS
from activation import Activator
from test_batch_integration import Fixture


class LocalLane(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = f = Fixture(Path(self.tmp.name))
        self.addCleanup(f.db.close)
        f.batches = f.controller
        f.cleanup = lambda project, path: f.git('worktree', 'remove', str(path))
        f.project['local_infrastructure'] = {'enabled': True}
        f.git('checkout', '-qb', 'infra', f.base)
        for path, content in {
            'tools/pr-coordinator/tests/test_smoke.py': 'import unittest\nclass Smoke(unittest.TestCase):\n def test_true(self): self.assertTrue(True)\n',
            'scripts/tests/test_tracking.py': 'import unittest\nclass Smoke(unittest.TestCase):\n def test_true(self): self.assertTrue(True)\n',
            'scripts/bootstrap_tracking.py': 'print("offline plan")\n',
        }.items():
            p = f.repo/path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content)
        # Baseline fixtures live on main; the PR changes only coordinator source.
        f.git('add', '.'); f.git('commit', '-qm', 'fixture baseline')
        f.base = f.git('rev-parse', 'HEAD'); f.git('push', '-q', 'origin', 'HEAD:main')
        p = f.repo/'tools/pr-coordinator/worker.py'; p.write_text('# infrastructure\n')
        f.git('add', '.'); f.git('commit', '-qm', 'infrastructure')
        head = f.git('rev-parse', 'HEAD'); f.git('push', '-q', 'origin', 'HEAD:work/infra')
        f.git('checkout', '-q', 'main')
        self.owner = f.owners[0]; self.owner['spec']['area'] = 'infra'; self.owner['branch'] = 'work/infra'
        self.pr = f.make_pr(1, head, 'work/infra'); f.source_prs[1] = self.pr
        self.lane = LocalInfrastructure(f)
        self.process = f.state/'implementations'/self.owner['session']/'process.json'
        self.process.parent.mkdir(parents=True); self.process.write_text(json.dumps(dict(pid=99999999, started='1')))
        self.helpers = patch('coordinator.claim_processes', return_value=[]); self.helpers.start(); self.addCleanup(self.helpers.stop)

    def proof(self):
        row = self.f.db.execute('SELECT status,receipt FROM local_integrations').fetchone()
        return row[0], json.loads(row[1])

    def test_scope(self):
        self.assertTrue(infrastructure_scope(['tools/pr-coordinator/coordinator.py']))
        for paths in ([], ['dapp/src/a.ts'], ['tools/pr-coordinator/a.py', 'contracts/sources/a.move'],
                      ['.github/workflows/testnet.yml'], ['scripts/testnet_pool.py']):
            self.assertFalse(infrastructure_scope(paths))
        self.f.project['local_infrastructure']['enabled'] = False
        self.assertFalse(self.lane.eligible(self.f.project, self.pr, self.owner))

    def test_success_real_sandbox_merge_and_activation(self):
        self.assertTrue(self.lane.tick(self.f.project, self.pr, self.owner))
        status, proof = self.proof(); self.assertEqual(status, 'published')
        self.assertEqual(self.f.controller.main(self.f.project), proof['commit'])
        self.assertEqual([c['command'] for c in proof['checks']], [list(c) for c in LOCAL_COMMANDS])
        engine = Activator(dict(project='test', root=str(self.f.repo), repository='owner/repo'),
                           Path(self.tmp.name), self.f.state)
        engine.gh = lambda endpoint: dict(merged_at='now', merge_commit_sha=proof['commit'],
            base=dict(ref='main', repo=dict(full_name='owner/repo')),
            head=dict(sha=proof['head'], repo=dict(full_name='owner/repo')))
        self.assertTrue(engine.verify_local(self.f.db, proof['commit']))
        for field, value in [('tree','0'*40), ('paths',['dapp/a']), ('authorization','approve')]:
            changed = {**proof, field:value}
            self.f.db.execute('UPDATE local_integrations SET receipt=?', (json.dumps(changed),))
            with self.assertRaises(RuntimeError): engine.verify_local(self.f.db, proof['commit'])
        self.f.db.execute('UPDATE local_integrations SET receipt=?', (json.dumps(proof),))
        Path(proof['checks'][0]['log']).write_text('tampered')
        with self.assertRaises(RuntimeError): engine.verify_local(self.f.db, proof['commit'])

    def test_failed_command_is_durable_and_not_retried(self):
        with patch('batch_integration.LOCAL_COMMANDS', (('python3','-c','raise SystemExit(7)'),)):
            with self.assertRaises(RuntimeError): self.lane.tick(self.f.project, self.pr, self.owner)
        status, proof = self.proof(); self.assertEqual(status, 'failed')
        self.assertEqual(proof['checks'][0]['exit'], 7)
        self.assertTrue(self.lane.tick(self.f.project, self.pr, self.owner))
        self.assertEqual(self.f.controller.main(self.f.project), self.f.base)

    def test_changed_claim_fences_publication(self):
        original = self.f.ticket
        def ticket(*args):
            data = json.loads(original(*args)); data['token'] = 'changed'; return json.dumps(data)
        self.f.ticket = ticket
        with self.assertRaisesRegex(RuntimeError, 'inputs changed'):
            self.lane.tick(self.f.project, self.pr, self.owner)
        self.assertEqual(self.f.controller.main(self.f.project), self.f.base)

    def test_active_uncertain_worker_and_dirty_worktree(self):
        self.f.db.execute("INSERT INTO jobs VALUES('test',1,'fix','orphaned')")
        with self.assertRaises(RuntimeError): self.lane.tick(self.f.project, self.pr, self.owner)
        self.f.db.execute('DELETE FROM jobs')
        self.owner['worktree'] = str(self.f.repo)
        (self.f.repo/'untracked').write_text('active edits')
        with self.assertRaises(RuntimeError): self.lane.tick(self.f.project, self.pr, self.owner)

    def test_push_uncertainty_never_replays(self):
        original = self.f.controller.git
        def git(project, *args, **kwargs):
            if args[:2] == ('push', 'origin'): raise RuntimeError('unknown push outcome')
            return original(project, *args, **kwargs)
        with patch.object(self.f.controller, 'git', side_effect=git):
            with self.assertRaises(RuntimeError): self.lane.tick(self.f.project, self.pr, self.owner)
        self.assertEqual(self.proof()[0], 'publish_pending')
        with self.assertRaises(Exception): self.lane.tick(self.f.project, self.pr, self.owner)
        self.assertEqual(self.f.controller.main(self.f.project), self.f.base)

    def test_changed_source_or_main_never_publishes(self):
        for field in ('headRefOid', 'baseRefOid'):
            with self.subTest(field=field):
                self.f.db.execute('DELETE FROM local_integrations')
                original = self.f.pr
                def changed(project, number):
                    pr = original(project, number); pr[field] = '0'*40; return pr
                with patch.object(self.f, 'pr', side_effect=changed):
                    with self.assertRaisesRegex(RuntimeError, 'inputs changed'):
                        self.lane.tick(self.f.project, self.pr, self.owner)
                self.assertEqual(self.f.controller.main(self.f.project), self.f.base)

    def test_metadata_and_feedback_scope(self):
        self.owner['spec']['area'] = 'edge'
        self.assertFalse(self.lane.eligible(self.f.project, self.pr, self.owner))
        self.owner['spec']['area'] = 'infra'
        self.pr['threads'] = [dict(isResolved=False)]
        self.assertFalse(self.lane.eligible(self.f.project, self.pr, self.owner))
        self.pr['threads'] = []
        self.pr['isCrossRepository'] = True
        self.assertFalse(self.lane.eligible(self.f.project, self.pr, self.owner))

    def test_sandbox_has_no_host_credentials_or_network(self):
        command = ('python3', '-c', "import os,socket; assert 'PR_COORDINATOR_CLAIM_TOKEN' not in os.environ; assert not os.path.exists('/home/tyler'); assert socket.if_nameindex() == [(1, 'lo')]")
        with patch('batch_integration.LOCAL_COMMANDS', (command,)):
            self.assertTrue(self.lane.tick(self.f.project, self.pr, self.owner))
        self.assertEqual(self.proof()[0], 'published')

    def test_live_implementation_is_fenced(self):
        import os
        from coordinator import process_identity
        self.process.write_text(json.dumps(dict(pid=os.getpid(), started=process_identity(os.getpid()))))
        with self.assertRaises(RuntimeError): self.lane.tick(self.f.project, self.pr, self.owner)


if __name__ == '__main__': unittest.main()

class PolicyUpgrade(unittest.TestCase):
    def test_explicit_bootstrap_retains_normal_verifier(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from upgrade_activation import verify_policy_upgrade
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory); sqlite3.connect(state/'reviews.sqlite3').close()
            old = SimpleNamespace(state=state, git=Mock(side_effect=['', 'normal\nlocal']),
                                  verify=Mock(side_effect=[None, RuntimeError('old protocol')]))
            new = SimpleNamespace(verify_local=Mock(return_value=True))
            verify_policy_upgrade(old, new, 'base', 'target')
            self.assertEqual(old.verify.call_args_list[0].args, ('base','normal'))
            self.assertEqual(old.verify.call_args_list[1].args, ('normal','local'))
            new.verify_local.assert_called_once()

    def test_unproven_step_is_rejected(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from upgrade_activation import verify_policy_upgrade
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory); sqlite3.connect(state/'reviews.sqlite3').close()
            old = SimpleNamespace(state=state, git=Mock(side_effect=['', 'unknown']),
                                  verify=Mock(side_effect=RuntimeError('unreviewed')))
            new = SimpleNamespace(verify_local=Mock(return_value=False))
            with self.assertRaises(RuntimeError): verify_policy_upgrade(old, new, 'base', 'target')

class SchedulerLane(unittest.TestCase):
    def test_local_lane_bypasses_source_batch_and_hosted_gates(self):
        from unittest.mock import Mock
        from test_streaming_source_reviews import fixture
        import coordinator
        with tempfile.TemporaryDirectory() as directory:
            engine = coordinator.Coordinator({'workers':{'Codex':['true'],'Claude':['true']}, 'projects':[]}, Path(directory))
            self.addCleanup(engine.store.db.close)
            pr, owner = fixture()
            project = dict(id='p', repository='a/b', integration_batch={'enabled':True})
            engine.local_infrastructure.eligible = Mock(return_value=True)
            engine.local_infrastructure.tick = Mock(return_value=True)
            engine.owners = Mock(return_value=[owner]); engine.collect = Mock()
            engine.prs = Mock(return_value=[pr]); engine.discover_merged = Mock()
            engine.ensure_closing_reference = Mock(); engine.pr = Mock(return_value=pr)
            engine.launch = Mock(); engine.batches.tick = Mock(return_value=set())
            self.assertFalse(engine.start_source_review(project, pr, [owner]))
            engine.tick_project(project, True)
            engine.local_infrastructure.tick.assert_called_once_with(project, pr, owner)
            engine.batches.tick.assert_called_once_with(project, [], [owner], True)
            engine.launch.assert_not_called()
