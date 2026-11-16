"""Already-Done closure continues its original authenticated acceptance checkpoint."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

import test_included_sources as source_fixtures
from batch_integration import LocalInfrastructure


def retain_fixture_checkpoint(f, token):
    """Supply the immutable review/claim/completion records a real Store retains."""
    owner=f.owners[0]
    receipt=json.loads(f.db.execute('SELECT receipt FROM batch_included_sources ORDER BY created DESC LIMIT 1').fetchone()[0])
    review=f.approvals[1];snapshot=copy.deepcopy(f.launched[0][0])
    snapshot['ticket']={k:owner.get(k) for k in ('key','agent','model','spec')}
    directory=f.state/'accepted-review';directory.mkdir(exist_ok=True)
    (directory/'snapshot.json').write_text(json.dumps(snapshot))
    columns={r[1] for r in f.db.execute('PRAGMA table_info(jobs)')}
    for name,kind in [('id','INTEGER'),('head','TEXT'),('family','TEXT'),('feedback','TEXT'),
                      ('result','TEXT'),('directory','TEXT'),('publication_done','INTEGER')]:
        if name not in columns:f.db.execute('ALTER TABLE jobs ADD COLUMN '+name+' '+kind)
    f.db.execute('INSERT INTO jobs(id,project,pr,head,kind,status,family,feedback,result,directory,publication_done) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (review['id'],'test',1,snapshot['headRefOid'],'review','done',review['family'],review['feedback'],
         json.dumps(review['result']),str(directory),1))
    owner.update(revision=2,published_revision=2,events=[
        dict(kind='claim',actor=owner['session'],ticket='test::'+owner['key'],detail='token='+token),
        dict(kind='complete',actor=review['family']+'/'+review['result']['model'],ticket='test::'+owner['key'],
             detail=receipt['evidence']+'\nMerged commit: '+snapshot['headRefOid'])])
    f.db.row_factory=sqlite3.Row
    return receipt


class CompletedCheckpointContinuation(unittest.TestCase):
    def setUp(self):
        self.original=source_fixtures.IncludedSourcesTests();self.original.setUp();self.addCleanup(self.original.doCleanups)
        f=self.f=self.original.f;self.pr=self.original.prepare_acceptance();owner=f.owners[0]
        self.token=owner['token'];self.completions=[];self.posts=[];self.fail_issue=False
        old=f.ticket
        def ticket(project,command,key,*args):
            if command=='complete':
                self.completions.append(key);owner.update(status='done',token=None,publish_pending=True);return '{}'
            if command!='show':raise AssertionError('metadata continuation cannot mutate claim')
            return old(project,command,key,*args)
        f.ticket=ticket;f.pr=lambda project,number:copy.deepcopy(f.source_prs[number])
        f.issue_number=lambda *args:246
        def gh(project,args):
            if '--method' in args:
                self.posts.append(args)
                if '/pulls/' in args[1]:f.source_prs[1]['state']='CLOSED'
                elif self.fail_issue:
                    self.fail_issue=False;raise RuntimeError('issue transport failed')
            return json.dumps({'state':'closed'})
        f.gh=gh
        with patch.object(LocalInfrastructure,'fenced'):
            with self.assertRaisesRegex(RuntimeError,'mirror is pending'):
                f.controller.included_source(f.project,self.pr,owner)
        self.receipt=retain_fixture_checkpoint(f,self.token)
        self.base=self.receipt['main']
        owner['publish_pending']=False
        self.evidence=patch.object(f.controller,'included_checkpoint_evidence',side_effect=lambda project,head,base,proof:copy.deepcopy(proof))
        self.evidence.start();self.addCleanup(self.evidence.stop)

    def continue_done(self):
        return self.f.controller.included_source(self.f.project,copy.deepcopy(self.f.source_prs[1]),copy.deepcopy(self.f.owners[0]))

    def advance_main(self):
        f=self.f;f.git('checkout','-qb','later-checkpoint-main',self.base)
        f.git('commit','--allow-empty','-qm','later independently integrated work')
        tip=f.git('rev-parse','HEAD');f.git('push','-q','origin','HEAD:main');return tip

    def test_failed_mirror_continues_without_new_completion_review_or_approval(self):
        self.f.owners[0]['publish_pending']=True
        self.assertFalse(self.continue_done());self.assertEqual(self.posts,[])
        self.f.owners[0]['publish_pending']=False
        self.assertTrue(self.continue_done())
        self.assertEqual(self.completions,['T-1']);self.assertEqual(len(self.f.launched),1)
        self.assertEqual([p[p.index('--method')+1] for p in self.posts],['PATCH','PATCH'])
        self.assertEqual(self.f.owners[0]['status'],'done');self.assertIsNone(self.f.owners[0]['token'])

    def test_newer_same_byte_ci_ids_do_not_require_another_approval(self):
        with patch.object(self.f.controller,'included_evidence',side_effect=AssertionError('must not select latest run IDs')):
            self.assertTrue(self.continue_done())
        self.assertEqual(len(self.f.launched),1)

    def test_main_can_advance_after_completed_acceptance(self):
        tip=self.advance_main();self.assertNotEqual(tip,self.base)
        self.assertTrue(self.continue_done())
        row=self.f.db.execute('SELECT base,receipt FROM batch_included_sources ORDER BY created DESC LIMIT 1').fetchone()
        value=json.loads(row[1]);self.assertEqual(row[0],self.base)
        self.assertEqual(value['completion_continuation']['main'],tip)
        self.assertEqual(value['status'],'included-source-closed')

    def test_closed_pr_retries_issue_only_even_after_main_advance(self):
        self.fail_issue=True
        with self.assertRaisesRegex(RuntimeError,'issue transport'):self.continue_done()
        self.advance_main();self.assertTrue(self.continue_done())
        self.assertEqual(sum('/pulls/' in p[1] for p in self.posts),1)
        self.assertEqual(self.completions,['T-1'])

    def test_changed_source_owner_spec_claim_and_feedback_are_blocked(self):
        for change in ('head','owner','spec','claim','feedback','security','request'):
            with self.subTest(change=change):
                source=copy.deepcopy(self.f.source_prs[1]);owner=copy.deepcopy(self.f.owners[0])
                if change=='head':self.f.source_prs[1]['headRefOid']=self.f.heads[1]
                if change=='owner':self.f.owners[0]['session']='other-session'
                if change=='spec':self.f.owners[0]['spec']={'changed':True}
                if change=='claim':self.f.owners[0]['events'][0]['detail']='token=other'
                if change=='feedback':self.f.source_prs[1]['comments'].append(dict(id='new',body='new concern',updatedAt='now'))
                if change=='security':self.f.source_prs[1]['statusCheckRollup'][0]['conclusion']='FAILURE'
                if change=='request':self.f.source_prs[1]['threads'].append(dict(isResolved=False))
                try:
                    result=self.continue_done();self.assertFalse(result)
                except (RuntimeError,subprocess.CalledProcessError):pass
                self.assertEqual(self.posts,[])
                self.f.source_prs[1]=source;self.f.owners[0]=owner

    def test_unknown_receipt_and_missing_published_review_are_blocked(self):
        original=self.f.db.execute('SELECT receipt FROM batch_included_sources ORDER BY created DESC LIMIT 1').fetchone()[0]
        altered=json.loads(original);altered['evidence']='altered'
        self.f.db.execute('UPDATE batch_included_sources SET receipt=?',(json.dumps(altered),))
        with self.assertRaisesRegex(RuntimeError,'completion evidence changed'):self.continue_done()
        self.f.db.execute('UPDATE batch_included_sources SET receipt=?',(original,))
        self.f.db.execute('UPDATE jobs SET publication_done=0')
        with self.assertRaisesRegex(RuntimeError,'review unavailable'):self.continue_done()
        self.assertEqual(self.posts,[])

    def test_unknown_checkpoint_and_changed_review_feedback_are_blocked(self):
        receipt=self.f.db.execute('SELECT receipt FROM batch_included_sources ORDER BY created DESC LIMIT 1').fetchone()[0]
        self.f.db.execute('DELETE FROM batch_included_sources')
        self.assertFalse(self.continue_done());self.assertEqual(self.posts,[])
        self.f.db.execute('INSERT INTO batch_included_sources VALUES(?,?,?,?,?,?)',
            ('test',1,self.pr['headRefOid'],self.base,receipt,0))
        self.f.db.execute("UPDATE jobs SET feedback='changed'")
        with self.assertRaisesRegex(RuntimeError,'review changed'):self.continue_done()
        self.assertEqual(self.posts,[])

    def test_main_or_owner_change_during_final_reads_keeps_metadata_open(self):
        with patch.object(self.f.controller,'main',side_effect=[self.base,'f'*40]):
            with self.assertRaisesRegex(RuntimeError,'changed before closure'):self.continue_done()
        self.assertEqual(self.posts,[])
        original=self.f.pr;calls=[]
        def changed(project,number):
            calls.append(1)
            if len(calls)>1:self.f.owners[0]['revision']+=1
            return original(project,number)
        self.f.pr=changed
        with self.assertRaisesRegex(RuntimeError,'source/owner changed'):self.continue_done()
        self.assertEqual(self.posts,[])

    def test_nonancestor_main_and_new_structured_source_finding_are_blocked(self):
        self.f.git('update-ref','refs/heads/main',self.f.base,cwd=self.f.remote)
        with self.assertRaises(subprocess.CalledProcessError):self.continue_done()
        self.f.git('update-ref','refs/heads/main',self.base,cwd=self.f.remote)
        self.f.db.execute("INSERT INTO jobs(id,project,pr,head,kind,status,result) VALUES(618,'test',1,?,'review','done',?)",
            (self.pr['headRefOid'],json.dumps(dict(verdict='changes_requested'))))
        with self.assertRaisesRegex(RuntimeError,'newer source finding'):self.continue_done()
        self.assertEqual(self.posts,[])


class HistoricalCheckpointEvidence(unittest.TestCase):
    def setUp(self):
        self.original=source_fixtures.IncludedMainEvidenceTests();self.original.setUp();self.addCleanup(self.original.doCleanups)
        o=self.original;self.expected=o.proof();self.f=o.f;self.reads=[]
        old=self.f.gh
        def gh(project,args):
            if args[:2]==['run','download']:return old(project,args)
            path=args[1].split('repos/owner/repo/')[1];self.reads.append(path)
            if path=='actions/runs/123/attempts/1':return json.dumps(o.ci)
            if path=='actions/runs/456/attempts/1':return json.dumps(o.testnet)
            if path=='actions/runs/123/attempts/1/jobs?per_page=100':return json.dumps(o.jobs)
            if path=='commits/'+o.base:return old(project,args)
            raise AssertionError('must not inspect latest producer IDs: '+path)
        self.f.gh=gh

    def proof(self):
        o=self.original
        return self.f.controller.included_checkpoint_evidence(self.f.project,self.f.heads[0],o.base,self.expected)

    def test_original_successful_attempts_and_effects_are_authenticated(self):
        self.assertEqual(self.proof(),self.expected)
        self.assertTrue(any('/attempts/1' in p for p in self.reads))
        self.assertFalse(any('/workflows/' in p or p.endswith('/status') for p in self.reads))

    def test_wrong_attempt_failed_security_and_changed_effects_fail_closed(self):
        o=self.original
        for target,key,value in [('ci','run_attempt',2),('ci','conclusion','failure'),
                                 ('testnet','event','push'),('testnet','head_sha','a'*40),
                                 ('ci','path','.github/workflows/other.yml'),
                                 ('ci','repository',dict(full_name='outsider/repo')),
                                 ('testnet','run_attempt',2),
                                 ('testnet','head_repository',dict(full_name='outsider/repo')),
                                 ('effects','chain','mainnet'),('effects','success',False)]:
            with self.subTest(target=target,key=key):
                obj=getattr(o,target);before=copy.deepcopy(obj);obj[key]=value
                with self.assertRaises(RuntimeError):self.proof()
                obj.clear();obj.update(before)
        o.jobs['jobs'][0]['conclusion']='failure'
        with self.assertRaisesRegex(RuntimeError,'CI/security'):self.proof()

    def test_malformed_checkpoint_or_changed_effect_hash_is_rejected(self):
        self.expected['ci_attempt']=0
        with self.assertRaisesRegex(RuntimeError,'malformed'):self.proof()
        self.expected['ci_attempt']=1;self.expected['effects_sha256']='0'*64
        with self.assertRaisesRegex(RuntimeError,'effects changed'):self.proof()
