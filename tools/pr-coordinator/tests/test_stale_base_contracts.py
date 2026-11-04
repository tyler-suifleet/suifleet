"""SF-238: authenticated stale-base failures may enter combined validation only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_batch_integration import Fixture
import coordinator as c


class StaleBaseTests(unittest.TestCase):
    def setup_case(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        f = Fixture(Path(temporary.name))
        pr = f.source_prs[1]
        head, base, ancestor = pr['headRefOid'], pr['baseRefOid'], 'a' * 40
        pr['_testnet_status'] = dict(state='failure', description='base:' + base + ' class:source',
            target_url='https://github.com/owner/repo/actions/runs/77')
        run = dict(id=77, event='workflow_run', head_branch='main', head_sha=base,
            name='Automatic testnet', path='.github/workflows/testnet.yml',
            repository=dict(full_name='owner/repo'), head_repository=dict(full_name='owner/repo'),
            status='completed', conclusion='failure', run_attempt=1)
        transactions = []
        used = 0
        for label in ['publish base', 'register device + issue cap (base)', 'add device to group (base)']:
            used += 10
            transactions.append(dict(label=label, status='success', digest='digest', gas_mist=10,
                                     allowance_used_mist=used))
        transactions.append(dict(label='upgrade to head', status='not_submitted', allowance_used_mist=used))
        evidence = dict(head=head, base=base, network='testnet', chain='4c78adac', success=False,
            failure_class='source', funding=dict(held_mist=0), reconciliations=[], transactions=transactions,
            max_spend_mist=100, allowance_used_mist=used,
            error='test-upgrade could not build a transaction: error[EUC01001]: missing public declaration; '
                  'this package requires changes to be compatible')
        trees = {ancestor:'c'*40, head:'c'*40, base:'d'*40}
        def immutable(project, path):
            if '/compare/' in path: return dict(merge_base_commit=dict(sha=ancestor))
            return dict(tree=[dict(path='contracts', type='tree', sha=trees[path.rsplit('/', 1)[1]])])
        def gh(project, args):
            if args[0] == 'api': return json.dumps(run)
            Path(args[-1], 'package-upgrade.json').write_text(json.dumps(evidence))
            return ''
        f.immutable_read = immutable
        f.gh = gh
        return f, pr, run, evidence, trees

    def test_observed_deadlock_routes_without_erasing_failure_or_owner(self):
        f, pr, run, evidence, trees = self.setup_case()
        owner = copy.deepcopy(f.owners[0]); failure = c.failed_checks(pr)
        self.assertIsNone(f.controller.source(f.project, pr, owner))
        f.controller.annotate_stale_base(f.project, pr)
        member = f.controller.source(f.project, pr, owner)
        self.assertIsNotNone(member)
        self.assertEqual(member['stale_base_contracts']['allowance_used_mist'], 30)
        self.assertEqual(c.failed_checks(pr), failure)
        self.assertEqual(c.source_check_failures(pr), [])
        self.assertEqual(f.owners[0], owner)
        self.assertIsNotNone(c.checks_blocker(pr))

    def test_negative_evidence_fails_closed(self):
        changes = [
            lambda f,p,r,e,t: t.update({p['headRefOid']:'e'*40}),
            lambda f,p,r,e,t: t.update({p['baseRefOid']:'c'*40}),
            lambda f,p,r,e,t: r.update(status='in_progress'),
            lambda f,p,r,e,t: r.update(head_sha='f'*40),
            lambda f,p,r,e,t: r.update(event='workflow_dispatch'),
            lambda f,p,r,e,t: r.update(repository=dict(full_name='foreign/repo')),
            lambda f,p,r,e,t: e.update(head='f'*40),
            lambda f,p,r,e,t: e.update(base='f'*40),
            lambda f,p,r,e,t: e.update(chain='other'),
            lambda f,p,r,e,t: e.update(error='ambiguous build failure'),
            lambda f,p,r,e,t: e.update(error=e['error'] + ' error[E99999]: another failure'),
            lambda f,p,r,e,t: e.update(quarantine={'unresolved':True}),
            lambda f,p,r,e,t: e.update(failure_class='uncertain'),
            lambda f,p,r,e,t: e['funding'].update(held_mist=1),
            lambda f,p,r,e,t: e['transactions'][-1].update(status='success', digest='submitted'),
            lambda f,p,r,e,t: e['transactions'][0].update(status='uncertain'),
            lambda f,p,r,e,t: e.update(allowance_used_mist=0),
            lambda f,p,r,e,t: e.update(reconciliations=[dict(status='unknown')]),
            lambda f,p,r,e,t: p.update(_batch_review_failure={'finding':'real source bug'}),
            lambda f,p,r,e,t: f.db.execute("INSERT INTO jobs VALUES('test',1,'fix','running')"),
            lambda f,p,r,e,t: p['_testnet_status'].update(state='pending'),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                f, pr, run, evidence, trees = self.setup_case()
                change(f, pr, run, evidence, trees)
                f.controller.annotate_stale_base(f.project, pr)
                self.assertNotIn('_stale_base_contracts', pr)

    def test_changed_snapshot_invalidates_annotation(self):
        for field in ('headRefOid', 'baseRefOid', 'body', '_testnet_status'):
            with self.subTest(field=field):
                f, pr, run, evidence, trees = self.setup_case()
                f.controller.annotate_stale_base(f.project, pr)
                pr[field] = dict(pr[field], id=99) if field == '_testnet_status' else 'changed'
                self.assertFalse(c.verified_stale_base(pr))
                self.assertIsNone(f.controller.source(f.project, pr, f.owners[0]))

    def test_artifact_and_run_read_failures_are_blocking(self):
        for mode in ('missing', 'extra', 'changed_run', 'unavailable'):
            with self.subTest(mode=mode):
                f, pr, run, evidence, trees = self.setup_case()
                original = f.gh; reads = []
                def gh(project, args):
                    if mode == 'unavailable': raise RuntimeError('offline')
                    result = original(project, args)
                    if args[0] == 'api':
                        reads.append(1)
                        if mode == 'changed_run' and len(reads)>1:
                            return json.dumps(dict(run, run_attempt=2))
                    elif mode == 'missing': Path(args[-1], 'package-upgrade.json').unlink()
                    elif mode == 'extra': Path(args[-1], 'other.json').write_text('{}')
                    return result
                f.gh = gh
                f.controller.annotate_stale_base(f.project, pr)
                self.assertNotIn('_stale_base_contracts', pr)

    def test_route_builds_candidate_but_never_approves_failed_testnet(self):
        f, pr, run, evidence, trees = self.setup_case()
        f.controller.annotate_stale_base(f.project, pr)
        f.gh = Fixture.gh.__get__(f)
        f.tick()
        batch = f.row()
        self.assertIn(1, [m['pr'] for m in json.loads(batch['members'])])
        self.assertEqual(batch['status'], 'awaiting_ci')
        self.assertEqual(f.reconciled, [])
        candidate = f.controller.candidate(f.project, batch)
        self.assertIsNotNone(c.checks_blocker(candidate))
        # Repeated scans keep one candidate and do not mutate the source/claim.
        f.tick()
        self.assertEqual(len(f.controller.rows(f.project)), 1)
        self.assertEqual(f.source_prs[1]['headRefOid'], pr['headRefOid'])
        self.assertEqual(f.owners[0]['status'], 'review')

    def test_real_git_trees_and_combined_candidate_retain_new_main_contracts(self):
        f, pr, run, evidence, trees = self.setup_case()
        # Construct the actual divergence: source changes app only; main adds a declaration.
        f.git('checkout', '-qb', 'contract-ancestor', f.base)
        contracts = f.repo/'contracts'; contracts.mkdir()
        (contracts/'module.move').write_text('original declaration')
        f.git('add', 'contracts'); f.git('commit', '-qm', 'original contracts')
        ancestor = f.git('rev-parse', 'HEAD')
        f.git('checkout', '-B', 'work/1', ancestor)
        (f.repo/'app').write_text('source feature')
        f.git('add', 'app'); f.git('commit', '-qm', 'source app only')
        head = f.git('rev-parse', 'HEAD'); f.git('push', '-q', '--force', 'origin', 'work/1')
        f.git('checkout', '-B', 'main', ancestor)
        (contracts/'module.move').write_text('original declaration plus new public declaration')
        f.git('add', 'contracts'); f.git('commit', '-qm', 'new main declaration')
        base = f.git('rev-parse', 'HEAD'); f.git('push', '-q', 'origin', 'main')
        f.base = base; f.source_prs = {1:pr}
        pr.update(headRefOid=head, baseRefOid=base)
        pr['_testnet_status']['description'] = 'base:' + base + ' class:source'
        run['head_sha'] = base; evidence.update(head=head, base=base)
        f.receipts[1].update(head=head, base=base, source_feedback=c.source_feedback(pr))
        def immutable(project, path):
            if '/compare/' in path:
                return dict(merge_base_commit=dict(sha=f.git('merge-base', base, head)))
            revision = path.rsplit('/', 1)[1]
            return dict(tree=[dict(path='contracts', type='tree', sha=f.git('rev-parse', revision+':contracts'))])
        f.immutable_read = immutable
        f.controller.annotate_stale_base(f.project, pr)
        self.assertIn('_stale_base_contracts', pr)
        f.gh = Fixture.gh.__get__(f)
        f.tick()
        batch = f.row()
        self.assertEqual(batch['status'], 'awaiting_ci')
        self.assertEqual(f.git('rev-parse', batch['head']+':contracts'), f.git('rev-parse', base+':contracts'))
        self.assertEqual(f.git('show', batch['head']+':app'), 'source feature')
        self.assertFalse(any(kind == 'fix' for _,_,kind in f.launched))

    def test_scheduler_does_not_launch_another_diagnosis_worker(self):
        from test_coordinator import CheckFreshnessTests
        f, pr, run, evidence, trees = self.setup_case()
        f.controller.annotate_stale_base(f.project, pr)
        with tempfile.TemporaryDirectory() as directory:
            engine, project, owner = CheckFreshnessTests().setup_scan(directory, pr)
            project.update(integration_batch=dict(enabled=True), review_github_account='reviewer')
            # No receipt yet: preserve source review scheduling, never CI repair.
            engine.source_receipt = lambda *args: None
            engine.ticket = lambda *args: json.dumps(owner)
            pr['_review_mode'] = 'source'
            pr['_stale_base_contracts']['source_feedback'] = c.source_feedback(pr)
            with patch.object(engine.batches, 'tick', return_value=set()), \
                 patch.object(engine, 'launch') as launch:
                engine.tick(True)
                self.assertTrue(all(call.args[-1] == 'review' for call in launch.call_args_list))
            self.assertEqual(len(c.failed_checks(pr)), 1)
            self.assertEqual(engine.store.db.execute('SELECT count(*) FROM ci_repairs').fetchone()[0], 0)
