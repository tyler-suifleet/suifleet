import copy
import importlib.util
import json
import multiprocessing
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('ticket_coordinator', ROOT / 'scripts/ticket_coordinator.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def catalog():
    original = json.loads((ROOT / 'docs/tracking/backlog.json').read_text())
    items = []
    for n, path in enumerate(['contracts/a.move', 'dapp/', 'contracts/', 'docs/readme.md'], 1):
        issue = copy.deepcopy(original['issues'][0])
        issue.update(key=f'SF-{n:03}', priority='P0', paths=[path], depends_on=[])
        items.append(issue)
    items[3]['depends_on'] = ['SF-001']
    original['issues'] = items
    return original


def snapshot(config):
    return {i['key']: {'state': 'open', 'status': 'Backlog', 'agent': 'Unassigned',
                       'assignees': [], 'url': f'https://example/{i["key"]}', 'item_id': i['key']}
            for i in config['issues']}


def race_worker(path, barrier, queue, ticket):
    coordinator = m.Coordinator(path)
    try:
        barrier.wait(timeout=20)
        claim = coordinator.claim('Codex', 'test-model', str(m.uuid.uuid4()), ticket)
        queue.put(('claimed', claim['key']))
    except m.CoordinationError:
        queue.put(('rejected', ticket))
    finally:
        coordinator.close()


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'claims.sqlite3'
        self.now = 1000
        self.c = m.Coordinator(self.path, clock=lambda: self.now)
        self.config = catalog()
        self.remote = snapshot(self.config)
        self.c.import_catalog(self.config, self.remote)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def claim(self, ticket='SF-001', session='codex-session'):
        result = self.c.claim('Codex', 'test-model', session, ticket)
        self.acknowledge(ticket)
        return result

    def acknowledge(self, key):
        # Simulates a successful mirror without any network calls.
        self.c.db.execute('UPDATE tickets SET published_revision=revision WHERE key=?', (key,))

    def test_simultaneous_processes_have_exactly_one_winner(self):
        self.c.db.execute("UPDATE metadata SET value=? WHERE key='snapshot_at'", (str(m.time.time()),))
        context = multiprocessing.get_context('fork')
        barrier, queue = context.Barrier(10), context.Queue()
        processes = [context.Process(target=race_worker, args=(str(self.path), barrier, queue, 'SF-001')) for _ in range(10)]
        for process in processes:
            process.start()
        results = [queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(kind == 'claimed' for kind, _ in results), 1)

    def test_overlapping_file_paths_do_not_block_claims(self):
        self.claim()
        self.claim('SF-002', 'claude-session')
        # SF-003 covers contracts/, which contains SF-001's contracts/a.move.
        self.assertEqual(self.claim('SF-003', 'another-session')['key'], 'SF-003')

    def test_expired_worker_remains_reserved_and_old_token_is_fenced_after_recovery(self):
        claim = self.claim()
        self.now += 901
        self.c.import_catalog(self.config, self.remote)
        with self.assertRaisesRegex(m.CoordinationError, 'expired'):
            self.c.assert_owner('SF-001', claim['token'])
        with self.assertRaises(m.CoordinationError):
            self.claim('SF-001', 'claude-session')
        with self.assertRaises(m.CoordinationError):
            self.c.recover('SF-001', claim['token'], 'process stopped', False)
        self.c.recover('SF-001', claim['token'], 'process stopped; worktree retained', True)
        self.acknowledge('SF-001')
        replacement = self.claim('SF-001', 'replacement')
        self.assertNotEqual(replacement['token'], claim['token'])
        with self.assertRaises(m.CoordinationError):
            self.c.heartbeat('SF-001', claim['token'])

    def test_same_owner_can_renew_expired_claim_without_reclaim(self):
        claim = self.claim()
        self.now += 901
        renewed = self.c.heartbeat('SF-001', claim['token'])
        self.assertFalse(renewed['expired'])
        self.c.assert_owner('SF-001', claim['token'])

    def test_review_retains_scope_and_does_not_unblock_dependencies(self):
        claim = self.claim()
        self.c.transition('SF-001', claim['token'], 'review', 'tests passed; PR prepared')
        self.assertIsNone(self.c.row('SF-001')['expires'])
        for ticket in ('SF-001', 'SF-004'):
            with self.assertRaises(m.CoordinationError):
                self.claim(ticket, 'claude-session')
        with self.assertRaises(m.CoordinationError):
            self.c.assert_owner('SF-001', claim['token'])
        self.c.complete('SF-001', claim['token'], 'human', 'merged and acceptance verified')
        with self.assertRaises(m.CoordinationError):
            self.claim('SF-004', 'claude-session')
        self.acknowledge('SF-001')
        self.c.import_catalog(self.config, self.remote)
        self.assertEqual(self.c.row('SF-001')['status'], 'done')
        self.claim('SF-004', 'claude-session')

    def test_import_never_shrinks_or_steals_active_scope(self):
        claim = self.claim()
        changed = copy.deepcopy(self.config)
        changed['issues'][0]['paths'] = ['scripts/']
        remote = copy.deepcopy(self.remote)
        remote['SF-001'].update(state='closed', status='Done')
        self.c.import_catalog(changed, remote)
        row = self.c.row('SF-001')
        self.assertEqual(row['token'], claim['token'])
        self.assertEqual(set(json.loads(row['spec'])['paths']), {'scripts', 'contracts/a.move'})
        with self.assertRaises(m.CoordinationError):
            self.claim('SF-004', 'claude-session')

    def test_remote_owner_reserves_ticket_not_overlapping_paths(self):
        remote = copy.deepcopy(self.remote)
        remote['SF-003'].update(agent='Claude', status='In progress')
        self.c.import_catalog(self.config, remote)
        with self.assertRaises(m.CoordinationError):
            self.claim('SF-003', 'codex-session')
        self.assertEqual(self.claim()['key'], 'SF-001')

    def test_remote_closed_or_assigned_tickets_are_not_claimable(self):
        for fields in ({'state': 'closed'}, {'assignees': ['human']}, {'status': 'Blocked'}, {'agent': 'Claude'}):
            remote = copy.deepcopy(self.remote)
            remote['SF-001'].update(fields)
            self.c.import_catalog(self.config, remote)
            with self.assertRaises(m.CoordinationError):
                self.claim()

    def test_live_dependencies_added_after_seeding_are_respected(self):
        remote = copy.deepcopy(self.remote)
        remote['SF-001']['depends_on'] = ['SF-002']
        self.c.import_catalog(self.config, remote)
        with self.assertRaisesRegex(m.CoordinationError, 'dependencies incomplete'):
            self.claim()
        remote['SF-001']['depends_on'] = []
        remote['SF-001']['external_blockers'] = ['https://example/another-repo/issues/1']
        self.c.import_catalog(self.config, remote)
        with self.assertRaisesRegex(m.CoordinationError, 'external GitHub dependencies'):
            self.claim()

    def test_partial_mirror_retries_latest_revision_and_only_updates_project_fields(self):
        claim = self.claim()
        self.c.db.execute('UPDATE tickets SET published_revision=0 WHERE key=?', ('SF-001',))
        class FakeGh:
            def __init__(self):
                self.calls = []
                self.fail_once = True
            def run(self, *args, **kwargs):
                return {'fields': [
                    {'name': 'Status', 'id': 'status', 'options': [{'name': 'In progress', 'id': 'ip'}, {'name': 'Review', 'id': 'review'}]},
                    {'name': 'Agent', 'id': 'agent', 'options': [{'name': 'Codex', 'id': 'codex'}]},
                    {'name': 'Model', 'id': 'model'}, {'name': 'Work branch', 'id': 'branch'}]}
            def graphql(self, query, variables):
                self.calls.append(variables['input'])
                if self.fail_once and len(self.calls) == 2:
                    self.fail_once = False
                    raise RuntimeError('partial network failure')
                return {}
        gh = FakeGh()
        state = {'project': {'number': 1, 'id': 'project', 'owner': {'login': 'test'}}}
        with self.assertRaises(RuntimeError):
            self.c.publish('SF-001', state, gh)
        self.assertTrue(self.c.view(self.c.row('SF-001'))['publish_pending'])
        self.c.transition('SF-001', claim['token'], 'review', 'handoff')
        self.c.publish('SF-001', state, gh)
        self.assertFalse(self.c.view(self.c.row('SF-001'))['publish_pending'])
        self.assertEqual(gh.calls[2]['value'], {'singleSelectOptionId': 'review'})
        self.assertEqual(len(gh.calls), 6)
        self.assertTrue(all(call['projectId'] == 'project' for call in gh.calls))

    def test_priority_and_dependency_selection(self):
        self.config['issues'][0]['priority'] = 'P1'
        self.config['issues'][2]['priority'] = 'P2'
        self.c.import_catalog(self.config, self.remote)
        result = self.c.claim('Claude', 'test-model', 'claude-session')
        self.assertEqual(result['key'], 'SF-002')

    def test_stale_snapshot_and_incomplete_catalog_fail_closed(self):
        self.now += 301
        with self.assertRaisesRegex(m.CoordinationError, 'stale'):
            self.claim()
        smaller = copy.deepcopy(self.config)
        smaller['issues'].pop()
        with self.assertRaisesRegex(m.CoordinationError, 'older/incomplete'):
            self.c.import_catalog(smaller, snapshot(smaller))

    def test_resource_expansion_conflicts_and_assertion(self):
        claim = self.claim()
        other = self.c.claim('Claude', 'test-model', 'claude-session', 'SF-002', resources=['hardware:pi'])
        self.acknowledge('SF-002')
        with self.assertRaisesRegex(m.CoordinationError, 'Resource conflicts'):
            self.c.expand('SF-001', claim['token'], ['hardware:pi'])
        self.c.expand('SF-001', claim['token'], ['port:3000'])
        self.acknowledge('SF-001')
        self.c.assert_owner('SF-001', claim['token'], ['port:3000'])
        with self.assertRaisesRegex(m.CoordinationError, 'not reserved'):
            self.c.assert_owner('SF-001', claim['token'], ['hardware:pi'])
        with self.assertRaises(m.CoordinationError):
            self.c.assert_owner('SF-001', claim['token'], ['../outside'])
        self.assertTrue(other['token'])

    def test_one_implementation_per_session_but_review_allows_next(self):
        claim = self.claim()
        with self.assertRaises(m.CoordinationError):
            self.claim('SF-002')
        self.c.transition('SF-001', claim['token'], 'review', 'handoff')
        self.claim('SF-002')
        with self.assertRaisesRegex(m.CoordinationError, 'another implementation'):
            self.c.resume('SF-001', claim['token'])

    def test_block_resume_and_release(self):
        claim = self.claim()
        self.c.transition('SF-001', claim['token'], 'blocked', 'needs compatible endpoint')
        with self.assertRaises(m.CoordinationError):
            self.c.assert_owner('SF-001', claim['token'])
        self.c.resume('SF-001', claim['token'])
        self.acknowledge('SF-001')
        self.c.assert_owner('SF-001', claim['token'])
        self.c.release('SF-001', claim['token'], 'handoff recorded; stopped editing', True)
        with self.assertRaisesRegex(m.CoordinationError, 'mirror pending'):
            self.claim('SF-001', 'replacement')

    def test_database_shared_by_real_linked_worktrees(self):
        repo = Path(self.tmp.name) / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'test fixture'], check=True)
        worktree = Path(self.tmp.name) / 'linked'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(worktree)], check=True, capture_output=True)
        self.assertEqual(m.database_path(repo), m.database_path(worktree))

    def test_wildcards_and_normalized_path_overlap(self):
        self.assertTrue(m.overlaps('dapp/src/*.ts', 'dapp/src/nested/file.ts'))
        self.assertTrue(m.overlaps('*.json', 'contracts/a.move'))
        self.assertFalse(m.overlaps('dapp/', 'dapp-other/file'))
        for path in ('/tmp/a', '../a', 'a/../b', 'a\\b'):
            with self.assertRaises(m.CoordinationError):
                m.scope_path(path)

    def test_worktree_setup_failure_keeps_claim_reserved(self):
        claim = self.claim()
        with self.assertRaises(m.CoordinationError):
            m.prepare_worktree(self.c, ROOT, claim, requested=self.tmp.name)
        self.assertEqual(self.c.row('SF-001')['token'], claim['token'])

    def test_failed_github_mirror_is_retryable_without_losing_claim(self):
        claim = self.c.claim('Codex', 'test-model', 'session', 'SF-001')
        class GhFailure:
            def run(self, *args, **kwargs):
                raise RuntimeError('network unavailable')
        with self.assertRaises(RuntimeError):
            self.c.publish('SF-001', {'project': {'number': 1, 'owner': {'login': 'test'}}}, GhFailure())
        row = self.c.view(self.c.row('SF-001'))
        self.assertEqual(row['token'], claim['token'])
        self.assertTrue(row['publish_pending'])
        with self.assertRaisesRegex(m.CoordinationError, 'mirror pending'):
            self.c.assert_owner('SF-001', claim['token'])


if __name__ == '__main__':
    unittest.main()
