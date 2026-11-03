"""Funding-only restoration retains failures and re-enters normal candidate gates."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
import test_shared_acceptance_repair as shared
from coordinator import failed_checks
from batch_integration import canonical


class FundingRecovery(unittest.TestCase):
    fixture = shared.SharedRepairs.fixture
    # Reuse the real Git candidate/Review fixture, not the inherited tests.
    def ready(self):
        f,r,b = self.fixture()
        f.owners.append(dict(key='T-9',status='done'))
        f.project['shared_acceptance_repair']['ticket']='T-9'
        candidate=f.candidate_prs[b['pr']]
        candidate.update(state='CLOSED',_testnet_status=dict(state='failure',
            description='base:'+b['base']+' class:funding',
            target_url='https://github.com/owner/repo/actions/runs/1'))
        f.controller.update(b,error=canonical(dict(kind='candidate_checks_failed',checks=failed_checks(candidate))))
        f.controller.clock=lambda: b['updated']+3601
        r.funding_receipt=Mock(return_value=dict(run='1',sha256='a'*64))
        return f,r,b

    def test_completed_owner_restores_selection_without_changing_failure(self):
        f,r,b=self.ready();before=copy.deepcopy(f.row())
        self.assertEqual(f.controller.select(f.project,json.loads(b['members']),b['base'],1),[])
        self.assertEqual(r.tick(f.project)['state'],'awaiting_validation')
        self.assertEqual(f.row(),before)
        self.assertEqual(len(f.controller.select(f.project,json.loads(b['members']),b['base'],1)),1)
        self.assertEqual(r.tick(f.project)['state'],'resolved')
        r.funding_receipt.assert_called_once();self.assertEqual(f.launched,[])
        f.tick();new=f.row()
        self.assertNotEqual(new['id'],b['id'])
        self.assertEqual(new['status'],'awaiting_ci')
        self.assertEqual(f.reconciled,[])

    def test_batch_mode_publication_and_nonfunding_fences(self):
        for case in ('disabled','publication','mixed'):
            f,r,b=self.ready()
            if case=='disabled':f.project['integration_batch']['enabled']=False
            if case=='publication':f.owners[-1]['publish_pending']=True
            if case=='mixed':
                error=json.loads(b['error']);error['checks'].append(dict(name='check',workflowName='CI',classification='source'))
                f.controller.update(b,error=canonical(error))
            r.tick(f.project)
            self.assertEqual(f.db.execute('SELECT count(*) FROM batch_funding_recoveries').fetchone()[0],0)
            r.funding_receipt.assert_not_called()

    def test_cooldown_reports_next_check(self):
        f,r,b=self.ready();f.controller.clock=lambda:b['updated']+30
        result=r.tick(f.project)
        self.assertEqual(result['state'],'funding_wait');self.assertIn('next check',result['diagnostic'])
        r.funding_receipt.assert_not_called()

    def test_changed_head_base_feedback_and_claim_reject(self):
        for case in ('head','base','feedback','claim','blocked'):
            with self.subTest(case=case):
                f,r,b=self.ready()
                if case=='head':f.source_prs[1]['headRefOid']='a'*40
                if case=='base':f.controller.main=Mock(return_value='a'*40)
                if case=='feedback':f.source_prs[1]['body']='changed feedback'
                if case=='claim':f.owners[0]['token']='replacement'
                if case=='blocked':f.owners[0]['status']='blocked'
                try:r.tick(f.project)
                except RuntimeError:pass
                self.assertEqual(f.db.execute('SELECT count(*) FROM batch_funding_recoveries').fetchone()[0],0)

    def test_candidate_changes_active_work_and_uncertainty_reject(self):
        for case in ('open','head','check','source_job','candidate_job','helper','publication'):
            with self.subTest(case=case):
                f,r,b=self.ready()
                candidate=f.candidate_prs[b['pr']]
                if case=='open':candidate['state']='OPEN'
                if case=='head':candidate['headRefOid']='a'*40
                if case=='check':candidate['_testnet_status']['state']='pending'
                if case.endswith('_job'):
                    f.db.execute('INSERT INTO jobs(project,pr,kind,status) VALUES(?,?,?,?)',
                        ('test',1 if case=='source_job' else b['pr'],'review','orphaned'))
                if case=='helper':f.local_infrastructure.fenced.side_effect=RuntimeError('helper live')
                if case=='publication':f.controller.update(b,publish_intent=1)
                try:r.tick(f.project)
                except RuntimeError:pass
                self.assertEqual(f.db.execute('SELECT count(*) FROM batch_funding_recoveries').fetchone()[0],0)

    def test_receipt_failure_and_mid_read_changes_retain_fence(self):
        for case in ('missing','changed'):
            f,r,b=self.ready()
            def read(*args):
                if case=='missing':raise RuntimeError('missing or uncertain receipt')
                f.owners[0]['token']='changed';return {}
            r.funding_receipt=read
            with self.assertRaises(RuntimeError):r.tick(f.project)
            self.assertEqual(f.controller.select(f.project,json.loads(b['members']),b['base'],1),[])

    def test_bound_not_reset_by_new_failed_candidate(self):
        f,r,b=self.ready();f.project['shared_acceptance_repair']['max_attempts']=1
        r.tick(f.project);f.tick();new=f.row()
        candidate=f.candidate_prs[new['pr']];candidate.update(state='CLOSED')
        f.controller.update(new,status='failed',error=b['error'])
        f.controller.clock=lambda:new['updated']+3601
        self.assertEqual(r.tick(f.project)['state'],'blocked')
        self.assertEqual(f.controller.select(f.project,json.loads(new['members']),new['base'],1),[])

    def test_changed_sources_cannot_use_old_authorization(self):
        f,r,b=self.ready();r.tick(f.project)
        candidates=json.loads(b['members'])
        candidates[0]['owner']['claim_hash']='changed'
        self.assertEqual(f.controller.select(f.project,candidates,b['base'],1),[])

    def test_restart_and_tampered_error_do_not_reset_authorization(self):
        from batch_integration import SharedAcceptanceRepair
        f,r,b=self.ready();r.tick(f.project)
        self.assertEqual(SharedAcceptanceRepair(f).tick(f.project)['state'],'resolved')
        f.controller.update(b,error=canonical(dict(kind='candidate_checks_failed',checks=[dict(name='new')])) )
        self.assertEqual(f.controller.select(f.project,json.loads(b['members']),b['base'],1),[])

class FundingReceipts(unittest.TestCase):
    def test_authenticated_effects_and_negative_receipts(self):
        from batch_integration import SharedAcceptanceRepair
        helper=shared.SharedRepairs();f,r,b=helper.fixture()
        self.addCleanup(helper.doCleanups)
        proof=dict(network='testnet',failure_class='funding',toolchain=dict(compatibility=dict(chain='4c78adac')),success=False,transactions=[],allowance_used_mist=0,max_spend_mist=2_000_000_000,
            funding=dict(required_mist=3_650_000_000,held_mist=0,address_mist=1_900_000_000,coin_mist=0),
            faucet=dict(attempts=1,cli_returncode=0,balance_before_mist=1_900_000_000,
                        balance_change_mist=1_000_000_000,balance_after_mist=2_900_000_000,success=False))
        run=dict(id=1,head_branch='main',event='workflow_run',head_repository=dict(full_name='owner/repo'),status='completed',conclusion='failure',name='Automatic testnet',
                 path='.github/workflows/testnet.yml',repository=dict(full_name='owner/repo'),run_attempt=1)
        checks=json.loads(b['error'])['checks']
        for case in ('valid','submitted','uncertain','spent','faucet','funded','active','extra','held','chain','class','changing_run'):
            with self.subTest(case=case):
                evidence=copy.deepcopy(proof);metadata=copy.deepcopy(run)
                if case=='submitted':evidence['transactions']=[dict(status='success')]
                if case=='uncertain':evidence['transactions']=[dict(status='unknown')]
                if case=='spent':evidence['allowance_used_mist']=1
                if case=='faucet':evidence['faucet']['attempts']=2
                if case=='funded':evidence['faucet']['balance_after_mist']=4_000_000_000
                if case=='active':metadata['status']='in_progress'
                if case=='held':evidence['funding']['held_mist']=400_000_000
                if case=='chain':evidence['toolchain']['compatibility']['chain']='mainnet'
                if case=='class':evidence['failure_class']='uncertain'
                reads=[]
                def gh(project,args):
                    if args[0]=='api':
                        reads.append(1)
                        if case=='changing_run' and len(reads)>1:metadata['run_attempt']=2
                        return json.dumps(metadata)
                    directory=Path(args[args.index('-D')+1]);(directory/'package-upgrade.json').write_text(json.dumps(evidence))
                    if case=='extra':(directory/'other.json').write_text('{}')
                    return ''
                f.gh=gh
                if case=='valid':self.assertEqual(r.funding_receipt(f.project,b,checks)['run'],'1')
                else:
                    with self.assertRaises(RuntimeError):r.funding_receipt(f.project,b,checks)
