"""Pinned shared harness attribution never changes claims, jobs or acceptance."""
import copy
import hashlib
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_batch_integration import Fixture
from batch_integration import canonical

HEAD = 'ebdca15ae1c244376701b323486a94e2ea002799'
BASE = '4aa4344c0a0b0749348ed78544531ed28ac857d3'
RUN = 41914017432
CHECK = dict(name='testnet/package-upgrade', workflowName='Automatic testnet',
             conclusion='FAILURE', classification='source',
             detailsUrl=f'https://github.com/tyler-suifleet/suifleet/actions/runs/{RUN}')

class SharedHarnessTests(unittest.TestCase):
    def fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Fixture(Path(tmp.name)); self.addCleanup(f.db.close)
        f.tick(); batch = dict(f.row()); f.launched.clear()
        f.project['repository'] = 'tyler-suifleet/suifleet'
        batch.update(head=HEAD, base=BASE, status='failed',
                     error=canonical(dict(kind='candidate_checks_failed', checks=[CHECK])))
        for pr in f.source_prs.values():
            pr['baseRefOid'] = BASE
        controller = f.controller
        controller.verify_manifest = Mock()
        controller.rows = Mock(return_value=[batch])
        run = dict(id=RUN, status='completed', conclusion='failure', event='workflow_run',
                   head_branch='main', path='.github/workflows/testnet.yml', name='Automatic testnet',
                   run_attempt=1, repository=dict(full_name=f.project['repository']),
                   head_repository=dict(full_name=f.project['repository']))
        package = dict(chain='4c78adac', head=HEAD, base=BASE)
        security = dict(chain='4c78adac', head=HEAD, transactions=[dict(label='initialize cached passkey authority',
            digest='AEbuyHfJEsQjPt9GJq9gM5bMFraHa2VSyrVoUvQGtQkw', status='failure', gas_mist=1_000_000)])
        def gh(project, args):
            if args[0] == 'api':
                return json.dumps(run)
            if args[:2] == ['run', 'download']:
                root = Path(args[args.index('-D')+1])
                for name, value in [('package-upgrade.json',package), ('upgrade-security.json',security)]:
                    (root/name).write_text(json.dumps(value))
                return ''
            raise AssertionError(args)
        f.gh = Mock(side_effect=gh)
        return f, controller, batch, run, package, security

    def test_authenticated_incident_durable_and_victims_not_repaired(self):
        f,c,b,run,package,security = self.fixture()
        before = copy.deepcopy(f.owners)
        for n in (1,2):
            context = c.repair_context(f.project, f.source_prs[n])
            self.assertNotIn('_batch_failure',context)
            receipt = context['_shared_acceptance_wait']
            self.assertEqual(receipt['owner_ticket'],'SF-243')
            self.assertEqual((receipt['candidate_head'],receipt['candidate_base'],receipt['run']),(HEAD,BASE,RUN))
        self.assertEqual(f.owners,before); self.assertEqual(f.launched,[])
        self.assertEqual(f.db.execute('SELECT count(*) FROM shared_harness_incidents').fetchone()[0],1)
        self.assertEqual(f.gh.call_count,3)  # run before/after plus one artifact read
        f.gh = Mock(side_effect=AssertionError('do not reread unchanged incident'))
        self.assertIn('_shared_acceptance_wait',c.repair_context(f.project,f.source_prs[1]))

    def test_advanced_main_releases_unchanged_members_for_collection(self):
        f,c,b,*_ = self.fixture()
        heads = {n: pr['headRefOid'] for n,pr in f.source_prs.items()}
        for n,pr in f.source_prs.items():
            self.assertIn('_shared_acceptance_wait', c.repair_context(f.project,pr))
            self.assertIsNone(c.source(f.project,pr,f.owners[n-1]))
        f.gh = Mock(side_effect=AssertionError('historical incident needs no new reads'))
        for n,pr in f.source_prs.items():
            pr['baseRefOid'] = f.base  # Refreshed main after prerequisite integration.
            self.assertEqual(c.repair_context(f.project,pr), {})
            self.assertIsNotNone(c.source(f.project,pr,f.owners[n-1]))
            self.assertEqual(pr['headRefOid'],heads[n])
        self.assertEqual(f.db.execute('SELECT count(*) FROM shared_harness_incidents').fetchone()[0],1)
        self.assertEqual(f.launched,[])

    def test_transient_evidence_read_failure_does_not_suppress_repairs(self):
        for failed_call,error in ((call,error) for call in (1,2,3)
                                  for error in (RuntimeError('temporary GitHub failure'),
                                                subprocess.CalledProcessError(1,['gh']))):
            with self.subTest(failed_call=failed_call,error=type(error).__name__):
                f,c,b,*_ = self.fixture()
                original = f.gh.side_effect
                calls = 0
                def flaky(project,args):
                    nonlocal calls
                    calls += 1
                    if calls == failed_call:
                        raise error
                    return original(project,args)
                f.gh.side_effect = flaky
                self.assertNotIn('_shared_acceptance_wait',c.repair_context(f.project,f.source_prs[1]))
                self.assertEqual(f.db.execute('SELECT count(*) FROM shared_harness_incidents').fetchone()[0],0)
                f.gh.side_effect = original
                self.assertIn('_shared_acceptance_wait',c.repair_context(f.project,f.source_prs[1]))

    def test_changed_identity_or_mixed_failure_never_suppressed(self):
        for case in ('head','base','repository','mixed','url','workflow','source_head'):
            with self.subTest(case=case):
                f,c,b,*_ = self.fixture()
                if case in ('head','base'): b[case]='a'*40
                elif case=='repository': f.project['repository']='other/repo'
                elif case=='source_head': f.source_prs[1]['headRefOid']='a'*40
                else:
                    error=json.loads(b['error'])
                    if case=='mixed':error['checks'].append(dict(name='source security'))
                    if case=='url':error['checks'][0]['detailsUrl']+='9'
                    if case=='workflow':error['checks'][0]['workflowName']='CI'
                    b['error']=canonical(error)
                self.assertNotIn('_shared_acceptance_wait',c.repair_context(f.project,f.source_prs[1]))
                self.assertEqual(f.db.execute('SELECT count(*) FROM shared_harness_incidents').fetchone()[0],0)

    def test_missing_or_untrusted_evidence_fails_closed(self):
        for case in ('active','foreign','event','attempt','package_head','package_base','chain','failure','gas','digest'):
            with self.subTest(case=case):
                f,c,b,run,package,security = self.fixture()
                if case=='active':run['status']='in_progress'
                if case=='foreign':run['head_repository']={}
                if case=='event':run['event']='pull_request'
                if case=='attempt':run['run_attempt']=0
                if case=='package_head':package['head']='a'*40
                if case=='package_base':package['base']='a'*40
                if case=='chain':security['chain']='mainnet'
                if case=='failure':security['transactions'][0]['status']='success'
                if case=='gas':security['transactions'][0]['gas_mist']=0
                if case=='digest':security['transactions'][0].pop('digest')
                with self.assertRaises(RuntimeError):c.repair_context(f.project,f.source_prs[1])
                self.assertEqual(f.db.execute('SELECT count(*) FROM shared_harness_incidents').fetchone()[0],0)
                self.assertEqual(f.launched,[])

    def test_durable_receipt_refuses_changed_manifest_or_error(self):
        for case in ('manifest','error'):
            f,c,b,*_ = self.fixture()
            c.repair_context(f.project,f.source_prs[1])
            if case=='manifest':b['manifest_digest']='a'*64
            else:
                error=json.loads(b['error']);error['checks'][0]['classification']='unknown'
                b['error']=canonical(error)
            with self.assertRaisesRegex(RuntimeError,'identity changed'):
                c.repair_context(f.project,f.source_prs[1])


class PackagePairIncidentTests(SharedHarnessTests):
    def pair_fixture(self):
        f,c,b,run,package,security=self.fixture()
        head='58b0b0828925404219e65853b56431cb19cd1ce1'
        base='d5702bc6be238f1887df00611ebf382b2bece755'
        b.update(head=head,base=base)
        run['id']=42031568923
        package.update(head=head,base=base)
        security.update(head=head,transactions=[])
        error=json.loads(b['error'])
        error['checks'][0]['detailsUrl']='https://github.com/tyler-suifleet/suifleet/actions/runs/42031568923'
        b['error']=canonical(error)
        for pr in f.source_prs.values():pr['baseRefOid']=base
        original=f.gh.side_effect
        def gh(project,args):
            if args[:2]==['run','view']:
                return 'RuntimeError: unfinished native passkey fixture belongs to another package pair'
            return original(project,args)
        f.gh.side_effect=gh
        # Synthetic authenticated artifacts use their own fixed hashes. Mutating
        # any downloaded evidence after this point must fail authentication.
        expected = {name: hashlib.sha256(json.dumps(value).encode()).hexdigest()
                    for name,value in [('package-upgrade.json',package),('upgrade-security.json',security)]}
        patcher=patch('batch_integration.PAIR_INCIDENT_HASHES',expected)
        patcher.start(); self.addCleanup(patcher.stop)
        return f,c,b,run,package,security

    def test_pair_incident_routes_only_unchanged_victims_to_existing_owner(self):
        f,c,b,*_=self.pair_fixture()
        owners=copy.deepcopy(f.owners)
        for pr in f.source_prs.values():
            context=c.repair_context(f.project,pr)
            self.assertNotIn('_batch_failure',context)
            self.assertEqual(context['_shared_acceptance_wait']['owner_ticket'],'SF-255')
        self.assertEqual(f.owners,owners)
        self.assertEqual(f.launched,[])
        f.source_prs[1]['headRefOid']='a'*40
        self.assertEqual(c.repair_context(f.project,f.source_prs[1]),{})
        f.source_prs[2]['baseRefOid']='b'*40
        self.assertEqual(c.repair_context(f.project,f.source_prs[2]),{})

    def test_pair_diagnostic_and_effects_are_required(self):
        for case in ('diagnostic','failure','success','chain','run'):
            f,c,b,run,package,security=self.pair_fixture()
            if case=='diagnostic':
                original=f.gh.side_effect
                f.gh.side_effect=lambda p,a: 'unrelated error' if a[:2]==['run','view'] else original(p,a)
            if case=='failure':security['transactions']=[dict(status='failure')]
            if case=='success':security['native_passkey']=dict(success=True)
            if case=='chain':security['chain']='mainnet'
            if case=='run':run['id']=1
            with self.subTest(case=case),self.assertRaises(RuntimeError):
                c.repair_context(f.project,f.source_prs[1])
            self.assertEqual(f.db.execute('SELECT count(*) FROM shared_harness_incidents').fetchone()[0],0)
            self.assertEqual(f.launched,[])
