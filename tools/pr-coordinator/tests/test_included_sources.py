"""Real Git regression for open stacked PRs already included in main."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_batch_integration import Fixture
import coordinator
from batch_integration import LocalInfrastructure


class IncludedSourcesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = Fixture(Path(temp.name))
        self.f.batches = self.f.controller
        for pr in self.f.source_prs.values():
            pr['comparisonBaseOid'] = self.f.base

    def candidates(self):
        f = self.f
        return [f.controller.source(f.project, p, o)
                for p, o in zip(f.source_prs.values(), f.owners)]

    def include_first(self):
        f = self.f
        f.git('push', '-q', 'origin', f.heads[0]+':refs/heads/main')
        return f.heads[0]

    def test_open_included_source_never_creates_empty_batch(self):
        f = self.f
        self.include_first()
        prs = [copy.deepcopy(f.source_prs[1])]  # GitHub still reports OPEN.
        owners = copy.deepcopy(f.owners)
        for _ in range(3):
            self.assertEqual(f.controller.tick(f.project, prs, f.owners, True), {1})
        self.assertEqual(f.controller.rows(f.project), [])
        self.assertEqual(f.owners, owners)
        self.assertEqual(prs[0]['state'], 'OPEN')
        self.assertEqual(f.reconciled, [])
        self.assertEqual(len(f.launched), 1)
        self.assertEqual(f.launched[0][0]['_review_mode'], 'included')
        self.assertEqual(f.events, [])
        receipt = json.loads(f.db.execute('SELECT receipt FROM batch_included_sources').fetchone()[0])
        self.assertFalse(receipt['acceptance_complete'])
        self.assertEqual(receipt['identity']['head'], f.heads[0])
        self.assertEqual(f.db.execute('SELECT count(*) FROM batch_included_sources').fetchone()[0], 1)

    def test_unrelated_source_keeps_progressing(self):
        f = self.f
        self.include_first()
        f.controller.tick(f.project, list(f.source_prs.values()), f.owners, True)
        row = f.row()
        self.assertEqual(row['status'], 'awaiting_ci')
        self.assertEqual([m['pr'] for m in json.loads(row['members'])], [2])
        self.assertEqual(f.reconciled, [])

    def test_new_descendant_is_eligible_despite_old_receipt(self):
        f = self.f
        base = self.include_first()
        candidates = self.candidates()
        self.assertEqual([m['pr'] for m in f.controller.select(f.project, candidates, base, 2)], [2])
        f.git('checkout', '-q', 'work/1')
        (f.repo/'new-work').write_text('still needs acceptance\n')
        f.git('add', 'new-work'); f.git('commit', '-qm', 'new source')
        head = f.git('rev-parse', 'HEAD')
        f.git('push', '-q', 'origin', 'work/1')
        candidates[0]['head'] = head
        self.assertEqual([m['pr'] for m in f.controller.select(f.project, candidates, base, 2)], [1, 2])

    def test_unknown_ancestry_fails_closed(self):
        f = self.f
        original = f.controller.git
        def git(project, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                raise subprocess.CalledProcessError(128, 'git')
            return original(project, *args, **kwargs)
        with patch.object(f.controller, 'git', side_effect=git):
            with self.assertRaises(subprocess.CalledProcessError):
                f.controller.select(f.project, self.candidates(), f.base, 2)
        self.assertEqual(f.controller.rows(f.project), [])
        self.assertEqual(f.reconciled, [])

    def prepare_acceptance(self):
        f = self.f
        self.include_first()
        pr = f.source_prs[1]
        pr['baseRefName'] = 'work/old-parent'
        pr['statusCheckRollup'] = [dict(__typename='CheckRun', name='security',
            workflowName='CI', status='COMPLETED', conclusion='SUCCESS')]
        pr['_testnet_status'] = dict(state='success', description='base:'+f.heads[0])
        with patch.object(f, 'pr', side_effect=lambda project, n: copy.deepcopy(f.source_prs[n])):
            self.assertTrue(f.controller.included_source(f.project, pr, f.owners[0]))
        snapshot = f.launched[0][0]
        f.approvals[1] = dict(id=617, head=pr['headRefOid'],
            feedback=coordinator.feedback(snapshot), family='Claude',
            result=dict(verdict='approve', acceptance_complete=True,
                        model='independent-test', evidence='real check fixtures'))
        return pr

    def test_missing_acceptance_dispatch_is_bounded_and_pins_current_main(self):
        f = self.f
        pr = self.prepare_acceptance()
        snapshot = f.launched[0][0]
        self.assertEqual(snapshot['baseRefOid'], f.heads[0])
        self.assertEqual(snapshot['_included_source']['base_ref'], 'work/old-parent')
        self.assertNotEqual(coordinator.feedback(snapshot), coordinator.feedback(pr))
        self.assertEqual(f.owners[0]['status'], 'review')

    def test_evidence_backed_completion_and_truthful_close(self):
        f = self.f
        pr = self.prepare_acceptance()
        original_ticket = f.ticket
        def ticket(project, command, key, *args):
            if command == 'complete':
                f.owners[0]['status'] = 'done'
                f.owners[0]['token'] = None
                return '{}'
            return original_ticket(project, command, key, *args)
        def gh(project, args):
            f.events.append(args)
            if '/pulls/' in args[1] and '--method' in args:
                f.source_prs[1]['state'] = 'CLOSED'
            return json.dumps(dict(state='closed'))
        with patch.object(f, 'pr', side_effect=lambda project, n: copy.deepcopy(f.source_prs[n])), \
             patch.object(f, 'ticket', side_effect=ticket), \
             patch.object(f, 'gh', side_effect=gh), \
             patch.object(f, 'issue_number', return_value=246, create=True), \
             patch.object(LocalInfrastructure, 'fenced') as fenced:
            self.assertTrue(f.controller.included_source(f.project, pr, f.owners[0]))
        fenced.assert_called_once()
        self.assertEqual(pr['state'], 'CLOSED')
        self.assertNotIn('mergeCommit', pr)
        self.assertEqual(f.owners[0]['status'], 'done')
        receipt = json.loads(f.db.execute('SELECT receipt FROM batch_included_sources').fetchone()[0])
        self.assertTrue(receipt['acceptance_complete'])
        self.assertEqual(receipt['status'], 'included-source-closed')
        self.assertIn('not a GitHub merge', receipt['evidence'])

    def test_stale_failed_pending_or_same_family_acceptance_cannot_complete(self):
        for failure in ('head', 'feedback', 'publication_pending', 'family', 'acceptance'):
            with self.subTest(failure=failure):
                f = self.f
                pr = self.prepare_acceptance()
                review = f.approvals[1]
                if failure == 'head': review['head'] = 'a'*40
                if failure == 'feedback': review['feedback'] = 'stale'
                if failure == 'publication_pending': review['publication_pending'] = True
                if failure == 'family': review['family'] = 'Codex'
                if failure == 'acceptance': review['result']['acceptance_complete'] = False
                self.assertTrue(f.controller.included_source(f.project, pr, f.owners[0]))
                self.assertEqual(f.owners[0]['status'], 'review')
                self.assertEqual(f.events, [])

    def test_changed_head_before_completion_is_preserved(self):
        f = self.f
        pr = self.prepare_acceptance()
        changed = {**pr, 'headRefOid': f.heads[1]}
        with patch.object(f, 'pr', return_value=changed), patch.object(LocalInfrastructure, 'fenced'):
            with self.assertRaisesRegex(RuntimeError, 'changed before completion'):
                f.controller.included_source(f.project, pr, f.owners[0])
        self.assertEqual(f.owners[0]['status'], 'review')
        self.assertEqual(f.events, [])

    def test_worker_fence_failure_preserves_claim(self):
        f = self.f
        pr = self.prepare_acceptance()
        with patch.object(LocalInfrastructure, 'fenced', side_effect=RuntimeError('live worker')):
            with self.assertRaisesRegex(RuntimeError, 'live worker'):
                f.controller.included_source(f.project, pr, f.owners[0])
        self.assertEqual(f.owners[0]['status'], 'review')
        self.assertEqual(f.events, [])

    def test_pr617_shape_dispatches_acceptance_even_without_mergeability(self):
        f = self.f
        self.include_first()
        pr = copy.deepcopy(f.source_prs[1])
        pr.update(number=617, baseRefName='work/suifleet/old-integrated-parent',
                  mergeable='UNKNOWN', baseIncluded=False)
        f.receipts[617] = {**f.receipts[1], 'source_feedback': coordinator.source_feedback(pr)}
        self.assertTrue(f.controller.included_source(f.project, pr, f.owners[0]))
        self.assertEqual(f.launched[0][0]['number'], 617)
        self.assertFalse(json.loads(f.db.execute('SELECT receipt FROM batch_included_sources').fetchone()[0])['acceptance_complete'])

    def test_failed_ci_and_missing_current_base_testnet_never_complete(self):
        f = self.f
        pr = self.prepare_acceptance()
        for field in ('ci', 'testnet'):
            with self.subTest(field=field):
                modified = copy.deepcopy(pr)
                if field == 'ci':
                    modified['statusCheckRollup'][0]['conclusion'] = 'FAILURE'
                else:
                    modified['_testnet_status']['description'] = 'base:'+f.base
                self.assertTrue(f.controller.included_source(f.project, modified, f.owners[0]))
                self.assertEqual(f.owners[0]['status'], 'review')
                self.assertEqual(f.events, [])

    def test_pending_owner_publication_and_changed_identity_preserved(self):
        f = self.f
        pr = self.prepare_acceptance()
        f.owners[0]['publish_pending'] = True
        self.assertFalse(f.controller.included_source(f.project, pr, f.owners[0]))
        f.owners[0]['publish_pending'] = False
        original = copy.deepcopy(f.owners[0])
        f.owners[0]['session'] = 'another-worker'
        with self.assertRaisesRegex(RuntimeError, 'owner identity changed'):
            f.controller.included_source(f.project, pr, original)
        self.assertEqual(f.events, [])

    def test_included_review_publication_uses_pinned_full_context(self):
        f = self.f
        pr = self.prepare_acceptance()
        snapshot = f.launched[0][0]
        engine = coordinator.Coordinator({'workers': {'Codex': ['true'], 'Claude': ['true']},
                                          'projects': []}, f.root/'publication')
        directory = f.root/'published-review'
        directory.mkdir()
        (directory/'snapshot.json').write_text(json.dumps(snapshot))
        job = engine.store.reserve('test', snapshot, 'Claude', 'review', directory)
        engine.store.db.execute("UPDATE jobs SET status='done',result=? WHERE id=?",
                               (json.dumps(f.approvals[1]['result']), job))
        posted = []
        def gh(project, args):
            posted.append(args)
            body = next(v[5:] for v in args if v.startswith('body='))
            return json.dumps(dict(node_id='published-'+str(len(posted)), body=body,
                                   state='APPROVED', commit_id=pr['headRefOid']))
        with patch.object(engine, 'pr', return_value=pr), \
             patch.object(engine, 'gh', side_effect=gh), \
             patch.object(engine.batches, 'main', return_value=f.heads[0]):
            engine.publish_results(f.project)
        self.assertEqual(len(posted), 2)
        self.assertIn('event=APPROVE', posted[1])
        self.assertFalse(engine.store.review('test', 1)['publication_pending'])

    def test_closed_source_retries_issue_closure_without_recompleting(self):
        f = self.f
        pr = self.prepare_acceptance()
        original_ticket = f.ticket
        completions = []
        def ticket(project, command, key, *args):
            if command == 'complete':
                completions.append(key)
                f.owners[0].update(status='done', token=None)
                return '{}'
            return original_ticket(project, command, key, *args)
        issue_attempts = []
        def gh(project, args):
            f.events.append(args)
            if '/pulls/' in args[1]:
                f.source_prs[1]['state'] = 'CLOSED'
            elif '--method' in args:
                issue_attempts.append(args)
                if len(issue_attempts) == 1:
                    raise RuntimeError('remote issue unavailable')
            return json.dumps(dict(state='closed'))
        with patch.object(f, 'pr', side_effect=lambda project, n: copy.deepcopy(f.source_prs[n])), \
             patch.object(f, 'ticket', side_effect=ticket), \
             patch.object(f, 'gh', side_effect=gh), \
             patch.object(f, 'issue_number', return_value=246, create=True), \
             patch.object(LocalInfrastructure, 'fenced'):
            with self.assertRaisesRegex(RuntimeError, 'remote issue unavailable'):
                f.controller.included_source(f.project, pr, f.owners[0])
            receipt = json.loads(f.db.execute('SELECT receipt FROM batch_included_sources').fetchone()[0])
            self.assertEqual(receipt['status'], 'included-source-accepted')
            self.assertEqual(f.controller.tick(f.project, [], f.owners, True), set())
        self.assertEqual(completions, ['T-1'])
        self.assertEqual(len(issue_attempts), 2)
        self.assertEqual(sum('/pulls/' in e[1] for e in f.events), 1)
        receipt = json.loads(f.db.execute('SELECT receipt FROM batch_included_sources').fetchone()[0])
        self.assertEqual(receipt['status'], 'included-source-closed')
