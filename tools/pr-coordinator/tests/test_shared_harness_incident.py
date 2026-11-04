"""Pinned shared harness attribution never changes claims, jobs or acceptance."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
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
