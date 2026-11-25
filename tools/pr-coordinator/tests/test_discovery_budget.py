"""Avoid expensive polling when local scheduling state cannot admit work."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from test_streaming_source_reviews import c, fixture

class DiscoveryBudget(unittest.TestCase):
    def setup(self, directory, lane):
        engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[],
                              'review_worker_limits':{'Claude':{'review':2}}},Path(directory))
        engine.lane=lane
        rows=[];owners=[];prs={}
        for number in range(1,11):
            pr,owner=fixture(number);pr['headRefName']=owner['branch']=f'work/{number}'
            owner['spec']['area']='frontend';owners.append(owner);prs[number]=pr
            rows.append(dict(number=number,head=dict(ref=owner['branch'],sha=pr['headRefOid'])))
        engine.gh=Mock(return_value=json.dumps([rows]));engine.pr=Mock(side_effect=lambda p,n,*a:prs[n])
        return engine,dict(id='p',repository='a/b',integration_batch={'enabled':True}),owners,prs

    def test_source_stops_full_snapshots_when_capacity_fills(self):
        with tempfile.TemporaryDirectory() as d:
            engine,project,owners,prs=self.setup(d,'source')
            def admit(pr):
                self.assertIsNotNone(engine.store.reserve('p',pr,'Claude','review',Path(d)/str(pr['number'])))
            result=engine.prs(project,admit,owners)
            self.assertEqual([p['number'] for p in result],[1,2])
            self.assertEqual(engine.pr.call_count,2,'remaining eight PRs need no expensive snapshot')
            engine.pr.reset_mock()
            self.assertEqual(engine.prs(project,admit,owners),[])
            engine.pr.assert_not_called()

    def test_pending_source_reviews_do_not_consume_integration_reads(self):
        with tempfile.TemporaryDirectory() as d:
            engine,project,owners,prs=self.setup(d,'integration')
            self.assertEqual(engine.prs(project,lambda pr:None,owners),[])
            engine.pr.assert_not_called()
            # A completed receipt is only a scheduling hint: fresh PR data is
            # still mandatory before selecting or publishing this source.
            job=engine.store.reserve('p',prs[3],'Claude','review',Path(d)/'source')
            engine.store.db.execute("UPDATE jobs SET status='done',result='{}' WHERE id=?",(job,))
            self.assertEqual(engine.prs(project,lambda pr:None,owners),[prs[3]])
            engine.pr.assert_called_once()
            engine.pr.reset_mock()
            prs[3]['headRefOid']='d'*40
            rows=json.loads(engine.gh.return_value);rows[0][2]['head']['sha']='d'*40
            engine.gh.return_value=json.dumps(rows)
            self.assertEqual(engine.prs(project,lambda pr:None,owners),[])
            engine.pr.assert_not_called()

    def test_reserved_members_skip_discovery_but_candidate_remains_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            engine,project,owners,prs=self.setup(d,'integration')
            engine.store.db.execute('INSERT INTO integration_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('p','batch','awaiting_ci','b'*40,'a'*40,'integration/batch',9,
                 json.dumps([dict(pr=1)]),d,None,0,0,None,0,None))
            self.assertFalse(engine.discovery_needed(project,dict(number=1,head=dict(ref='work/1',sha='a'*40)),owners))
            self.assertEqual(engine.prs(project,lambda pr:None,owners),[prs[9]])
            self.assertEqual(engine.pr.call_args.args[1],9)

    def test_unpublished_batch_keeps_member_snapshots_on_next_scan(self):
        # Below-capacity batches wait a collection interval. Their next scan
        # must still include their reserved sources, or advance() sees an empty
        # eligible list and invalidates the batch instead of building it.
        for phase in ('collecting', 'building'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as d:
                engine,project,owners,prs=self.setup(d,'integration')
                engine.store.db.execute('INSERT INTO integration_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('p','batch',phase,'b'*40,None,'integration/batch',None,
                     json.dumps([dict(pr=1)]),d,None,0,0,None,0,None))
                self.assertEqual(engine.prs(project,lambda pr:None,owners),[prs[1]])
                engine.pr.assert_called_once()
                # Discovery remains a fresh read, including a changed source
                # head. It cannot authorize reuse of the old reviewed head.
                engine.pr.reset_mock()
                prs[1]['headRefOid']='e'*40
                self.assertEqual(engine.prs(project,lambda pr:None,owners)[0]['headRefOid'],'e'*40)
                engine.pr.assert_called_once()

    def test_local_infrastructure_and_unknown_prs_keep_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            engine,project,owners,prs=self.setup(d,'integration')
            owners[1]['spec']['area']='infra'
            self.assertTrue(engine.discovery_needed(project,dict(number=2,head=dict(ref='work/2',sha='a'*40)),owners))
            self.assertTrue(engine.discovery_needed(project,dict(number=99,head=dict(ref='untracked',sha='a'*40)),owners))

