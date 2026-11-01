"""Held source receipts still use the existing bounded CI repair machinery."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_ci_repairs as repairs
from test_coordinator import c


class HeldCI(unittest.TestCase):
    def test_held_source_failure_observed_and_reserved_once(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, project, pr, owner = repairs.CIRepairs().setup(directory)
            project['integration_batch'] = dict(enabled=True)
            launches = []
            def reserve(project, current, owner, kind):
                self.assertEqual(kind, 'fix')
                self.assertEqual(current['_ci_repair'], c.failed_checks(pr))
                job = engine.store.reserve(project['id'], current, owner['agent'], kind, Path(directory)/'fix')
                if job:launches.append(job)
            # Another batch owns integration. Both it and waiting source receipts
            # are held by the real batch controller's scheduling contract.
            with patch.object(engine.batches, 'tick', return_value={pr['number'], 999}), \
                 patch.object(engine, 'source_receipt', return_value={'source_complete':True}), \
                 patch.object(engine, 'launch', side_effect=reserve), patch.object(engine, 'merge') as merge:
                engine.tick_project(project, True)
                engine.tick_project(project, True)
                merge.assert_not_called()
            self.assertEqual(len(launches), 1)
            receipt = engine.store.db.execute('select * from ci_observations').fetchone()
            self.assertEqual(receipt['head'], pr['headRefOid'])
            self.assertEqual(receipt['base'], pr['baseRefOid'])
            self.assertEqual(json.loads(receipt['failures']), c.failed_checks(pr))

    def test_held_source_preserves_worker_budget_and_lease_fences(self):
        for fence in ('review','fix','budget','backoff','expired','unpublished','active'):
            with self.subTest(fence=fence), tempfile.TemporaryDirectory() as directory:
                engine, project, pr, owner = repairs.CIRepairs().setup(directory)
                project['integration_batch'] = dict(enabled=True)
                if fence in ('review','fix'):
                    engine.store.reserve(project['id'], {**pr,'comments':[{'id':'x','body':'new'}]},
                                         'Claude', fence, Path(directory)/'existing')
                elif fence in ('budget','backoff'):
                    engine.store.db.execute('insert into ci_check_budget values(?,?,?,?,?)',
                                            (project['id'],pr['number'],'*',100 if fence=='budget' else 1,9999999999 if fence=='backoff' else 0))
                elif fence=='expired':owner['expired']=True
                elif fence=='unpublished':owner['publish_pending']=True
                else:owner['status']='in_progress'
                reservations = []
                def reserve(project, current, owner, kind):
                    reservations.append(engine.store.reserve(project['id'],current,owner['agent'],kind,Path(directory)/'new'))
                with patch.object(engine.batches,'tick',return_value={pr['number']}), \
                     patch.object(engine,'source_receipt',return_value={}), \
                     patch.object(engine,'retry_review_publication',return_value=None), \
                     patch.object(engine,'launch',side_effect=reserve) as launch:
                    engine.tick_project(project, True)
                    if fence in ('expired','unpublished','active','budget','backoff'):launch.assert_not_called()
                self.assertTrue(all(job is None for job in reservations))
                self.assertEqual(engine.store.db.execute('select count(*) from ci_observations').fetchone()[0],1)
                self.assertEqual(engine.store.db.execute('select count(*) from ci_repairs').fetchone()[0],0)

    def test_green_held_source_gets_no_duplicate_review(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, project, pr, owner = repairs.CIRepairs().setup(directory)
            project['integration_batch'] = dict(enabled=True)
            pr['statusCheckRollup'][0]['conclusion']='SUCCESS'
            with patch.object(engine.batches,'tick',return_value={pr['number']}), \
                 patch.object(engine,'source_receipt',return_value={'source_complete':True}), \
                 patch.object(engine,'launch') as launch, patch.object(engine,'merge') as merge:
                engine.tick_project(project, True)
                launch.assert_not_called();merge.assert_not_called()
