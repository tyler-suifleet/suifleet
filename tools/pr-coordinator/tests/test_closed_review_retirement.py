"""Obsolete review jobs require stopped wrapper/helpers and exact closed batch."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_coordinator import c

class ClosedReviewRetirement(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.project=dict(id='test',repository='owner/repo',root=str(self.root))
        self.engine=c.Coordinator(dict(workers={'Codex':['true'],'Claude':['true']},projects=[self.project]),self.root)
        self.addCleanup(self.engine.store.db.close)
        self.db=self.engine.store.db
        self.directory=self.root/'job';self.directory.mkdir()
        self.identity=dict(pid=99999999,started='1')
        (self.directory/'process.json').write_text(json.dumps(self.identity))
        (self.directory/'exit.json').write_text(json.dumps({'returncode':1}))
        self.db.execute("INSERT INTO jobs(id,project,pr,family,kind,head,status,directory,pid,started) VALUES(1,'test',687,'Claude','review',?,'retry',?,?,?)",('a'*40,str(self.directory),99999999,'1'))
        self.db.execute("INSERT INTO integration_batches(project,id,pr,head,branch,status) VALUES('test','batch',687,?,'integration/batch/test','invalidated')",('a'*40,))
        self.remote=dict(state='closed',head=dict(sha='a'*40,ref='integration/batch/test',repo=dict(full_name='owner/repo')))
        self.reads=[]
        def gh(project,args):
            self.assertEqual(args,['api','repos/owner/repo/pulls/687'])
            self.reads.append(args);return json.dumps(self.remote)
        self.engine.gh=gh
        stopped=patch.object(c,'worker_stopped',return_value=True);self.stopped=stopped.start();self.addCleanup(stopped.stop)

    def status(self):return self.db.execute('SELECT status FROM jobs').fetchone()[0]
    def run_retirement(self):self.engine.retire_closed_review_retries(self.project)

    def test_exact_closed_invalidated_stopped_retry_is_retired_once(self):
        self.run_retirement()
        self.assertEqual(self.status(),'superseded')
        self.assertEqual(self.db.execute('SELECT count(*) FROM review_retirements').fetchone()[0],1)
        self.assertEqual(len(self.reads),2)
        self.run_retirement();self.assertEqual(len(self.reads),2)

    def test_live_wrapper_or_helpers_prevent_retirement(self):
        self.stopped.return_value=False;self.run_retirement()
        self.assertEqual(self.status(),'retry');self.assertEqual(self.reads,[])

    def test_unknown_exit_or_process_identity_is_retained(self):
        for code in (0,124,-15,'1',None):
            with self.subTest(code=code):
                (self.directory/'exit.json').write_text(json.dumps({'returncode':code}))
                self.run_retirement();self.assertEqual(self.status(),'retry')
        (self.directory/'exit.json').write_text(json.dumps({'returncode':1}))
        (self.directory/'process.json').write_text(json.dumps({'pid':123,'started':'other'}))
        self.run_retirement();self.assertEqual(self.status(),'retry')

    def test_open_changed_head_branch_or_fork_is_retained(self):
        original=copy.deepcopy(self.remote)
        for change in ('state','head','branch','repository'):
            with self.subTest(change=change):
                self.remote=copy.deepcopy(original)
                if change=='state':self.remote['state']='open'
                if change=='head':self.remote['head']['sha']='b'*40
                if change=='branch':self.remote['head']['ref']='different'
                if change=='repository':self.remote['head']['repo']['full_name']='fork/repo'
                self.run_retirement();self.assertEqual(self.status(),'retry')

    def test_nonterminal_batch_and_pending_launch_are_retained(self):
        self.db.execute("UPDATE integration_batches SET status='awaiting_review'")
        self.run_retirement();self.assertEqual(self.status(),'retry')
        self.db.execute("UPDATE integration_batches SET status='invalidated'")
        self.db.execute("UPDATE jobs SET launch_intent='pending'")
        self.run_retirement();self.assertEqual(self.status(),'retry')

    def test_reopened_remote_or_helper_race_never_retires(self):
        old=self.engine.gh
        def reopened(project,args):
            if self.reads:self.remote['state']='open'
            return old(project,args)
        self.engine.gh=reopened
        self.run_retirement();self.assertEqual(self.status(),'retry')
        self.remote['state']='closed';self.engine.gh=old
        self.stopped.side_effect=[True,False]
        self.run_retirement();self.assertEqual(self.status(),'retry')

    def test_remote_failure_isolated_without_altering_job(self):
        def unavailable(*args):raise RuntimeError('private transport detail')
        self.engine.gh=unavailable
        with patch('builtins.print') as logged:self.run_retirement()
        self.assertEqual(self.status(),'retry')
        self.assertNotIn('private transport detail',str(logged.call_args_list))
