"""Legacy build diagnostics cannot stop unrelated recovery or grant retries."""
import json
import unittest
from unittest.mock import patch

import test_batch_infrastructure as infrastructure
import test_funding_recovery as funding

DIAGNOSTICS = ('CalledProcessError: git diff --check failed', '{partial',
               '[]', 'null', '"build failed"', '42')


def legacy_row(f, row, diagnostic):
    old = dict(row, id='legacy', error=diagnostic, created=row['created'] + 1,
               updated=row['updated'] + 1, members='[]')
    columns = list(old)
    f.db.execute('INSERT INTO integration_batches (' + ','.join(columns) + ') VALUES ('
                 + ','.join('?' for _ in columns) + ')', [old[k] for k in columns])
    return old


class ErrorHistory(unittest.TestCase):
    def fixture(self, helper):
        self.addCleanup(helper.doCleanups)
        return helper

    def test_infrastructure_retry_survives_mixed_history_without_retrying_diagnostics(self):
        for diagnostic in DIAGNOSTICS:
            with self.subTest(diagnostic=diagnostic):
                f, row = self.fixture(infrastructure.InfrastructureRetryTests()).failed()
                old = legacy_row(f, row, diagnostic)
                members = json.loads(row['members'])
                f.controller.clock = lambda: row['updated'] + 301
                with patch.object(f.controller, 'infrastructure_receipt', return_value={'run': 10}) as proof:
                    f.controller.recover_infrastructure(f.project, members, row['base'])
                    proof.assert_called_once()
                    self.assertEqual(proof.call_args.args[1]['id'], row['id'])
                self.assertEqual(next(r for r in f.controller.rows(f.project) if r['id']=='legacy'), old)
                self.assertEqual(f.controller.repair_context(f.project, f.source_prs[1]), {})
                self.assertFalse(f.reconciled)

    def test_funding_recovery_preserves_legacy_errors_and_real_failure_fence(self):
        for diagnostic in DIAGNOSTICS:
            with self.subTest(diagnostic=diagnostic):
                f, recovery, row = self.fixture(funding.FundingRecovery()).ready()
                old = legacy_row(f, row, diagnostic)
                members = json.loads(row['members'])
                self.assertEqual(f.controller.select(f.project, members, row['base'], 1), [])
                self.assertEqual(recovery.tick(f.project)['state'], 'awaiting_validation')
                recovery.funding_receipt.assert_called_once()
                self.assertEqual(len(f.controller.select(f.project, members, row['base'], 1)), 1)
                self.assertEqual(next(r for r in f.controller.rows(f.project) if r['id']=='legacy'), old)
                self.assertFalse(f.reconciled)
