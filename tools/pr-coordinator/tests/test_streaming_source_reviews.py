import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coordinator as c


def fixture(number=1):
    pr = dict(number=number, headRefOid='a'*40, baseRefOid='b'*40,
              headRefName='work/source', baseRefName='main', state='OPEN',
              isDraft=False, isCrossRepository=False, mergeable='MERGEABLE',
              body='Closes #9.', comments=[], reviews=[], threads=[],
              statusCheckRollup=[], _review_mode='source')
    owner = dict(key='SF-001', token='claim', model='test', session='test', agent='Codex', branch='work/source', status='review',
                 publish_pending=False, expired=False, spec={'priority':'P1'})
    return pr, owner


class StreamingSourceReviews(unittest.TestCase):
    def engine(self, directory):
        engine = c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},
                                'projects':[]}, Path(directory))
        engine.ensure_closing_reference = Mock()
        engine.launch = Mock()
        engine.source_receipt = Mock(return_value=None)
        engine.ticket = Mock(return_value=json.dumps(fixture()[1]))
        return engine

    def test_review_starts_before_next_snapshot_and_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=self.engine(directory);pr,owner=fixture();events=[]
            project=dict(id='p',repository='a/b',integration_batch={'enabled':True})
            engine.owners=Mock(return_value=[owner]);engine.collect=Mock()
            engine.gh=Mock(return_value=json.dumps([[{'number':1},{'number':2}]]))
            def snapshot(_, number, fields):
                events.append('snapshot'+str(number))
                if number==2: raise RuntimeError('unrelated outage')
                return pr
            engine.pr=Mock(side_effect=snapshot)
            engine.launch=Mock(side_effect=lambda *args:events.append('review'))
            engine.discover_merged=Mock(side_effect=lambda *args:events.append('discovery'))
            engine.batches.tick=Mock(return_value=set())
            engine.tick_project(project,True)
            self.assertEqual(events,['snapshot1','review','snapshot2','discovery'])
            engine.launch.assert_called_once_with(project,pr,owner,'review')

    def test_active_batch_members_cannot_stream_review_after_receipt_changes(self):
        for status in ['collecting', 'testing', 'publish_pending', 'published', 'reconciling']:
            for change in ['head', 'feedback']:
                with self.subTest(status=status, change=change), tempfile.TemporaryDirectory() as directory:
                    engine=self.engine(directory);pr,owner=fixture()
                    project=dict(id='p',repository='a/b',integration_batch={'enabled':True})
                    engine.store.db.execute('INSERT INTO integration_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('p','batch',status,'b'*40,'d'*40,'integration/batch/batch',99,
                         json.dumps([dict(pr=1,head=pr['headRefOid'])]),directory,None,0,0,None,0,None))
                    if change=='head':
                        pr['headRefOid']='c'*40
                    else:
                        pr['comments']=[dict(id='new',body='New source feedback')]
                    engine.owners=Mock(return_value=[owner]);engine.collect=Mock()
                    engine.gh=Mock(return_value=json.dumps([[{'number':1}]]))
                    engine.pr=Mock(return_value=pr);engine.discover_merged=Mock()
                    # The main loop already honors this reservation. The streaming
                    # callback must independently honor the persisted epoch too.
                    engine.batches.tick=Mock(return_value={1})
                    engine.tick_project(project,True)
                    engine.launch.assert_not_called()

    def test_terminal_and_other_project_batches_do_not_reserve_source(self):
        for project_id,status in [('p','done'),('p','invalidated'),('p','failed'),('other','testing')]:
            with self.subTest(project=project_id,status=status), tempfile.TemporaryDirectory() as directory:
                engine=self.engine(directory);pr,owner=fixture()
                engine.store.db.execute('INSERT INTO integration_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (project_id,'batch',status,'b'*40,'d'*40,'integration/batch/batch',99,
                     json.dumps([dict(pr=1)]),directory,None,0,0,None,0,None))
                self.assertTrue(engine.start_source_review(
                    dict(id='p',integration_batch={'enabled':True}),pr,[owner]))
                engine.launch.assert_called_once()

    def test_callback_failure_retains_snapshot_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=self.engine(directory);a,_=fixture(1);b,_=fixture(2)
            engine.gh=Mock(return_value=json.dumps([[{'number':1},{'number':2}]]))
            engine.pr=Mock(side_effect=[a,b]);called=[]
            def callback(pr):
                called.append(pr['number'])
                if pr['number']==1:raise RuntimeError('provider unavailable')
            self.assertEqual(engine.prs({'id':'p','repository':'a/b'},callback),[a,b])
            self.assertEqual(called,[1,2])

    def test_stream_order_uses_owner_priority_then_number(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=self.engine(directory);seen=[]
            rows=[dict(number=3,head={'ref':'low'}),dict(number=2,head={'ref':'high'}),dict(number=1,head={'ref':'high'})]
            engine.gh=Mock(return_value=json.dumps([rows]));engine.pr=lambda _,n,*args:dict(number=n)
            owners=[dict(branch='high',spec={'priority':'P0'}),dict(branch='low',spec={'priority':'P2'})]
            engine.prs({'id':'p','repository':'a/b'},lambda pr:seen.append(pr['number']),owners)
            self.assertEqual(seen,[1,2,3])

    def test_non_execute_and_non_batch_do_not_use_early_callback(self):
        for execute,batch in [(False,True),(True,False)]:
            with self.subTest(execute=execute,batch=batch),tempfile.TemporaryDirectory() as directory:
                engine=self.engine(directory);project=dict(id='p',integration_batch={'enabled':batch})
                engine.owners=Mock(return_value=[]);engine.collect=Mock();engine.discover_merged=Mock()
                engine.prs=Mock(return_value=[]);engine.start_source_review=Mock()
                engine.batches.tick=Mock(return_value=set())
                engine.tick_project(project,execute)
                engine.prs.assert_called_once_with(project);engine.start_source_review.assert_not_called()

    def test_ineligible_or_repair_snapshots_do_not_start_source_review(self):
        changes=[{'_review_mode':'integration'},{'_integration_batch':{'id':'x'}},
                 {'state':'MERGED'},{'isDraft':True},{'isCrossRepository':True},
                 {'_batch_conflict':{'reason':'conflict'}},{'_batch_failure':{'reason':'CI'}},
                 {'statusCheckRollup':[dict(__typename='CheckRun',name='check',status='COMPLETED',conclusion='FAILURE',detailsUrl='https://github.com/a/b/actions/runs/1/job/2')]}]
        for change in changes:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                engine=self.engine(directory);pr,owner=fixture();pr.update(change)
                self.assertFalse(engine.start_source_review({'id':'p','integration_batch':{'enabled':True}},pr,[owner]))
                engine.launch.assert_not_called()
        for change in [{'status':'in_progress'},{'publish_pending':True},{'expired':True},{'branch':'another'}]:
            with self.subTest(owner=change),tempfile.TemporaryDirectory() as directory:
                engine=self.engine(directory);pr,owner=fixture();owner.update(change)
                self.assertFalse(engine.start_source_review({'id':'p','integration_batch':{'enabled':True}},pr,[owner]))
                engine.launch.assert_not_called()

    def test_owner_changes_after_snapshot_are_rejected_before_launch(self):
        for change in [{'token':'replacement'}, {'branch':'moved'}, {'agent':'Claude'},
                       {'model':'changed'}, {'session':'changed'}, {'status':'in_progress'},
                       {'publish_pending':True}, {'expired':True}]:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                engine=self.engine(directory);pr,owner=fixture()
                engine.ticket.return_value=json.dumps({**owner,**change})
                self.assertFalse(engine.start_source_review({'id':'p','integration_batch':{'enabled':True}},pr,[owner]))
                engine.launch.assert_not_called()

    def test_current_receipt_and_actionable_findings_do_not_repeat_review(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=self.engine(directory);pr,owner=fixture();project={'id':'p','integration_batch':{'enabled':True}}
            for verdict,fp,pending in [('blocked',c.feedback(pr),False),('changes_requested','old',False),('blocked','old',True)]:
                engine.store.review=Mock(return_value=dict(head=pr['headRefOid'],feedback=fp,publication_pending=pending,result={'verdict':verdict}))
                self.assertFalse(engine.start_source_review(project,pr,[owner]))
            engine.store.review=Mock(return_value=None);engine.source_receipt.return_value={'job':7}
            self.assertFalse(engine.start_source_review(project,pr,[owner]));engine.launch.assert_not_called()

    def test_pending_ci_can_overlap_source_review_and_old_head_review_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=self.engine(directory);pr,owner=fixture();project={'id':'p','integration_batch':{'enabled':True}}
            pr['statusCheckRollup']=[dict(__typename='CheckRun',name='check',status='IN_PROGRESS',conclusion=None)]
            engine.store.review=Mock(return_value=dict(head='c'*40,feedback='old',result={'verdict':'changes_requested'}))
            self.assertTrue(engine.start_source_review(project,pr,[owner]));engine.launch.assert_called_once()

    def test_active_or_uncertain_worker_fence_is_still_owned_by_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=self.engine(directory);pr,_=fixture()
            first=engine.store.reserve('p',pr,'Claude','review',Path(directory)/'first')
            self.assertIsNotNone(first)
            for state in ['reserved','running','orphaned','quarantined','quota_wait','quota_launch']:
                engine.store.db.execute('UPDATE jobs SET status=? WHERE id=?',(state,first))
                self.assertIsNone(engine.store.reserve('p',pr,'Claude','review',Path(directory)/'second'))

    def test_published_closing_reference_updates_immediate_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=self.engine(directory);pr,owner=fixture();pr['body']='Description'
            engine.gh=Mock();engine.issue_number=Mock(return_value=9)
            c.Coordinator.ensure_closing_reference(engine,{'repository':'a/b'},pr,owner)
            self.assertEqual(pr['body'],'Description\n\nCloses #9.')
            self.assertIn('body='+pr['body'],engine.gh.call_args.args[1])


if __name__=='__main__':unittest.main()
