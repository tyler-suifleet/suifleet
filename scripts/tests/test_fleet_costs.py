import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fleet_costs import calculate, effects_cost, reconcile, walrus_quote, break_even
from build_fleet_cost_model import build


class CostTests(unittest.TestCase):
    def model(self):
        return {"schema_version": 1, "workload": {"devices": 1000, "shared_releases": 4},
                "lines": [{"category": "receipts", "name": "report", "kind": "measured", "unit": "MIST",
                           "network": "testnet", "count": 1, "source": "effects", "digest": "test-digest",
                           "status": "success", "budget_mist": 50000000,
                           "effects": {"computationCost": "1000000", "storageCost": "7926800",
                                       "storageRebate": "7675232", "nonRefundableStorageFee": "768"}}]}

    def test_receipt_and_nonrefundable_not_double_counted(self):
        result = calculate(self.model())
        self.assertEqual(result['subtotals']['testnet:measured:MIST'], '1251568')
        self.assertFalse(result['complete_mainnet_bill'])

    def test_cleanup_signed_rebate(self):
        self.assertEqual(effects_cost({'computationCost': 100, 'storageCost': 0, 'storageRebate': 500}), -400)

    def test_failed_execution_charged(self):
        model = self.model()
        model['lines'][0]['status'] = 'failure'
        self.assertEqual(calculate(model)['subtotals']['testnet:measured:MIST'], '1251568')

    def test_unknown_cannot_be_settled(self):
        model = self.model()
        model['lines'][0]['status'] = 'unknown'
        with self.assertRaises(ValueError): calculate(model)

    def test_unknown_exposure_is_not_zero_cost(self):
        model = self.model()
        row = model['lines'][0]
        row.update(kind='unquoted', amount=None, unknown_reserved_mist=50000000)
        row.pop('effects')
        result = calculate(model)
        self.assertEqual(result['exposure_not_cost']['testnet:unknown_MIST'], 50000000)
        self.assertFalse(result['complete_mainnet_bill'])

    def test_duplicate_digest_or_scaled_measurement_rejected(self):
        model = self.model()
        model['lines'] *= 2
        with self.assertRaises(ValueError): calculate(model)
        model = self.model()
        model['lines'][0]['count'] = 4000
        with self.assertRaises(ValueError): calculate(model)

    def test_no_cross_network_currency_sum(self):
        model = self.model()
        model['lines'].append(dict(category='walrus_storage', name='raw rate', kind='estimated', unit='FROST',
                                   network='mainnet', count=4, amount='100', source='read-only snapshot'))
        self.assertEqual(len(calculate(model)['subtotals']), 2)

    def test_invalid_amounts(self):
        for amount in ['NaN', '-1', '0.5', 'Infinity']:
            model = self.model()
            model['lines'][0].pop('effects')
            model['lines'][0]['amount'] = amount
            with self.assertRaises(ValueError): calculate(model)

    def test_exact_quote_expiry_and_subsidy(self):
        quote = dict(client_version='pinned', ciphertext_sha256='a'*64, source='client dry run', network='mainnet',
                     encoded_bytes=70038000, start_epoch=42, end_epoch=44,
                     storage_frost='7681014', write_frost='6977045', subsidy_frost='100')
        self.assertEqual(walrus_quote(quote)['net_frost'], 14657959)
        quote['end_epoch'] = 42
        with self.assertRaises(ValueError): walrus_quote(quote)

    def test_token_flow_internal_funding_not_expense(self):
        self.assertEqual(reconcile(dict(start=1000, end=900, external_in=0, external_out=0, consumed=100)), 0)
        self.assertEqual(reconcile(dict(start=1000, end=900, external_in=50, external_out=0, consumed=100)), 50)

    def test_break_even_and_no_solution(self):
        self.assertEqual(break_even('100', '0.06', '0.16'), '1.0E+3')
        self.assertIsNone(break_even(100, '.16', '.16'))
        with self.assertRaises(ValueError): break_even(-1, 0, 1)

    def test_frozen_scenario_scaling_and_incompleteness(self):
        model = build(1000, 4)
        result = calculate(model)
        self.assertEqual(result['subtotals']['mainnet:estimated:FROST'], '113764040')
        self.assertEqual(result['subtotals']['testnet:estimated:MIST'], '12040316576')
        self.assertFalse(result['complete_mainnet_bill'])
        self.assertIsNone(result['comparison']['break_even_devices'])
        # Shared bytes scale with releases; each individually targeted deployment
        # and its receipt/cleanup scale with devices too.
        small = calculate(build(1, 4))
        self.assertEqual(result['subtotals']['mainnet:estimated:FROST'], small['subtotals']['mainnet:estimated:FROST'])
        self.assertGreater(int(result['subtotals']['testnet:estimated:MIST']), int(small['subtotals']['testnet:estimated:MIST']))
        self.assertFalse(any(k.startswith('testnet') for k in result['mainnet_usd_subtotals_not_bill']))

    def test_native_publisher_token_reconciliation(self):
        data = Path(__file__).resolve().parents[2] / 'docs/business/sf054/walrus-testnet.json'
        evidence = json.loads(data.read_text())
        total_wal = 0
        for tx in evidence['transactions']:
            gas = effects_cost(tx['effects']['gasEffects']['gasSummary'])
            sui = sum(int(c['amount']) for c in tx['effects']['balanceChangesJson'] if c['coinType'].endswith('::sui::SUI'))
            self.assertEqual(sui, -gas)
            total_wal -= sum(int(c['amount']) for c in tx['effects']['balanceChangesJson'] if c['coinType'].endswith('::wal::WAL'))
        self.assertEqual(total_wal, sum(u['publisher_response']['newlyCreated']['cost'] for u in evidence['uploads']))


if __name__ == '__main__':
    unittest.main()
