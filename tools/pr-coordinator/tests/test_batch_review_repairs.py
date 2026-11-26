import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import coordinator as c
from batch_integration import canonical
from test_batch_integration import Fixture


class BatchReviewRepairs(unittest.TestCase):
    def fixture(self, singleton=True, three=False):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);f=Fixture(Path(temp.name))
        if singleton:
            f.source_prs.pop(2);f.receipts.pop(2);f.owners=f.owners[:1]
        if three:
            f.project['integration_batch']['max_members'] = 3
            f.git('checkout', '-qb', 'work/3', f.base)
            (f.repo / 'feature-3').write_text('feature 3')
            f.git('add', 'feature-3')
            f.git('commit', '-qm', 'feature 3')
            third = f.git('rev-parse', 'HEAD')
            f.git('push', '-q', 'origin', 'work/3')
            f.git('checkout', '-q', 'main')
            f.source_prs[3] = {**f.make_pr(3, third, 'work/3'), '_review_mode': 'source'}
            f.receipts[3] = {**f.receipts[2], 'job': 3, 'head': third,
                             'source_feedback': c.source_feedback(f.source_prs[3])}
            f.owners.append({**f.owners[1], 'key': 'T-3', 'branch': 'work/3',
                             'session': 'third', 'token': 'third'})
            for old, new in [(1, 845), (2, 853), (3, 850)]:
                f.source_prs[new] = {**f.source_prs.pop(old), 'number': new}
                f.receipts[new] = f.receipts.pop(old)
                f.formal_reviews[new] = f.formal_reviews.pop(old, [])
        f.tick();f.green();batch=f.row();candidate=f.controller.candidate(f.project,batch)
        review=dict(id=888,kind='review',status='done',publication_done=1,publication_pending=False,
                    family='Claude',head=candidate['headRefOid'],feedback=c.feedback(candidate),
                    result=dict(verdict='changes_requested',acceptance_complete=False,model='test-reviewer',
                                evidence='Reproduced enrollment regression. Add credential/policy/claim regressions.'))
        f.approvals[batch['pr']]=review
        return f,candidate,review

    def publish_attribution(self, f, candidate, review, ids, login=None):
        batch = f.row()
        review.update(project=f.project['id'], pr=batch['pr'], directory=str(f.root),
                      review_id='published-review')
        review['result']['affected_source_prs'] = ids
        (f.root / 'snapshot.json').write_text(json.dumps(candidate))
        published = dict(id='published-review', state='CHANGES_REQUESTED',
                         author={'login': login or f.project['review_github_account']},
                         commit={'oid': batch['head']},
                         body=c.review_result_body(review, review['result']))
        f.candidate_prs[batch['pr']].update(
            reviews=[published], _receipts={published['id']: c.publication_hash(published)})
        return published

    def test_three_member_attribution_launches_only_same_claim_and_keeps_siblings_ready(self):
        f, candidate, review = self.fixture(singleton=False, three=True)
        self.publish_attribution(f, candidate, review, [853])
        engine = c.Coordinator({'workers': {'Codex': ['true'], 'Claude': ['true']}, 'projects': []}, f.root / 'engine')
        engine.batches = f.controller
        engine.owners = Mock(return_value=f.owners)
        engine.collect = Mock()
        engine.discover_merged = Mock()
        engine.prs = Mock(side_effect=lambda *args: [f.pr(f.project, n) for n in f.source_prs])
        engine.ensure_closing_reference = Mock()
        engine.launch = Mock()
        engine.tick_project(f.project, True)
        fixes = [call.args for call in engine.launch.call_args_list if call.args[3] == 'fix']
        self.assertEqual([source['number'] for _, source, _, _ in fixes], [853])
        _, source, owner, _ = fixes[0]
        self.assertEqual(owner, f.owners[1])
        self.assertTrue(c.matches_repair_claim(source, owner))
        self.assertFalse(c.matches_repair_claim(source, {**owner, 'token': 'replacement'}))
        self.assertEqual(source['_batch_review_failure']['source_head'], f.source_prs[853]['headRefOid'])
        self.assertFalse(f.controller.repair_context(f.project, {**source, 'headRefOid': 'e' * 40}))
        f.tick()
        self.assertEqual({m['pr'] for m in json.loads(f.row()['members'])}, {845, 850})

    def test_composition_attribution_routes_multiple_members(self):
        f, candidate, review = self.fixture(singleton=False, three=True)
        self.publish_attribution(f, candidate, review, [853, 850],
                                 login=f.project['review_github_account'].swapcase())
        f.tick()
        self.assertEqual({n for n, pr in f.source_prs.items()
                          if '_batch_review_failure' in f.controller.repair_context(f.project, pr)}, {853, 850})

    def test_invalid_attribution_retains_failure_without_owner_routing(self):
        cases = [
            ([], {}),
            ([1, 1], {}),
            ([1, 999], {}),
            ([True], {}),
            (['1'], {}),
            ({'pr': 1}, {}),
            ([1], {'baseRefOid': 'e' * 40}),
            ([1], {'_integration_batch': {'manifest_digest': 'e' * 64}}),
            ([1], {'body': 'changed reviewed feedback'}),
        ]
        for ids, snapshot_changes in cases:
            with self.subTest(ids=ids, snapshot_changes=snapshot_changes):
                f, candidate, review = self.fixture(singleton=False)
                self.publish_attribution(f, candidate, review, ids)
                (f.root / 'snapshot.json').write_text(json.dumps({**candidate, **snapshot_changes}))
                f.tick()
                error = json.loads(f.row()['error'])
                self.assertTrue(error['attribution_required'])
                self.assertNotIn('affected_source_prs', error)
                for pr in f.source_prs.values():
                    self.assertEqual(f.controller.repair_context(f.project, pr), {})

    def test_modified_publication_cannot_route_attribution(self):
        f, candidate, review = self.fixture(singleton=False)
        self.publish_attribution(f, candidate, review, [1])
        # The durable job result and the publication must carry the same IDs.
        review['result']['affected_source_prs'] = [2]
        f.tick()
        self.assertTrue(json.loads(f.row()['error'])['attribution_required'])
        for pr in f.source_prs.values():
            self.assertEqual(f.controller.repair_context(f.project, pr), {})

    def test_singleton_finding_retires_candidate_and_binds_original_source_context(self):
        f,candidate,review=self.fixture();f.tick();batch=f.row()
        self.assertEqual(batch['status'],'failed');self.assertEqual(f.candidate_prs[batch['pr']]['state'],'CLOSED')
        self.assertEqual(len(f.launched),1)  # Only the initial review, launched during CI.
        source=f.source_prs[1];context=f.controller.repair_context(f.project,source)['_batch_review_failure']
        self.assertEqual(context['review_id'],888);self.assertEqual(context['source_head'],source['headRefOid'])
        self.assertIn('Reproduced enrollment',context['findings']);self.assertNotIn('_ci_repair',context)
        with tempfile.TemporaryDirectory() as temp:
            engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(temp))
            self.assertIsNone(engine.source_receipt(f.project,{**source,'_batch_review_failure':context},f.owners[0]))
        self.assertNotEqual(c.source_feedback(source),c.source_feedback({**source,'_batch_review_failure':context}))
        advanced={**source,'baseRefOid':'d'*40};self.assertEqual(f.controller.repair_context(f.project,advanced)['_batch_review_failure'],context)
        self.assertEqual(f.controller.repair_context(f.project,{**source,'headRefOid':'e'*40}),{})

    def test_singleton_routes_with_unverifiable_attribution_including_persisted_failure(self):
        f, candidate, review = self.fixture()
        self.publish_attribution(f, candidate, review, [1])
        (f.root / 'snapshot.json').unlink()
        f.tick()
        batch = f.row()
        self.assertEqual(batch['status'], 'failed')
        self.assertEqual(f.candidate_prs[batch['pr']]['state'], 'CLOSED')
        error = json.loads(batch['error'])
        self.assertNotIn('attribution_required', error)
        self.assertNotIn('affected_source_prs', error)
        source = f.source_prs[1]
        context = f.controller.repair_context(f.project, source)['_batch_review_failure']
        self.assertEqual(context['review_id'], review['id'])
        self.assertEqual(context['source_head'], source['headRefOid'])
        self.assertTrue(c.matches_repair_claim({**source, '_batch_review_failure': context}, f.owners[0]))
        f.controller.update(batch, error=canonical({**error, 'attribution_required': True}))
        self.assertEqual(f.controller.repair_context(f.project, source)['_batch_review_failure'], context)

    def test_same_scan_source_snapshots_receive_the_genuine_repair_context(self):
        f,_,_=self.fixture();sources=[f.pr(f.project,1)]
        f.controller.tick(f.project,sources,f.owners,True)
        self.assertEqual(sources[0]['_batch_review_failure']['review_id'],888)
        self.assertNotIn('_ci_repair',sources[0])

    def test_real_batch_rejection_routes_owner_fix_in_same_project_tick(self):
        for singleton in (True, False):
            with self.subTest(singleton=singleton):
                f,_,_=self.fixture(singleton=singleton)
                with tempfile.TemporaryDirectory() as temp:
                    engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(temp))
                    # Keep the real batch controller and its Git/remote fixture;
                    # exercise the actual caller that consumes its held set.
                    engine.batches=f.controller
                    engine.owners=Mock(return_value=f.owners)
                    engine.collect=Mock();engine.discover_merged=Mock()
                    engine.prs=Mock(side_effect=lambda *args: [f.pr(f.project,n) for n in f.source_prs])
                    engine.ensure_closing_reference=Mock();engine.launch=Mock()
                    engine.tick_project(f.project,True)
                    self.assertEqual(f.row()['status'],'failed')
                    fixes=[call for call in engine.launch.call_args_list if call.args[3]=='fix']
                    if singleton:
                        self.assertEqual(len(fixes),1)
                        project,source,owner,kind=fixes[0].args
                        self.assertEqual(project,f.project)
                        self.assertEqual(owner,f.owners[0])
                        self.assertEqual(source['headRefOid'],f.source_prs[1]['headRefOid'])
                        self.assertEqual(source['_batch_review_failure']['review_id'],888)
                        self.assertNotIn('_ci_repair',source)
                    else:
                        self.assertEqual(fixes,[])

    def test_stale_unpublished_incomplete_or_already_publishing_review_never_routes(self):
        for change in [dict(head='f'*40),dict(feedback='stale'),dict(publication_done=0),
                       dict(publication_pending=True),dict(status='running'),dict(family='Codex'),dict(family='Other'),
                       dict(result=dict(verdict='blocked',acceptance_complete=False,evidence='wait'))]:
            with self.subTest(change=change):
                f,candidate,review=self.fixture();batch=f.row()
                self.assertFalse(f.controller.reject_review(f.project,batch,candidate,{**review,**change}))
                self.assertNotEqual(f.row()['status'],'failed');self.assertEqual(f.candidate_prs[batch['pr']]['state'],'OPEN')
        f,candidate,review=self.fixture();batch=f.row();batch['publish_intent']=1
        self.assertFalse(f.controller.reject_review(f.project,batch,candidate,review))
        for change in [dict(headRefOid='f'*40),dict(baseRefOid='f'*40)]:
            self.assertFalse(f.controller.reject_review(f.project,f.row(),{**candidate,**change},review))

    def test_unattributed_multi_member_review_splits_without_guessing_an_owner(self):
        f,_,_=self.fixture(singleton=False);f.tick();failed=f.row();self.assertEqual(failed['status'],'failed')
        members=json.loads(failed['members'])
        for source in f.source_prs.values():self.assertEqual(f.controller.repair_context(f.project,source),{})
        selected=f.controller.select(f.project,members,failed['base'],2)
        self.assertEqual(len(selected),1)
        self.assertEqual(len(f.controller.select(f.project,list(reversed(members)),failed['base'],2)),1)
        requests = f.controller.attribution_requests(f.project, {'_integration_batch': {'members': selected}})
        self.assertEqual(requests, [dict(candidate_pr=failed['pr'], review_id=888,
                                         source_prs=[selected[0]['pr']])])
        # Selection now verifies real ancestry; advance a real base rather than
        # supplying an object ID that cannot exist in this fixture repository.
        f.git('checkout', '-q', 'main')
        f.git('commit', '--allow-empty', '-qm', 'advance integration base')
        new_base = f.git('rev-parse', 'HEAD')
        f.git('push', '-q', 'origin', 'main')
        self.assertEqual(len(f.controller.select(f.project,members,new_base,2)),1)
        # A clock step must not make the fixture's latest-batch helper mistake
        # the correctly created successor for the old failed epoch.
        f.controller.clock = lambda: failed['created'] - 1
        f.tick();new=f.row();self.assertNotEqual(new['id'],failed['id']);self.assertEqual(len(json.loads(new['members'])),1)

    def test_rejected_source_does_not_hold_an_independent_ready_pr(self):
        f,_,_=self.fixture();f.tick();failed=f.row()
        # The independent second source has a genuine existing source receipt.
        head=f.heads[1];owner=dict(key='T-2',agent='Codex',model='test',session='two',branch='work/2',token='two',status='review',expired=False,publish_pending=False,spec={'acceptance':['two']})
        pr=f.make_pr(2,head,'work/2');pr['_review_mode']='source';f.source_prs[2]=pr;f.owners.append(owner)
        f.receipts[2]=dict(job=2,family='Claude',head=head,base=f.base,source_feedback=c.source_feedback(pr),evidence_hash='d'*64)
        f.tick();new=f.row();self.assertNotEqual(new['id'],failed['id']);self.assertEqual([m['pr'] for m in json.loads(new['members'])],[2])

    def test_ambiguous_remote_close_preserves_finding_and_retries_without_duplicate_mutation(self):
        f,_,_=self.fixture();original=f.gh;lost=False
        def remote(project,args):
            nonlocal lost
            result=original(project,args)
            if '--method' in args and args[args.index('--method')+1]=='PATCH' and not lost:
                lost=True;raise subprocess.CalledProcessError(1,['gh'],stderr='connection lost after close')
            return result
        f.gh=remote;f.tick();batch=f.row();error=json.loads(batch['error'])
        self.assertEqual(batch['status'],'failed');self.assertEqual(error['review_id'],888)
        self.assertIn('_batch_review_failure',f.controller.repair_context(f.project,f.source_prs[1]))
        self.assertTrue(json.loads(f.row()['error'])['candidate_retired'])
        patches=[e for e in f.events if '--method' in e and e[e.index('--method')+1]=='PATCH']
        self.assertEqual(len(patches),1)
        # Another observation only verifies the durable closed receipt.
        f.tick();self.assertEqual(len([e for e in f.events if '--method' in e and e[e.index('--method')+1]=='PATCH']),1)

    def test_remote_close_failure_before_mutation_does_not_block_independent_candidate(self):
        f,_,_=self.fixture();original=f.gh
        def remote(project,args):
            if '--method' in args and args[args.index('--method')+1]=='PATCH':
                raise subprocess.CalledProcessError(1,['gh'],stderr='transient read/write transport')
            return original(project,args)
        f.gh=remote;f.tick();failed=f.row()
        self.assertFalse(json.loads(failed['error'])['candidate_retired'])
        self.assertIn('_batch_review_failure',f.controller.repair_context(f.project,f.source_prs[1]))
        owner=dict(key='T-2',agent='Codex',model='test',session='two',branch='work/2',token='two',status='review',expired=False,publish_pending=False,spec={'acceptance':['two']})
        pr=f.make_pr(2,f.heads[1],'work/2');pr['_review_mode']='source';f.source_prs[2]=pr;f.owners.append(owner)
        f.receipts[2]=dict(job=2,family='Claude',head=f.heads[1],base=f.base,source_feedback=c.source_feedback(pr),evidence_hash='d'*64)
        f.tick();self.assertNotEqual(f.row()['id'],failed['id']);self.assertEqual([m['pr'] for m in json.loads(f.row()['members'])],[2])

    def test_successful_patch_without_readback_cannot_claim_closed(self):
        f,_,_=self.fixture();original=f.gh
        def remote(project,args):
            if '--method' in args and args[args.index('--method')+1]=='PATCH':return '{}'
            return original(project,args)
        f.gh=remote;f.tick();batch=f.row()
        self.assertEqual(batch['status'],'failed');self.assertFalse(json.loads(batch['error'])['candidate_retired'])
        self.assertEqual(f.candidate_prs[batch['pr']]['state'],'OPEN')

    def test_cleanup_failure_does_not_abort_other_selection_or_erase_review_proof(self):
        f,_,_=self.fixture();original=f.controller.cleanup
        f.controller.cleanup=Mock(side_effect=RuntimeError('busy mount'))
        f.tick();self.assertEqual(f.row()['status'],'failed');self.assertEqual(json.loads(f.row()['error'])['review_id'],888)
        f.tick();self.assertEqual(f.row()['status'],'failed')
        f.controller.cleanup=original;f.tick()

    def test_repair_context_wins_over_early_read_only_review_and_uses_owner_fix(self):
        with tempfile.TemporaryDirectory() as temp:
            project=dict(id='p',repository='a/b',integration_batch={'enabled':True})
            engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(temp))
            pr=dict(number=1,headRefOid='a'*40,baseRefOid='b'*40,headRefName='work/source',baseRefName='main',
                    state='OPEN',isDraft=False,isCrossRepository=False,mergeable='MERGEABLE',baseIncluded=False,
                    comments=[],reviews=[],threads=[],body='Closes #1.',statusCheckRollup=[],_review_mode='source',
                    _batch_review_failure={'review_id':888,'findings':'actual regression'})
            owner=dict(key='T1',agent='Codex',status='review',publish_pending=False,branch='work/source',spec={})
            self.assertFalse(engine.start_source_review(project,pr,[owner]))
            engine.owners=Mock(return_value=[owner]);engine.collect=Mock();engine.discover_merged=Mock()
            engine.prs=Mock(return_value=[pr]);engine.batches.tick=Mock(return_value=set())
            engine.ensure_closing_reference=Mock();engine.launch=Mock()
            engine.tick_project(project,True)
            engine.launch.assert_called_once_with(project,pr,owner,'fix')


if __name__=='__main__':unittest.main()
