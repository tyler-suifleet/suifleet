"""Trusted deployment applicability is separate from a generic skipped check."""
import copy
import unittest
from test_coordinator import c, fixture


class OptionalSkips(unittest.TestCase):
    def setup_evidence(self, name='preview'):
        self.pr = fixture()[0]
        self.pr.update(number=7, baseRefName='main', isCrossRepository=False)
        self.check = dict(__typename='CheckRun', name=name, status='COMPLETED', conclusion='SKIPPED',
                          detailsUrl='https://github.com/owner/repo/actions/runs/1/job/2')
        self.pr['statusCheckRollup'] = [self.check]
        self.path = 'repos/owner/repo/actions/runs/1'
        self.run = dict(id=1, head_sha=self.pr['headRefOid'], run_attempt=1, status='completed',
                        conclusion='success', head_repository=dict(full_name='owner/repo'),
                        path=c.OPTIONAL_DEPLOYMENT_WORKFLOW, event='pull_request', check_suite_id=3,
                        pull_requests=[dict(number=7, head=dict(sha=self.pr['headRefOid']),
                                            base=dict(sha=self.pr['baseRefOid'], ref='main'))])
        self.job = dict(id=2, run_id=1, head_sha=self.pr['headRefOid'], name=name,
                        status='completed', conclusion='skipped')
        self.build = dict(id=4, name='build', status='completed', conclusion='success')
        self.jobs = dict(total_count=2, jobs=[self.job, self.build])
        self.verified = dict(id=2, head_sha=self.pr['headRefOid'], app=dict(slug='github-actions'),
                             check_suite=dict(id=3), name=name, status='completed', conclusion='skipped',
                             details_url=self.check['detailsUrl'])
        self.tree = dict(truncated=False, tree=[dict(path=c.OPTIONAL_DEPLOYMENT_WORKFLOW,
                                                    type='blob', sha=c.OPTIONAL_DEPLOYMENT_BLOB)])
        self.data = {self.path:self.run, self.path+'/attempts/1/jobs?per_page=100':self.jobs,
                     'repos/owner/repo/check-runs/2':self.verified}

    def annotate(self, read=None):
        return c.annotate_optional_skips(self.pr, 'owner/repo', read or
                                        (lambda p:copy.deepcopy(self.data[p])), lambda p:copy.deepcopy(self.tree))

    def test_only_verified_optional_jobs_pass_and_conclusions_are_preserved(self):
        for name in c.OPTIONAL_DEPLOYMENT_JOBS:
            self.setup_evidence(name)
            self.assertIsNotNone(c.checks_blocker(self.pr))
            self.annotate()
            self.assertIsNone(c.checks_blocker(self.pr))
            self.assertEqual(self.check['conclusion'], 'SKIPPED')
            self.pr['baseRefOid'] = 'changed'
            self.assertIsNotNone(c.checks_blocker(self.pr))

    def test_hosting_test_workflow_preserves_exact_applicability_proof(self):
        hosting_blob = '9d3526058a8dda53e0e64ce49c4bff5e3ed9b0d0'
        for name in c.OPTIONAL_DEPLOYMENT_JOBS:
            with self.subTest(name=name):
                self.setup_evidence(name)
                self.tree['tree'][0]['sha'] = hosting_blob
                self.annotate()
                self.assertIsNone(c.checks_blocker(self.pr))
                self.assertEqual(self.check['_optional_skip']['workflow_blob'], hosting_blob)
                self.assertEqual(self.check['conclusion'], 'SKIPPED')
                self.check['_optional_skip']['workflow_blob'] = 'unreviewed'
                self.assertIsNotNone(c.checks_blocker(self.pr))
                # New workflow recognition still requires fresh run provenance.
                self.run['pull_requests'][0]['base']['sha'] = 'stale'
                self.annotate()
                self.assertNotIn('_optional_skip', self.check)
                self.assertIsNotNone(c.checks_blocker(self.pr))

    def test_required_unknown_and_live_skips_block(self):
        for name in ('check','build','Security CodeQL (rust)','gate','unknown'):
            self.setup_evidence(name);self.annotate()
            self.assertIsNotNone(c.checks_blocker(self.pr))

    def test_changed_untrusted_or_incomplete_evidence_blocks(self):
        mutations = [lambda:self.tree.update(truncated=True),
                     lambda:self.tree['tree'][0].update(sha='unknown'),
                     lambda:self.run.update(event='push'),
                     lambda:self.run.update(event='workflow_dispatch'),
                     lambda:self.run.update(path='.github/workflows/live-gates.yml'),
                     lambda:self.run.update(head_sha='wrong'),
                     lambda:self.run['head_repository'].update(full_name='foreign/repo'),
                     lambda:self.run.update(pull_requests=[]),
                     lambda:self.run['pull_requests'][0]['base'].update(sha='stale'),
                     lambda:self.run.update(status='in_progress'),
                     lambda:self.run.update(conclusion='failure'),
                     lambda:self.jobs.update(total_count=101),
                     lambda:self.jobs['jobs'].append(copy.deepcopy(self.job)),
                     lambda:self.build.update(conclusion='skipped'),
                     lambda:self.job.update(head_sha='wrong'),
                     lambda:self.job.update(run_id=99),
                     lambda:self.verified['app'].update(slug='foreign'),
                     lambda:self.verified['check_suite'].update(id=99),
                     lambda:self.pr.update(isCrossRepository=True),
                     lambda:self.pr.update(baseIncluded=False),
                     lambda:self.check.update(detailsUrl='https://github.com/foreign/repo/actions/runs/1/job/2')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.setup_evidence();mutate();self.annotate()
                self.assertIsNotNone(c.checks_blocker(self.pr))

    def test_rerun_or_unavailable_evidence_removes_previous_proof(self):
        self.setup_evidence();self.annotate()
        self.assertIsNone(c.checks_blocker(self.pr))
        calls = []
        def rerun(path):
            data = copy.deepcopy(self.data[path])
            if path == self.path:
                calls.append(path)
                if len(calls)>1:data['run_attempt']=2
            return data
        self.annotate(rerun)
        self.assertIsNotNone(c.checks_blocker(self.pr))
        def unavailable(path):raise RuntimeError('private transport error')
        self.annotate(unavailable)
        self.assertIsNotNone(c.checks_blocker(self.pr))

    def test_testnet_and_other_required_checks_remain_required(self):
        self.setup_evidence();self.annotate()
        self.pr['_testnet_required']=True
        self.assertIsNotNone(c.checks_blocker(self.pr))
        self.pr['_testnet_status']=dict(state='success',description='base:'+self.pr['baseRefOid'])
        self.assertIsNone(c.checks_blocker(self.pr))
        self.pr['statusCheckRollup'].append(dict(__typename='CheckRun',name='security',status='COMPLETED',conclusion='SKIPPED'))
        self.assertIsNotNone(c.checks_blocker(self.pr))
