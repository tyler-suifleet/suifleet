import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_stacked_member_recognition as stacked


class HistoricalRecognitionTests(unittest.TestCase):
    def fixture(self, descendant=True):
        helper = stacked.StackedRecognitionTests()
        self.addCleanup(helper.doCleanups)
        f = helper.fixture()
        self.assertEqual(f.owners[1]['status'], 'review')
        f.git('checkout', '-q', 'work/2' if descendant else 'main')
        (f.repo/'new-owner-work').write_text('new work still requires review\n')
        f.git('add', 'new-owner-work'); f.git('commit', '-qm', 'new owner work')
        head = f.git('rev-parse', 'HEAD')
        f.git('push', '-q', 'origin', 'HEAD:work/2', '--force')
        f.source_prs[2]['headRefOid'] = head
        f.remote_change = lambda value: {
            **value, 'head': {**value['head'], 'sha': head, 'ref': 'work/2'}}
        return f

    def test_published_history_finishes_without_completing_or_retargeting_new_work(self):
        f = self.fixture()
        before = copy.deepcopy(f.owners[1])
        f.controller.reconcile(f.project, f.row())
        self.assertEqual(f.row()['status'], 'done')
        self.assertEqual(f.owners[1], before)
        self.assertEqual(f.reconciled, [1])
        self.assertEqual(f.patches, [])
        self.assertEqual(f.pr(f.project, 2)['state'], 'OPEN')
        self.assertIsNone(f.controller.source(f.project, f.pr(f.project, 2), f.owners[1]))
        self.assertNotIn(2, [r[0] for r in f.db.execute('SELECT pr FROM integrations')])
        row = f.db.execute('SELECT receipt FROM batch_historical_members').fetchone()
        receipt = json.loads(row[0])
        self.assertEqual(receipt['published_head'], f.heads[1])
        self.assertEqual(receipt['observed_head'], f.source_prs[2]['headRefOid'])
        self.assertEqual(receipt['manifest'], f.row()['manifest_digest'])
        f.controller.reconcile(f.project, f.row())
        self.assertEqual(f.db.execute('SELECT COUNT(*) FROM batch_historical_members').fetchone()[0], 1)

    def test_active_owner_is_preserved(self):
        f = self.fixture(); f.owners[1]['status'] = 'in_progress'
        f.controller.reconcile(f.project, f.row())
        self.assertEqual(f.row()['status'], 'done')
        self.assertEqual(f.owners[1]['status'], 'in_progress')
        self.assertEqual(f.patches, [])

    def test_changed_claim_or_missing_approval_retains_batch(self):
        for kind in ('token', 'session', 'model', 'status', 'publish_pending', 'approval'):
            with self.subTest(kind=kind):
                f = self.fixture()
                if kind == 'approval':
                    f.db.execute("UPDATE batch_member_approvals SET status='uncertain' WHERE pr=2")
                else:
                    f.owners[1][kind] = True if kind == 'publish_pending' else 'changed'
                with self.assertRaises(RuntimeError):
                    f.controller.reconcile(f.project, f.row())
                self.assertEqual(f.row()['status'], 'reconciling')
                self.assertEqual(f.db.execute('SELECT COUNT(*) FROM batch_historical_members').fetchone()[0], 0)
                self.assertEqual(f.reconciled, [1])
                self.assertEqual(f.patches, [])

    def test_rewritten_source_without_published_contribution_is_rejected(self):
        f = self.fixture(descendant=False)
        with self.assertRaises(subprocess.CalledProcessError):
            f.controller.reconcile(f.project, f.row())
        self.assertEqual(f.row()['status'], 'reconciling')
        self.assertEqual(f.patches, [])

    def test_remote_changes_and_fork_are_rejected(self):
        for kind in ('head', 'branch', 'fork', 'draft', 'state', 'base'):
            with self.subTest(kind=kind):
                f = self.fixture(); original = f.remote_change
                def change(value):
                    value = original(value)
                    if kind == 'head': value['head']['sha'] = 'f'*40
                    if kind == 'branch': value['head']['ref'] = 'unrelated'
                    if kind == 'fork': value['head']['repo']['full_name'] = 'fork/repo'
                    if kind == 'draft': value['draft'] = True
                    if kind == 'state': value['state'] = 'closed'
                    if kind == 'base': value['base']['ref'] = 'unrelated'
                    return value
                f.remote_change = change
                with self.assertRaisesRegex(RuntimeError, 'remote identity'):
                    f.controller.reconcile(f.project, f.row())
                self.assertEqual(f.patches, [])

    def test_push_during_recognition_is_rejected(self):
        f = self.fixture(); original = f.remote_change; calls = []
        def change(value):
            value = original(value); calls.append(1)
            if len(calls) > 1: value['head']['sha'] = 'f'*40
            return value
        f.remote_change = change
        with self.assertRaisesRegex(RuntimeError, 'changed during historical'):
            f.controller.reconcile(f.project, f.row())
        self.assertEqual(f.db.execute('SELECT COUNT(*) FROM batch_historical_members').fetchone()[0], 0)
        self.assertEqual(f.patches, [])

    def test_unverified_candidate_or_publication_cannot_record_history(self):
        for kind in ('candidate', 'manifest', 'publish_intent', 'review_id'):
            with self.subTest(kind=kind):
                f = self.fixture(); batch = dict(f.row())
                if kind == 'candidate':
                    original = f.pr
                    def unmerged(project, number):
                        value = original(project, number)
                        if number == batch['pr']: value['state'] = 'OPEN'
                        return value
                    f.pr = unmerged
                    f.controller.reconcile(f.project, batch)
                else:
                    batch[kind if kind != 'manifest' else 'manifest_digest'] = None
                    with self.assertRaises(RuntimeError):
                        f.controller.reconcile(f.project, batch)
                self.assertEqual(f.db.execute('SELECT COUNT(*) FROM batch_historical_members').fetchone()[0], 0)
                self.assertEqual(f.patches, [])


if __name__ == '__main__':
    unittest.main()
