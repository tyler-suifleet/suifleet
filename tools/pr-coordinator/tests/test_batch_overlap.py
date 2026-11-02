"""Real Git candidates: overlap work without overlapping publication authority."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from test_batch_integration import Fixture
from coordinator import feedback


class Overlap(unittest.TestCase):
    def fixture(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        f = Fixture(Path(tmp.name)); self.addCleanup(f.db.close)
        f.project['integration_batch'].update(max_members=1, max_in_flight=2)
        return f

    def test_reviews_start_during_ci_and_second_candidate_is_prepared(self):
        f = self.fixture(); f.tick()
        self.assertEqual(len(f.launched), 1)
        first = f.row()
        self.assertEqual(first['status'], 'awaiting_ci')
        f.tick()
        active = [r for r in f.controller.rows(f.project) if r['status'] == 'awaiting_ci']
        self.assertEqual(len(active), 2)
        self.assertEqual(len(f.launched), 2)
        self.assertEqual(len(f.candidate_prs), 2)
        self.assertEqual({m['pr'] for r in active for m in json.loads(r['members'])}, {1, 2})
        for _ in range(2): f.tick()
        self.assertEqual(len(f.launched), 2)  # Same pending snapshot is not reviewed again.
        self.assertEqual(f.git('rev-parse', 'main', cwd=f.remote), f.base)
        self.assertFalse(f.reconciled)

    def test_later_ready_candidate_is_not_blocked_by_earlier_pending_ci(self):
        f = self.fixture(); f.tick(); first = f.row(); f.tick(); second = f.row()
        f.green(); f.approve(); f.tick()
        self.assertEqual(f.git('rev-parse', 'main', cwd=f.remote), second['head'])
        self.assertEqual(f.reconciled, [2])
        self.assertNotEqual(first['head'], second['head'])
        f.tick()
        stale = next(r for r in f.controller.rows(f.project) if r['id'] == first['id'])
        self.assertEqual(stale['status'], 'invalidated')
        self.assertFalse(f.candidate_prs[second['pr']]['_testnet_status']['state'] == 'pending')

    def test_two_ready_candidates_publish_once_and_reject_stale_base(self):
        f=self.fixture(); f.tick(); first=f.row(); f.tick(); second=f.row()
        for row in (first,second):
            pr=f.candidate_prs[row['pr']]
            pr['statusCheckRollup']=[dict(__typename='CheckRun',name='check',workflowName='CI',status='COMPLETED',conclusion='SUCCESS')]
            pr['_testnet_status']=dict(state='success',description='base:'+row['base'])
            candidate=f.controller.candidate(f.project,row)
            f.approvals[row['pr']]=dict(id=row['pr'],head=row['head'],feedback=feedback(candidate),family='Claude',
                result=dict(verdict='approve',acceptance_complete=True,evidence='Real exact-tree review'))
        f.tick()
        self.assertEqual(f.git('rev-parse','main',cwd=f.remote),first['head'])
        self.assertEqual(f.reconciled,[1])
        self.assertEqual(f.row()['status'],'invalidated')

    def test_failed_candidate_does_not_block_ready_sibling(self):
        f=self.fixture(); f.tick(); first=f.row(); f.tick(); second=f.row()
        from test_ci_repairs import failure
        f.candidate_prs[first['pr']]['statusCheckRollup']=[failure()]
        f.green(); f.approve(); f.tick()
        self.assertEqual(f.git('rev-parse','main',cwd=f.remote),second['head'])
        self.assertEqual(f.reconciled,[2])
        self.assertEqual(f.controller.rows(f.project)[0]['status'],'failed')

    def test_early_approval_never_overrides_pending_ci(self):
        f = self.fixture(); f.tick(); f.approve(); f.tick()
        self.assertEqual(f.git('rev-parse', 'main', cwd=f.remote), f.base)
        self.assertFalse(f.reconciled)

    def test_duplicate_reservations_are_rejected(self):
        f = self.fixture(); f.tick(); first = f.row()
        columns = list(first); duplicate = dict(first, id='duplicate', branch='integration/batch/duplicate')
        f.db.execute('INSERT INTO integration_batches VALUES('+','.join('?' for _ in columns)+')',
                     tuple(duplicate[k] for k in columns))
        with self.assertRaisesRegex(RuntimeError, 'duplicate active integration member'): f.tick()

    def test_pending_publication_fences_ready_sibling(self):
        f = self.fixture(); f.tick(); first = f.row(); f.tick(); second = f.row()
        f.controller.update(first, status='publish_pending', publish_intent=1)
        f.green(); f.approve(); f.tick()
        self.assertEqual(f.git('rev-parse', 'main', cwd=f.remote), f.base)
        self.assertFalse(f.reconciled)
        self.assertEqual(len(f.candidate_prs), 2)

    def test_complete_ci_requests_fresh_evidence_review(self):
        f = self.fixture(); f.project['integration_batch']['max_in_flight'] = 1; f.tick()
        row=f.row(); candidate=f.controller.candidate(f.project,row)
        f.approvals[row['pr']]=dict(id=1,head=row['head'],feedback=feedback(candidate),family='Claude',
            result=dict(verdict='blocked',acceptance_complete=False,evidence='Source reviewed; CI pending'))
        f.tick(); self.assertEqual(len(f.launched),1)
        f.green(); f.tick(); self.assertEqual(len(f.launched),2)
        self.assertFalse(f.reconciled)

    def test_early_finding_routes_to_owner_before_ci(self):
        f = self.fixture(); f.project['integration_batch']['max_in_flight']=1; f.tick(); row=f.row()
        candidate=f.controller.candidate(f.project,row)
        f.approvals[row['pr']]=dict(id=17,head=row['head'],feedback=feedback(candidate),family='Claude',
            publication_done=1,publication_pending=False,status='done',
            result=dict(verdict='changes_requested',acceptance_complete=False,evidence='Real source defect'))
        f.tick()
        self.assertEqual(f.row()['status'],'failed')
        self.assertIn('_batch_review_failure', f.controller.repair_context(f.project, f.source_prs[1]))
        self.assertFalse(f.reconciled)

    def test_invalid_capacity_fails_closed(self):
        for limit in (0,5,True,'2'):
            f=self.fixture(); f.project['integration_batch']['max_in_flight']=limit
            with self.assertRaises(ValueError): f.tick()
            self.assertEqual(f.candidate_prs,{})
