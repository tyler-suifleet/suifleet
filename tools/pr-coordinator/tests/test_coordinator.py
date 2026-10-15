import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('coordinator',Path(__file__).resolve().parents[1]/'coordinator.py')
c = importlib.util.module_from_spec(spec);spec.loader.exec_module(c)


def fixture():
    pr = dict(number=49,headRefOid='a'*40,baseRefOid='b'*40,state='OPEN',isDraft=False,mergeable='MERGEABLE',comments=[],reviews=[],threads=[],statusCheckRollup=[{'__typename':'CheckRun','status':'COMPLETED','conclusion':'SUCCESS'}])
    owner = dict(status='review',agent='Codex',publish_pending=False)
    review = dict(head=pr['headRefOid'],feedback=c.feedback(pr),family='Claude',result=dict(verdict='approve',acceptance_complete=True,model='test',evidence='verified'))
    return pr,owner,review


class Gates(unittest.TestCase):
    def test_current_independent_review_passes(self):
        self.assertIsNone(c.gate(*fixture()))

    def test_head_base_comments_and_reviews_invalidate(self):
        for field,value in [('headRefOid','new'),('baseRefOid','new'),('comments',[{'id':'x','body':'fix bug','updatedAt':'new'}]),('reviews',[{'id':'x','state':'CHANGES_REQUESTED','body':'fix','commit':{'oid':'a'*40},'author':{'login':'human'}}])]:
            pr,owner,review=fixture();pr[field]=value
            self.assertIsNotNone(c.gate(pr,owner,review),field)

    def test_blockers_fail_closed(self):
        for field,value in [('state','CLOSED'),('isDraft',True),('mergeable','UNKNOWN'),('mergeable','CONFLICTING'),('threads',[{'isResolved':False}]),('statusCheckRollup',[]),('statusCheckRollup',[{'__typename':'CheckRun','status':'COMPLETED','conclusion':'SKIPPED'}]),('statusCheckRollup',[{'__typename':'CheckRun','status':'IN_PROGRESS','conclusion':None}]),('statusCheckRollup',[{'__typename':'StatusContext','state':'FAILURE'}])]:
            pr,owner,review=fixture();pr[field]=value;review['feedback']=c.feedback(pr)
            self.assertIsNotNone(c.gate(pr,owner,review),field)

    def test_ownership_acceptance_and_self_review(self):
        for field,value in [('status','in_progress'),('publish_pending',True),('agent','Claude')]:
            pr,owner,review=fixture();owner[field]=value;self.assertIsNotNone(c.gate(pr,owner,review))
        for field,value in [('verdict','changes_requested'),('acceptance_complete',False)]:
            pr,owner,review=fixture();review['result'][field]=value;self.assertIsNotNone(c.gate(pr,owner,review))

    def test_formal_changes_requested_must_be_resolved(self):
        pr,owner,review=fixture();pr['reviews']=[{'id':'r','state':'CHANGES_REQUESTED','author':{'login':'human'},'commit':{'oid':pr['headRefOid']}}];review['feedback']=c.feedback(pr)
        self.assertIn('GitHub',c.gate(pr,owner,review))
        pr['reviews'].append({'id':'r2','state':'APPROVED','author':{'login':'human'},'commit':{'oid':pr['headRefOid']}});review['feedback']=c.feedback(pr)
        self.assertIsNone(c.gate(pr,owner,review))

    def test_publication_does_not_trigger_infinite_review_loop(self):
        pr,owner,review=fixture();pr['comments']=[{'id':'x','body':c.MARKER+' approved','updatedAt':'new'}];pr['reviews']=[{'id':'y','state':'APPROVED','body':c.MARKER+' approved','author':{'login':'therealtime'}}]
        pr['_receipts'] = {item['id']:c.publication_hash(item) for item in pr['comments']+pr['reviews']}
        self.assertIsNone(c.gate(pr,owner,review))
        pr['comments'].append({'id':'spoof','body':c.MARKER+' hidden request','updatedAt':'new'})
        self.assertIsNotNone(c.gate(pr,owner,review))


class Reservations(unittest.TestCase):
    def test_concurrent_model_reservation_is_atomic_across_projects(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.sqlite3';c.Store(path);barrier=threading.Barrier(2);results=[]
            def acquire(project):
                store=c.Store(path);barrier.wait();results.append(store.reserve(project,fixture()[0],'Claude','review',Path(d)));store.db.close()
            threads=[threading.Thread(target=acquire,args=(p,)) for p in ('one','two')]
            for t in threads:t.start()
            for t in threads:t.join()
            self.assertEqual(sum(r is not None for r in results),1)
            # Restart preserves a live job; no duplicate CLI process.
            self.assertIsNone(c.Store(path).reserve('three',fixture()[0],'Claude','review',Path(d)))

    def test_pr_exclusion_and_completed_same_input_deduplication(self):
        with tempfile.TemporaryDirectory() as d:
            store=c.Store(Path(d)/'state.sqlite3');pr=fixture()[0];job=store.reserve('one',pr,'Claude','review',Path(d))
            self.assertIsNone(store.reserve('one',pr,'Codex','fix',Path(d)))
            store.db.execute("UPDATE jobs SET status='done',result=? WHERE id=?",(json.dumps(fixture()[2]['result']),job))
            self.assertIsNone(store.reserve('one',pr,'Claude','review',Path(d)))
            pr['headRefOid']='new';self.assertIsNotNone(store.reserve('one',pr,'Claude','review',Path(d)))

    def test_cli_wrapper_records_success_and_failure_across_daemon_restarts(self):
        import subprocess,sys
        worker=Path(c.__file__).with_name('worker.py')
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);(directory/'prompt.txt').write_text('test')
            for code in (0,7):
                (directory/'command.json').write_text(json.dumps([sys.executable,'-c',f'import sys; sys.exit({code})']))
                result=subprocess.run([sys.executable,str(worker),d])
                self.assertEqual(result.returncode,code);self.assertEqual(json.loads((directory/'exit.json').read_text())['returncode'],code)
                self.assertIn('started',json.loads((directory/'process.json').read_text()))


if __name__=='__main__':unittest.main()

class IntegrationRaces(unittest.TestCase):
    def coordinator(self,d):
        return c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(d))

    def test_changed_base_stops_before_merge_or_push(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator=self.coordinator(d);pr,owner,review=fixture();pr.update(baseRefName='main',title='test')
            with patch.object(c,'run',side_effect=['','changed']) as command:
                with self.assertRaisesRegex(RuntimeError,'base changed'):
                    coordinator.merge({'id':'test','root':d},pr,owner,review)
                self.assertFalse(any('push' in call.args[0] for call in command.call_args_list))

    def test_feedback_changed_stops_before_preparing_merge(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator=self.coordinator(d);pr,owner,review=fixture();pr.update(baseRefName='main',title='test')
            changed=copy.deepcopy(pr);changed['comments']=[{'id':'new','body':'blocker','updatedAt':'now'}]
            with patch.object(c,'run',side_effect=['',pr['baseRefOid']]) as command,patch.object(coordinator,'pr',return_value=changed):
                with self.assertRaisesRegex(RuntimeError,'PR changed'):
                    coordinator.merge({'id':'test','root':d},pr,owner,review)
                self.assertFalse(any('merge' in call.args[0] or 'push' in call.args[0] for call in command.call_args_list))

    def test_dirty_owner_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator=self.coordinator(d);pr,owner,review=fixture();owner.update(key='T1',token='local',worktree=d)
            with patch.object(coordinator,'ticket',return_value=json.dumps(owner)) as ticket,patch.object(c,'run',return_value='dirty'):
                with self.assertRaisesRegex(RuntimeError,'dirty'):
                    coordinator.launch({'id':'test'},pr,owner,'fix')
                self.assertEqual(ticket.call_count,1)
                self.assertNotIn('resume',ticket.call_args.args)

class RetryTests(unittest.TestCase):
    def test_backoff_persists_and_does_not_duplicate_worker(self):
        with tempfile.TemporaryDirectory() as d:
            store=c.Store(Path(d)/'state.sqlite3');pr=fixture()[0]
            job=store.reserve('one',pr,'Claude','review',Path(d))
            store.db.execute("UPDATE jobs SET status='retry',retry_after=? WHERE id=?",(c.time.time()+3600,job))
            self.assertIsNone(store.reserve('one',pr,'Claude','review',Path(d)))
            store.db.execute("UPDATE jobs SET retry_after=0 WHERE id=?",(job,))
            self.assertEqual(store.reserve('one',pr,'Claude','review',Path(d)),job)
            self.assertIsNone(store.reserve('one',pr,'Claude','review',Path(d)))
