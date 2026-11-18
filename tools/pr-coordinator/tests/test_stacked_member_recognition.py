import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_batch_integration import Fixture


class StackedRecognitionTests(unittest.TestCase):
    def fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Fixture(Path(tmp.name))
        original_pr, original_gh = f.pr, f.gh
        f.retargeted = False
        f.patches = []
        f.remote_change = lambda value: value

        def pr(project, number):
            value = original_pr(project, number)
            if number == 2 and not f.retargeted:
                value.update(state='OPEN', baseRefName='work/1', mergeCommit=None)
            return value

        def gh(project, args):
            if args[:2] == ['api', 'repos/owner/repo/pulls/2']:
                if '--method' in args:
                    f.patches.append(args)
                    f.retargeted = True
                    return '{}'
                value = dict(number=2, state='open', draft=False,
                             head=dict(sha=f.heads[1], repo=dict(full_name='owner/repo')),
                             base=dict(ref='work/1', repo=dict(full_name='owner/repo')))
                return json.dumps(f.remote_change(value))
            return original_gh(project, args)

        # Publication API reads must keep their original fixture behavior.
        f.pr = pr
        f.tick(); f.green(); f.tick(); f.approve()
        with patch.object(f.controller, 'recognize_stacked_member', return_value=False):
            f.tick()
        self.assertEqual(f.row()['status'], 'reconciling')
        self.assertEqual(f.reconciled, [1])
        f.gh = gh
        return f

    def test_verified_published_member_retargets_once_and_reconciles(self):
        f = self.fixture()
        f.controller.reconcile(f.project, f.row())
        self.assertEqual(f.row()['status'], 'done')
        self.assertEqual(f.reconciled, [1, 2])
        self.assertEqual(f.patches, [['api', 'repos/owner/repo/pulls/2', '--method', 'PATCH', '-f', 'base=main']])
        f.controller.reconcile(f.project, f.row())
        self.assertEqual(len(f.patches), 1)

    def test_changed_remote_identity_fails_without_write(self):
        changes = [lambda x: {**x, 'number': 3},
                   lambda x: {**x, 'draft': True},
                   lambda x: {**x, 'state': 'closed'},
                   lambda x: {**x, 'head': {**x['head'], 'sha': 'f'*40}},
                   lambda x: {**x, 'head': {**x['head'], 'repo': dict(full_name='fork/repo')}},
                   lambda x: {**x, 'base': {**x['base'], 'repo': dict(full_name='fork/repo')}},
                   lambda x: {**x, 'base': {**x['base'], 'ref': 'unrelated'}}]
        for change in changes:
            with self.subTest(change=change):
                f = self.fixture(); f.remote_change = change
                with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                    f.controller.reconcile(f.project, f.row())
                self.assertEqual(f.patches, [])
                self.assertEqual(f.reconciled, [1])

    def test_missing_published_approval_or_changed_claim_fails(self):
        for kind in ('approval', 'claim'):
            with self.subTest(kind=kind):
                f = self.fixture()
                if kind == 'approval':
                    f.db.execute("UPDATE batch_member_approvals SET status='uncertain' WHERE pr=2")
                else:
                    f.owners[1]['token'] = 'replacement'
                with self.assertRaises(RuntimeError):
                    f.controller.reconcile(f.project, f.row())
                self.assertEqual(f.patches, [])

    def test_missing_publication_intent_or_review_does_not_write(self):
        for field in ('publish_intent', 'review_id'):
            with self.subTest(field=field):
                f = self.fixture(); batch = dict(f.row()); batch[field] = None
                f.controller.reconcile(f.project, batch)
                self.assertEqual(f.patches, [])
                self.assertEqual(f.row()['status'], 'reconciling')

    def test_candidate_not_merged_or_manifest_tampered_does_not_write(self):
        for kind in ('candidate', 'manifest'):
            with self.subTest(kind=kind):
                f = self.fixture(); batch = dict(f.row())
                if kind == 'candidate':
                    old = f.pr
                    def unmerged(project, number):
                        value = old(project, number)
                        if number == batch['pr']: value['state'] = 'OPEN'
                        return value
                    f.pr = unmerged
                    f.controller.reconcile(f.project, batch)
                else:
                    batch['manifest_digest'] = 'invalid'
                    with self.assertRaisesRegex(RuntimeError, 'manifest'):
                        f.controller.reconcile(f.project, batch)
                self.assertEqual(f.patches, [])


if __name__ == '__main__':
    unittest.main()
