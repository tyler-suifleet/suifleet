import copy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from subscription_pricing import calculate


class SubscriptionPricingTests(unittest.TestCase):
    def setUp(self):
        self.model = json.loads((ROOT / 'docs/business/subscription-example.json').read_text())
        self.policy = json.loads((ROOT / 'docs/business/subscription-policy.json').read_text())

    def test_sui_only_stress_and_payment_adjusted_margin(self):
        result = calculate(self.model, self.policy)
        self.assertEqual(result['baseline_recurring_usd'], '9.00')
        self.assertEqual(result['stressed_recurring_usd'], '25.00')
        self.assertEqual(result['monthly_subscription_usd'], '148.83')
        self.assertEqual(result['amount_due_usd'], '148.83')
        for projection in result['sensitivity'][:3]:
            self.assertGreaterEqual(Decimal(projection['infrastructure_margin_fraction']), Decimal('0.80'))
        # The 5x scenario can breach the target; it is not a guarantee beyond 3x.
        self.assertLess(Decimal(result['sensitivity'][3]['infrastructure_margin_fraction']), Decimal('0.80'))

    def test_walrus_exposure_independent_of_sui(self):
        result = calculate(self.model, self.policy)
        self.assertEqual(Decimal(result['sensitivity'][-1]['recurring_cost_usd']), Decimal('9.50'))
        self.policy['walrus_cost_multiple'] = '2'
        self.assertEqual(Decimal(calculate(self.model, self.policy)['stressed_recurring_usd']), Decimal('25.50'))

    def test_discount_never_undercuts_floor_and_fixed_fee_is_amortized(self):
        result = calculate(self.model, self.policy, annual=True)
        self.assertEqual(result['commercial_after_discount_usd'], '72.00')
        self.assertEqual(result['monthly_subscription_usd'], '147.21')
        self.assertEqual(result['amount_due_usd'], '1766.52')
        self.assertGreaterEqual(Decimal(result['sensitivity'][2]['infrastructure_margin_fraction']), Decimal('0.80'))
        self.model['commercial']['candidate_monthly_usd'] = '1000'
        self.assertEqual(calculate(self.model, self.policy, annual=True)['monthly_subscription_usd'], '900.00')

    def test_volume_candidate_and_minimum_cannot_undercut_risk_floor(self):
        self.model['commercial']['candidate_monthly_usd'] = '30'
        self.assertEqual(calculate(self.model, self.policy)['monthly_subscription_usd'], '148.83')
        self.model['commercial']['monthly_minimum_usd'] = '200'
        self.assertEqual(calculate(self.model, self.policy)['monthly_subscription_usd'], '200.00')

    def test_working_capital_does_not_change_recurring_price(self):
        before = calculate(self.model, self.policy)
        self.model['working_capital']['device_gas_float_usd'] = '1000000'
        after = calculate(self.model, self.policy)
        self.assertEqual(before['monthly_subscription_usd'], after['monthly_subscription_usd'])
        self.assertEqual(after['working_capital_usd']['device_gas_float_usd'], '1000000')

    def test_unknown_required_costs_refuse_quote_instead_of_becoming_zero(self):
        for key in ('sui_sensitive_usd', 'walrus_usd', 'services_usd'):
            model = copy.deepcopy(self.model)
            model['recurring_costs'][key] = None
            with self.subTest(key=key), self.assertRaises(ValueError):
                calculate(model, self.policy)
        for value in (None, ['Seal production provider']):
            self.model['unquoted_costs'] = value
            with self.assertRaises(ValueError):
                calculate(self.model, self.policy)

    def test_invalid_policy_and_workload_fail_closed(self):
        for key, value in [('sui_cost_multiple', '0.5'), ('sui_cost_multiple', True),
                           ('walrus_cost_multiple', '-1'), ('stressed_margin_fraction', '0.99'),
                           ('payment_fee_fraction', 'NaN'), ('payment_fee_fraction', 'Infinity')]:
            policy = {**self.policy, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                calculate(self.model, policy)
        for value in (0, -1, True, 1.5):
            with self.subTest(devices=value), self.assertRaises(ValueError):
                calculate({**self.model, 'devices': value}, self.policy)

    def test_rounding_up_protects_target_and_zero_cost_is_explicit(self):
        self.model['commercial'].update(candidate_monthly_usd='0', monthly_minimum_usd='0')
        self.model['recurring_costs'] = dict(sui_sensitive_usd='0', walrus_usd='0', services_usd='0')
        self.policy.update(payment_fixed_usd_per_invoice='0', payment_fee_fraction='0')
        result = calculate(self.model, self.policy)
        self.assertEqual(result['monthly_subscription_usd'], '0.00')
        self.assertIsNone(result['sensitivity'][0]['infrastructure_margin_fraction'])

    def test_cli_reports_planning_basis_and_refuses_unquoted_costs(self):
        command = [sys.executable, str(ROOT / 'scripts/subscription_pricing.py')]
        result = subprocess.run(command + [str(ROOT / 'docs/business/subscription-example.json')],
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['kind'], 'subscription-planning-only')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'unknown.json'
            self.model['unquoted_costs'] = ['production RPC']
            path.write_text(json.dumps(self.model))
            result = subprocess.run(command + [str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(result.stdout)
            self.assertIn('unquoted', result.stderr)


if __name__ == '__main__':
    unittest.main()
