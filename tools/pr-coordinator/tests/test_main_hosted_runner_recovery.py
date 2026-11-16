"""Current-main capacity recovery needs retained, independently accepted source."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import coordinator
from test_coordinator import fixture
from test_hosted_runner_recovery import Fixture


class MainFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.tmp = tempfile.TemporaryDirectory()
        self.source, self.owner, _ = fixture()
        self.source.update(number=5, headRefOid='b'*40, headRefName='work/source',
            baseRefName='work/parent', baseRefOid='c'*40, comparisonBaseOid='d'*40,
            isCrossRepository=False)
        self.owner.update(key='SF-246', status='done', token=None, agent='Codex', model='test-model',
            session='original-session', branch='work/source', spec={'key':'SF-246'}, publish_pending=False)
        self.identity = dict(head='b'*40, branch='work/source', base_ref='work/parent', base='c'*40,
            comparison='d'*40, claim_hash='e'*64,
            owner={k:self.owner.get(k) for k in ('key','agent','model','session','branch')})
        self.snapshot = {**copy.deepcopy(self.source), 'baseRefName':'main', 'baseRefOid':'a'*40,
            '_review_mode':'included', '_included_source':self.identity,
            '_included_evidence':dict(source='b'*40,head='a'*40,base='f'*40,effects_sha256='e'*64),
            'ticket':{k:self.owner.get(k) for k in ('key','agent','model','spec')}}
        self.directory = Path(self.tmp.name)
        (self.directory/'snapshot.json').write_text(json.dumps(self.snapshot))
        self.review = dict(id=10, head='b'*40, family='Claude', directory=str(self.directory),
            feedback=coordinator.feedback(self.snapshot), publication_pending=False,
            result=dict(verdict='approve',acceptance_complete=True,model='test-review',evidence='checked'))
        self.receipt = dict(main='a'*40, identity=self.identity, status='included-source-accepted',
            acceptance_complete=True, review=10, check_evidence=self.snapshot['_included_evidence'])
        self.db.execute('CREATE TABLE batch_included_sources(project TEXT,pr INTEGER,head TEXT,base TEXT,receipt TEXT,created REAL)')
        self.persist()
        self.main = 'a'*40
        self.run.update(event='workflow_dispatch', head_branch='main', pull_requests=[])
        self.job['html_url'] = 'https://github.com/owner/repo/actions/runs/100/job/200'
        self.store.review = lambda project, number: self.review
        self.controller = types.SimpleNamespace(store=self.store, gh=self.gh,
            ticket=lambda project, *args: json.dumps(self.owner),
            pr=lambda project, number: copy.deepcopy(self.source),
            source_receipt=lambda *args: {'verified':True},
            batches=types.SimpleNamespace(git=self.git))
        self.recovery.c = self.controller

    def persist(self):
        self.db.execute('DELETE FROM batch_included_sources')
        self.db.execute('INSERT INTO batch_included_sources VALUES(?,?,?,?,?,?)',
            ('test',5,'b'*40,'a'*40,json.dumps(self.receipt),0))

    def git(self, project, *args):
        if args != ('merge-base','--is-ancestor','b'*40,'a'*40): raise AssertionError(args)
        return ''

    def gh(self, project, args):
        path = args[1]
        if '/git/ref/heads/main' in path:
            return json.dumps({'object':{'sha':self.main}})
        if '/actions/workflows/ci.yml/runs?' in path:
            return json.dumps({'workflow_runs':[{'id':100}]})
        if '/attempts/' in path and '/jobs?' in path:
            return json.dumps({'total_count':1,'jobs':[self.job]})
        return super().gh(project,args)

    def tick(self):
        return self.recovery.included_main(self.project,self.source,self.owner,self.receipt)


class IncludedMainRunnerRecovery(unittest.TestCase):
    def make(self):
        f=MainFixture(); self.addCleanup(f.tmp.cleanup); self.addCleanup(f.db.close); return f

    def test_authentic_accepted_main_capacity_failure_retries_only_one_job(self):
        f=self.make()
        self.assertEqual(f.tick()['state'],'retry_submitted')
        self.assertEqual(f.posts,['repos/owner/repo/actions/jobs/200/rerun'])
        self.assertEqual(f.tick()['state'],'submitted')
        self.assertEqual(len(f.posts),1)
        self.assertEqual(f.owner['status'],'done')

    def test_changed_main_or_owner_or_source_or_review_prevents_retry(self):
        for change in ('main','owner','source','spec','review','publication'):
            with self.subTest(change=change):
                f=self.make()
                if change=='main': f.main='f'*40
                if change=='owner': f.owner['session']='other'
                if change=='source': f.source['headRefOid']='f'*40
                if change=='spec': f.owner['spec']={'changed':True}
                if change=='review': f.review['family']='Codex'
                if change=='publication': f.owner['publish_pending']=True
                self.assertEqual(f.tick()['state'],'included_main_authority_changed')
                self.assertEqual(f.posts,[])

    def test_missing_or_altered_retained_acceptance_does_not_authorize(self):
        f=self.make()
        f.db.execute('DELETE FROM batch_included_sources')
        self.assertEqual(f.tick()['state'],'included_main_authority_changed')
        f.receipt['acceptance_complete']=False; f.persist()
        self.assertEqual(f.tick()['state'],'included_main_authority_changed')
        self.assertEqual(f.posts,[])

    def test_workflow_identity_and_arbitrary_cancellation_are_not_retried(self):
        for change in ('event','path','branch','steps','annotation'):
            with self.subTest(change=change):
                f=self.make()
                if change=='event': f.run['event']='pull_request'
                if change=='path': f.run['path']='.github/workflows/other.yml'
                if change=='branch': f.run['head_branch']='other'
                if change=='steps': f.job['steps']=[{'name':'real failed test'}]
                if change=='annotation': f.annotations=[]
                f.tick();self.assertEqual(f.posts,[])

    def test_main_retry_budget_is_shared_across_included_source_prs(self):
        f=self.make()
        for i in range(3):
            f.db.execute('INSERT INTO hosted_runner_retries VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('test',900+i,'a'*40,i+1,i+1,1,'observed',0,0,'proof'))
        self.assertEqual(f.tick()['state'],'exhausted')
        self.assertEqual(f.posts,[])

    def test_main_moves_after_discovery_before_post(self):
        f=self.make(); original=f.recovery.main_authority; count=[]
        def changing(project,pr):
            count.append(1)
            if len(count)>1:f.main='f'*40
            return original(project,pr)
        f.recovery.main_authority=changing
        self.assertEqual(f.tick()['state'],'changed_before_retry')
        self.assertEqual(f.posts,[])

    def test_uncertain_delivery_retains_intent_and_does_not_retry(self):
        import subprocess
        f=self.make();f.error=subprocess.TimeoutExpired('gh',10)
        self.assertEqual(f.tick()['state'],'uncertain')
        f.now+=3600
        self.assertEqual(f.tick()['state'],'uncertain')
        self.assertEqual(len(f.posts),1)
