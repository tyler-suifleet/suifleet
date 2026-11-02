"""Shared cost repair ownership with real candidate manifests, no remote writes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from test_batch_integration import Fixture
from batch_integration import SharedAcceptanceRepair, canonical
from coordinator import feedback, source_feedback

CHECK=dict(name='testnet/package-upgrade',workflowName='Automatic testnet',classification='funding',
           conclusion='FAILURE',detailsUrl='https://github.com/owner/repo/actions/runs/1')

class SharedRepairs(unittest.TestCase):
    def fixture(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        f=Fixture(Path(tmp.name));self.addCleanup(f.db.close)
        f.source_prs.pop(2);f.receipts.pop(2);f.owners=f.owners[:1]
        f.tick();batch=f.row()
        f.controller.update(batch,status='failed',error=canonical(dict(kind='candidate_checks_failed',checks=[CHECK])))
        f.batches=f.controller;f.project['shared_acceptance_repair']=dict(ticket='T-1',max_attempts=3)
        f.db.execute('DROP TABLE jobs')
        f.db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,project TEXT,pr INTEGER,kind TEXT,status TEXT,head TEXT,feedback TEXT)')
        f.local_infrastructure=SimpleNamespace(fenced=Mock(side_effect=lambda project,pr,owner:f.assert_review_owner(project,owner)))
        f.issue_number=lambda project,owner: 1
        original_gh=f.gh
        def gh(project,args):
            if args[:2]==['pr','list']:return json.dumps([{k:v for k,v in f.source_prs[1].items() if k in
                ('number','headRefName','headRefOid','baseRefName','isDraft','isCrossRepository','state')}])
            return original_gh(project,args)
        f.gh=gh
        def launch(project,pr,owner,kind):
            f.launched.append((copy.deepcopy(pr),copy.deepcopy(owner),kind))
            f.db.execute('INSERT INTO jobs(project,pr,kind,status,head,feedback) VALUES(?,?,?,?,?,?)',
                         ('test',pr['number'],kind,'running',pr['headRefOid'],feedback(pr)))
        f.launched=[];f.launch=launch
        return f,SharedAcceptanceRepair(f),batch

    def test_inactive_claims_skip_all_source_reads(self):
        for change in ({'status':'blocked'}, {'status':'in_progress'}, {'expired':True},
                       {'publish_pending':True}, {'token':None}):
            with self.subTest(change=change):
                f,r,b=self.fixture();f.owners[0].update(change)
                f.gh=Mock(side_effect=AssertionError('source read'))
                f.pr=Mock(side_effect=AssertionError('full hydration'))
                self.assertEqual(r.tick(f.project)['state'],'blocked')
                f.pr.assert_not_called();f.gh.assert_not_called()

    def test_cheap_holds_never_hydrate_ci(self):
        for case in ('draft','missing_draft','foreign','bad_head','active','unknown_job',
                     'candidate','no_failure','retained','capacity','exhausted'):
            with self.subTest(case=case):
                f,r,b=self.fixture()
                if case=='draft':f.source_prs[1]['isDraft']=True
                elif case=='missing_draft':f.source_prs[1].pop('isDraft')
                elif case=='foreign':f.source_prs[1]['isCrossRepository']=True
                elif case=='bad_head':f.source_prs[1]['headRefOid']='bad'
                elif case in ('active','unknown_job'):
                    f.db.execute('INSERT INTO jobs VALUES(1,?,?,?,?,?,?)',
                        ('test',1,'fix','running' if case=='active' else 'orphaned',b['head'],'proof'))
                elif case=='candidate':f.controller.update(b,status='awaiting_ci')
                elif case=='no_failure':f.controller.update(b,error='{}')
                elif case in ('retained','capacity'):
                    r.tick(f.project)
                    f.db.execute("UPDATE jobs SET status='done'")
                    if case=='capacity':f.db.execute("UPDATE shared_acceptance_repairs SET job=NULL,state='held'")
                elif case=='exhausted':
                    for n in range(3):r.record(f.project,str(n),'T-1',f.heads[0],b['base'],None,'held','old')
                f.pr=Mock(side_effect=AssertionError('full hydration'))
                result=r.tick(f.project)
                self.assertIn(result['state'],('blocked','owner_busy','awaiting_validation','held'))
                f.pr.assert_not_called()

    def test_ambiguous_or_missing_sources_skip_hydration(self):
        for sources in ([], [dict(number=1),dict(number=2)], [None], [{}]):
            with self.subTest(sources=sources):
                f,r,b=self.fixture();f.gh=lambda *args:json.dumps(sources)
                f.pr=Mock(side_effect=AssertionError('full hydration'))
                self.assertEqual(r.tick(f.project)['state'],'blocked')
                f.pr.assert_not_called();self.assertEqual(f.launched,[])

    def test_hydration_must_match_cheap_source_and_fresh_claim(self):
        for field,value in (('headRefOid','a'*40),('isDraft',True),('isCrossRepository',True),
                            ('baseRefName','other'),('state','CLOSED'),('number',2),('isDraft',None)):
            with self.subTest(field=field,value=value):
                f,r,b=self.fixture();original=f.pr
                def moved(project,number):
                    pr=original(project,number);pr[field]=value;return pr
                f.pr=Mock(side_effect=moved)
                self.assertEqual(r.tick(f.project)['state'],'held')
                f.pr.assert_called_once();self.assertEqual(f.launched,[])
        f,r,b=self.fixture();original=f.pr
        def revoked(project,number):
            pr=original(project,number);f.owners[0]['status']='blocked';return pr
        f.pr=revoked
        def assert_current(project,owner):
            current=json.loads(f.ticket(project,'show',owner['key']))
            if current['status']!='review' or current['token']!=owner['token']:
                raise RuntimeError('review ownership changed')
        f.assert_review_owner=assert_current
        with self.assertRaises(RuntimeError):r.tick(f.project)
        self.assertEqual(f.launched,[])

    def test_one_real_shared_owner_despite_multiple_victims(self):
        f,r,b=self.fixture();f.pr=Mock(wraps=f.pr);result=r.tick(f.project)
        f.pr.assert_called_once_with(f.project,1)
        self.assertEqual(result['state'],'repair_owned');self.assertEqual(len(f.launched),1)
        pr,owner,kind=f.launched[0]
        self.assertEqual((owner['key'],kind),('T-1','fix'))
        self.assertEqual(pr['_batch_failure']['candidate_head'],b['head'])
        self.assertEqual(r.tick(f.project)['state'],'owner_busy')
        self.assertEqual(len(f.launched),1)
        f.db.execute("UPDATE jobs SET status='done'")
        self.assertEqual(r.tick(f.project)['state'],'awaiting_validation')
        self.assertEqual(len(f.launched),1)

    def test_active_or_uncertain_job_and_claim_are_fenced(self):
        for state in ('running','orphaned','quarantined','quota_wait'):
            f,r,b=self.fixture()
            f.db.execute('INSERT INTO jobs VALUES(1,?,?,?,?,?,?)',('test',1,'fix',state,b['head'],'proof'))
            self.assertEqual(r.tick(f.project)['state'],'owner_busy');self.assertEqual(f.launched,[])
        f,r,b=self.fixture();f.owners[0]['status']='blocked'
        self.assertEqual(r.tick(f.project)['state'],'blocked');self.assertEqual(f.launched,[])
        f,r,b=self.fixture();f.owners[0]['publish_pending']=True
        self.assertEqual(r.tick(f.project)['state'],'blocked');self.assertEqual(f.launched,[])

    def test_stale_or_mixed_failure_does_not_trigger_shared_repair(self):
        for check in ({**CHECK,'classification':'unknown'}, {**CHECK,'name':'check'}, {**CHECK,'workflowName':'CI'}):
            f,r,b=self.fixture();f.controller.update(b,error=canonical(dict(kind='candidate_checks_failed',checks=[CHECK,check])))
            self.assertEqual(r.tick(f.project)['state'],'awaiting_validation');self.assertEqual(f.launched,[])
        f,r,b=self.fixture();f.source_prs[1]['headRefOid']='a'*40
        self.assertEqual(r.tick(f.project)['state'],'awaiting_validation');self.assertEqual(f.launched,[])

    def test_new_source_with_new_verified_failure_gets_owned_repair(self):
        f,r,b=self.fixture();r.tick(f.project);f.db.execute("UPDATE jobs SET status='done'")
        f.git('checkout','-q','work/1');(f.repo/'feature-1').write_text('source cost repair\n')
        f.git('add','.');f.git('commit','-qm','reduce source cost');head=f.git('rev-parse','HEAD')
        f.git('push','-q','origin','work/1');f.git('checkout','-q','main')
        f.source_prs[1]['headRefOid']=head
        f.receipts[1].update(head=head,source_feedback=source_feedback(f.source_prs[1]))
        f.tick();new=f.row();f.db.execute("UPDATE jobs SET status='done'")
        f.controller.update(new,status='failed',error=canonical(dict(kind='candidate_checks_failed',checks=[CHECK])))
        self.assertEqual(r.tick(f.project)['state'],'repair_owned')
        self.assertEqual(f.db.execute("SELECT count(*) FROM jobs WHERE kind='fix'").fetchone()[0],2)

    def test_attempt_bound_is_not_reset_by_new_victim_run(self):
        f,r,b=self.fixture()
        for n in range(3):r.record(f.project,str(n),'T-1',f.heads[0],str(n)*40,None,'held','previous incident')
        self.assertEqual(r.tick(f.project)['state'],'blocked');self.assertEqual(f.launched,[])

    def test_active_candidate_is_preserved(self):
        f,r,b=self.fixture();f.controller.update(b,status='awaiting_ci',error=None)
        self.assertEqual(r.tick(f.project)['state'],'awaiting_validation');self.assertEqual(f.launched,[])

    def test_uncertain_launch_is_never_replayed(self):
        f,r,b=self.fixture();calls=[]
        def fail(*args):calls.append(1);raise RuntimeError('unknown launch')
        f.launch=fail
        with self.assertRaises(RuntimeError):r.tick(f.project)
        self.assertEqual(r.tick(f.project)['state'],'launch_uncertain');self.assertEqual(calls,[1])

    def test_capacity_wait_without_a_job_retries_only_after_delay(self):
        f,r,b=self.fixture();launch=f.launch;f.launch=lambda *args:None
        self.assertEqual(r.tick(f.project)['state'],'held')
        f.launch=launch
        self.assertEqual(r.tick(f.project)['state'],'held');self.assertEqual(f.launched,[])
        f.db.execute('UPDATE shared_acceptance_repairs SET updated=updated-61')
        self.assertEqual(r.tick(f.project)['state'],'repair_owned');self.assertEqual(len(f.launched),1)

    def test_wrong_source_and_tampered_manifest_are_fenced(self):
        f,r,b=self.fixture();f.source_prs[1]['headRefName']='different'
        self.assertEqual(r.tick(f.project)['state'],'blocked');self.assertEqual(f.launched,[])
        f,r,b=self.fixture();f.controller.update(b,manifest_digest='0'*64)
        with self.assertRaisesRegex(RuntimeError,'manifest'):r.tick(f.project)
        self.assertEqual(f.launched,[])

    def test_unknown_helpers_or_dirty_worktree_cannot_launch(self):
        f,r,b=self.fixture();f.local_infrastructure.fenced.side_effect=RuntimeError('unknown helper')
        with self.assertRaises(RuntimeError):r.tick(f.project)
        self.assertEqual(f.launched,[])
        self.assertEqual(f.db.execute('SELECT state FROM shared_acceptance_status').fetchone()[0],'held')

    def test_config_is_explicit_and_bounded(self):
        f,r,b=self.fixture();f.project.pop('shared_acceptance_repair')
        self.assertIsNone(r.tick(f.project))
        for config in ({'ticket':'bad url'},{'ticket':'T-1','max_attempts':True},{'ticket':'T-1','max_attempts':6}):
            f.project['shared_acceptance_repair']=config
            with self.assertRaises(ValueError):r.tick(f.project)
        self.assertEqual(f.launched,[])

    def test_empty_collecting_candidate_releases_its_slot(self):
        f,r,b=self.fixture();f.controller.update(b,status='collecting',head=None,pr=None,manifest_digest=None)
        f.receipts.clear();f.tick()
        self.assertEqual(f.row()['status'],'invalidated')
        self.assertEqual(f.controller.reserved_members(f.project),set())


class SharedStatus(unittest.TestCase):
    def test_cli_projects_safe_shared_status_and_filters_projects(self):
        import sqlite3
        from test_dashboard import Fixture as DashboardFixture, NOW, dash
        with tempfile.TemporaryDirectory() as directory:
            f=DashboardFixture(Path(directory))
            with sqlite3.connect(f.paths['reviews']) as db:
                db.execute('CREATE TABLE integration_batches(project,id,status,base,head,pr,members,updated)')
                db.execute('CREATE TABLE shared_acceptance_status(project,owner_ticket,state,job,diagnostic,updated)')
                db.execute('CREATE TABLE shared_acceptance_repairs(private_claim)')
                db.execute('INSERT INTO shared_acceptance_repairs VALUES(?)',('PRIVATE-CLAIM-SECRET',))
                db.executemany('INSERT INTO shared_acceptance_status VALUES(?,?,?,?,?,?)',[
                    ('suifleet','SF-221','awaiting_validation',17,'new source awaits validation',NOW),
                    ('vortech','VT-1','held',None,'other project',NOW)])
            model=f.collect(project='suifleet');output='\n'.join(dash.render(model,200))
            self.assertIn('SHARED ACCEPTANCE REPAIRS',output)
            self.assertIn('SF-221: awaiting validation',output)
            self.assertNotIn('VT-1',output)
            self.assertNotIn('PRIVATE-CLAIM-SECRET',json.dumps(model))
