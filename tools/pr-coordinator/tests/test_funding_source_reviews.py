"""Funding-only source scheduling never grants combined acceptance."""
import json
from pathlib import Path
import tempfile
import unittest

import test_streaming_source_reviews as streaming
from test_streaming_source_reviews import fixture
import coordinator as c
from test_batch_integration import Fixture
from test_ci_repairs import failure


def funding(pr, classification='funding'):
    pr['_testnet_required'] = True
    pr['_testnet_status'] = dict(state='failure', description='base:'+pr['baseRefOid']+
        ((' class:'+classification) if classification else ''),
        target_url='https://github.com/owner/repo/actions/runs/77')


class FundingSourceReviews(unittest.TestCase):
    def test_start_source_review_classification_and_mixed_failures(self):
        for classification, software, expected in [('funding',False,True),
                ('funding',True,False),(None,False,False),('source',False,False),
                ('infrastructure',False,False),('uncertain',False,False)]:
            with self.subTest(classification=classification,software=software), tempfile.TemporaryDirectory() as temp:
                engine=streaming.StreamingSourceReviews().engine(temp);pr,owner=fixture()
                funding(pr,classification)
                if software: pr['statusCheckRollup']=[failure()]
                self.assertEqual(engine.start_source_review(
                    dict(id='p',integration_batch={'enabled':True}),pr,[owner]),expected)
                self.assertTrue(c.failed_checks(pr))  # repair evidence is retained

    def test_pending_source_checks_and_disclosure(self):
        with tempfile.TemporaryDirectory() as temp:
            engine=streaming.StreamingSourceReviews().engine(temp);pr,owner=fixture();funding(pr)
            pr['statusCheckRollup']=[dict(__typename='CheckRun',name='test',status='IN_PROGRESS')]
            project=dict(id='p',integration_batch={'enabled':True})
            self.assertTrue(engine.start_source_review(project,pr,[owner]))
            prompt=engine.prompt(project,pr,owner,'review','Claude',Path(temp),Path(temp)/'result.json')
            self.assertIn('disclose the missing funded acceptance',prompt)
            self.assertIn('This is NOT merge approval',prompt)

    def test_active_repair_prevents_real_review_reservation(self):
        for state in ['reserved','running','orphaned','quarantined','quota_wait','quota_launch']:
            with self.subTest(state=state),tempfile.TemporaryDirectory() as temp:
                engine=streaming.StreamingSourceReviews().engine(temp);pr,_=fixture();funding(pr)
                job=engine.store.reserve('p',pr,'Codex','fix',Path(temp)/'repair')
                engine.store.db.execute('UPDATE jobs SET status=? WHERE id=?',(state,job))
                self.assertIsNone(engine.store.reserve('p',pr,'Claude','review',Path(temp)/'review'))

    def test_collection_preserves_receipts_ownership_and_active_repairs(self):
        with tempfile.TemporaryDirectory() as temp:
            f=Fixture(Path(temp));self.addCleanup(f.db.close)
            pr=f.source_prs[1];owner=f.owners[0];funding(pr)
            self.assertIsNotNone(f.controller.source(f.project,pr,owner))
            pr['statusCheckRollup']=[failure()]
            self.assertIsNone(f.controller.source(f.project,pr,owner))
            pr['statusCheckRollup']=[];funding(pr,None)
            self.assertIsNone(f.controller.source(f.project,pr,owner))
            funding(pr);f.receipts[1]['source_feedback']='changed'
            self.assertIsNone(f.controller.source(f.project,pr,owner))
            f.receipts[1]['source_feedback']=c.source_feedback(pr)
            for state in ['reserved','running','orphaned','quarantined','quota_wait','quota_launch']:
                f.db.execute('INSERT INTO jobs VALUES(?,?,?,?)',(f.project['id'],pr['number'],'fix',state))
                self.assertIsNone(f.controller.source(f.project,pr,owner))
                f.db.execute('DELETE FROM jobs')
            owner['publish_pending']=True
            self.assertIsNone(f.controller.source(f.project,pr,owner))

    def test_funding_sources_collect_but_candidate_requires_funded_success(self):
        with tempfile.TemporaryDirectory() as temp:
            f=Fixture(Path(temp));self.addCleanup(f.db.close)
            for pr in f.source_prs.values():funding(pr)
            f.tick();row=f.row()
            self.assertEqual(len(json.loads(row['members'])),2)
            f.green();candidate=f.candidate_prs[row['pr']];funding(candidate)
            f.tick()
            self.assertEqual(f.row()['status'],'failed')
            self.assertFalse(f.reconciled)
            self.assertFalse(f.approval_posts)
            # Funding failures remain real acceptance failures, with replay fencing.
            self.assertTrue(c.failed_checks(candidate))
            members=json.loads(row['members'])
            self.assertNotEqual(f.controller.select(f.project,members,row['base'],2),members)


if __name__=='__main__':unittest.main()
