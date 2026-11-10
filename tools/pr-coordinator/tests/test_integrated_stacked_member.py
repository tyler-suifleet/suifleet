import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_batch_integration import Fixture


class IntegratedStackedMemberTests(unittest.TestCase):
    def fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Fixture(Path(tmp.name))
        f.tick(); f.green(); f.tick(); f.approve(); f.tick()
        self.assertEqual(f.row()['status'], 'done')
        f.owners[0]['status'] = 'review'
        f.reconciled.clear()
        f.git('push', '-q', 'origin', f.base+':refs/heads/previous')
        f.base_sha = f.base
        f.comparison_base_sha = f.base
        f.remote_state = 'open'
        f.writes = []
        original_pr, original_gh = f.pr, f.gh
        f.mutate = lambda value: value

        def pr(project, number):
            value = original_pr(project, number)
            if number == 1:
                value.update(state=f.remote_state.upper(), baseRefName='previous',
                             baseRefOid=f.base_sha, comparisonBaseOid=f.comparison_base_sha, mergeCommit=None)
            return value

        def gh(project, args):
            if args[:2] == ['api', 'repos/owner/repo/git/ref/heads/previous']:
                return json.dumps(dict(ref='refs/heads/previous',
                    object=dict(type='commit', sha=f.base_sha)))
            if args[:2] == ['api', 'repos/owner/repo/pulls/1']:
                if '--method' in args:
                    self.assertEqual(args[-1], 'state=closed')
                    f.writes.append(args)
                    f.remote_state = 'closed'
                value = dict(number=1, state=f.remote_state, draft=False,
                             head=dict(sha=f.heads[0], ref='work/1', repo=dict(full_name='owner/repo')),
                             base=dict(ref='previous', sha=f.comparison_base_sha, repo=dict(full_name='owner/repo')))
                return json.dumps(f.mutate(value))
            return original_gh(project, args)
        f.pr, f.gh = pr, gh
        original_reconcile = f.reconcile

        def reconcile(project, candidate, owner, review):
            original_reconcile(project, candidate, owner, review)
            # The real ticket CLI clears the token on completion, and owner()
            # re-reads persisted state rather than the object passed above.
            for persisted in f.owners:
                if persisted['key'] == owner['key']:
                    persisted.update(status='done', token=None)
        f.reconcile = reconcile
        return f

    def test_first_member_completes_via_real_candidate_and_closes_source(self):
        f = self.fixture()
        candidate = f.row()['pr']
        seen = []
        original = f.reconcile
        def reconcile(project, pr, owner, review):
            seen.append(copy.deepcopy(pr))
            original(project, pr, owner, review)
            f.owners[0]['status'] = owner['status']
        f.reconcile = reconcile
        f.controller.reconcile(f.project, f.row())
        self.assertEqual(f.row()['status'], 'done')
        self.assertEqual(seen[0]['number'], candidate)
        self.assertEqual(seen[0]['state'], 'MERGED')
        self.assertEqual(f.owners[0]['status'], 'done')
        self.assertEqual(f.remote_state, 'closed')
        self.assertEqual(len(f.writes), 1)
        f.controller.reconcile(f.project, f.row())
        self.assertEqual(len(f.writes), 1)

    def test_historical_comparison_base_differs_from_integrated_live_tip(self):
        f = self.fixture()
        f.base_sha = f.heads[1]
        f.git('push', '-q', 'origin', f.base_sha+':refs/heads/previous')
        self.assertNotEqual(f.base_sha, f.comparison_base_sha)
        f.controller.reconcile(f.project, f.row())
        self.assertEqual(f.row()['status'], 'done')
        self.assertEqual(f.remote_state, 'closed')

    def test_live_base_race_before_completion_or_closure_is_rejected(self):
        for boundary in ('fetch', 'completion', 'closure', 'confirmation'):
            with self.subTest(boundary=boundary):
                f = self.fixture()
                original = f.gh
                reads = 0

                def gh(project, args):
                    nonlocal reads
                    value = json.loads(original(project, args))
                    if args[1].endswith('/git/ref/heads/previous'):
                        reads += 1
                        if reads >= dict(fetch=1, completion=2, closure=3, confirmation=4)[boundary]:
                            value['object']['sha'] = f.heads[1]
                    return json.dumps(value)

                f.gh = gh
                with self.assertRaisesRegex(RuntimeError, 'live base changed'):
                    f.controller.reconcile(f.project, f.row())
                self.assertEqual(len(f.writes), int(boundary == 'confirmation'))
                if boundary in ('fetch', 'completion'): self.assertFalse(f.reconciled)

    def test_fetch_race_and_unintegrated_comparison_base_cannot_complete(self):
        for kind in ('fetch', 'comparison'):
            with self.subTest(kind=kind):
                f = self.fixture()
                original = f.controller.git
                if kind == 'comparison':
                    f.comparison_base_sha = f.git('commit-tree', f.base+'^{tree}',
                                                  '-p', f.base, '-m', 'unintegrated')
                else:
                    def git(project, *args):
                        if args == ('rev-parse', 'FETCH_HEAD'): return f.heads[1]
                        return original(project, *args)
                    f.controller.git = git
                with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                    f.controller.reconcile(f.project, f.row())
                self.assertFalse(f.writes)
                self.assertFalse(f.reconciled)

    def test_volatile_repository_metadata_does_not_require_retry(self):
        f = self.fixture()
        reads = 0

        def metadata(value):
            nonlocal reads
            reads += 1
            for side in ('head', 'base'):
                value[side]['repo'].update(
                    open_issues_count=15 - int(f.owners[0]['status'] == 'done')
                    - int(f.remote_state == 'closed'),
                    pushed_at=str(reads), updated_at=str(reads))
            return value

        f.mutate = metadata
        f.controller.reconcile(f.project, f.row())
        self.assertEqual(f.row()['status'], 'done')
        self.assertEqual(f.remote_state, 'closed')
        self.assertEqual(len(f.writes), 1)
        self.assertGreaterEqual(reads, 4)

    def test_identity_changes_during_closure_are_rejected(self):
        for field in ('number', 'draft', 'head_sha', 'head_ref', 'head_repo',
                      'base_sha', 'base_ref', 'base_repo'):
            with self.subTest(field=field):
                f = self.fixture()

                def mutate(value):
                    if f.remote_state == 'closed':
                        if field == 'number': value['number'] = 999
                        elif field == 'draft': value['draft'] = True
                        else:
                            side, key = field.split('_')
                            if key == 'repo': value[side]['repo']['full_name'] = 'fork/repo'
                            else: value[side][key] = 'changed'
                    return value

                f.mutate = mutate
                with self.assertRaisesRegex(RuntimeError, 'closure unconfirmed'):
                    f.controller.reconcile(f.project, f.row())
                self.assertEqual(f.row()['status'], 'reconciling')

    def test_completion_with_cleared_token_retries_closure_failures(self):
        for failure in ('before_closure', 'patch', 'confirmation'):
            with self.subTest(failure=failure):
                f = self.fixture()
                original_gh = f.gh
                completed_reads = 0
                failed = False

                def gh(project, args):
                    nonlocal completed_reads, failed
                    if (args[:2] == ['api', 'repos/owner/repo/pulls/1']
                            and f.owners[0]['status'] == 'done' and not failed):
                        if '--method' in args:
                            if failure == 'patch':
                                failed = True
                                raise RuntimeError('transient closure failure')
                        else:
                            completed_reads += 1
                            if ((failure == 'before_closure' and completed_reads == 1)
                                    or (failure == 'confirmation' and completed_reads == 2)):
                                failed = True
                                raise RuntimeError('transient closure failure')
                    return original_gh(project, args)

                f.gh = gh
                with self.assertRaisesRegex(RuntimeError, 'transient closure failure'):
                    f.controller.reconcile(f.project, f.row())
                self.assertEqual(f.owners[0]['status'], 'done')
                self.assertIsNone(f.owners[0]['token'])
                self.assertEqual(f.row()['status'], 'reconciling')
                f.controller.reconcile(f.project, f.row())
                self.assertEqual(f.row()['status'], 'done')
                self.assertEqual(f.remote_state, 'closed')
                self.assertEqual(len(f.writes), 1)

    def test_completed_owner_retry_rejects_changed_identity_or_live_token(self):
        for field in ('key', 'agent', 'model', 'session', 'branch', 'token'):
            with self.subTest(field=field):
                f = self.fixture()
                f.owners[0].update(status='done', token=None)
                f.owners[0][field] = 'replacement'
                if field == 'key':
                    f.controller.owner = lambda project, member: copy.deepcopy(f.owners[0])
                with self.assertRaises(RuntimeError):
                    f.controller.reconcile(f.project, f.row())
                self.assertFalse(f.writes)
                self.assertFalse(f.reconciled)

    def test_changed_identity_approval_and_claim_cannot_complete(self):
        for kind in ('head', 'base', 'fork', 'approval', 'claim', 'active', 'pending'):
            with self.subTest(kind=kind):
                f = self.fixture()
                if kind == 'head':
                    f.mutate = lambda v: {**v, 'head': {**v['head'], 'sha': 'f'*40}}
                elif kind == 'base':
                    f.mutate = lambda v: {**v, 'base': {**v['base'], 'sha': 'f'*40}}
                elif kind == 'fork':
                    f.mutate = lambda v: {**v, 'base': {**v['base'], 'repo': dict(full_name='fork/repo')}}
                elif kind == 'approval':
                    f.db.execute("UPDATE batch_member_approvals SET status='uncertain' WHERE pr=1")
                elif kind == 'claim': f.owners[0]['token'] = 'replacement'
                elif kind == 'active': f.owners[0]['status'] = 'in_progress'
                else: f.owners[0]['publish_pending'] = True
                with self.assertRaises(RuntimeError):
                    f.controller.reconcile(f.project, f.row())
                self.assertFalse(f.writes)
                self.assertFalse(f.reconciled)

    def test_unintegrated_base_and_unmerged_candidate_cannot_complete(self):
        for kind in ('base', 'candidate'):
            with self.subTest(kind=kind):
                f = self.fixture()
                if kind == 'base':
                    f.base_sha = f.git('commit-tree', f.base+'^{tree}', '-p', f.base, '-m', 'unintegrated')
                    f.git('push', '-q', 'origin', f.base_sha+':refs/heads/previous')
                    with self.assertRaises(subprocess.CalledProcessError):
                        f.controller.reconcile(f.project, f.row())
                else:
                    original = f.pr
                    def pr(project, number):
                        value = original(project, number)
                        if number == f.row()['pr']: value['state'] = 'OPEN'
                        return value
                    f.pr = pr
                    f.controller.reconcile(f.project, f.row())
                self.assertFalse(f.writes)
                self.assertFalse(f.reconciled)

    def test_head_change_during_completion_preserves_new_source(self):
        f = self.fixture()
        original = f.reconcile
        def reconcile(*args):
            original(*args)
            f.mutate = lambda v: {**v, 'head': {**v['head'], 'sha': 'f'*40}}
        f.reconcile = reconcile
        with self.assertRaisesRegex(RuntimeError, 'changed before closure'):
            f.controller.reconcile(f.project, f.row())
        self.assertFalse(f.writes)


if __name__ == '__main__':
    unittest.main()
