import copy
import json
import sqlite3
import types
import unittest
from unittest.mock import Mock
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from batch_integration import ObsoleteBatchCI


class ObsoleteBatchCITests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.now=2000
        self.project=dict(id='suifleet',repository='tyler-suifleet/suifleet')
        self.batch=dict(id='abc',pr=12,head='a'*40,branch='integration/batch/abc',status='invalidated',updated=1000)
        repo=dict(full_name=self.project['repository'])
        self.pr=dict(number=12,state='closed',merged_at=None,head=dict(sha='a'*40,ref=self.batch['branch'],repo=repo),base=dict(repo=repo))
        self.run=dict(id=42,event='pull_request',head_branch=self.batch['branch'],head_sha='a'*40,
                      repository=repo,head_repository=repo,path='.github/workflows/ci.yml',status='in_progress')
        self.calls=[];self.detail=None;self.cancel_error=False
        self.engine=ObsoleteBatchCI(types.SimpleNamespace(store=types.SimpleNamespace(db=self.db),gh=self.gh),lambda:self.now)

    def gh(self, project, args):
        self.calls.append(args)
        if '--paginate' in args:return json.dumps([dict(workflow_runs=[self.run])])
        path=args[1]
        if '/pulls/' in path:return json.dumps(self.pr)
        if path.endswith('/cancel'):
            if self.cancel_error:raise RuntimeError('private network diagnostics')
            return ''
        return json.dumps(self.detail or self.run)

    def tick(self):self.engine.tick(self.project,[self.batch])
    def canceled(self):return [a for a in self.calls if a[1].endswith('/cancel')]
    def state(self):return self.db.execute('select status from obsolete_batch_ci').fetchone()[0]

    def test_cancels_exact_closed_candidate_and_confirms_on_later_scan(self):
        self.tick();self.assertEqual(len(self.canceled()),1);self.assertEqual(self.state(),'retry')
        self.run['status']='completed';self.now+=61;self.tick();self.assertEqual(self.state(),'settled')
        receipt=json.loads(self.db.execute('select receipt from obsolete_batch_ci').fetchone()[0])
        self.assertEqual(receipt['runs'],[dict(run=42,cancel_requested=True)])
        count=len(self.calls);self.now+=10000;self.tick();self.assertEqual(len(self.calls),count)

    def test_queued_runs_are_canceled(self):
        self.run['status']='queued';self.tick();self.assertEqual(len(self.canceled()),1)

    def test_protected_unknown_foreign_and_stale_runs_untouched(self):
        original=copy.deepcopy(self.run)
        changes=[('path','.github/workflows/testnet.yml'),('path','.github/workflows/live-gates.yml'),
                 ('path','.github/workflows/release.yml'),('path','unknown.yml'),('event','push'),
                 ('head_sha','b'*40),('head_branch','main'),('repository',dict(full_name='fork/repo')),
                 ('head_repository',dict(full_name='fork/repo')),('status','completed'),('id','42')]
        for field,value in changes:
            with self.subTest(field=field,value=value):
                self.db.execute('delete from obsolete_batch_ci');self.calls=[];self.run=copy.deepcopy(original);self.run[field]=value
                self.tick();self.assertFalse(self.canceled())

    def test_open_merged_changed_or_foreign_candidate_untouched(self):
        original=copy.deepcopy(self.pr)
        for change in ['open','merged','head','branch','fork','base','number']:
            with self.subTest(change=change):
                self.db.execute('delete from obsolete_batch_ci');self.calls=[];self.pr=copy.deepcopy(original)
                if change=='open':self.pr['state']='open'
                elif change=='merged':self.pr['merged_at']='date'
                elif change=='head':self.pr['head']['sha']='b'*40
                elif change=='branch':self.pr['head']['ref']='main'
                elif change=='fork':self.pr['head']['repo']['full_name']='fork/repo'
                elif change=='base':self.pr['base']['repo']['full_name']='fork/repo'
                else:self.pr['number']=99
                self.tick();self.assertFalse(self.canceled())

    def test_reopen_race_and_fresh_run_identity_change_are_retained(self):
        old=self.engine.c.gh
        def reopen(project,args):
            if '/actions/runs/42' in args[1]:self.pr['state']='open'
            return old(project,args)
        self.engine.c.gh=reopen;self.tick();self.assertFalse(self.canceled())
        self.engine.c.gh=old;self.pr['state']='closed';self.now+=61
        self.detail=dict(self.run,head_sha='b'*40);self.tick();self.assertFalse(self.canceled())

    def test_transient_cancel_failure_is_retried_without_exposing_message(self):
        self.cancel_error=True;self.tick();self.assertEqual(self.state(),'retry')
        receipt=self.db.execute('select receipt from obsolete_batch_ci').fetchone()[0]
        self.assertNotIn('private network',receipt)
        self.cancel_error=False;self.now+=61;self.tick();self.assertEqual(len(self.canceled()),2)

    def test_transport_failure_isolated_and_bounded(self):
        self.engine.retire=Mock(side_effect=RuntimeError('private transport'))
        batches=[dict(self.batch,id=str(i),branch='integration/batch/'+str(i)) for i in range(7)]
        self.engine.tick(self.project,batches);self.assertEqual(self.engine.retire.call_count,4)
        self.engine.tick(self.project,batches);self.assertEqual(self.engine.retire.call_count,7)
        self.engine.tick(self.project,batches);self.assertEqual(self.engine.retire.call_count,7)

    def test_delayed_run_delivery_stays_pollable(self):
        self.run['status']='completed';self.batch['updated']=self.now-10
        self.tick();self.assertEqual(self.state(),'retry')
        self.run['status']='queued';self.now+=61;self.tick();self.assertEqual(len(self.canceled()),1)

    def test_failed_active_and_other_repositories_never_canceled(self):
        for status in ['failed','awaiting_ci','done','published']:
            self.batch['status']=status;self.tick()
        self.batch['status']='invalidated';self.project['repository']='another/repo';self.tick();self.assertFalse(self.calls)

    def test_all_paginated_runs_considered_and_one_failure_does_not_stop_next(self):
        old=self.engine.c.gh
        def paged(project,args):
            if '--paginate' in args:return json.dumps([dict(workflow_runs=[self.run]),dict(workflow_runs=[dict(self.run,id=43)])])
            if args[1].endswith('/42/cancel'):raise RuntimeError('transient')
            if args[1].endswith('/43'):return json.dumps(dict(self.run,id=43))
            return old(project,args)
        self.engine.c.gh=paged;self.tick();self.assertEqual([a[1] for a in self.canceled()],['repos/tyler-suifleet/suifleet/actions/runs/43/cancel'])

