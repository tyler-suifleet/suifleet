"""SF-148: testnet statuses must have bounded repair ownership, not disappear."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_ci_repairs as repairs
import test_coordinator as previous
from test_coordinator import c, fixture
import dispatcher

BASE = 'b' * 40
RUN = 'https://github.com/owner/repo/actions/runs/77'


def status(state='failure', base=BASE, classification=None, url=RUN):
    return dict(context='testnet/package-upgrade', state=state, target_url=url,
                description='base:' + base + ((' class:' + classification) if classification else ''),
                created_at='2026-10-24T22:00:00Z', updated_at='2026-10-24T22:05:00Z')


class TestnetStatusRepairs(unittest.TestCase):
    def snapshot(self, **fields):
        return dict(statusCheckRollup=[], baseRefOid=BASE, _testnet_status=status(**fields))

    def test_current_base_failure_includes_run_and_classification(self):
        for classification in (None, 'source', 'infrastructure', 'funding', 'uncertain'):
            with self.subTest(classification=classification):
                failures = c.failed_checks(self.snapshot(classification=classification))
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]['detailsUrl'], RUN)
                self.assertEqual(failures[0]['classification'], classification or 'source')

    def test_pending_cancelled_stale_and_malformed_evidence_does_not_launch(self):
        for fields in ({'state':'pending'}, {'state':'error'}, {'state':'success'},
                       {'base':'c'*40}, {'url':None}, {'url':'https://example.test/run'},
                       {'url':RUN+'/../../pulls'}, {'classification':'source trailing'}):
            with self.subTest(**fields):
                self.assertEqual(c.failed_checks(self.snapshot(**fields)), [])

    def test_every_failure_class_reserves_one_repair_across_scans_and_restart(self):
        for classification in ('source', 'infrastructure', 'funding', 'uncertain'):
            with self.subTest(classification=classification), tempfile.TemporaryDirectory() as directory:
                engine, project, pr, owner = repairs.CIRepairs().setup(directory)
                pr['statusCheckRollup'] = fixture()[0]['statusCheckRollup']
                pr.update(_testnet_required=True, _testnet_status=status(classification=classification))
                launched = []
                def launch(project, current, owner, kind):
                    self.assertEqual(kind, 'fix')
                    job = engine.store.reserve(project['id'], current, owner['agent'], kind, Path(directory)/'repair')
                    if job is not None: launched.append(job)
                with patch.object(engine, 'launch', side_effect=launch), patch.object(engine, 'merge') as merge:
                    engine.tick(True)
                    engine.tick(True)
                    merge.assert_not_called()
                self.assertEqual(len(launched), 1)
                observation = engine.store.db.execute('SELECT failures,responsibility FROM ci_observations').fetchone()
                self.assertEqual(json.loads(observation[0])[0]['classification'], classification)
                self.assertIn('coordinator CI repair', observation[1])
                engine.store.db.execute("UPDATE jobs SET status='done' WHERE kind='fix'")
                engine = c.Coordinator(engine.config, Path(directory))
                previous.CheckFreshnessTests().configure_scan(engine, pr, owner)
                with patch.object(engine, 'launch', side_effect=launch): engine.tick(True)
                self.assertEqual(len(launched), 1)
                pr['_testnet_status'] = status(state='pending')
                with patch.object(engine, 'launch') as launch_again:
                    engine.tick(True)
                    launch_again.assert_not_called()
                self.assertEqual(engine.store.db.execute('SELECT COUNT(*) FROM ci_observations').fetchone()[0], 0)

    def test_failed_testnet_blocks_approval_and_success_requests_final_review(self):
        with tempfile.TemporaryDirectory() as directory:
            pr, owner, approval = fixture()
            pr.update(_testnet_required=True, _testnet_status=status())
            approval['feedback'] = c.feedback(pr)
            self.assertIsNotNone(c.gate(pr, owner, approval))
            engine, project, owner = previous.CheckFreshnessTests().setup_scan(directory, pr)
            pending_fingerprint = c.feedback(pr)
            pr['_testnet_status'] = status(state='success')
            self.assertNotEqual(c.feedback(pr), pending_fingerprint)
            with patch.object(engine, 'launch') as launch, patch.object(engine, 'merge') as merge:
                engine.tick(True)
                self.assertEqual(launch.call_args.args[-1], 'review')
                merge.assert_not_called()

    def test_dispatcher_revalidation_reads_status_for_exact_head(self):
        project = dict(id='test', root='/unused', repository='owner/repo', github_account='acct')
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[:3] == ['gh', 'auth', 'token']: return 'private-token'
            if args[:3] == ['gh', 'pr', 'view']:
                return json.dumps(dict(number=7, headRefOid='a'*40, headRefName='work/x', baseRefName='main', statusCheckRollup=[]))
            if args[-1].endswith('/status'): return json.dumps({'statuses':[status()]})
            return json.dumps({'object':{'sha':BASE}})
        worker = dispatcher.Dispatcher.__new__(dispatcher.Dispatcher)
        with patch.object(dispatcher, 'run', side_effect=run):
            pr = worker.fresh_pr(project, 7)
        self.assertEqual(calls[-1][-1], 'repos/owner/repo/commits/'+'a'*40+'/status')
        self.assertEqual(c.failed_checks(pr)[0]['detailsUrl'], RUN)
