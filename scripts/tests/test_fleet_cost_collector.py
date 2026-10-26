import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleet_cost_collector as collector
from fleet_costs import CATEGORIES, calculate

ROOT = Path(__file__).resolve().parents[2]
A = '0x' + 'a' * 64
B = '0x' + 'b' * 64
C = '0x' + 'c' * 64
D = '2' * 43


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'run.json'
        self.db = self.root / 'state.sqlite'
        self.run = dict(schema_version=1, run_id='test', fleet_id='fixture',
                        provenance='offline synthetic evidence approved in test',
                        deployment=json.loads((ROOT / 'manifests/fixture.localnet.json').read_text()),
                        accounts=[A, B], revisions=dict(code='a'*40, workload='b'*40),
                        workload=dict(devices=2, shared_releases=1, report_seconds=30,
                                      upload_seconds=300, retention_epochs=2, poll_seconds=60,
                                      max_records=100, max_transactions=10),
                        interval=dict(start='2026-10-01T00:00:00Z', end='2026-10-15T00:00:00Z'),
                        required_categories=sorted(CATEGORIES), sources=[])
        self.tx = dict(type='transaction', id=D, at='2026-10-02T00:00:00Z',
                       source='synthetic effect fixture', sender=A, gas_owner=A,
                       category='receipts', status='success',
                       gas=dict(computationCost='100', storageCost='200', storageRebate='50'))
        self.serial = 0

    def source(self, records, **overrides):
        batch = dict(network='localnet', chain_identifier='1234abcd', run_id='test', records=records)
        batch.update(overrides)
        name = f'source-{self.serial}.json'
        self.serial += 1
        raw = json.dumps(batch).encode()
        (self.root / name).write_bytes(raw)
        self.run['sources'].append(dict(path=name, sha256=hashlib.sha256(raw).hexdigest(),
                                        provenance='offline test only', approved_by='test harness'))
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.run))

    def collect(self):
        return collector.ingest(self.path, self.db)

    def row(self, record_type, id, **values):
        return dict(type=record_type, id=id, at=self.tx['at'], source='synthetic fixture', **values)

    def test_restart_duplicate_and_accounting_export(self):
        self.source([self.tx, self.tx])
        one = self.collect()
        self.source([self.tx])
        two = self.collect()
        self.assertEqual(one['accounting_model'], two['accounting_model'])
        self.assertEqual(len(two['records']), 1)
        self.assertEqual(calculate(two['accounting_model'])['subtotals']['localnet:measured:MIST'], '250')
        self.assertFalse(two['complete_mainnet_bill'])
        self.assertIsNone(two['subscription_input'])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from history').fetchone()[0], 1)

    def test_failed_effects_charge_and_signed_cleanup(self):
        self.tx['status'] = 'failure'
        self.tx['gas']['storageRebate'] = '500'
        self.source([self.tx])
        self.assertEqual(self.collect()['accounting']['subtotals']['localnet:measured:MIST'], '-200')

    def test_unknown_then_resolved_and_audited(self):
        unknown = {k: self.tx[k] for k in ('type', 'id', 'at', 'source', 'category')}
        unknown['status'] = 'unknown'
        self.source([unknown])
        self.assertNotIn('localnet:measured:MIST', self.collect()['accounting']['subtotals'])
        self.source([self.tx])
        result = self.collect()
        self.assertEqual(result['records'][0]['status'], 'success')
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from history').fetchone()[0], 2)

    def test_settled_then_unknown_batch_commits_and_resumes(self):
        for status in ('success', 'failure'):
            with self.subTest(status=status):
                self.tx['status'] = status
                self.db = self.root / (status + '.sqlite')
                self.run['sources'] = []
                self.source([self.tx])
                self.collect()
                unknown = {k: self.tx[k] for k in ('type', 'id', 'at', 'source', 'category')}
                unknown.update(status='unknown', error='temporary read failure')
                self.source([unknown, dict(self.tx, id='3'*43)])
                result = self.collect()
                self.assertEqual(len(result['records']), 2)
                self.assertTrue(all(r['status'] == status for r in result['records']))
                self.assertEqual(result, self.collect())
                self.source([self.tx, dict(self.tx, id='4'*43)])
                self.assertEqual(len(self.collect()['records']), 3)
                with sqlite3.connect(self.db) as db:
                    self.assertEqual(db.execute('select count(*) from sources').fetchone()[0], 3)

    def test_settled_metadata_changes_preserve_original_effects(self):
        self.source([self.tx])
        original = self.collect()
        self.source([dict(self.tx, source='https://other-fullnode', raw={'adapter': 'changed'},
                          at='2026-10-02T00:00:00.000+00:00')])
        result = self.collect()
        self.assertEqual(original['records'], result['records'])
        self.assertEqual(original['accounting_model'], result['accounting_model'])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from history').fetchone()[0], 1)
            self.assertEqual(db.execute('select count(*) from sources').fetchone()[0], 2)

    def test_settled_identity_and_effect_conflicts_rejected(self):
        self.source([self.tx])
        self.collect()
        for change in (dict(status='failure'), dict(category='renewal'), dict(sender=B),
                       dict(gas_owner=B), dict(checkpoint='123'),
                       dict(at='2026-10-03T00:00:00Z')):
            with self.subTest(change=change):
                pins = copy.deepcopy(self.run['sources'])
                self.source([dict(self.tx, **change)])
                with self.assertRaisesRegex(ValueError, 'conflicting'):
                    self.collect()
                self.run['sources'] = pins
        self.source([dict(self.tx, status='unknown', category='renewal')])
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.collect()

    def test_conflict_rolls_back_whole_batch(self):
        self.source([self.tx])
        self.collect()
        tx = copy.deepcopy(self.tx)
        tx['gas']['computationCost'] = '99'
        self.source([dict(self.tx, id='3'*43), tx])
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.collect()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from records').fetchone()[0], 1)

    def test_killed_ingestion_rolls_back_and_resumes(self):
        self.source([self.tx])
        self.collect()
        self.source([dict(self.tx, id='3'*43), dict(self.tx, id='4'*43)])
        code = """
import sys, os
sys.path.insert(0, sys.argv[1])
import fleet_cost_collector as c
original = c.validate_record
def killed(row, run):
    if row['id'].startswith('4'): os._exit(17)
    original(row, run)
c.validate_record = killed
c.ingest(sys.argv[2], sys.argv[3])
"""
        result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts'), str(self.path), str(self.db)])
        self.assertEqual(result.returncode, 17)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from records').fetchone()[0], 1)
        self.assertEqual(len(self.collect()['records']), 3)

    def test_hash_pin_refuses_tampering(self):
        self.source([self.tx])
        (self.root / 'source-0.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.collect()

    def test_source_network_chain_and_run_mismatch(self):
        for field, value in [('network', 'mainnet'), ('chain_identifier', 'ffffffff'), ('run_id', 'other')]:
            self.run['sources'] = []
            self.source([self.tx], **{field: value})
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                self.collect()

    def test_resume_manifest_immutable(self):
        self.source([self.tx])
        self.collect()
        self.run['workload']['devices'] = 3
        self.save()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.collect()

    def test_bounds_and_interval(self):
        self.source([self.tx])
        self.run['workload']['max_transactions'] = 1
        self.source([dict(self.tx, id='3'*43)])
        with self.assertRaisesRegex(ValueError, 'bound'):
            self.collect()
        self.run['sources'] = []
        self.source([dict(self.tx, at='2026-11-01T00:00:00Z')])
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.collect()

    def test_invalid_manifest_and_effects(self):
        self.source([self.tx])
        for mutate in (lambda r: r['revisions'].update(code='main'),
                       lambda r: r['workload'].update(devices=0),
                       lambda r: r['deployment'].update(network='testnet'),
                       lambda r: r.update(required_categories=[])):
            run = copy.deepcopy(self.run)
            mutate(run)
            with self.assertRaises(ValueError):
                collector.validate(run)
        self.tx['gas']['computationCost'] = '-1'
        self.run['sources'] = []
        self.source([self.tx])
        with self.assertRaises(ValueError):
            self.collect()

    def test_internal_funding_not_added_to_cost_and_balances_reconcile(self):
        rows = [self.tx, self.row('transfer', 'funding', unit='MIST', amount=100,
                                 **{'from': A, 'to': B}, digest=D)]
        for unit in ('MIST', 'FROST'):
            for account in (A, B):
                for boundary in ('start', 'end'):
                    amount = 1000
                    if unit == 'MIST' and boundary == 'end':
                        amount = 650 if account == A else 1100
                    rows.append(self.row('balance', unit+account+boundary,
                                         account=account, unit=unit, boundary=boundary, amount=amount))
        self.source(rows)
        result = self.collect()
        self.assertEqual(result['accounting']['token_reconciliation_residuals'], {'MIST': 0, 'FROST': 0})
        self.assertEqual(result['accounting']['subtotals']['localnet:measured:MIST'], '250')

    def test_walrus_retention_capital_and_missing_provider(self):
        rows = [self.tx, self.row('walrus', 'blob-purchase', operation='purchase', category='walrus_storage',
                                 encoded_bytes=66034000, start_epoch=10, end_epoch=12,
                                 frost=120, digest=D, blob_id='blob'),
                self.row('requests', 'download-window', basis='measured', requests=10,
                         download_bytes=4000, provider_usd=None),
                self.row('capital', 'deposit-object-at-end', unit='MIST', amount=500,
                         kind='refundable_storage', object_or_account='object')]
        self.source(rows)
        result = self.collect()
        self.assertEqual(result['accounting']['subtotals']['localnet:measured:FROST'], '120')
        self.assertEqual(result['capital_snapshots'][0]['amount'], 500)
        self.assertIn('unavailable provider charge: download-window', result['incomplete'])

    def test_failed_walrus_payment_is_distinct_from_unsettled(self):
        self.source([dict(self.tx, status='failure'),
                     self.row('walrus', 'failed-renew', operation='renewal', category='renewal',
                              encoded_bytes=100, start_epoch=10, end_epoch=11,
                              frost=10, digest=D, blob_id='blob')])
        result = self.collect()
        self.assertIn('failed WAL payment transaction: failed-renew', result['incomplete'])
        self.assertNotIn('localnet:measured:FROST', result['accounting']['subtotals'])
        self.assertEqual(result['accounting']['subtotals']['localnet:measured:MIST'], '250')

    def test_unknown_walrus_payment_not_zero(self):
        self.source([self.row('walrus', 'renew', operation='renewal', category='renewal',
                              encoded_bytes=100, start_epoch=10, end_epoch=11,
                              frost=None, digest=D, blob_id='blob')])
        result = self.collect()
        self.assertNotIn('localnet:measured:FROST', result['accounting']['subtotals'])
        self.assertIn('unsettled WAL observation: renew', result['incomplete'])

    def test_duplicate_balance_rejected_before_commit(self):
        rows = [self.row('balance', id, unit='MIST', amount=100, account=A, boundary='start')
                for id in ('one', 'two')]
        self.source(rows)
        with self.assertRaisesRegex(ValueError, 'duplicate balance'):
            self.collect()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from records').fetchone()[0], 0)

    def test_external_funding_is_liquidity_not_fleet_gas(self):
        tx = dict(self.tx, sender=C, gas_owner=C)
        rows = [tx, self.row('transfer', 'external-funding', unit='MIST', amount=300,
                            **{'from': C, 'to': A}, digest=D)]
        for account in (A, B):
            for boundary in ('start', 'end'):
                rows.append(self.row('balance', account+boundary, account=account,
                                     unit='MIST', boundary=boundary,
                                     amount=1300 if account == A and boundary == 'end' else 1000))
        self.source(rows)
        result = self.collect()
        self.assertEqual(result['accounting']['token_reconciliation_residuals']['MIST'], 0)
        self.assertNotIn('localnet:measured:MIST', result['accounting']['subtotals'])

    def test_renamed_economic_duplicate_rejected(self):
        row = self.row('walrus', 'one', operation='purchase', category='walrus_storage',
                       encoded_bytes=100, start_epoch=1, end_epoch=2, frost=10,
                       digest=D, blob_id='blob')
        self.source([self.tx, row, dict(row, id='two')])
        with self.assertRaisesRegex(ValueError, 'duplicate economic'):
            self.collect()

    def test_outside_account_and_bad_request_measurements(self):
        for row in (dict(self.tx, sender=C, gas_owner=C),
                    self.row('requests', 'bad', basis='unavailable', requests=0,
                             download_bytes=None, provider_usd=None)):
            self.run['sources'] = []
            self.source([row])
            with self.assertRaises(ValueError):
                self.collect()


if __name__ == '__main__':
    unittest.main()
