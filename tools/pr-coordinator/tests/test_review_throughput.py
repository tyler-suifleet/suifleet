"""Source review can overlap CI, without becoming final merge evidence."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_coordinator as previous
from test_coordinator import c, fixture


class ParallelSourceReview(unittest.TestCase):
    def test_pending_checks_share_one_source_review_until_success(self):
        with tempfile.TemporaryDirectory() as d:
            pr, owner, _ = fixture()
            pr['statusCheckRollup'][0].update(status='QUEUED', conclusion=None)
            helper = previous.CheckFreshnessTests()
            engine, project, owner = helper.setup_scan(d, pr)
            original = c.feedback(pr)
            for status, conclusion in [('IN_PROGRESS',None),('COMPLETED','FAILURE')]:
                pr['statusCheckRollup'][0].update(status=status,conclusion=conclusion)
                self.assertEqual(c.feedback(pr),original)
                with patch.object(engine,'launch') as launch,patch.object(engine,'merge') as merge:
                    engine.tick(True)
                    launch.assert_not_called(); merge.assert_not_called()
            pr['statusCheckRollup'][0].update(status='COMPLETED',conclusion='SUCCESS')
            self.assertNotEqual(c.feedback(pr),original)
            with patch.object(engine,'launch') as launch,patch.object(engine,'merge') as merge:
                engine.tick(True)
                self.assertEqual(launch.call_args.args[-1],'review')
                merge.assert_not_called()

    def test_source_result_cannot_claim_acceptance_or_hide_code_defects(self):
        result=fixture()[2]['result']
        source=c.source_only_result(result)
        self.assertEqual(source['verdict'],'blocked')
        self.assertFalse(source['acceptance_complete'])
        self.assertTrue(result['acceptance_complete'])
        findings=c.source_only_result({**result,'verdict':'changes_requested'})
        self.assertEqual(findings['verdict'],'changes_requested')
        self.assertFalse(findings['acceptance_complete'])

    def test_collection_enforces_source_only_snapshot_and_comment_publication(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d); pr,_,review=fixture()
            pr['statusCheckRollup']=[]
            engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},directory)
            project={'id':'test','repository':'owner/repo','review_github_account':'reviewer'}
            job=engine.store.reserve('test',pr,'Claude','review',directory)
            engine.store.db.execute("UPDATE jobs SET status='running',pid=999999,started='old' WHERE id=?",(job,))
            (directory/'exit.json').write_text('{"returncode":0}')
            (directory/'result.json').write_text(json.dumps(review['result']))
            (directory/'snapshot.json').write_text(json.dumps(pr))
            events=[]
            def gh(project,args):
                body=next(a[5:] for a in args if a.startswith('body='))
                if '/comments' in args[1]:return json.dumps({'node_id':'comment','body':body})
                events.append(next(a[6:] for a in args if a.startswith('event=')))
                return json.dumps({'node_id':'review','body':body,'state':'COMMENTED','commit_id':pr['headRefOid']})
            with patch.object(c,'worker_stopped',return_value=True),patch.object(c,'process_identity',return_value=None),patch.object(engine,'pr',return_value=pr),patch.object(engine,'gh',side_effect=gh):
                engine.collect(project,[])
            result=engine.store.review('test',pr['number'])['result']
            self.assertFalse(result['acceptance_complete'])
            self.assertEqual(result['verdict'],'blocked')
            self.assertEqual(events,['COMMENT'])

    def test_source_review_preserves_head_base_threads_comments_policy(self):
        pr,_,_=fixture(); pr['statusCheckRollup']=[]
        before=c.feedback(pr)
        changes=[{'headRefOid':'new'}, {'baseRefOid':'new'},
                 {'threads':[{'isResolved':False}]},
                 {'comments':[{'id':'new','body':'fix','updatedAt':'now'}]},
                 {'_acceptance_policy':{'tickets':{'T1':{}}}}]
        for change in changes:
            self.assertNotEqual(c.feedback({**pr,**change}),before)

    def test_no_review_for_draft_unpublished_owner_or_nonmergeable(self):
        with tempfile.TemporaryDirectory() as d:
            helper=previous.CheckFreshnessTests(); pr=fixture()[0]
            pr['statusCheckRollup']=[]
            engine,project,owner=helper.setup_scan(d,pr)
            pr['comments']=[{'id':'new','body':'review','updatedAt':'now'}]
            for change, owner_change in [({'isDraft':True},{}),({'state':'CLOSED'},{}),
                                         ({'mergeable':'UNKNOWN'},{}),({}, {'publish_pending':True}),
                                         ({},{'status':'in_progress'})]:
                candidate={**pr,**change}; current={**owner,**owner_change}
                helper.configure_scan(engine,candidate,current)
                with patch.object(engine,'launch') as launch,patch.object(engine,'merge') as merge:
                    engine.tick(True);launch.assert_not_called();merge.assert_not_called()

    def test_pending_and_failed_checks_never_merge_even_with_forged_approval(self):
        for checks in ([],[{'__typename':'CheckRun','status':'IN_PROGRESS'}],
                       [{'__typename':'CheckRun','status':'COMPLETED','conclusion':'FAILURE'}]):
            pr,owner,review=fixture();pr['statusCheckRollup']=checks
            review['feedback']=c.feedback(pr)
            self.assertIsNotNone(c.gate(pr,owner,review))
        pr,owner,review=fixture()
        self.assertIsNone(c.gate(pr,owner,review))


class SharedReads(unittest.TestCase):
    def engine(self,d):
        return c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(d))

    def test_rate_limit_defers_other_prs_and_survives_restart_without_account_switch(self):
        with tempfile.TemporaryDirectory() as d:
            engine=self.engine(d)
            project={'id':'p','github_account':'expected','root':d,'repository':'owner/repo'}
            error=subprocess.CalledProcessError(1,['gh'],stderr='HTTP 429 Retry-After: 120')
            with patch.object(c,'run',side_effect=['private-token',error]) as run:
                with self.assertRaises(RuntimeError):engine.gh(project,['pr','view','1'])
                self.assertEqual(run.call_count,2)
                engine=self.engine(d)
                with self.assertRaises(RuntimeError):engine.gh(project,['pr','view','2'])
                self.assertEqual(run.call_count,2)
                self.assertEqual(engine.store.db.execute('SELECT account FROM github_backoff').fetchone()[0],'expected')
            engine.store.db.execute('UPDATE github_backoff SET retry_after=0')
            with patch.object(c,'run',side_effect=['private-token','ok']):
                self.assertEqual(engine.gh(project,['pr','view','2']),'ok')

    def test_immutable_reads_key_exact_sha_and_account_only(self):
        with tempfile.TemporaryDirectory() as d:
            engine=self.engine(d);project={'github_account':'one','repository':'owner/repo'}
            path='repos/owner/repo/git/trees/'+'a'*40+'?recursive=1'
            with patch.object(engine,'gh',return_value='{"tree":[]}') as gh:
                engine.immutable_read(project,path);engine.immutable_read(project,path)
                self.assertEqual(gh.call_count,1)
                engine.immutable_read(project,path.replace('a'*40,'b'*40))
                engine.immutable_read({**project,'github_account':'two'},path)
                self.assertEqual(gh.call_count,3)

    def test_failed_reads_never_enter_cache(self):
        with tempfile.TemporaryDirectory() as d:
            engine=self.engine(d);project={'github_account':'one','repository':'owner/repo'}
            with patch.object(engine,'gh',side_effect=RuntimeError('unavailable')) as gh:
                for _ in range(2):
                    with self.assertRaises(RuntimeError):engine.immutable_read(project,'sha-path')
                self.assertEqual(gh.call_count,2)
