import copy
import json
from unittest.mock import patch
import unittest
import test_batch_integration as fixtures
from batch_integration import canonical


class InfrastructureRetryTests(unittest.TestCase):
    fixture = fixtures.BatchTests.fixture
    def failed(self):
        f=self.fixture();f.project['integration_batch']['max_members']=1
        f.source_prs.pop(2);f.tick();row=f.row()
        f.candidate_prs[row['pr']]['_testnet_status']=dict(state='failure',
            description='base:'+row['base']+' class:infrastructure',
            target_url='https://github.com/owner/repo/actions/runs/10')
        f.tick();row=f.row()
        self.assertEqual(row['status'],'failed')
        return f,row

    def test_delayed_durable_single_retry_retains_all_final_gates(self):
        f,row=self.failed();members=json.loads(row['members'])
        self.assertEqual(f.controller.repair_context(f.project,f.source_prs[1]),{})
        with patch.object(f.controller,'infrastructure_receipt',return_value={'run':10}) as proof:
            f.tick();proof.assert_not_called()
            f.controller.clock=lambda:row['updated']+301
            f.tick();proof.assert_called_once()
            self.assertNotEqual(f.row()['id'],row['id'])
            self.assertEqual(f.row()['status'],'awaiting_ci')
            replacement=json.loads(f.row()['members'])
            for old,new in zip(members,replacement):
                self.assertEqual({k:v for k,v in old.items() if k!='commit'},
                                 {k:v for k,v in new.items() if k!='commit'})
            self.assertEqual(f.controller.rows(f.project)[0]['status'],'failed')
            self.assertFalse(f.reconciled)
            # Fail the replacement: no third candidate or source repair.
            fresh=f.row()
            f.candidate_prs[fresh['pr']]['_testnet_status']=dict(state='failure',
                description='base:'+fresh['base']+' class:infrastructure',
                target_url='https://github.com/owner/repo/actions/runs/11')
            f.tick();failed=f.row();f.controller.clock=lambda:failed['updated']+301
            f.tick();self.assertEqual(len(f.controller.rows(f.project)),2)
            self.assertEqual(f.controller.select(f.project,members,row['base'],1),[])
            self.assertEqual(f.controller.repair_context(f.project,f.source_prs[1]),{})

    def test_active_or_uncertain_worker_and_changed_pins_prevent_retry(self):
        for state in ['reserved','running','orphaned','quarantined','quota_wait','quota_launch']:
            with self.subTest(state=state):
                f,row=self.failed();members=json.loads(row['members'])
                f.controller.clock=lambda:row['updated']+301
                f.db.execute('insert into jobs values(?,?,?,?)',('test',1,'fix',state))
                with patch.object(f.controller,'infrastructure_receipt') as proof:
                    f.controller.recover_infrastructure(f.project,members,row['base']);proof.assert_not_called()
                self.assertEqual(f.controller.select(f.project,members,row['base'],1),[])
        f,row=self.failed();members=json.loads(row['members']);f.controller.clock=lambda:row['updated']+301
        with patch.object(f.controller,'infrastructure_receipt') as proof:
            f.controller.recover_infrastructure(f.project,members,'f'*40)
            changed=copy.deepcopy(members);changed[0]['head']='f'*40
            f.controller.recover_infrastructure(f.project,changed,row['base'])
            proof.assert_not_called()

    def test_unavailable_provider_evidence_keeps_fence_without_source_dispatch(self):
        f,row=self.failed();members=json.loads(row['members'])
        f.controller.clock=lambda:row['updated']+301
        with patch.object(f.controller,'infrastructure_receipt',side_effect=RuntimeError('API unavailable')):
            f.tick()
        self.assertEqual(len(f.controller.rows(f.project)),1)
        self.assertEqual(f.controller.select(f.project,members,row['base'],1),[])
        self.assertFalse(f.db.execute('select 1 from batch_infrastructure_retries').fetchone())
        self.assertEqual(f.controller.repair_context(f.project,f.source_prs[1]),{})

    def test_source_uncertain_and_mixed_errors_keep_owner_route(self):
        f,row=self.failed();error=json.loads(row['error']);check=error['checks'][0]
        for checks in [[dict(check,classification='source')], [dict(check,classification='uncertain')],
                       [check,dict(check,name='security',workflowName='CI')]]:
            f.controller.update(row,error=canonical(dict(kind='candidate_checks_failed',checks=checks)))
            self.assertIn('_batch_failure',f.controller.repair_context(f.project,f.source_prs[1]))

    def test_proof_requires_zero_spend_steps_transient_logs_and_stable_run(self):
        f,row=self.failed();error=json.loads(row['error'])
        run=dict(event='workflow_run',head_branch='main',path='.github/workflows/testnet.yml',
                 head_repository=dict(full_name='owner/repo'),repository=dict(full_name='owner/repo'),
                 status='completed',conclusion='failure',run_attempt=1)
        job=dict(id=42,name='validate',conclusion='failure',steps=[
            dict(name='Recheck head and base immediately before spending',conclusion='failure'),
            dict(name='Run bounded testnet acceptance',conclusion='skipped')])
        jobs=dict(total_count=1,jobs=[job])
        logs='No server is currently available to service your request'
        responses=[run,jobs,logs,run]
        def verify(values):
            with patch.object(f,'gh',side_effect=[json.dumps(x) if isinstance(x,dict) else x for x in values]):
                return f.controller.infrastructure_receipt(f.project,row,error)
        self.assertEqual(verify(responses)['job'],42)
        for index,value in [(0,dict(run,event='pull_request')), (1,dict(jobs,total_count=2)),
                            (2,'permission denied'), (3,dict(run,run_attempt=2))]:
            bad=copy.deepcopy(responses);bad[index]=value
            with self.subTest(index=index),self.assertRaises(RuntimeError):verify(bad)
        bad=copy.deepcopy(responses);bad[1]['jobs'][0]['steps'][1]['conclusion']='success'
        with self.assertRaises(RuntimeError):verify(bad)
