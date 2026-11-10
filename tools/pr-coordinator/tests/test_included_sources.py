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
        def evidence(project, source, base):
            pr = self.f.source_prs[1]
            if (not pr['statusCheckRollup'] or any(c.get('conclusion') != 'SUCCESS' for c in pr['statusCheckRollup'])
                    or pr['_testnet_status'].get('description') != 'base:'+base):
                raise RuntimeError('main evidence unavailable; rerun CI once')
            return dict(source=source, head=base, base=self.f.base, effects_sha256='a'*64)
        self.evidence = patch.object(self.f.controller, 'included_evidence', side_effect=evidence)
        self.evidence.start()
        self.addCleanup(self.evidence.stop)
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
        self.assertEqual(len(f.launched), 0)
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

    def test_source_only_review_cannot_be_promoted_and_blocked_review_is_bounded(self):
        f = self.f
        pr = self.prepare_acceptance()
        f.launched.clear()
        f._launch_keys.clear()
        old = f.approvals[1]
        old.update(feedback=coordinator.feedback(pr))
        old['result'].update(verdict='source_complete', acceptance_complete=False)
        self.assertTrue(f.controller.included_source(f.project, pr, f.owners[0]))
        self.assertEqual(len(f.launched), 1)
        snapshot = f.launched[-1][0]
        old.update(feedback=coordinator.feedback(snapshot))
        old['result'].update(verdict='blocked', acceptance_complete=False)
        for _ in range(3):
            self.assertTrue(f.controller.included_source(f.project, pr, f.owners[0]))
        self.assertEqual(len(f.launched), 1)
        self.assertEqual(f.events, [])

    def test_changed_claim_invalidates_independent_acceptance(self):
        f = self.f
        pr = self.prepare_acceptance()
        f.owners[0]['token'] = 'replacement-private-claim'
        self.assertTrue(f.controller.included_source(f.project, pr, f.owners[0]))
        self.assertEqual(f.owners[0]['status'], 'review')
        self.assertEqual(f.events, [])
        self.assertEqual(len(f.launched), 2)
        self.assertNotEqual(f.launched[0][0]['_included_source']['claim_hash'],
                            f.launched[1][0]['_included_source']['claim_hash'])

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
        self.assertEqual(f.launched, [])
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
                with patch.object(f.controller, 'included_evidence', side_effect=RuntimeError('failed main evidence')):
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
        # A transient evidence outage must retain publication, not silently mark
        # an unposted approval delivered. Changed claims are fenced here too.
        for changed_claim in (True, False):
            current = copy.deepcopy(f.owners[0])
            if changed_claim: current['token'] = 'replacement-claim'
            with patch.object(engine, 'pr', return_value=pr), \
                 patch.object(engine, 'ticket', return_value=json.dumps(current)), \
                 patch.object(engine, 'assert_review_owner'), \
                 patch.object(engine.batches, 'main', return_value=f.heads[0]), \
                 patch.object(engine.batches, 'included_evidence', side_effect=RuntimeError('temporary outage')):
                engine.publish_results(f.project)
            self.assertTrue(engine.store.review('test', 1)['publication_pending'])
            engine.store.db.execute('UPDATE jobs SET retry_after=NULL WHERE id=?', (job,))
        with patch.object(engine, 'ticket', return_value=json.dumps(f.owners[0])), \
             patch.object(engine, 'assert_review_owner'), \
             patch.object(engine.batches, 'included_evidence', side_effect=f.controller.included_evidence), \
             patch.object(engine, 'pr', return_value=pr), \
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


class IncludedMainEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = Fixture(Path(temp.name))
        f = self.f
        f.git('checkout', '-q', 'work/1')
        (f.repo/'integrated-later').write_text('current main includes source plus later work\n')
        f.git('add', 'integrated-later'); f.git('commit', '-qm', 'later integrated work')
        self.base = f.git('rev-parse', 'HEAD')
        f.git('push', '-q', 'origin', self.base+':refs/heads/main')
        self.ci = dict(id=123, run_attempt=1, head_sha=self.base, head_branch='main',
                       event='push', name='CI', path='.github/workflows/ci.yml',
                       repository=dict(full_name='owner/repo'), head_repository=dict(full_name='owner/repo'),
                       status='completed', conclusion='success')
        self.testnet = dict(self.ci, id=456, event='workflow_run', name='Automatic testnet',
                            path='.github/workflows/testnet.yml')
        self.status = dict(context='testnet/package-upgrade', state='success',
                           description='base:'+f.heads[0], target_url='https://github.com/owner/repo/actions/runs/456')
        names = ['check', 'Security dependencies, secrets and configuration'] + [
            'Security CodeQL ('+lang+')' for lang in ('javascript-typescript', 'python', 'rust', 'actions')]
        self.jobs = dict(total_count=len(names), jobs=[dict(name=n, head_sha=self.base,
            status='completed', conclusion='success') for n in names])
        self.effects = dict(success=True, head=self.base, base=f.heads[0], chain='4c78adac')
        self.reads = []
        def gh(project, args):
            if args[:2] == ['run', 'download']:
                Path(args[-1], 'package-upgrade.json').write_text(json.dumps(self.effects))
                return ''
            path = args[1].split('repos/owner/repo/')[1]
            self.reads.append(path)
            if path == 'commits/'+self.base:
                value = dict(sha=self.base, parents=[dict(sha=f.heads[0])])
            elif path.endswith('/status'): value = dict(statuses=[self.status])
            elif path.startswith('actions/workflows/'): value = dict(workflow_runs=[self.ci])
            elif '/jobs?' in path: value = self.jobs
            elif path == 'actions/runs/123': value = self.ci
            elif path == 'actions/runs/456': value = self.testnet
            else: raise AssertionError(path)
            return json.dumps(value)
        self.gh = patch.object(f, 'gh', side_effect=gh)
        self.gh.start(); self.addCleanup(self.gh.stop)

    def proof(self):
        return self.f.controller.included_evidence(self.f.project, self.f.heads[0], self.base)

    def test_main_parent_pair_preserves_original_source_identity(self):
        proof = self.proof()
        self.assertEqual(proof['head'], self.base)
        self.assertEqual(proof['base'], self.f.heads[0])
        self.assertEqual(proof['ci'], 123)
        self.assertEqual(proof['testnet'], 456)
        source = self.f.source_prs[1]
        source['statusCheckRollup'] = [dict(__typename='CheckRun', name='check', status='COMPLETED', conclusion='SUCCESS')]
        source['_testnet_status']['description'] = 'base:'+('b'*40)
        snapshot = self.f.controller.included_snapshot(self.f.project, source, self.base, dict(head=source['headRefOid'], comparison=self.f.base, base_ref='main'))
        self.assertIsNone(coordinator.checks_blocker(snapshot))
        self.assertEqual(snapshot['_testnet_status'], source['_testnet_status'])
        self.assertNotEqual(coordinator.feedback(snapshot), coordinator.feedback(source))
        changed = copy.deepcopy(snapshot)
        changed['_included_evidence']['testnet_attempt'] += 1
        self.assertNotEqual(coordinator.feedback(changed), coordinator.feedback(snapshot))
        changed['statusCheckRollup'][0]['conclusion'] = 'FAILURE'
        self.assertEqual(coordinator.checks_blocker(changed), 'checks have not all passed')
        snapshot['statusCheckRollup'].append(dict(__typename='StatusContext',
            context='testnet/package-upgrade', state='FAILURE'))
        self.assertIsNone(coordinator.checks_blocker(snapshot))

    def test_failed_stale_unknown_and_mismatched_producers_rejected(self):
        for target, key, value in [
            ('ci', 'conclusion', 'failure'), ('ci', 'head_sha', 'a'*40),
            ('ci', 'event', 'pull_request'), ('ci', 'run_attempt', 0),
            ('testnet', 'repository', dict(full_name='outsider/repo')),
            ('testnet', 'path', '.github/workflows/untrusted.yml'),
            ('testnet', 'status', 'in_progress'), ('testnet', 'head_sha', 'b'*40),
            ('status', 'state', 'failure'), ('status', 'description', 'base:'+self.base),
            ('status', 'target_url', 'https://example.com/actions/runs/456'),
            ('effects', 'head', 'c'*40), ('effects', 'base', 'c'*40),
            ('effects', 'success', False), ('effects', 'chain', 'mainnet')]:
            with self.subTest(target=target, key=key):
                obj = getattr(self, target)
                original = copy.deepcopy(obj)
                obj[key] = value
                with self.assertRaises(RuntimeError): self.proof()
                obj.clear(); obj.update(original)

    def test_main_movement_and_retained_proof_cannot_authorize_acceptance(self):
        with patch.object(self.f.controller, 'main', return_value='f'*40):
            with self.assertRaisesRegex(RuntimeError, 'changed while'): self.proof()
        source = self.f.source_prs[1]
        source.update(_included_evidence=dict(source=source['headRefOid'], head=self.base,
                                             effects_sha256='old'))
        self.status['state'] = 'failure'
        snapshot = self.f.controller.included_snapshot(self.f.project, source, self.base, {})
        self.assertNotIn('_included_evidence', snapshot)
        self.assertIsNotNone(coordinator.checks_blocker(snapshot))

    def test_missing_or_failed_security_job_is_not_ci_success(self):
        self.jobs['jobs'][0]['conclusion'] = 'skipped'
        with self.assertRaisesRegex(RuntimeError, 'CI/security'): self.proof()
        self.jobs['jobs'].pop(0)
        self.jobs['total_count'] -= 1
        with self.assertRaisesRegex(RuntimeError, 'CI/security'): self.proof()

    def test_mutable_run_changes_during_authentication_fail_closed(self):
        original = self.f.gh
        count = 0
        def gh(project, args):
            nonlocal count
            result = original(project, args)
            if args[1].endswith('actions/runs/456'):
                count += 1
                if count == 2:
                    return json.dumps(dict(self.testnet, run_attempt=2))
            return result
        with patch.object(self.f, 'gh', side_effect=gh):
            with self.assertRaisesRegex(RuntimeError, 'changed while'): self.proof()

    def test_missing_evidence_has_bounded_action_and_no_review_launch(self):
        self.status['state'] = 'pending'
        snapshot = self.f.controller.included_snapshot(self.f.project, self.f.source_prs[1], self.base, {})
        self.assertIn('rerun current-main CI once', coordinator.checks_blocker(snapshot))
        self.assertEqual(self.f.launched, [])
