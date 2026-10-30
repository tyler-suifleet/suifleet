"""Real Git and durable-review proofs for combined runtime activation."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import coordinator as c
spec = importlib.util.spec_from_file_location('batch_activation', SOURCE/'activation.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class BatchActivation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'; self.repo.mkdir()
        self.state = self.root/'state'; self.state.mkdir()
        self.library = self.root/'library'; self.library.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Tyler Bush')
        self.git('config', 'user.email', 'tyler.jonathan.bush@gmail.com')
        self.main = self.git('branch', '--show-current')
        runtime = self.repo/a.PREFIX; runtime.mkdir(parents=True)
        (runtime/'worker.py').write_text('# worker\n')
        (runtime/'dispatcher.py').write_text('# dispatcher\n')
        workflow = self.repo/'.github/workflows'; workflow.mkdir(parents=True)
        (workflow/'testnet.yml').write_text('# required protected testnet\n')
        self.commit('base')
        self.base = self.git('rev-parse', 'HEAD')
        self.heads = []
        for index, name in enumerate(('worker.py', 'dispatcher.py'), 1):
            self.git('checkout', '-qb', f'source-{index}', self.base)
            (runtime/name).write_text(f'# reviewed change {index}\n')
            self.commit(f'source {index}')
            self.heads.append(self.git('rev-parse', 'HEAD'))
        self.git('checkout', '-q', self.main)
        self.steps = []
        for index, head in enumerate(self.heads, 1):
            self.git('merge', '--no-ff', '-qm', f'batch member {index}', head)
            self.steps.append(self.git('rev-parse', 'HEAD'))
        self.head = self.steps[-1]
        self.db = sqlite3.connect(self.state/'reviews.sqlite3')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE activation_merges(project,pr,head,base,commit_id,review_id);
            CREATE TABLE jobs(id PRIMARY KEY,project,pr,head,family,kind,status,publication_done,result,directory,feedback);
            CREATE TABLE integration_batches(project,id,status,base,head,branch,pr,members,directory,error,created,updated,review_id,publish_intent,manifest_digest,PRIMARY KEY(project,id));
            CREATE TABLE batch_activation_commits(project,commit_id,batch_id,PRIMARY KEY(project,commit_id));
            CREATE TABLE batch_member_approvals(project,batch_id,pr,head,account,body_hash,body,status,review_id,review_node,created,updated,error,PRIMARY KEY(project,batch_id,pr));
        ''')
        self.engine = a.Activator(dict(root=str(self.repo), project='p', repository='owner/repo', github_account='user'), self.library, self.state, Mock())
        self.batch_id = 'batch-fixture'
        self.members = []
        previous = self.base
        for index, (head, step, owner) in enumerate(zip(self.heads, self.steps, ('Codex', 'Claude')), 1):
            family = 'Claude' if owner == 'Codex' else 'Codex'
            source = self.snapshot(head, self.base, dict(key=f'SF-{index:03}', agent=owner))
            result = dict(verdict='blocked', acceptance_complete=False, source_complete=True,
                          acceptance_ready=True, model='fixture-model', evidence='Source reviewed; only combined CI pending.')
            self.job(index, index, head, family, source, result)
            proof = dict(job=index, family=family, head=head, base=self.base,
                         source_feedback=c.source_feedback(source), evidence_hash=a.Activator.batch_hash(result))
            self.members.append(dict(pr=index, head=head, reviewed_base=self.base, source_receipt=proof,
                                     source_feedback=proof['source_feedback'], owner=dict(key=f'SF-{index:03}', agent=owner,
                                     model='fixture-model', session=f'session-{index}', branch=f'source-{index}', claim_hash='c'*64),
                                     commit=step, step_base=previous, acceptance=['source acceptance'], title=f'PR {index}'))
            self.db.execute('INSERT INTO batch_activation_commits VALUES(?,?,?)', ('p', step, self.batch_id))
            previous = step
        self.digest = self.manifest_digest()
        self.integration = self.snapshot(self.head, self.base, dict(key='integration:'+self.batch_id, agent='Codex'))
        self.integration['_integration_batch'] = dict(id=self.batch_id, manifest_digest=self.digest, members=copy.deepcopy(self.members))
        self.integration_result = dict(verdict='approve', acceptance_complete=True, model='fixture-model', evidence='Combined code and exact candidate CI/testnet verified.')
        self.job(99, 99, self.head, 'Claude', self.integration, self.integration_result)
        self.db.execute('INSERT INTO integration_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('p', self.batch_id, 'published', self.base, self.head, 'integration/fixture', 99,
                         json.dumps(self.members), '/unused', None, 1, 2, 99, 1, self.digest))
        self.db.commit()
        self.remote_reviews = {}
        self.approval_bodies = {}
        for member in self.members:
            proof = member['source_receipt']
            body = ('<!-- pr-coordinator -->\nIntegration-batch member acceptance publication.\n\n'
                    f"Source: PR #{member['pr']} at `{member['head']}`, independently source-reviewed by "
                    f"{proof['family']} in job {proof['job']} (evidence `{proof['evidence_hash']}`).\n"
                    f"Combined candidate: PR #99 `{self.head}` against `{self.base}`; "
                    'ordinary/security CI and any required protected testnet checks passed for that combined candidate.\n'
                    'Independent combined acceptance review: Claude job 99.\n'
                    f'Batch manifest: `{self.digest}`.\n')
            remote_id = 100 + member['pr']; node = f'REVIEW_{remote_id}'
            self.db.execute('INSERT INTO batch_member_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            ('p', self.batch_id, member['pr'], member['head'], 'review-account',
                             hashlib.sha256(body.encode()).hexdigest(), body, 'applied', remote_id, node, 1, 2, None))
            self.approval_bodies[member['pr']] = body
            self.remote_reviews[f'repos/owner/repo/pulls/{member["pr"]}/reviews/{remote_id}'] = dict(
                id=remote_id, node_id=node, commit_id=member['head'], state='APPROVED',
                body=body, user=dict(login='review-account'))
        self.db.commit()
        self.remote = dict(merged_at='now', merge_commit_sha=self.head,
                           base=dict(ref='main', repo=dict(full_name='owner/repo')),
                           head=dict(sha=self.head, repo=dict(full_name='owner/repo')))
        self.engine.gh = Mock(side_effect=lambda endpoint: copy.deepcopy(self.remote_reviews.get(endpoint, self.remote)))

    def git(self, *args):
        return a.run(['git', *args], self.repo)

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-qm', message)

    def snapshot(self, head, base, ticket):
        return dict(number=99, headRefOid=head, baseRefOid=base, baseIncluded=True,
                    comments=[], reviews=[], threads=[], ticket=ticket,
                    statusCheckRollup=[dict(__typename='CheckRun', name='check', status='COMPLETED', conclusion='SUCCESS')],
                    _testnet_required=True, _testnet_status=dict(state='success', description='base:'+base))

    def job(self, job, number, head, family, snapshot, result):
        directory = self.state/f'job-{job}'; directory.mkdir(exist_ok=True)
        (directory/'snapshot.json').write_text(json.dumps(snapshot))
        self.db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (job, 'p', number, head, family, 'review', 'done', 1, json.dumps(result), str(directory), c.feedback(snapshot)))

    def manifest_digest(self):
        return a.Activator.batch_hash(dict(project='p', id=self.batch_id, base=self.base, head=self.head, members=self.members))

    def rewrite_manifest(self):
        self.digest = self.manifest_digest()
        self.db.execute('UPDATE integration_batches SET members=?,manifest_digest=?', (json.dumps(self.members), self.digest))
        self.integration['_integration_batch'] = dict(id=self.batch_id, manifest_digest=self.digest, members=copy.deepcopy(self.members))
        self.rewrite_integration()

    def rewrite_integration(self):
        self.job(99, 99, self.head, 'Claude', self.integration, self.integration_result)
        self.db.commit()

    def verify(self, target=None):
        self.db.commit()
        self.engine.verify(self.base, target or self.head)

    def test_complete_mixed_family_batch_is_verified_once(self):
        self.verify()
        self.engine.gh.assert_called_with('repos/owner/repo/pulls/99')
        self.assertEqual(self.engine.gh.call_count, 3)
        self.assertEqual(self.git('rev-list', '--first-parent', self.base+'..'+self.head).splitlines(), list(reversed(self.steps)))

    def test_missing_receipt_and_unpublished_batch_fail_closed(self):
        self.db.execute('DELETE FROM batch_activation_commits WHERE commit_id=?', (self.steps[0],))
        with self.assertRaisesRegex(RuntimeError, 'receipt'): self.verify()
        self.db.execute('INSERT INTO batch_activation_commits VALUES(?,?,?)', ('p', self.steps[0], self.batch_id))
        for status in ('candidate_published', 'awaiting_ci', 'invalidated'):
            with self.subTest(status=status):
                self.db.execute('UPDATE integration_batches SET status=?', (status,))
                with self.assertRaisesRegex(RuntimeError, 'publication'): self.verify()
        self.db.execute("UPDATE integration_batches SET status='published',publish_intent=0")
        with self.assertRaisesRegex(RuntimeError, 'publication'): self.verify()

    def test_digest_and_step_ancestry_are_checked(self):
        self.db.execute("UPDATE integration_batches SET manifest_digest='wrong'")
        with self.assertRaisesRegex(RuntimeError, 'digest'): self.verify()
        self.members[1]['step_base'] = self.base
        self.rewrite_manifest()
        with self.assertRaisesRegex(RuntimeError, 'ancestry'): self.verify()

    def test_partial_batch_target_cannot_activate(self):
        with self.assertRaises(subprocess.CalledProcessError): self.verify(self.steps[0])

    def test_untyped_historical_blocked_source_review_is_not_approval(self):
        raw = json.loads(self.db.execute('SELECT result FROM jobs WHERE id=1').fetchone()[0])
        raw.pop('source_complete'); raw.pop('acceptance_ready')
        raw['evidence'] = 'No actionable findings; please merge.'
        self.db.execute('UPDATE jobs SET result=? WHERE id=1', (json.dumps(raw),))
        self.members[0]['source_receipt']['evidence_hash'] = a.Activator.batch_hash(raw)
        self.rewrite_manifest()
        with self.assertRaisesRegex(RuntimeError, 'source review proof'): self.verify()

    def test_source_review_changed_or_unpublished_is_rejected(self):
        self.db.execute('UPDATE jobs SET publication_done=0 WHERE id=1')
        with self.assertRaisesRegex(RuntimeError, 'review is unavailable'): self.verify()
        self.db.execute("UPDATE jobs SET publication_done=1,result=? WHERE id=1", (json.dumps(dict(verdict='approve', acceptance_complete=True, source_complete=True, acceptance_ready=True, model='fixture-model', evidence='changed')),))
        with self.assertRaisesRegex(RuntimeError, 'source review proof'): self.verify()

    def test_nonindependent_source_and_integration_reviews_are_rejected(self):
        self.db.execute("UPDATE jobs SET family='Codex' WHERE id=1")
        with self.assertRaisesRegex(RuntimeError, 'independent'): self.verify()
        self.db.execute("UPDATE jobs SET family='Claude' WHERE id=1")
        self.db.execute("UPDATE jobs SET family='Codex' WHERE id=99")
        with self.assertRaisesRegex(RuntimeError, 'independent'): self.verify()

    def test_source_snapshot_feedback_and_ticket_binding_are_checked(self):
        path = self.state/'job-1/snapshot.json'
        snapshot = json.loads(path.read_text())
        snapshot['comments'].append(dict(id='new-request', body='Unresolved code finding', updatedAt='now'))
        path.write_text(json.dumps(snapshot))
        with self.assertRaisesRegex(RuntimeError, 'source review proof'): self.verify()
        snapshot['comments'] = []
        snapshot['ticket']['key'] = 'SF-OTHER'
        path.write_text(json.dumps(snapshot))
        with self.assertRaisesRegex(RuntimeError, 'source review proof'): self.verify()

    def test_stale_integration_feedback_receipt_is_rejected(self):
        self.db.execute("UPDATE jobs SET feedback='stale' WHERE id=99")
        with self.assertRaisesRegex(RuntimeError, 'review feedback'): self.verify()

    def test_approval_must_bind_exact_manifest_and_snapshot(self):
        self.integration['_integration_batch']['manifest_digest'] = 'wrong'
        self.rewrite_integration()
        with self.assertRaisesRegex(RuntimeError, 'bind the manifest'): self.verify()
        self.integration['_integration_batch']['manifest_digest'] = self.digest
        self.integration['baseRefOid'] = 'f'*40
        self.rewrite_integration()
        with self.assertRaisesRegex(RuntimeError, 'bind the manifest'): self.verify()

    def test_pending_failed_stale_or_missing_testnet_cannot_activate(self):
        for status in (None, dict(state='pending', description='base:'+self.base),
                       dict(state='failure', description='base:'+self.base),
                       dict(state='success', description='base:'+'f'*40)):
            with self.subTest(status=status):
                self.integration['_testnet_status'] = status
                self.rewrite_integration()
                with self.assertRaisesRegex(RuntimeError, 'combined checks'): self.verify()
        self.integration['_testnet_required'] = False
        self.rewrite_integration()
        with self.assertRaisesRegex(RuntimeError, 'combined checks'): self.verify()

    def test_pending_ordinary_ci_or_incomplete_approval_cannot_activate(self):
        self.integration['statusCheckRollup'][0]['conclusion'] = 'FAILURE'
        self.rewrite_integration()
        with self.assertRaisesRegex(RuntimeError, 'combined checks'): self.verify()
        self.integration['statusCheckRollup'][0]['conclusion'] = 'SUCCESS'
        self.integration_result['acceptance_complete'] = False
        self.rewrite_integration()
        with self.assertRaisesRegex(RuntimeError, 'approval is incomplete'): self.verify()

    def test_remote_candidate_must_be_merged_in_same_repository(self):
        for field, value in (('merged_at', None), ('merge_commit_sha', self.steps[0])):
            previous = self.remote[field]; self.remote[field] = value
            with self.assertRaisesRegex(RuntimeError, 'remote merged batch'): self.verify()
            self.remote[field] = previous
        self.remote['head']['repo']['full_name'] = 'foreign/repo'
        with self.assertRaisesRegex(RuntimeError, 'remote merged batch'): self.verify()

    def test_member_formal_approval_requires_complete_applied_receipt(self):
        self.db.execute("UPDATE batch_member_approvals SET status='uncertain' WHERE pr=1")
        with self.assertRaisesRegex(RuntimeError, 'approval receipt is incomplete'): self.verify()
        self.db.execute("UPDATE batch_member_approvals SET status='applied',body_hash='wrong' WHERE pr=1")
        with self.assertRaisesRegex(RuntimeError, 'approval receipt is incomplete'): self.verify()
        self.db.execute('DELETE FROM batch_member_approvals WHERE pr=1')
        with self.assertRaisesRegex(RuntimeError, 'approval receipt is missing'): self.verify()

    def test_member_formal_approval_body_must_bind_actual_proof(self):
        body = self.approval_bodies[1].replace(f'Batch manifest: `{self.digest}`.', 'Batch manifest: `different`.')
        self.db.execute('UPDATE batch_member_approvals SET body=?,body_hash=? WHERE pr=1',
                        (body, hashlib.sha256(body.encode()).hexdigest()))
        with self.assertRaisesRegex(RuntimeError, 'link source and combined evidence'): self.verify()

    def test_actual_member_review_must_match_head_account_state_and_body(self):
        endpoint = 'repos/owner/repo/pulls/1/reviews/101'
        original = copy.deepcopy(self.remote_reviews[endpoint])
        for field, value in [('commit_id', 'f'*40), ('state', 'CHANGES_REQUESTED'), ('body', 'different'),
                             ('node_id', 'wrong'), ('user', dict(login='different-account'))]:
            with self.subTest(field=field):
                self.remote_reviews[endpoint] = {**original, field: value}
                with self.assertRaisesRegex(RuntimeError, 'remote batch member approval'): self.verify()

    def test_cached_batch_does_not_authorize_an_unlisted_later_runtime_commit(self):
        path = self.repo/a.PREFIX/'worker.py'; path.write_text('# unreviewed follow-up\n')
        self.commit('unlisted runtime change')
        bad = self.git('rev-parse', 'HEAD')
        self.db.execute('INSERT INTO batch_activation_commits VALUES(?,?,?)', ('p', bad, self.batch_id))
        with self.assertRaisesRegex(RuntimeError, 'not a member'): self.verify(bad)

    def test_nonuser_source_commit_authorship_is_rejected(self):
        self.git('checkout', '-qb', 'foreign-source', self.base)
        (self.repo/a.PREFIX/'worker.py').write_text('# foreign author\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'foreign')
        bad = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', self.main)
        self.git('reset', '--hard', self.base)
        self.git('merge', '--no-ff', '-qm', 'batch foreign member', bad)
        step = self.git('rev-parse', 'HEAD')
        self.members = self.members[:1]
        self.members[0].update(head=bad, commit=step)
        proof = self.members[0]['source_receipt']; proof.update(head=bad)
        source = self.snapshot(bad, self.base, dict(key='SF-001', agent='Codex'))
        result = json.loads(self.db.execute('SELECT result FROM jobs WHERE id=1').fetchone()[0])
        proof['source_feedback'] = c.source_feedback(source); self.members[0]['source_feedback'] = proof['source_feedback']
        self.job(1, 1, bad, 'Claude', source, result)
        self.head = step; self.integration['headRefOid'] = step
        self.remote['head']['sha'] = step; self.remote['merge_commit_sha'] = step
        self.db.execute('UPDATE integration_batches SET head=?', (step,))
        self.db.execute('DELETE FROM batch_activation_commits')
        self.db.execute('INSERT INTO batch_activation_commits VALUES(?,?,?)', ('p', step, self.batch_id))
        self.rewrite_manifest()
        with self.assertRaisesRegex(RuntimeError, 'authorship'): self.verify()


if __name__ == '__main__': unittest.main()
