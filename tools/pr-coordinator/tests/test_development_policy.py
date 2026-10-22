import copy
import json
from pathlib import Path
import unittest

from test_coordinator import c, fixture
from test_dispatcher import d


class DevelopmentPolicy(unittest.TestCase):
    def policy(self):
        return {'schema_version':1,'development_defaults':{
            'authorization':'User defers physical hardware and mainnet acceptance only',
            'acceptance_only_tickets':['SF-031','SF-087'],
            'deferred_checks':['hardware','mainnet'],
            'acceptance_issues':['https://example.test/hardware','https://example.test/mainnet'],
            'retained_gates':['security','local tests','testnet','CI','independent review']},
            'tickets':{'SF-022':{'deferred_checks':['hardware','specific check'],
                                 'retained_gates':['encryption']}}}

    def test_future_ticket_gets_narrow_default_scope(self):
        result=c.effective_policy(self.policy(),'SF-999')['tickets']['SF-999']
        self.assertEqual(result['deferred_checks'],['hardware','mainnet'])
        self.assertIn('testnet',result['retained_gates'])
        self.assertIn('independent review',result['retained_gates'])

    def test_specific_scope_is_additive_without_mutating_review_evidence(self):
        policy=self.policy();original=copy.deepcopy(policy)
        result=c.effective_policy(policy,'SF-022')
        self.assertEqual(result['tickets']['SF-022']['deferred_checks'],['hardware','mainnet','specific check'])
        self.assertIn('encryption',result['tickets']['SF-022']['retained_gates'])
        self.assertEqual(policy,original)
        self.assertEqual(c.effective_policy(result,'SF-022'),result)

    def test_acceptance_only_ticket_is_not_waived(self):
        policy=self.policy()
        self.assertEqual(c.effective_policy(policy,'SF-031'),policy)
        self.assertNotIn('SF-087',c.effective_policy(policy,'SF-087')['tickets'])

    def test_legacy_policy_is_preserved(self):
        policy={'tickets':{'SF-022':{'deferred_checks':['one check']}}}
        self.assertEqual(c.effective_policy(policy,'SF-999'),policy)
        self.assertEqual(c.effective_policy({},'SF-999'),{})

    def test_policy_change_invalidates_review_fingerprint(self):
        pr,_,_=fixture();pr['_acceptance_policy']=self.policy()
        before=c.feedback(pr)
        pr['_acceptance_policy']['development_defaults']['deferred_checks'].append('new check')
        self.assertNotEqual(c.feedback(pr),before)

    def test_both_worker_prompts_resolve_same_scope(self):
        policy=self.policy();project={'acceptance_policy':policy}
        owner={'key':'SF-999','agent':'Codex','model':'test','session':'session',
               'branch':'work','worktree':'/tmp/work','spec':{}}
        engine=c.Coordinator.__new__(c.Coordinator)
        engine.store=type('EmptyStore',(),{'review':lambda *args:None})()
        project['id']='test'
        pr,_,_=fixture();pr['_acceptance_policy']=policy;pr['baseRefName']='main'
        review=engine.prompt(project,pr,owner,'review','Claude',Path('/tmp/review'),Path('/tmp/result'))
        implementation=d.Dispatcher.__new__(d.Dispatcher).prompt(project,owner,Path('/tmp/implementation'))
        review_line=next(line for line in review.splitlines() if line.startswith('USER-AUTHORIZED ACCEPTANCE SCOPE: '))
        impl_line=next(line for line in implementation.splitlines() if line.startswith('User-authorized implementation acceptance scope: '))
        self.assertEqual(json.loads(review_line.split(': ',1)[1]),json.loads(impl_line.split(': ',1)[1]))
        self.assertIn('SF-999',review_line)

    def test_committed_policy_preserves_test_and_authorization_boundaries(self):
        root=Path(__file__).resolve().parents[3]
        policy=json.loads((root/'docs/tracking/acceptance-policy.json').read_text())
        defaults=policy['development_defaults']
        self.assertTrue({'SF-031','SF-042','SF-066','SF-087'} <= set(defaults['acceptance_only_tickets']))
        retained=' '.join(defaults['retained_gates']).lower()
        for gate in ('security','local','testnet','ci','independent review'):
            self.assertIn(gate,retained)
        self.assertIn('not mainnet or physical programming authorization',defaults['authorization'])


class DispatchWakeup(unittest.TestCase):
    def test_completion_wakes_idle_family_without_relaxing_transport_backoff(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            worker=d.Dispatcher({'projects':[],'implementation_slots':[]},Path(folder))
            project={'id':'suifleet'}
            owners=[{'key':'SF-022','status':'review','agent':'Claude'}]
            worker.wake_on_change(project,owners)
            worker.db.execute("INSERT INTO backoff VALUES('suifleet','Codex',5,9999999999,'idle: no eligible ticket')")
            worker.db.execute("INSERT INTO backoff VALUES('suifleet','Claude',5,9999999999,'refresh unavailable before claim')")
            worker.wake_on_change(project,[{**owners[0],'heartbeat':999}])
            self.assertEqual(worker.db.execute("SELECT retry_after FROM backoff WHERE family='Codex'").fetchone()[0],9999999999)
            worker.wake_on_change(project,[{**owners[0],'status':'done'}])
            self.assertEqual(worker.db.execute("SELECT retry_after FROM backoff WHERE family='Codex'").fetchone()[0],0)
            self.assertEqual(worker.db.execute("SELECT retry_after FROM backoff WHERE family='Claude'").fetchone()[0],9999999999)

    def test_new_ticket_wakes_existing_idle_backoff_after_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            config={'projects':[],'implementation_slots':[]}
            worker=d.Dispatcher(config,Path(folder));project={'id':'suifleet'}
            worker.wake_on_change(project,[])
            worker.db.execute("INSERT INTO backoff VALUES('suifleet','Codex',5,9999999999,'idle: no eligible ticket')")
            worker.db.close()
            worker=d.Dispatcher(config,Path(folder))
            worker.wake_on_change(project,[{'key':'SF-094','status':'backlog','agent':None}])
            self.assertEqual(worker.db.execute('SELECT retry_after FROM backoff').fetchone()[0],0)


class FinalExternalAcceptance(unittest.TestCase):
    def test_production_seal_and_manual_wallet_checks_have_explicit_final_owners(self):
        root=Path(__file__).resolve().parents[3]
        policy=json.loads((root/'docs/tracking/acceptance-policy.json').read_text())
        seal=c.effective_policy(policy,'SF-022')['tickets']['SF-022']
        wallet=c.effective_policy(policy,'SF-088')['tickets']['SF-088']
        self.assertTrue(any('criterion 7' in check for check in seal['deferred_checks']))
        self.assertTrue(any('Google/Enoki' in check for check in wallet['deferred_checks']))
        self.assertIn('https://github.com/tyler-suifleet/suifleet/issues/159',seal['acceptance_issues'])
        self.assertIn('https://github.com/tyler-suifleet/suifleet/issues/32',wallet['acceptance_issues'])
        for scope in (seal,wallet):
            gates=' '.join(scope['retained_gates']).lower()
            for gate in ('security','local','testnet','independent'):
                self.assertIn(gate,gates)
        self.assertNotEqual(c.effective_policy(policy,'SF-022'),policy)
        self.assertEqual(c.effective_policy(policy,'SF-087'),policy)
