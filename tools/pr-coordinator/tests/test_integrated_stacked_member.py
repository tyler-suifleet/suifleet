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
        f.remote_state = 'open'
        f.writes = []
        original_pr, original_gh = f.pr, f.gh
        f.mutate = lambda value: value

        def pr(project, number):
            value = original_pr(project, number)
            if number == 1:
                value.update(state=f.remote_state.upper(), baseRefName='previous',
                             baseRefOid=f.base_sha, mergeCommit=None)
            return value

        def gh(project, args):
            if args[:2] == ['api', 'repos/owner/repo/pulls/1']:
                if '--method' in args:
                    self.assertEqual(args[-1], 'state=closed')
                    f.writes.append(args)
                    f.remote_state = 'closed'
                value = dict(number=1, state=f.remote_state, draft=False,
                             head=dict(sha=f.heads[0], ref='work/1', repo=dict(full_name='owner/repo')),
                             base=dict(ref='previous', sha=f.base_sha, repo=dict(full_name='owner/repo')))
                return json.dumps(f.mutate(value))
            return original_gh(project, args)
        f.pr, f.gh = pr, gh
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
