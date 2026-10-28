"""API-authenticated cancellation classification, entirely offline."""
import copy
import io
import json
import subprocess
import unittest
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch

import test_coordinator as previous
from test_coordinator import c, fixture
from test_ci_redirects import d as dispatcher
from test_ci_repairs import failure


class JobTimeouts(unittest.TestCase):
    def setup_evidence(self):
        self.pr = fixture()[0]
        self.check = failure()
        self.check.update(name='check', conclusion='CANCELLED')
        self.pr['statusCheckRollup'] = [self.check]
        self.prefix = 'repos/owner/repo'
        self.run_path = self.prefix + '/actions/runs/1'
        self.run = dict(id=1, head_sha=self.pr['headRefOid'], run_attempt=1,
                        status='completed', conclusion='cancelled', check_suite_id=3,
                        head_repository={'full_name': 'owner/repo'})
        self.job = dict(id=1, run_id=1, head_sha=self.pr['headRefOid'], name='check',
                        started_at=self.check['startedAt'], completed_at=self.check['completedAt'],
                        status='completed', conclusion='cancelled')
        self.verified = dict(id=1, head_sha=self.pr['headRefOid'], app={'slug': 'github-actions'},
                             check_suite={'id': 3}, name='check', status='completed',
                             conclusion='cancelled', details_url=self.check['detailsUrl'],
                             output={'annotations_count': 1})
        self.annotation = dict(annotation_level='failure',
                               message='The job has exceeded the maximum execution time of 1h0m0s')
        self.data = {
            self.run_path: self.run,
            self.run_path + '/attempts/1/jobs?per_page=100': {'total_count': 1, 'jobs': [self.job]},
            self.prefix + '/check-runs/1': self.verified,
            self.prefix + '/check-runs/1/annotations?per_page=100': [self.annotation]}
        return self.pr

    def classify(self, reader=None):
        c.annotate_job_timeouts(self.pr, 'owner/repo', reader or (lambda path: copy.deepcopy(self.data[path])))
        return c.failed_checks(self.pr)

    def test_verified_timeout_preserves_blocking_and_sanitizes_evidence(self):
        self.setup_evidence()
        failures = self.classify()
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['conclusion'], 'TIMED_OUT')
        self.assertEqual(failures[0]['classification'], 'job_timeout')
        self.assertEqual(self.check['conclusion'], 'CANCELLED')
        self.assertIsNotNone(c.checks_blocker(self.pr))
        self.assertNotIn('message', json.dumps(self.pr))
        # Same failure key/budget as an ordinary failure, never a new allowance.
        repair = {**self.pr, '_ci_repair': failures}
        keys = c.ci_check_keys(repair)
        self.check['conclusion'] = 'FAILURE'
        self.assertEqual(keys, c.ci_check_keys({**self.pr, '_ci_repair': c.failed_checks(self.pr)}))

    def test_timeout_in_workflow_with_another_failed_job(self):
        self.setup_evidence()
        self.run['conclusion'] = 'failure'
        self.assertTrue(self.classify())

    def test_dispatcher_fresh_reads_use_same_authenticated_classifier(self):
        self.setup_evidence()
        self.pr.update(number=7, headRefName='work/x', baseRefName='main')
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            if args[:3] == ['gh', 'auth', 'token']:
                return 'private-token'
            if args[:3] == ['gh', 'pr', 'view']:
                return json.dumps(self.pr)
            if '/git/ref/' in args[-1]:
                return json.dumps({'object': {'sha': self.pr['baseRefOid']}})
            if args[-1].endswith('/status'):
                return json.dumps({'statuses': []})
            return json.dumps(self.data[args[-1]])
        project = dict(repository='owner/repo', github_account='acct', root='/unused')
        worker = dispatcher.Dispatcher.__new__(dispatcher.Dispatcher)
        with patch.object(dispatcher, 'run', side_effect=run):
            fresh = worker.fresh_pr(project, 7)
        self.assertEqual(c.failed_checks(fresh)[0]['classification'], 'job_timeout')
        for args, options in calls[1:]:
            self.assertEqual(options['env']['GH_TOKEN'], 'private-token')

    def test_coordinator_snapshot_enriches_and_dispatches_through_existing_gates(self):
        self.setup_evidence()
        with tempfile.TemporaryDirectory() as directory:
            engine, project, owner = previous.CheckFreshnessTests().setup_scan(directory, self.pr)
            owner.update(model='gpt-6.1-sol', expired=False)
            # Exercise the real PR reader; only GitHub transport is a fixture.
            def gh(project, args):
                if args[0] == 'pr':
                    return json.dumps({**self.pr, 'baseRefName': 'main'})
                if args[1] == 'graphql':
                    return json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {
                        'nodes': [], 'pageInfo': {'hasNextPage': False}}}}}})
                path = args[1]
                if '/git/ref/' in path:
                    return json.dumps({'object': {'sha': self.pr['baseRefOid']}})
                if '/compare/' in path:
                    return json.dumps({'merge_base_commit': {'sha': self.pr['baseRefOid']}})
                if '/git/trees/' in path:
                    return json.dumps({'tree': []})
                if path in self.data:
                    return json.dumps(self.data[path])
                return '[[]]'
            engine.immutable_reads = {}
            with patch.object(engine, 'gh', side_effect=gh):
                fresh = c.Coordinator.pr(engine, {**project, 'repository': 'owner/repo',
                                                   'github_account': 'acct'}, 7)
            self.assertEqual(c.failed_checks(fresh)[0]['classification'], 'job_timeout')
            previous.CheckFreshnessTests().configure_scan(engine, fresh, owner)
            with patch.object(engine, 'launch') as launch, patch.object(engine, 'merge') as merge:
                engine.tick(True)
                self.assertEqual(launch.call_args.args[-1], 'fix')
                merge.assert_not_called()
            # Base/head movement invalidates proof before a new reservation.
            fresh['baseRefOid'] = 'c'*40
            self.assertEqual(c.failed_checks(fresh), [])

    def test_intentional_cancellation_and_untrusted_or_incomplete_evidence(self):
        changes = [
            ('annotation', 'message', 'The operation was canceled.'),
            ('annotation', 'message', 'quoted: The job has exceeded the maximum execution time of 1h0m0s'),
            ('annotation', 'annotation_level', 'warning'),
            ('run', 'head_sha', 'c'*40), ('run', 'id', 9),
            ('run', 'status', 'in_progress'), ('run', 'run_attempt', 2),
            ('run', 'head_repository', {'full_name': 'fork/repo'}),
            ('job', 'id', 9), ('job', 'run_id', 9), ('job', 'head_sha', 'c'*40),
            ('job', 'name', 'other'), ('job', 'completed_at', 'later'),
            ('verified', 'head_sha', 'c'*40), ('verified', 'app', {'slug': 'other'}),
            ('verified', 'check_suite', {'id': 4}), ('verified', 'details_url', 'other'),
            ('verified', 'output', {'annotations_count': 2}),
            ('verified', 'output', {'annotations_count': 101}),
        ]
        for target, key, value in changes:
            with self.subTest(target=target, key=key):
                self.setup_evidence()
                getattr(self, target)[key] = value
                self.assertEqual(self.classify(), [])
        self.setup_evidence()
        self.data[self.run_path + '/attempts/1/jobs?per_page=100']['total_count'] = 101
        self.assertEqual(self.classify(), [])

    def test_unavailable_and_malformed_reads_never_log_secrets_or_reuse_proof(self):
        for error in (subprocess.CalledProcessError(1, ['secret-argv'], stderr='secret-token'),
                      subprocess.TimeoutExpired(['secret-argv'], 1), ValueError('secret')):
            self.setup_evidence()
            self.assertTrue(self.classify())
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.classify(lambda path: (_ for _ in ()).throw(error)), [])
            self.assertEqual(output.getvalue(), '')
        self.setup_evidence()
        self.assertEqual(self.classify(lambda path: {}), [])

    def test_rerun_race_superseded_and_ambiguous_rollups(self):
        self.setup_evidence()
        reads = []
        def read(path):
            reads.append(path)
            result = copy.deepcopy(self.data[path])
            if path == self.run_path and reads.count(path) == 2:
                result['run_attempt'] = 2
            return result
        self.assertEqual(self.classify(read), [])
        for extra in (
            {**failure(2), 'name': 'check', 'conclusion': 'SUCCESS'},
            {**failure(2), 'name': 'check', 'status': 'QUEUED', 'startedAt': None},
            {**failure(), 'name': 'check', 'conclusion': 'SUCCESS'},
        ):
            self.setup_evidence()
            self.pr['statusCheckRollup'].append(extra)
            self.assertEqual(self.classify(), [])

    def test_head_base_and_url_changes_invalidate_saved_proof(self):
        for key in ('headRefOid', 'baseRefOid'):
            self.setup_evidence()
            self.assertTrue(self.classify())
            self.pr[key] = 'c'*40
            self.assertEqual(c.failed_checks(self.pr), [])
        self.setup_evidence()
        self.assertTrue(self.classify())
        self.check['detailsUrl'] += '0'
        self.assertEqual(c.failed_checks(self.pr), [])

    def test_only_same_repository_job_urls_are_probed(self):
        for url in ('https://github.com/fork/repo/actions/runs/1/job/1',
                    'https://example.com/owner/repo/actions/runs/1/job/1', None):
            self.setup_evidence()
            self.check['detailsUrl'] = url
            with patch('builtins.print') as output:
                self.assertEqual(self.classify(lambda path: self.fail('unexpected read')), [])
                output.assert_not_called()


if __name__ == '__main__':
    unittest.main()
