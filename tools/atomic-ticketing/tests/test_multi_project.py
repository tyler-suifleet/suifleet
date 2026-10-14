import copy
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'atomic_ticketing.py'
spec = importlib.util.spec_from_file_location('atomic_ticketing_test', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def catalog(repo='owner/repo', paths=None, resources=None):
    return {'schema_version': 1, 'repository': repo, 'issues': [{
        'key': 'ONE', 'title': 'Existing issue', 'priority': 'P0', 'paths': paths or ['src/'],
        'resources': resources or [], 'depends_on': [], 'acceptance': ['Demonstrate behavior'], 'validation': 'Run acceptance tests'
    }]}


def snapshot(config):
    return {i['key']: {'state': 'open', 'status': 'Backlog', 'agent': 'Unassigned', 'assignees': [],
                       'url': 'https://github.com/' + config['repository'] + '/issues/1', 'item_id': config['repository']}
            for i in config['issues']}


def claim_process(path, project, barrier, queue):
    c = m.Coordinator(path, project_id=project)
    try:
        barrier.wait(timeout=20)
        result = c.claim('Codex', 'test-model', project + '-session')
        queue.put((project, True, result['key']))
    except m.CoordinationError:
        queue.put((project, False, None))
    finally:
        c.close()


class MultiProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'central' / 'claims.sqlite3'
        self.a = m.Coordinator(self.path, project_id='suifleet')
        self.b = m.Coordinator(self.path, project_id='vortech')

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.tmp.cleanup()

    def seed(self, a=None, b=None):
        a, b = a or catalog('tyler-suifleet/suifleet'), b or catalog('therealtime/vortech')
        self.a.import_catalog(a, snapshot(a))
        self.b.import_catalog(b, snapshot(b))

    def acknowledge(self, c, key='ONE'):
        c.db.execute('UPDATE tickets SET published_revision=revision WHERE key=?', (c.qualified(key),))

    def test_same_ticket_key_and_path_are_isolated_between_repositories(self):
        self.seed()
        a = self.a.claim('Codex', 'test-model', 'codex')
        b = self.b.claim('Claude', 'test-model', 'claude')
        self.assertEqual(a['key'], b['key'])
        self.assertNotEqual(a['token'], b['token'])
        self.assertEqual(a['spec']['paths'], ['src'])
        self.assertEqual(len(list(self.a.db.execute('SELECT * FROM tickets'))), 2)
        with self.assertRaises(m.CoordinationError):
            self.b.heartbeat('ONE', a['token'])
        with self.assertRaises(m.CoordinationError):
            self.a.row('vortech::ONE')

    def test_shared_hardware_resource_has_one_winner_across_processes(self):
        self.seed(catalog('tyler-suifleet/suifleet', resources=['hardware:shared-board']),
                  catalog('therealtime/vortech', resources=['hardware:shared-board']))
        context = multiprocessing.get_context('fork')
        barrier, queue = context.Barrier(2), context.Queue()
        processes = [context.Process(target=claim_process, args=(str(self.path), project, barrier, queue)) for project in ('suifleet','vortech')]
        for process in processes:
            process.start()
        results = [queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(won for _, won, _ in results), 1)

    def test_resource_expansion_and_review_hold_across_projects(self):
        self.seed()
        a = self.a.claim('Codex', 'test-model', 'codex', resources=['hardware:shared'])
        b = self.b.claim('Claude', 'test-model', 'claude')
        with self.assertRaisesRegex(m.CoordinationError, 'Scope conflicts'):
            self.b.expand('ONE', b['token'], [], ['hardware:shared'])
        self.a.transition('ONE', a['token'], 'review', 'Review evidence')
        with self.assertRaises(m.CoordinationError):
            self.b.expand('ONE', b['token'], [], ['hardware:shared'])

    def test_global_session_cannot_own_two_implementations(self):
        self.seed()
        self.a.claim('Codex', 'test-model', 'same-session')
        with self.assertRaisesRegex(m.CoordinationError, 'already owns'):
            self.b.claim('Codex', 'test-model', 'same-session')

    def test_cross_project_dependency_requires_completed_published_work(self):
        a = catalog('tyler-suifleet/suifleet')
        b = catalog('therealtime/vortech')
        b['issues'][0]['depends_on'] = ['suifleet::ONE']
        self.seed(a, b)
        with self.assertRaisesRegex(m.CoordinationError, 'dependencies incomplete'):
            self.b.claim('Claude', 'test-model', 'claude')
        claim = self.a.claim('Codex', 'test-model', 'codex')
        self.a.transition('ONE', claim['token'], 'review', 'Review evidence')
        self.a.complete('ONE', claim['token'], 'human', 'Integrated acceptance')
        with self.assertRaises(m.CoordinationError):
            self.b.claim('Claude', 'test-model', 'claude')
        self.acknowledge(self.a)
        self.b.claim('Claude', 'test-model', 'claude')

    def test_refresh_one_repository_does_not_modify_other_claims_or_freshness(self):
        self.seed()
        b = self.b.claim('Claude', 'test-model', 'claude')
        config = catalog('tyler-suifleet/suifleet', paths=['docs/'])
        self.a.import_catalog(config, snapshot(config))
        self.assertEqual(self.b.row('ONE')['token'], b['token'])
        self.assertEqual(json.loads(self.b.row('ONE')['spec'])['paths'], ['repositories/vortech/src'])

    def test_project_registration_preserves_ssh_remote_and_rejects_other_clone(self):
        repo = Path(self.tmp.name) / 'repo'
        clone = Path(self.tmp.name) / 'clone'
        for path in (repo, clone):
            path.mkdir()
            subprocess.run(['git','init','-q',str(path)],check=True)
            subprocess.run(['git','-C',str(path),'remote','add','origin','git@personal:therealtime/vortech.git'],check=True)
        config = {'repository':'therealtime/vortech','git_remote':'git@personal:therealtime/vortech.git'}
        self.b.register(repo, config)
        self.assertEqual(m.run_git(repo,'remote','get-url','origin'), config['git_remote'])
        with self.assertRaisesRegex(m.CoordinationError, 'another namespace/clone'):
            self.b.register(clone, config)
        wrong = dict(config, git_remote='git@suifleet:therealtime/vortech.git')
        with self.assertRaisesRegex(m.CoordinationError, 'differs from origin'):
            self.b.register(repo, wrong)

    def test_api_credentials_are_per_command_and_do_not_switch_active_account(self):
        calls = []
        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            if args[1:3] == ['auth','token']:
                return subprocess.CompletedProcess(args,0,'personal-test-token\n','')
            return subprocess.CompletedProcess(args,0,'{}','')
        with patch.object(m.subprocess,'run',side_effect=fake_run), patch.dict(os.environ,{'GH_TOKEN':'wrong-token','GITHUB_TOKEN':'wrong-token'}):
            gh = m.Gh('therealtime')
            gh.run('api','user',as_json=True)
            self.assertEqual(os.environ['GH_TOKEN'],'wrong-token')
        self.assertNotIn('GH_TOKEN',calls[0][1]['env'])
        self.assertEqual(calls[1][1]['env']['GH_TOKEN'],'personal-test-token')
        self.assertFalse(any('switch' in args for args, _ in calls))

    def test_installed_cli_runs_without_repository_imports_and_preserves_other_commands(self):
        prefix = Path(self.tmp.name) / 'installation'
        installer = SOURCE.with_name('install.py')
        result = subprocess.run([m.sys.executable, str(installer), '--prefix', str(prefix)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        launcher = prefix / 'bin/ticket'
        result = subprocess.run([str(launcher), '--version'], cwd=self.tmp.name, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), m.VERSION)
        launcher.unlink()
        launcher.write_text('unrelated command')
        result = subprocess.run([m.sys.executable, str(installer), '--prefix', str(prefix)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(launcher.read_text(), 'unrelated command')

    def test_resource_assertion_requires_reserved_resource(self):
        self.seed()
        claim = self.a.claim('Codex', 'test-model', 'codex', resources=['hardware:shared'])
        self.acknowledge(self.a)
        self.a.assert_owner('ONE', claim['token'], resources=['hardware:shared'])
        with self.assertRaises(m.CoordinationError):
            self.a.assert_owner('ONE', claim['token'], resources=['hardware:another'])

    def test_missing_api_account_fails_without_falling_back_or_exposing_token(self):
        with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'sensitive-output','missing')):
            with self.assertRaises(m.CoordinationError) as error:
                m.Gh('therealtime')
        self.assertNotIn('sensitive-output',str(error.exception))

    def test_custom_database_does_not_chmod_existing_parent_directory(self):
        parent = Path(self.tmp.name) / 'shared-parent'
        parent.mkdir(mode=0o755)
        c = m.Coordinator(parent / 'custom.sqlite3', project_id='third')
        try:
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(c.path.stat().st_mode & 0o777, 0o600)
        finally:
            c.close()

    def test_github_catalog_reserves_entire_repository_without_explicit_scope(self):
        class FakeGh:
            def run(self,*args,**kwargs):
                return {'totalCount':1,'items':[{'content':{'url':'https://github.com/therealtime/vortech/issues/1'},'priority':'P0'}]}
            def api(self,*args,**kwargs):
                return [{'number':1,'id':1,'html_url':'https://github.com/therealtime/vortech/issues/1','title':'Fix audio','body':'Acceptance evidence','labels':[]}]
        config = {'repository':'therealtime/vortech','github':{'owner':'therealtime'}}
        catalog, state = m.github_catalog(config,FakeGh(),{'number':1})
        self.assertEqual(catalog['issues'][0]['paths'],['.'])
        self.assertEqual(catalog['issues'][0]['key'],'GH-1')
        config['ticket_overrides'] = {'GH-1':{'paths':['fpga/hdl/']}}
        catalog, _ = m.github_catalog(config,FakeGh(),{'number':1})
        self.assertEqual(catalog['issues'][0]['paths'],['fpga/hdl/'])

    def test_legacy_migration_preserves_active_token_and_retires_old_database(self):
        legacy_path = Path(self.tmp.name) / 'legacy.sqlite3'
        legacy = sqlite3.connect(legacy_path)
        legacy.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE tickets (
                key TEXT PRIMARY KEY, spec TEXT NOT NULL, remote TEXT NOT NULL,
                status TEXT NOT NULL, token TEXT, agent TEXT, model TEXT, session TEXT,
                heartbeat REAL, expires REAL, worktree TEXT, branch TEXT, evidence TEXT,
                revision INTEGER NOT NULL, published_revision INTEGER NOT NULL
            );
        """)
        config = catalog('tyler-suifleet/suifleet')
        claim = {'token': m.uuid.uuid4().hex}
        legacy.executemany('INSERT INTO metadata VALUES (?,?)', [('schema','1'),('repository',config['repository']),('snapshot_at',str(m.time.time()))])
        legacy.execute('INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            'ONE', json.dumps(config['issues'][0]), json.dumps(snapshot(config)['ONE']),
            'in_progress', claim['token'], 'Codex', 'test-model', 'legacy-worker',
            m.time.time(), m.time.time()+900, '/tmp/retained-worktree', 'work/retained', 'retained evidence', 2, 1))
        legacy.commit()
        legacy.close()
        self.a.legacy_path = legacy_path
        with self.assertRaisesRegex(m.CoordinationError, 'not retired'):
            self.a.claim('Claude','test-model','replacement')
        result = m.migrate_legacy(self.a,legacy_path)
        self.assertTrue(result['legacy_retired'])
        self.assertEqual(self.a.row('ONE')['token'],claim['token'])
        self.assertEqual(json.loads(self.a.row('ONE')['spec'])['paths'],['repositories/suifleet/src'])
        retired = sqlite3.connect(legacy_path)
        self.assertNotEqual(retired.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0], '1')
        retired.close()
        self.assertEqual(self.a.row('ONE')['worktree'], '/tmp/retained-worktree')
        self.assertEqual(self.a.row('ONE')['evidence'], 'retained evidence')
        self.a.ensure_legacy_retired()
        self.assertTrue(m.migrate_legacy(self.a,legacy_path)['already_migrated'])
        self.a.db.execute('DELETE FROM metadata WHERE key=?', (self.a.meta_key('legacy_imported'),))
        with self.assertRaises(m.CoordinationError):
            self.a.ensure_legacy_retired()
        with self.assertRaisesRegex(m.CoordinationError, 'Central migration record missing'):
            m.migrate_legacy(self.a,legacy_path)


if __name__ == '__main__':
    unittest.main()
