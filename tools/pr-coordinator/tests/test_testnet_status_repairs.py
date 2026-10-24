"""Automatic testnet commit-status failures reach bounded repair (SF-146)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_ci_repairs as repairs
from test_coordinator import c
import dispatcher as d

BASE = 'b' * 40  # fixture()'s baseRefOid
RUN = 'https://github.com/owner/repo/actions/runs/77'


def status(state='failure', base=BASE, cls='source', url=RUN):
    return dict(context=c.TESTNET_CONTEXT, state=state, target_url=url,
                description=f'base:{base}' + (f' class:{cls}' if cls else ''),
                created_at='2026-10-24T13:00:00Z', updated_at='2026-10-24T13:05:00Z')


class TestnetStatusRepairs(unittest.TestCase):
    def snapshot(self, **fields):
        return {'statusCheckRollup': [], 'baseRefOid': BASE, '_testnet_status': status(**fields)}

    def test_exact_head_current_base_failure_with_durable_run_is_a_failure(self):
        failures = c.failed_checks(self.snapshot())
        self.assertEqual(failures, [{'name': 'testnet/package-upgrade', 'workflowName': 'Automatic testnet',
                                     'conclusion': 'FAILURE', 'detailsUrl': RUN,
                                     'startedAt': '2026-10-24T13:00:00Z', 'completedAt': '2026-10-24T13:05:00Z',
                                     'classification': 'source'}])
        self.assertEqual(c.repairable(failures), failures)
        # Statuses written before classes existed are source failures.
        self.assertEqual(c.failed_checks(self.snapshot(cls=None))[0]['classification'], 'source')

    def test_pending_cancelled_stale_and_unlinked_statuses_never_schedule(self):
        for fields in ({'state': 'pending', 'cls': None}, {'state': 'error', 'cls': None},
                       {'state': 'success', 'cls': None}, {'base': 'c' * 40}, {'url': None},
                       {'url': 'https://example.test/actions/runs/77'},
                       {'url': RUN + '/../../pulls'}):
            with self.subTest(**fields):
                self.assertEqual(c.failed_checks(self.snapshot(**fields)), [])
        self.assertEqual(c.failed_checks({'statusCheckRollup': [], 'baseRefOid': BASE}), [])
        stale = self.snapshot()
        stale['_testnet_status']['description'] = 'base:' + BASE + ' class:source trailing'
        self.assertEqual(c.failed_checks(stale), [])

    def test_transient_classes_stay_visible_without_source_repair(self):
        owner = {'status': 'review', 'agent': 'Claude', 'model': 'm', 'expired': False}
        for cls in c.TRANSIENT_CI:
            with self.subTest(cls=cls):
                failures = c.failed_checks(self.snapshot(cls=cls))
                self.assertEqual(len(failures), 1)
                self.assertEqual(c.repairable(failures), [])
                self.assertIn('no source repair launched', c.ci_responsibility({'state': 'OPEN'}, owner, failures))
        mixed = c.failed_checks({**self.snapshot(cls='funding'), 'statusCheckRollup': [repairs.failure()]})
        self.assertEqual(len(mixed), 2)
        self.assertEqual(c.repairable(mixed), [row for row in mixed if row['name'] != c.TESTNET_CONTEXT])
        self.assertIn('coordinator CI repair', c.ci_responsibility({'state': 'OPEN'}, owner, mixed))

    def scan(self, directory, cls):
        engine, project, pr, owner = repairs.CIRepairs().setup(directory)
        pr['statusCheckRollup'] = [{'__typename': 'CheckRun', 'name': 'check', 'status': 'COMPLETED',
                                    'conclusion': 'SUCCESS'}]
        pr.update(_testnet_required=True, _testnet_status=status(cls=cls))
        launched = []

        def launch(project, current, owner, kind):
            if kind == 'fix' and current.get('_ci_repair'):
                job = engine.store.reserve(project['id'], current, 'Codex', 'fix', Path('unused'))
                if job:
                    launched.append(current['_ci_repair'])
        with patch.object(engine, 'launch', side_effect=launch), patch.object(engine, 'merge') as merge:
            engine.tick(True)
            engine.tick(True)
            merge.assert_not_called()  # A failed exact-head testnet status still blocks merge.
        observed = engine.store.db.execute('SELECT failures,responsibility FROM ci_observations').fetchall()
        return engine, pr, launched, observed

    def test_scan_launches_one_bounded_source_repair_with_run_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, pr, launched, observed = self.scan(directory, 'source')
            self.assertEqual(len(launched), 1)  # Deduplicated across ticks by the repair receipt.
            self.assertEqual(launched[0][0]['detailsUrl'], RUN)
            self.assertEqual(len(observed), 1)
            self.assertEqual(c.checks_blocker(pr), 'exact-head testnet status for current base is missing or unsuccessful')
            # A new rerun reports pending first: the observation clears and nothing relaunches.
            pr['_testnet_status'] = status(state='pending', cls=None)
            with patch.object(engine, 'launch') as launch:
                engine.tick(True)
                launch.assert_not_called()
            self.assertEqual(engine.store.db.execute('SELECT COUNT(*) FROM ci_observations').fetchone()[0], 0)

    def test_scan_records_transient_failure_without_launching_a_worker(self):
        for cls in c.TRANSIENT_CI:
            with self.subTest(cls=cls), tempfile.TemporaryDirectory() as directory:
                _, _, launched, observed = self.scan(directory, cls)
                self.assertEqual(launched, [])
                self.assertEqual(len(observed), 1)
                self.assertEqual(json.loads(observed[0][0])[0]['classification'], cls)
                self.assertIn('no source repair launched', observed[0][1])

    def test_dispatcher_fresh_read_includes_exact_head_testnet_status(self):
        config = {'projects': [{'id': 'test', 'root': '/unused', 'repository': 'owner/repo', 'github_account': 'acct'}]}
        project = config['projects'][0]
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if args[:3] == ['gh', 'auth', 'token']:
                return 'token\n'
            if args[:3] == ['gh', 'pr', 'view']:
                return json.dumps({'number': 7, 'headRefOid': 'a' * 40, 'headRefName': 'work/x', 'baseRefName': 'main',
                                   'state': 'OPEN', 'isDraft': False, 'isCrossRepository': False, 'statusCheckRollup': []})
            if args[-1].endswith('/status'):
                return json.dumps({'statuses': [status(), {**status(cls='funding'), 'context': 'other'}]})
            return json.dumps({'object': {'sha': BASE}})
        with tempfile.TemporaryDirectory() as directory:
            worker = d.Dispatcher.__new__(d.Dispatcher)
            with patch.object(d, 'run', side_effect=run):
                pr = worker.fresh_pr(project, 7)
        self.assertEqual(calls[-1], ['gh', 'api', 'repos/owner/repo/commits/' + 'a' * 40 + '/status'])
        self.assertEqual(c.failed_checks(pr)[0]['classification'], 'source')

    def test_dispatcher_redirects_active_owner_only_for_repairable_failures(self):
        worker = d.Dispatcher.__new__(d.Dispatcher)

        class NoDatabase:
            def execute(self, *args):
                raise AssertionError('transient-only failures must not create redirects')
        worker.db = NoDatabase()
        observation = {'failures': json.dumps(c.failed_checks(self.snapshot(cls='uncertain'))),
                       'fingerprint': 'f', 'pr': 1, 'head': 'a' * 40, 'base': BASE}
        worker.detect_redirect({'id': 'test'}, {'session': 's', 'family': 'Claude'},
                               {'token': 't', 'key': 'SF-1', 'branch': 'b'}, observation)


if __name__ == '__main__':
    unittest.main()
