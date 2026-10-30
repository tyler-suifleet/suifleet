"""Pipeline states explain work without changing ticket ownership or totals."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_dashboard import Fixture, NOW, dash

HEAD = 'a' * 40
BASE = 'b' * 40
FINGERPRINT = 'c' * 64


def projection(**overrides):
    result = dict(pr=7, title='Example', branch='work/sf-001', head=HEAD[:12], head_oid=HEAD,
                base_oid=BASE, source_head=HEAD, source_fingerprint=FINGERPRINT,
                unresolved_threads=False, mergeable='MERGEABLE', draft=False,
                checks={'success': 5, 'pending': 0, 'failed': 0}, testnet='success',
                mtime=NOW-10)
    result.update(overrides)
    return result


class FreshObservationTests(unittest.TestCase):
    def test_active_generation_helper_is_preferred_to_legacy_mirror(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'current').mkdir()
            (root/'coordinator.py').write_text('def source_feedback(data): return "legacy"\n')
            (root/'current/coordinator.py').write_text('def source_feedback(data): return "active"\n')
            with patch.object(dash,'__file__',str(root/'dashboard.py')),patch.object(dash,'_SOURCE_MODULE',None):
                self.assertEqual(dash.source_fingerprint(dict(headRefOid=HEAD,comments=[],reviews=[],threads=[])),'active')

    def test_safe_observation_refreshes_checks_and_rejects_other_repositories(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=root/'p/pr-observations';folder.mkdir(parents=True)
            raw=dict(schema=1,project='p',repository='owner/repo',observed=NOW-1,source_fingerprint=FINGERPRINT,
                     pr=dict(number=7,headRefOid=HEAD,baseRefOid=BASE,headRefName='work',state='OPEN',
                             threads=[],statusCheckRollup=[dict(__typename='CheckRun',status='COMPLETED',conclusion='SUCCESS')]))
            (folder/'pr-7.json').write_text(json.dumps(raw))
            args=({'reviews':root/'reviews.sqlite3'},{'projects':[dict(id='p',repository='owner/repo')]},NOW,None)
            value=dash.observed_prs(*args)[('p',7)]
            self.assertEqual(value['checks'],dict(success=1,pending=0,failed=0))
            self.assertEqual(value['source_fingerprint'],FINGERPRINT)
            raw['repository']='different/repo';(folder/'pr-7.json').write_text(json.dumps(raw))
            self.assertEqual(dash.observed_prs(*args),{})


class PipelineStageTests(unittest.TestCase):
    def test_source_ready_in_batch_mode_does_not_wait_on_individual_testnet(self):
        snapshot=projection(testnet='missing',checks=dict(success=2,pending=3,failed=0))
        proof=dict(source_complete=True,acceptance_ready=True,verdict='blocked',acceptance_complete=False)
        self.assertEqual(dash.pipeline_stage(snapshot,proof,now=NOW,batch_mode=True)[0],'ready-for-batch')
        self.assertEqual(dash.pipeline_stage(snapshot,proof,now=NOW)[0],'awaiting-ci')

    def test_each_wait_is_distinct_and_no_prose_grants_source_approval(self):
        current = projection()
        source = dict(source_complete=True, acceptance_ready=True, verdict='blocked', acceptance_complete=False)
        cases = [
            ({'active': True}, 'actively-working'),
            ({'review': dict(verdict='changes_requested')}, 'changes-requested'),
            ({'review': dict(evidence='no actionable defects, source complete')}, 'needs-source-review'),
            ({'snapshot': projection(checks={'success': 3, 'pending': 2, 'failed': 0})}, 'awaiting-ci'),
            ({'snapshot': projection(testnet='missing')}, 'awaiting-acceptance'),
            ({'review': dict(source, acceptance_ready=False)}, 'awaiting-acceptance'),
            ({}, 'awaiting-evidence-review'),
            ({'review': dict(source, verdict='approve', acceptance_complete=True, base_oid=BASE)}, 'ready-to-merge'),
            ({'review': dict(source, verdict='approve', acceptance_complete=True, base_oid='d'*40)}, 'awaiting-evidence-review'),
        ]
        for changes, expected in cases:
            options = dict(snapshot=current, review=source, now=NOW)
            options.update(changes)
            with self.subTest(expected=expected):
                self.assertEqual(dash.pipeline_stage(**options)[0], expected)

    def test_failed_and_unavailable_checks_are_not_approval(self):
        source = dict(source_complete=True, acceptance_ready=True, verdict='approve',
                      acceptance_complete=True, base_oid=BASE)
        cases = [
            (projection(checks={'success': 4, 'pending': 0, 'failed': 1}), 'ci-failed'),
            (projection(testnet='error'), 'ci-failed'),
            (projection(mergeable='CONFLICTING'), 'conflict'),
            (projection(unresolved_threads=True), 'changes-requested'),
            (projection(source_fingerprint=None), 'unavailable'),
            (projection(mtime=NOW-dash.CI_MAX_AGE-1), 'unavailable'),
            (projection(ci_mismatch=True), 'unavailable'),
            (None, 'unavailable'),
        ]
        for snapshot, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(dash.pipeline_stage(snapshot, source, now=NOW)[0], expected)

    def test_batch_members_and_candidate_have_different_waits(self):
        batch = dict(id='batch-1', base=BASE, head=HEAD, status='awaiting_ci')
        self.assertEqual(dash.pipeline_stage(projection(), None, batch=batch, now=NOW)[0], 'queued-in-batch')
        self.assertEqual(dash.pipeline_stage(projection(checks={'success': 0, 'pending': 3, 'failed': 0}),
                                           None, batch=batch, candidate=True, now=NOW)[0], 'awaiting-ci')
        self.assertEqual(dash.pipeline_stage(projection(), None, batch=batch, candidate=True, now=NOW)[0],
                         'awaiting-evidence-review')
        batch['status'] = 'ready'
        self.assertEqual(dash.pipeline_stage(projection(), None, batch=batch, candidate=True, now=NOW)[0],
                         'ready-to-merge')
        self.assertEqual(dash.pipeline_stage(projection(base_oid='d'*40), None, batch=batch, candidate=True, now=NOW)[0],
                         'unavailable')


class PipelineCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Fixture(self.temp.name)
        # Hash availability and comparison, not coordinator hash semantics, is
        # under test here. The real helper's implementation is tested separately.
        self.helper = patch.object(dash, 'source_fingerprint', side_effect=lambda raw: raw.get('_source_test', FINGERPRINT))
        self.helper.start()
        self.addCleanup(self.helper.stop)

    def job(self, pr, ticket='SF-001', **result):
        f = self.fixture
        folder = f.job(pr, status='done', family='Codex', ticket=ticket)
        path = folder/'snapshot.json'
        raw = json.loads(path.read_text())
        raw.update(baseRefOid=BASE, mergeable='MERGEABLE', reviews=[], threads=[], _source_test=FINGERPRINT)
        path.write_text(json.dumps(raw))
        os.utime(path, (NOW-10, NOW-10))
        result = dict(verdict='blocked', acceptance_complete=False, source_complete=True,
                      acceptance_ready=True, model='reviewer', evidence='private details', **result)
        with sqlite3.connect(f.paths['reviews']) as db:
            db.execute('UPDATE jobs SET result=?,publication_done=1 WHERE id=?', (json.dumps(result), f.job_id))
        return folder

    def batches(self, members, pr=90, status='awaiting_ci'):
        with sqlite3.connect(self.fixture.paths['reviews']) as db:
            db.execute('''CREATE TABLE integration_batches(project TEXT,id TEXT,status TEXT,base TEXT,head TEXT,
                pr INTEGER,members TEXT,updated REAL)''')
            db.execute('INSERT INTO integration_batches VALUES(?,?,?,?,?,?,?,?)',
                       ('suifleet', 'batch-1', status, BASE, hashlib.sha1(str(pr).encode()).hexdigest(), pr,
                        json.dumps(members), NOW-5))

    def test_distinct_pipeline_states_preserve_authoritative_queue_totals(self):
        f = self.fixture
        for i in range(1,4):
            f.ticket(f'SF-{i:03}', status='review')
        folders = [self.job(i, ticket=f'SF-{i:03}') for i in range(1,4)]
        path = folders[0]/'snapshot.json'
        raw = json.loads(path.read_text());raw['statusCheckRollup'][0]['conclusion'] = 'PENDING'
        path.write_text(json.dumps(raw));os.utime(path,(NOW-10,NOW-10))
        with sqlite3.connect(f.paths['reviews']) as db:
            db.execute('UPDATE jobs SET result=? WHERE pr=2',
                       (json.dumps(dict(verdict='blocked', source_complete=True, acceptance_ready=False)),))
            db.execute('UPDATE jobs SET result=? WHERE pr=3',
                       (json.dumps(dict(verdict='blocked', evidence='source complete, no defects')),))
        model = f.collect(project='suifleet')
        self.assertEqual(model['queue']['suifleet'], {'review': 3})
        self.assertEqual(model['pipeline']['total'], 3)
        self.assertEqual(model['pipeline']['counts'], {'awaiting-ci': 1, 'awaiting-acceptance': 1, 'needs-source-review': 1})
        self.assertEqual(model['pipeline']['batch_receipts'], 'unavailable')
        self.assertIn('Batch status unavailable', '\n'.join(dash.render(model)))
        self.assertNotIn('private details', json.dumps(model))

    def test_batch_candidates_do_not_add_tickets_or_duplicate_members(self):
        f = self.fixture
        for i in [1,2]:
            f.ticket(f'SF-{i:03}', status='review')
            self.job(i, ticket=f'SF-{i:03}')
        candidate = self.job(90, ticket='integration:batch-1')
        path = candidate/'snapshot.json';raw=json.loads(path.read_text())
        raw['statusCheckRollup'][0]['conclusion']='PENDING';path.write_text(json.dumps(raw));os.utime(path,(NOW-10,NOW-10))
        self.batches([dict(pr=i,head=hashlib.sha1(str(i).encode()).hexdigest(),owner={'claim_hash':'never-show'}) for i in [1,2]])
        before = f.paths['reviews'].read_bytes()
        model = f.collect(project='suifleet')
        self.assertEqual(f.paths['reviews'].read_bytes(), before)
        self.assertEqual(model['queue']['suifleet'], {'review': 2})
        self.assertEqual(model['inventory']['suifleet']['tracked_open'], 2)
        self.assertEqual(model['pipeline']['counts'], {'queued-in-batch': 2, 'awaiting-ci': 1})
        self.assertEqual(sum(r['integration'] for r in model['pipeline']['rows']), 1)
        self.assertEqual(model['pipeline']['total'], 3)
        rendered='\n'.join(dash.render(model))
        self.assertIn('2 source PRs · 1 integration candidates',rendered)
        self.assertNotIn('never-show',json.dumps(model))

    def test_unpublished_same_family_and_changed_feedback_receipts_are_not_reused(self):
        f = self.fixture;f.ticket('SF-001',status='review');self.job(7)
        for field,value in [('publication_done',0),('family','Claude')]:
            with sqlite3.connect(f.paths['reviews']) as db:
                db.execute(f'UPDATE jobs SET {field}=? WHERE pr=7',(value,))
            model=f.collect();self.assertEqual(model['pipeline']['counts'],{'needs-source-review':1})
            with sqlite3.connect(f.paths['reviews']) as db:
                db.execute("UPDATE jobs SET publication_done=1,family='Codex' WHERE pr=7")
        snapshots={('suifleet',7):projection()}
        job=dict(id=1,project='suifleet',pr=7,kind='review',status='done',publication_done=1,
                 head=HEAD,family='Codex',directory='unused',result=json.dumps(dict(source_complete=True,acceptance_ready=True)))
        class Changed:
            def get(self, directory):return projection(source_fingerprint='different')
        tickets={('suifleet','SF-001'):dict(key='SF-001',branch='work/sf-001',agent='Claude')}
        result=dash.derive_pipeline(snapshots,[job],tickets,[],{'state':'ok','rows':[]},Changed(),NOW)
        self.assertEqual(result['counts'],{'needs-source-review':1})

    def test_corrupt_batch_receipts_do_not_hide_worker_sources(self):
        f=self.fixture;f.ticket('SF-001',status='review');self.job(7)
        self.batches([dict(pr=7,head='bad-head')])
        model=f.collect()
        self.assertEqual(model['sources']['reviews']['state'],'ok')
        self.assertEqual(model['pipeline']['batch_receipts'],'unavailable')
        self.assertEqual(model['pipeline']['total'],1)

    def test_stale_snapshot_is_unavailable_even_with_old_approval(self):
        f=self.fixture;f.ticket('SF-001',status='review');folder=self.job(7)
        os.utime(folder/'snapshot.json',(NOW-1800,NOW-1800))
        model=f.collect()
        self.assertEqual(model['pipeline']['counts'],{'unavailable':1})


if __name__ == '__main__':unittest.main()
