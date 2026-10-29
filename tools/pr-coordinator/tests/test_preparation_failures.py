"""Authenticated preparation ERROR routing; no network or wallet access."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

import test_coordinator as previous
from test_coordinator import c, fixture
from test_ci_redirects import d


class PreparationFailures(unittest.TestCase):
    def setUp(self):
        self.pr = fixture()[0]
        self.pr.update(number=7, baseRefName='main', _testnet_required=True)
        self.prefix = 'repos/tyler-suifleet/suifleet'
        self.path = self.prefix + '/actions/runs/42'
        self.status = dict(id=90, state='error', context=c.TESTNET_CONTEXT,
                           description='base:' + self.pr['baseRefOid'],
                           target_url='https://github.com/tyler-suifleet/suifleet/actions/runs/42',
                           creator={'login': 'github-actions[bot]'})
        self.pr['_testnet_status'] = self.status
        self.run = dict(id=42, run_attempt=2, head_sha='c' * 40, head_branch='main',
                        event='workflow_run', path='.github/workflows/testnet.yml',
                        head_repository={'full_name': 'tyler-suifleet/suifleet'},
                        repository={'full_name': 'tyler-suifleet/suifleet'},
                        status='completed', conclusion='failure')
        self.jobs = [dict(id=i, name=name, run_id=42, head_sha='c' * 40,
                          status='completed', conclusion=conclusion, run_attempt=2)
                     for i, (name, conclusion) in enumerate(
                         [('plan', 'success'), ('prepare', 'failure'), ('preparation-status', 'success')], 1)]
        self.jobs[1]['steps'] = [dict(name='Checkout authenticated test revision', conclusion='failure')]
        self.receipt = dict(status=90, head=self.pr['headRefOid'], base=self.pr['baseRefOid'],
                            run=42, attempt=2, controller='c' * 40)
        self.data = {
            self.path: self.run,
            self.path + '/attempts/2/jobs?per_page=100': dict(total_count=3, jobs=self.jobs),
            self.prefix + '/compare/' + 'c' * 40 + '...' + self.pr['baseRefOid']:
                {'merge_base_commit': {'sha': 'c' * 40}},
            self.prefix + '/commits/' + self.pr['headRefOid'] + '/status': {'statuses': [self.status]},
            self.prefix + '/pulls/7': {'head': {'sha': self.pr['headRefOid']}},
            self.prefix + '/git/ref/heads/main': {'object': {'sha': self.pr['baseRefOid']}},
        }

    def classify(self, read=None, log=None):
        c.annotate_preparation_failure(
            self.pr, 'tyler-suifleet/suifleet', read or (lambda path: copy.deepcopy(self.data[path])),
            lambda path: log if log is not None else
            '2026-10-29T10:00:00.000Z SUIFLEET_PREPARATION_V1 ' + json.dumps(self.receipt) + '\n')
        return c.failed_checks(self.pr)

    def test_actual_error_shape_routes_to_existing_checkout_owner(self):
        failures = self.classify()
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['classification'], 'preparation')
        self.assertEqual(failures[0]['repair_owner'], 'SF-204')
        self.assertIn('SF-204', c.ci_responsibility(self.pr, None, failures))
        self.assertIsNotNone(c.checks_blocker(self.pr))
        self.assertEqual(self.status['state'], 'error')
        self.assertNotIn('SUIFLEET_PREPARATION_V1', json.dumps(self.pr))

    def test_nonshared_failure_uses_normal_bounded_check_budget(self):
        self.jobs[1]['steps'][0]['name'] = 'Build tools'
        failures = self.classify()
        self.assertNotIn('repair_owner', failures[0])
        self.assertEqual(c.ci_check_keys({**self.pr, '_ci_repair': failures}),
                         [json.dumps(['Automatic testnet', c.TESTNET_CONTEXT], separators=(',', ':'))])

    def test_reject_status_and_producer_mutations(self):
        for key, value in [('state', 'pending'), ('state', 'success'), ('description', 'base:' + 'f'*40),
                           ('target_url', 'https://github.com/foreign/repo/actions/runs/42'),
                           ('target_url', 'https://github.com/tyler-suifleet/suifleet/actions/runs/42/attempts/1'),
                           ('creator', {'login': 'attacker'})]:
            with self.subTest(key=key, value=value):
                self.setUp()
                self.status[key] = value
                self.assertEqual(self.classify(), [])

    def test_reject_run_and_attempt_mutations(self):
        for key, value in [('event', 'pull_request'), ('head_branch', 'feature'),
                           ('path', '.github/workflows/evil.yml'), ('status', 'in_progress'),
                           ('conclusion', 'cancelled'), ('run_attempt', 3),
                           ('head_repository', {'full_name': 'evil/repo'}),
                           ('head_sha', 'e'*40)]:
            with self.subTest(key=key):
                self.setUp()
                self.run[key] = value
                self.assertEqual(self.classify(), [])

    def test_reject_jobs_and_missing_failure(self):
        for index, field, value in [(0, 'conclusion', 'failure'), (1, 'conclusion', 'cancelled'),
                                    (2, 'conclusion', 'failure'), (2, 'run_id', 7),
                                    (2, 'head_sha', 'f'*40), (2, 'run_attempt', 1), (1, 'steps', [])]:
            with self.subTest(index=index, field=field):
                self.setUp()
                self.jobs[index][field] = value
                self.assertEqual(self.classify(), [])

    def test_reject_forged_or_historical_receipts(self):
        for field in self.receipt:
            with self.subTest(field=field):
                self.setUp()
                self.receipt[field] = 'wrong'
                self.assertEqual(self.classify(), [])
        for log in ['', 'SUIFLEET_PREPARATION_V1 {}\n', 'x'* (1024*1024+1)]:
            self.assertEqual(self.classify(log=log), [])
        self.setUp()
        line = 'SUIFLEET_PREPARATION_V1 ' + json.dumps(self.receipt) + '\n'
        self.assertEqual(self.classify(log=line*2), [])

    def test_reject_stale_base_head_status_and_rerun_races(self):
        for path, mutation in [
            (self.prefix + '/pulls/7', {'head': {'sha': 'f'*40}}),
            (self.prefix + '/git/ref/heads/main', {'object': {'sha': 'f'*40}}),
            (self.prefix + '/commits/' + self.pr['headRefOid'] + '/status', {'statuses': []})]:
            with self.subTest(path=path):
                self.setUp()
                self.data[path] = mutation
                self.assertEqual(self.classify(), [])
        self.setUp()
        calls = 0
        def read(path):
            nonlocal calls
            row = copy.deepcopy(self.data[path])
            if path == self.path:
                calls += 1
                if calls == 2:
                    row['run_attempt'] += 1
            return row
        self.assertEqual(self.classify(read=read), [])

    def test_unavailable_evidence_fails_closed(self):
        for unavailable in list(self.data):
            with self.subTest(path=unavailable):
                self.setUp()
                def read(path):
                    if path == unavailable:
                        raise RuntimeError('private diagnostic')
                    return copy.deepcopy(self.data[path])
                self.assertEqual(self.classify(read=read), [])

    def test_shared_observation_never_redirects_product_worker(self):
        failures = self.classify()
        dispatcher = d.Dispatcher.__new__(d.Dispatcher)
        # A shared failure is ignored before any process/lease/budget operation.
        with tempfile.TemporaryDirectory() as directory:
            import sqlite3
            dispatcher.db = sqlite3.connect(':memory:')
            dispatcher.db.executescript('CREATE TABLE ci_redirects(claim TEXT, fingerprint TEXT, state TEXT);')
            dispatcher.detect_redirect(
                {'id': 'suifleet'}, {}, {'token': 'local-fixture'},
                {'fingerprint': 'f', 'failures': json.dumps(failures)})
            self.assertEqual(dispatcher.db.execute('SELECT count(*) FROM ci_redirects').fetchone()[0], 0)

    def test_dispatcher_fresh_reads_share_classifier(self):
        self.pr.update(headRefName='work/x')
        def run(args, **kwargs):
            if args[:3] == ['gh', 'auth', 'token']:
                return 'fixture-private'
            if args[:3] == ['gh', 'pr', 'view']:
                return json.dumps(self.pr)
            if args[-1].endswith('/logs'):
                return 'SUIFLEET_PREPARATION_V1 ' + json.dumps(self.receipt) + '\n'
            return json.dumps(self.data[args[-1]])
        worker = d.Dispatcher.__new__(d.Dispatcher)
        with patch.object(d, 'run', side_effect=run):
            fresh = worker.fresh_pr(dict(repository='tyler-suifleet/suifleet', github_account='acct', root='/unused'), 7)
        self.assertEqual(c.failed_checks(fresh)[0]['repair_owner'], 'SF-204')

    def test_all_21_prs_observed_without_21_repairs_or_budget_consumption(self):
        self.classify()
        with tempfile.TemporaryDirectory() as directory:
            engine, project, owner = previous.CheckFreshnessTests().setup_scan(directory, self.pr)
            # Existing independent review is retained; no additional source feedback.
            rows = [{**copy.deepcopy(self.pr), 'number': number} for number in range(1, 22)]
            engine.prs = lambda project: rows
            review = engine.store.review(project['id'], self.pr['number'])
            with patch.object(engine.store, 'review', return_value=review), \
                    patch.object(engine, 'launch') as launch, patch.object(engine, 'merge') as merge:
                engine.tick(True)
                launch.assert_not_called()
                merge.assert_not_called()
            observations = engine.store.db.execute('SELECT * FROM ci_observations').fetchall()
            self.assertEqual(len(observations), 21)
            self.assertTrue(all('SF-204' in row['responsibility'] for row in observations))
            self.assertEqual(engine.store.db.execute('SELECT count(*) FROM ci_check_budget').fetchone()[0], 0)

    def test_other_preparation_failure_dispatches_existing_bounded_owner_repair(self):
        self.jobs[1]['steps'][0]['name'] = 'Build tools'
        self.classify()
        with tempfile.TemporaryDirectory() as directory:
            engine, project, owner = previous.CheckFreshnessTests().setup_scan(directory, self.pr)
            owner.update(model='gpt-6.1-sol', expired=False)
            with patch.object(engine, 'launch') as launch, patch.object(engine, 'merge') as merge:
                engine.tick(True)
                self.assertEqual(launch.call_args.args[-1], 'fix')
                merge.assert_not_called()
            self.assertTrue(engine.store.db.execute('SELECT 1 FROM ci_observations').fetchone())

    def test_coordinator_pr_reader_uses_same_authenticated_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, project, owner = previous.CheckFreshnessTests().setup_scan(directory, self.pr)
            def gh(project, args):
                if args[0] == 'pr':
                    return json.dumps(self.pr)
                if args[1] == 'graphql':
                    return json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {
                        'nodes': [], 'pageInfo': {'hasNextPage': False}}}}}})
                path = args[1]
                if path.endswith('/logs'):
                    return 'SUIFLEET_PREPARATION_V1 ' + json.dumps(self.receipt) + '\n'
                if path in self.data:
                    return json.dumps(self.data[path])
                if '/compare/' in path:
                    return json.dumps({'merge_base_commit': {'sha': self.pr['baseRefOid']}})
                if '/git/trees/' in path:
                    return json.dumps({'tree': [{'path': '.github/workflows/testnet.yml'}]})
                return '[[]]'
            with patch.object(engine, 'gh', side_effect=gh):
                fresh = c.Coordinator.pr(engine, {**project, 'repository': 'tyler-suifleet/suifleet',
                                                  'github_account': 'acct'}, 7)
            self.assertEqual(c.failed_checks(fresh)[0]['repair_owner'], 'SF-204')
            fresh['baseRefOid'] = 'f'*40
            self.assertEqual(c.failed_checks(fresh), [])
