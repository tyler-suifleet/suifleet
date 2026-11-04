import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import types
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coordinator import HostedRunnerRecovery


class Fixture:
    def __init__(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.store = types.SimpleNamespace(db=self.db)
        self.now = 1000
        self.posts = []
        self.error = None
        self.run = dict(id=100, event='pull_request', path='.github/workflows/ci.yml',
                        head_sha='a'*40, run_attempt=1, status='completed', conclusion='failure',
                        repository=dict(full_name='owner/repo'), head_repository=dict(full_name='owner/repo'),
                        pull_requests=[dict(number=5, head=dict(sha='a'*40))])
        self.job = dict(id=200, run_id=100, run_attempt=1, head_sha='a'*40, name='macOS runtime (launchd)',
                        status='completed', conclusion='cancelled', steps=[], runner_id=0, runner_name='',
                        labels=['macos-15'], check_run_url='https://api.github.com/repos/owner/repo/check-runs/200')
        self.annotations = [dict(message='The job was not acquired by Runner of type hosted even after multiple attempts'),
                            dict(message='Due to capacity constraints, jobs targeting macOS arm64 runners may experience longer queue times.')]
        self.fresh = dict(state='open', draft=False, head=dict(sha='a'*40, repo=dict(full_name='owner/repo')))
        self.pr = dict(number=5, state='OPEN', isDraft=False, isCrossRepository=False, headRefOid='a'*40,
                       statusCheckRollup=[dict(name=self.job['name'], status='COMPLETED', conclusion='CANCELLED',
                                              detailsUrl='https://github.com/owner/repo/actions/runs/100/job/200')])
        self.project = dict(id='test', repository='owner/repo')
        self.recovery = HostedRunnerRecovery(self, clock=lambda: self.now)

    def gh(self, project, args):
        path = args[1]
        if args[-2:] == ['--method', 'POST']:
            state = self.db.execute('SELECT state FROM hosted_runner_retries WHERE job=200').fetchone()[0]
            if state != 'intent':
                raise AssertionError('POST must follow a durable intent')
            self.posts.append(path)
            if self.error:
                raise self.error
            return ''
        if '/actions/runs/' in path:
            return json.dumps(self.run)
        if '/actions/jobs/' in path:
            return json.dumps(self.job)
        if '/annotations?' in path:
            return json.dumps(self.annotations)
        if '/pulls/' in path:
            return json.dumps(self.fresh)
        raise AssertionError(path)

    def tick(self):
        return self.recovery.tick(self.project, self.pr)

    def state(self):
        row = self.db.execute('SELECT state FROM hosted_runner_retries WHERE job=200').fetchone()
        return row[0] if row else None


class HostedRunnerRecoveryTest(unittest.TestCase):
    def test_realistic_capacity_proof_submits_only_one_job(self):
        f = Fixture()
        self.assertEqual(f.tick()['state'], 'retry_submitted')
        self.assertEqual(f.posts, ['repos/owner/repo/actions/jobs/200/rerun'])
        self.assertEqual(f.state(), 'submitted')
        self.assertEqual(f.tick()['state'], 'submitted')
        self.assertEqual(len(f.posts), 1)

    def test_sibling_still_running_is_retried_after_run_finishes(self):
        f = Fixture()
        f.run.update(status='in_progress', conclusion=None)
        self.assertEqual(f.tick()['state'], 'waiting_for_run_completion')
        self.assertFalse(f.posts)
        f.run.update(status='completed', conclusion='failure')
        self.assertEqual(f.tick()['state'], 'waiting')
        f.now += 301
        self.assertEqual(f.tick()['state'], 'retry_submitted')

    def test_uncertain_post_is_never_replayed_after_restart(self):
        f = Fixture()
        f.error = subprocess.TimeoutExpired('gh', 10)
        self.assertEqual(f.tick()['state'], 'uncertain')
        f.recovery = HostedRunnerRecovery(f, clock=lambda: f.now)
        f.now += 3600
        self.assertEqual(f.tick()['state'], 'uncertain')
        self.assertEqual(len(f.posts), 1)
        f.run.update(run_attempt=2, status='in_progress', conclusion=None)
        f.tick()
        self.assertEqual(f.state(), 'observed')
        self.assertEqual(len(f.posts), 1)

    def test_crash_after_intent_without_response_never_replays(self):
        f = Fixture()
        f.tick()
        f.db.execute("UPDATE hosted_runner_retries SET state='intent'")
        f.recovery = HostedRunnerRecovery(f, clock=lambda: f.now)
        self.assertEqual(f.tick()['state'], 'intent')
        self.assertEqual(len(f.posts), 1)

    def test_active_run_race_is_a_definite_refusal_not_unknown_delivery(self):
        f = Fixture()
        f.error = subprocess.CalledProcessError(1, ['gh'], stderr='The workflow run containing this job is already running (HTTP 403)')
        self.assertEqual(f.tick()['state'], 'waiting_for_run_completion')
        self.assertEqual(f.state(), 'waiting')
        f.error = None
        f.now += 301
        self.assertEqual(f.tick()['state'], 'retry_submitted')

    def test_budget_is_persistent_and_not_reset_by_another_run(self):
        f = Fixture()
        for i in range(3):
            f.db.execute('INSERT INTO hosted_runner_retries VALUES(?,?,?,?,?,?,?,?,?,?)',
                         ('test', 5, 'a'*40, i+1, i+1, 1, 'observed', 0, 0, 'proof'))
        self.assertEqual(f.tick()['state'], 'exhausted')
        self.assertFalse(f.posts)
        self.assertEqual(f.state(), 'exhausted')
        self.assertIsNone(f.tick())

    def test_refuses_source_failures_started_jobs_user_cancels_and_wrong_identity(self):
        changes = [
            ('run', 'path', '.github/workflows/testnet.yml'),
            ('run', 'event', 'push'), ('run', 'head_sha', 'b'*40),
            ('run', 'conclusion', 'cancelled'), ('run', 'id', 101),
            ('run', 'head_repository', dict(full_name='attacker/fork')),
            ('run', 'repository', dict(full_name='attacker/fork')), ('run', 'pull_requests', []),
            ('job', 'steps', [dict(name='tests', conclusion='failure')]),
            ('job', 'runner_id', 4), ('job', 'runner_name', 'assigned-host'),
            ('job', 'labels', ['self-hosted']), ('job', 'conclusion', 'failure'),
            ('job', 'run_attempt', 2), ('job', 'run_id', 101),
            ('job', 'head_sha', 'b'*40), ('job', 'check_run_url', 'https://attacker.test/200'),
            ('pr', 'isDraft', True), ('pr', 'isCrossRepository', True), ('pr', 'state', 'CLOSED'),
            ('fresh', 'head', dict(sha='b'*40, repo=dict(full_name='owner/repo'))),
            ('fresh', 'state', 'closed'),
        ]
        for target, key, value in changes:
            with self.subTest(target=target, key=key):
                f = Fixture()
                getattr(f, target)[key] = value
                f.tick()
                self.assertFalse(f.posts)

    def test_generic_cancellation_annotations_are_not_capacity_authority(self):
        for messages in [[], [dict(message='Cancelled by user')],
                         [dict(message='Due to capacity constraints, jobs targeting macOS arm64 runners may experience longer queue times.')]]:
            f = Fixture()
            f.annotations = messages
            f.tick()
            self.assertFalse(f.posts)

    def test_one_broken_pr_does_not_abort_following_recovery(self):
        f = Fixture()
        original = f.recovery.tick
        def tick(project, pr):
            if pr['number'] == 1:
                raise RuntimeError('private transport error')
            return original(project, pr)
        f.recovery.tick = tick
        notices = f.recovery.scan(f.project, [dict(number=1), f.pr])
        self.assertEqual(notices[0]['state'], 'unavailable')
        self.assertNotIn('private', str(notices))
        self.assertEqual(notices[1]['state'], 'retry_submitted')


if __name__ == '__main__':
    unittest.main()
