import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coordinator as c


def fixture():
    pr = dict(number=7, headRefOid='a'*40, baseRefOid='b'*40, headRefName='work/source',
              baseRefName='main', state='OPEN', isDraft=False, mergeable='MERGEABLE',
              baseIncluded=False, comments=[], reviews=[], threads=[], body='scope',
              statusCheckRollup=[], _review_mode='source', _acceptance_policy={})
    owner = dict(key='SF-007', agent='Codex', model='test', branch='work/source',
                 status='review', publish_pending=False, spec={'acceptance':['source works']})
    return pr, owner


class SourceReviews(unittest.TestCase):
    def test_source_identity_survives_ci_and_base_but_not_scope_or_feedback(self):
        pr,_ = fixture(); baseline=c.source_feedback(pr); fingerprint=c.feedback(pr)
        later=copy.deepcopy(pr);later['baseRefOid']='c'*40
        later['statusCheckRollup']=[dict(__typename='CheckRun',status='COMPLETED',conclusion='SUCCESS')]
        self.assertEqual(c.source_feedback(later),baseline)
        self.assertEqual(c.feedback(later),fingerprint)
        for change in [dict(headRefOid='d'*40),dict(body='changed requirements'),
                       dict(comments=[dict(id='C',body='fix this',updatedAt='now')]),
                       dict(threads=[dict(isResolved=False)]),dict(_acceptance_policy={'changed':True}),
                       dict(_batch_failure={'candidate_head':'e'*40})]:
            value={**later,**change};self.assertNotEqual(c.source_feedback(value),baseline)

    def test_integration_feedback_binds_manifest(self):
        pr,_=fixture();pr.pop('_review_mode');pr['_integration_batch']={'manifest_digest':'one'}
        before=c.feedback(pr);pr['_integration_batch']['manifest_digest']='two'
        self.assertNotEqual(c.feedback(pr),before)

    def test_typed_published_source_proof_rejects_stale_or_untrusted_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(temp))
            pr,owner=fixture();directory=Path(temp)/'review';directory.mkdir()
            snapshot={**pr,'ticket':copy.deepcopy(owner)}
            (directory/'snapshot.json').write_text(json.dumps(snapshot))
            job=engine.store.reserve('p',pr,'Claude','review',directory)
            good=dict(verdict='blocked',acceptance_complete=False,source_complete=True,
                      acceptance_ready=True,model='reviewer',evidence='Independent source tests pass; batch CI pending')
            def record(result=good, published=1, family='Claude'):
                engine.store.db.execute("UPDATE jobs SET status='done',result=?,publication_done=?,family=? WHERE id=?",
                                        (json.dumps(result),published,family,job))
            record();proof=engine.source_receipt({'id':'p'},pr,owner)
            self.assertEqual(proof['job'],job);self.assertEqual(proof['base'],'b'*40)
            self.assertEqual(engine.source_receipt({'id':'p'},{**pr,'baseRefOid':'c'*40},owner),proof)
            for result in [{k:v for k,v in good.items() if k not in ('source_complete','acceptance_ready')},
                           {**good,'acceptance_ready':False},{**good,'verdict':'fixed'}]:
                record(result);self.assertIsNone(engine.source_receipt({'id':'p'},pr,owner))
            record(published=0);self.assertIsNone(engine.source_receipt({'id':'p'},pr,owner))
            record(family='Codex');self.assertIsNone(engine.source_receipt({'id':'p'},pr,owner))
            record()
            for modified in [{**pr,'headRefOid':'d'*40},{**pr,'body':'changed'},
                             {**pr,'_batch_conflict':{'candidate_base':'c'*40}}]:
                self.assertIsNone(engine.source_receipt({'id':'p'},modified,owner))
            self.assertIsNone(engine.source_receipt({'id':'p'},pr,{**owner,'spec':{'acceptance':['new scope']}}))
            (directory/'snapshot.json').write_text('{broken')
            self.assertIsNone(engine.source_receipt({'id':'p'},pr,owner))

    def test_bad_typed_receipts_cannot_upgrade_review(self):
        base=dict(verdict='blocked',acceptance_complete=False,model='reviewer',evidence='checked')
        for extra in [dict(source_complete='yes'),dict(source_complete=True,acceptance_ready=1),
                      dict(source_complete=False,acceptance_ready=True),
                      dict(source_complete=True,acceptance_ready=True,verdict='changes_requested')]:
            with self.assertRaises(ValueError):c.normalize_result({**base,**extra})
        result=c.source_only_result({**base,'source_complete':True,'acceptance_ready':True,'verdict':'approve','acceptance_complete':True})
        self.assertEqual(result['verdict'],'blocked');self.assertFalse(result['acceptance_complete'])
        self.assertTrue(result['source_complete']);self.assertTrue(result['acceptance_ready'])

    def test_observer_contains_fresh_checks_without_raw_feedback_or_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(temp))
            pr,_=fixture();pr['comments']=[dict(id='C',body='PRIVATE_FEEDBACK_CANARY',updatedAt='now')]
            pr.update(_testnet_required=True,_testnet_status=dict(state='success',description='base:'+'f'*40))
            engine.observe_pr({'id':'p','repository':'owner/repo'},pr)
            path=Path(temp)/'p/pr-observations/pr-7.json';raw=path.read_text();value=json.loads(raw)
            self.assertNotIn('PRIVATE_FEEDBACK_CANARY',raw);self.assertNotIn('comments',value['pr'])
            self.assertEqual(value['source_fingerprint'],c.source_feedback(pr))
            self.assertEqual(value['pr']['_testnet_status']['state'],'stale')
            pr['statusCheckRollup']=[dict(__typename='CheckRun',status='COMPLETED',conclusion='SUCCESS')]
            engine.observe_pr({'id':'p','repository':'owner/repo'},pr)
            self.assertEqual(json.loads(path.read_text())['pr']['statusCheckRollup'][0]['conclusion'],'SUCCESS')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_batch_mode_does_not_rebase_or_repeat_unchanged_source_review(self):
        with tempfile.TemporaryDirectory() as temp:
            project=dict(id='p',repository='owner/repo',integration_batch={'enabled':True})
            engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(temp))
            pr,owner=fixture();owner['remote']={'url':'https://github.com/owner/repo/issues/1'}
            review=dict(head=pr['headRefOid'],feedback=c.feedback(pr),family='Claude',
                        result=dict(verdict='blocked',acceptance_complete=False,source_complete=True,acceptance_ready=True))
            engine.owners=Mock(return_value=[owner]);engine.collect=Mock();engine.discover_merged=Mock()
            engine.prs=Mock(return_value=[pr]);engine.store.review=Mock(return_value=review)
            engine.batches=types.SimpleNamespace(enabled=lambda _:True,tick=lambda *_:set())
            engine.ensure_closing_reference=Mock();engine.launch=Mock();engine.merge=Mock()
            engine.tick_project(project,True)
            engine.launch.assert_not_called();engine.merge.assert_not_called()
            # Advancing main plus pending/green ordinary checks does not restart it.
            pr['baseRefOid']='e'*40;pr['statusCheckRollup']=[dict(__typename='CheckRun',status='COMPLETED',conclusion='SUCCESS')]
            engine.tick_project(project,True);engine.launch.assert_not_called()
            # A real combined conflict does require owner action.
            pr['_batch_conflict']={'candidate_base':'e'*40};engine.tick_project(project,True)
            self.assertEqual(engine.launch.call_args.args[-1],'fix')


if __name__=='__main__':unittest.main()
