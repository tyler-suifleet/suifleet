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


class ConfigurableRepairLimit(unittest.TestCase):
    def test_raise_limit_preserves_attempts_backoff_and_duplicate_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, project, pr, _ = repairs.CIRepairs().setup(directory)
            for run in range(1, 4):
                pr['statusCheckRollup'] = [repairs.failure(run)]
                with patch.object(c.time, 'time', return_value=run*10000):
                    self.assertIsNotNone(repairs.CIRepairs().reserve(engine, project, pr))
                repairs.CIRepairs().finish(engine)
            engine.store.db.close()
            config = {**engine.config, 'ci_repair_attempt_limit': 8}
            engine = c.Coordinator(config, Path(directory))
            self.assertEqual(engine.store.db.execute('SELECT attempts FROM ci_check_budget').fetchone()[0], 3)
            with patch.object(c.time, 'time', return_value=30001):
                self.assertIsNone(repairs.CIRepairs().reserve(engine, project, pr))
                pr['statusCheckRollup'] = [repairs.failure(4)]
                self.assertIsNone(repairs.CIRepairs().reserve(engine, project, pr))
            for run in range(4, 9):
                pr['statusCheckRollup'] = [repairs.failure(run)]
                with patch.object(c.time, 'time', return_value=run*10000):
                    self.assertIsNotNone(repairs.CIRepairs().reserve(engine, project, pr))
                repairs.CIRepairs().finish(engine)
            pr['statusCheckRollup'] = [repairs.failure(9)]
            with patch.object(c.time, 'time', return_value=100000):
                self.assertIsNone(repairs.CIRepairs().reserve(engine, project, pr))
                self.assertIn('8 attempts', engine.store.ci_repair_blocker(project['id'], {**pr, '_ci_repair': c.failed_checks(pr)}))
            self.assertEqual(engine.store.db.execute('SELECT attempts FROM ci_check_budget').fetchone()[0], 8)

    def test_invalid_limits_rejected(self):
        for limit in (None, True, False, 0, -1, 101, 3.5, '8'):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'ci_repair_attempt_limit'):
                    c.Store(Path(directory)/'state.sqlite3', ci_repair_attempt_limit=limit)
