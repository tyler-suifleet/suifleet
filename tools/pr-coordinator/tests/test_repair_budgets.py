"""Different failed checks must not exhaust each other's repair allowance."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_ci_repairs as repairs
from test_coordinator import c


class RepairBudgets(unittest.TestCase):
    def test_three_unrelated_repairs_do_not_block_first_testnet_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, project, pr, _ = repairs.CIRepairs().setup(directory)
            for n, name in enumerate(('compiler-lints-regressions', 'testnet-preflight', 'compiler-lints-regressions')):
                pr['statusCheckRollup'] = [{**repairs.failure(n+1), 'name':name}]
                with patch.object(c.time, 'time', return_value=1000+n*1000):
                    self.assertIsNotNone(repairs.CIRepairs().reserve(engine, project, pr))
                repairs.CIRepairs().finish(engine)
            pr['statusCheckRollup'] = [{**repairs.failure(4), 'name':'testnet/package-upgrade'}]
            with patch.object(c.time, 'time', return_value=9000):
                self.assertIsNotNone(repairs.CIRepairs().reserve(engine, project, pr))
            repairs.CIRepairs().finish(engine)
            pr['statusCheckRollup'] = [{**repairs.failure(5), 'name':'compiler-lints-regressions'}]
            with patch.object(c.time, 'time', return_value=10000):
                self.assertIsNotNone(repairs.CIRepairs().reserve(engine, project, pr))
            repairs.CIRepairs().finish(engine)
            pr['headRefOid'] = 'c'*40
            pr['statusCheckRollup'] = [{**repairs.failure(6), 'name':'compiler-lints-regressions'}]
            with patch.object(c.time, 'time', return_value=20000):
                self.assertIsNone(repairs.CIRepairs().reserve(engine, project, pr))

    def legacy(self, directory, with_snapshot):
        engine, project, pr, _ = repairs.CIRepairs().setup(directory)
        for n, name in enumerate(('compiler', 'preflight', 'compiler')):
            pr['statusCheckRollup'] = [{**repairs.failure(n+1), 'name':name}]
            current = {**pr, '_ci_repair':c.failed_checks(pr)}
            folder = Path(directory)/str(n);folder.mkdir()
            with patch.object(c.time, 'time', return_value=1000+n*1000):
                job = engine.store.reserve(project['id'], current, 'Codex', 'fix', folder)
            self.assertIsNotNone(job)
            if with_snapshot:(folder/'snapshot.json').write_text(json.dumps(current))
            repairs.CIRepairs().finish(engine)
        engine.store.db.execute('DROP TABLE ci_check_budget')
        engine.store.db.close()
        return c.Store(Path(directory)/'reviews.sqlite3')

    def test_migration_reconstructs_per_check_limits_from_verified_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.legacy(directory, True)
            rows = store.db.execute('SELECT check_key,attempts FROM ci_check_budget').fetchall()
            self.assertEqual(sorted(row['attempts'] for row in rows), [1,2])
            self.assertFalse(any(row['check_key']=='*' for row in rows))

    def test_missing_historical_evidence_keeps_conservative_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.legacy(directory, False)
            row = store.db.execute("SELECT attempts FROM ci_check_budget WHERE check_key='*'").fetchone()
            self.assertEqual(row['attempts'], 3)
