import copy
from decimal import Decimal
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from subscription_pricing import calculate, table

BUSINESS = ROOT / 'docs/business'
EXAMPLES = {
    '1k': BUSINESS / 'subscription-example.json',
    '10k': BUSINESS / 'subscription-example-10k.json',
    '100k': BUSINESS / 'subscription-example-100k.json',
}


def load(path):
    return json.loads(path.read_text())


def scenario(result, sui_multiple, walrus_multiple='1'):
    return next(row for row in result['sensitivity']
                if row['sui_cost_multiple'] == sui_multiple and row['walrus_cost_multiple'] == walrus_multiple)


class SubscriptionPricingTests(unittest.TestCase):
    def setUp(self):
        self.model = load(EXAMPLES['1k'])
        self.policy = load(BUSINESS / 'subscription-policy.json')

    def test_list_price_prevails_when_floors_are_below_it(self):
        result = calculate(self.model, self.policy)
        self.assertEqual(result['recurring_costs_usd']['total'], '9')
        self.assertEqual(result['sui_share_of_recurring_cost'], '0.6667')
        self.assertEqual([f['floor_unhedged_usd'] for f in result['floors']], ['45.32', '65.26'])
        self.assertEqual(result['risk_floor_monthly_usd'], '65.26')
        self.assertEqual(result['binding_constraint'], 'list_price')
        self.assertEqual(result['monthly_subscription_usd'], '80.00')
        self.assertEqual(result['effective_per_device_month_usd'], '0.08000')
        self.assertEqual(result['amount_due_usd'], '80.00')

    def test_margin_falls_with_sui_cost_and_goes_negative_beyond_breakeven(self):
        result = calculate(self.model, self.policy)
        self.assertEqual([row['sui_cost_multiple'] for row in result['sensitivity'] if row['walrus_cost_multiple'] == '1'],
                         ['0.25', '0.5', '1', '2', '3', '5', '10', '20'])
        one = scenario(result, '1')
        self.assertEqual((one['recurring_cost_usd'], one['payment_cost_usd'], one['gross_profit_usd']), ('9.00', '2.70', '68.30'))
        self.assertEqual(one['infrastructure_margin_fraction'], '0.8538')
        self.assertEqual(one['implied_sui_usd'], '0.9870')
        ten = scenario(result, '10')
        self.assertEqual((ten['recurring_cost_usd'], ten['gross_profit_usd'], ten['infrastructure_margin_fraction']),
                         ('63.00', '14.30', '0.1788'))
        self.assertEqual(ten['implied_sui_usd'], '9.8700')
        twenty = scenario(result, '20')
        self.assertEqual((twenty['gross_profit_usd'], twenty['infrastructure_margin_fraction']), ('-45.70', '-0.5712'))
        margins = [Decimal(row['infrastructure_margin_fraction']) for row in result['sensitivity'] if row['walrus_cost_multiple'] == '1']
        self.assertEqual(margins, sorted(margins, reverse=True))

    def test_breakeven_and_headroom_report_the_absorbable_sui_move(self):
        result = calculate(self.model, self.policy)
        self.assertAlmostEqual(Decimal(result['breakeven']['sui_cost_multiple']), Decimal('12.3833'), places=3)
        self.assertAlmostEqual(Decimal(result['breakeven']['implied_sui_usd']), Decimal('12.2224'), places=3)
        self.assertIsNone(result['breakeven']['hedged_sui_cost_multiple'])
        headroom = {h['minimum_margin_fraction']: h for h in result['headroom']}
        self.assertEqual(set(headroom), {'0', '0.5'})
        self.assertAlmostEqual(Decimal(headroom['0.5']['sui_cost_multiple']), Decimal('5.7167'), places=3)
        self.assertAlmostEqual(Decimal(headroom['0.5']['implied_sui_usd']), Decimal('5.6424'), places=3)
        self.assertEqual(headroom['0']['sui_cost_multiple'], result['breakeven']['sui_cost_multiple'])

    def test_downside_rows_raise_margin_but_shrink_sui_denominated_capital(self):
        result = calculate(self.model, self.policy)
        half = scenario(result, '0.5')
        self.assertEqual((half['recurring_cost_usd'], half['infrastructure_margin_fraction']), ('6.00', '0.8912'))
        self.assertEqual(result['working_capital_usd']['sui_denominated_total_usd'], '89.22')
        self.assertEqual(result['working_capital_usd']['sui_denominated_total_sui'], '90.40')
        self.assertEqual(half['sui_denominated_capital_value_usd'], '44.61')
        self.assertEqual(scenario(result, '10')['sui_denominated_capital_value_usd'], '892.20')
        self.model['working_capital']['usd_denominated'] = {'prepaid_rpc_usd': '500'}
        same = calculate(self.model, self.policy)
        self.assertEqual(same['monthly_subscription_usd'], result['monthly_subscription_usd'])
        self.assertEqual(scenario(same, '10')['sui_denominated_capital_value_usd'], '892.20')

    def test_walrus_stress_is_independent_of_sui(self):
        result = calculate(self.model, self.policy)
        wal = scenario(result, '1', '2')
        self.assertEqual((wal['recurring_cost_usd'], wal['implied_sui_usd']), ('12.00', '0.9870'))
        self.policy['floors'][0]['walrus_cost_multiple'] = '2'
        self.assertEqual(calculate(self.model, self.policy)['floors'][0]['floor_unhedged_usd'], '51.71')

    def test_hedge_locks_the_term_and_removes_breakeven(self):
        result = calculate(self.model, self.policy, hedged=True)
        self.assertEqual([f['floor_hedged_usd'] for f in result['floors']], ['19.79', '9.59'])
        self.assertEqual(result['risk_floor_monthly_usd'], '19.79')
        self.assertEqual(result['hedge_purchase_usd'], '6.00')
        self.assertEqual(scenario(result, '10')['hedged_margin_fraction'], '0.8538')
        self.assertEqual(scenario(result, '10')['infrastructure_margin_fraction'], '0.1788')
        self.assertIsNone(result['breakeven']['hedged_sui_cost_multiple'])
        self.policy['hedge']['term_sui_purchased_fraction'] = '0.5'
        partial = calculate(self.model, self.policy, hedged=True)
        self.assertEqual(partial['hedge_purchase_usd'], '3.00')
        self.assertAlmostEqual(Decimal(partial['breakeven']['hedged_sui_cost_multiple']), Decimal('23.7667'), places=3)
        self.assertEqual(scenario(partial, '10')['hedged_recurring_cost_usd'], '36.00')
        # Without --hedged the policy's hedge is still reported, but it does not move the floors.
        unhedged = calculate(self.model, self.policy)
        self.assertFalse(unhedged['hedge_applied_to_floors'])
        self.assertEqual(unhedged['hedge_purchase_usd'], '3.00')
        self.assertEqual(scenario(unhedged, '10')['hedged_recurring_cost_usd'], '36.00')
        self.assertEqual(unhedged['risk_floor_monthly_usd'], '65.26')

    def test_annual_term_amortizes_fixed_fee_and_floor_overrides_discount(self):
        result = calculate(self.model, self.policy, annual=True)
        self.assertEqual(result['term_months'], 12)
        self.assertEqual(result['commercial_after_discount_usd'], '72.00')
        self.assertEqual([f['floor_unhedged_usd'] for f in result['floors']], ['44.74', '64.98'])
        self.assertEqual((result['monthly_subscription_usd'], result['amount_due_usd']), ('72.00', '864.00'))
        self.assertEqual(scenario(result, '1')['payment_cost_usd'], '2.18')
        self.assertEqual(result['term_sui_spend_usd'], '72.00')
        self.model['commercial']['list_monthly_usd'] = '70'
        floored = calculate(self.model, self.policy, annual=True)
        self.assertEqual(floored['commercial_after_discount_usd'], '63.00')
        self.assertEqual((floored['monthly_subscription_usd'], floored['binding_constraint']), ('64.98', 'risk_floor'))
        self.assertEqual(floored['amount_due_usd'], '779.76')

    def test_minimum_binds_above_floor_and_list(self):
        self.model['commercial']['monthly_minimum_usd'] = '600'
        result = calculate(self.model, self.policy)
        self.assertEqual((result['monthly_subscription_usd'], result['binding_constraint']), ('600.00', 'monthly_minimum'))

    def test_volume_tier_floor_binds_unhedged_and_releases_when_hedged(self):
        model = load(EXAMPLES['100k'])
        result = calculate(model, self.policy)
        self.assertEqual([f['floor_unhedged_usd'] for f in result['floors']], ['5681.49', '7082.79'])
        self.assertEqual((result['monthly_subscription_usd'], result['binding_constraint']), ('7082.79', 'risk_floor'))
        self.assertEqual(result['effective_per_device_month_usd'], '0.07083')
        self.assertGreaterEqual(Decimal(scenario(result, '10')['infrastructure_margin_fraction']), Decimal(0))
        self.assertGreaterEqual(Decimal(scenario(result, '3')['infrastructure_margin_fraction']), Decimal('0.5'))
        hedged = calculate(model, self.policy, hedged=True)
        self.assertEqual([f['floor_hedged_usd'] for f in hedged['floors']], ['3128.30', '1515.78'])
        self.assertEqual((hedged['monthly_subscription_usd'], hedged['binding_constraint']), ('5300.00', 'list_price'))
        self.assertAlmostEqual(Decimal(hedged['breakeven']['sui_cost_multiple']), Decimal('7.1178'), places=3)
        headroom = {h['minimum_margin_fraction']: h for h in hedged['headroom']}
        self.assertAlmostEqual(Decimal(headroom['0.5']['sui_cost_multiple']), Decimal('2.7012'), places=3)
        annual = calculate(model, self.policy, annual=True, hedged=True)
        self.assertEqual((annual['monthly_subscription_usd'], annual['binding_constraint']), ('4770.00', 'list_price'))
        self.assertEqual(annual['hedge_purchase_usd'], '7200.00')

    def test_mid_scale_example_keeps_list_price_with_less_headroom_than_small_fleets(self):
        result = calculate(load(EXAMPLES['10k']), self.policy)
        self.assertEqual([f['floor_unhedged_usd'] for f in result['floors']], ['551.71', '700.31'])
        self.assertEqual((result['monthly_subscription_usd'], result['binding_constraint']), ('800.00', 'list_price'))
        self.assertAlmostEqual(Decimal(result['breakeven']['sui_cost_multiple']), Decimal('11.6117'), places=3)
        small = calculate(self.model, self.policy)
        self.assertLess(Decimal(result['breakeven']['sui_cost_multiple']), Decimal(small['breakeven']['sui_cost_multiple']))

    def test_renewal_trigger_and_competitor_parity_are_reported(self):
        result = calculate(self.model, self.policy)
        repricing = result['renewal_repricing']
        self.assertEqual((repricing['trigger_multiple'], repricing['implied_sui_usd']), ('3', '2.9610'))
        self.assertEqual(repricing['infrastructure_margin_fraction'], '0.7038')
        self.assertEqual(repricing['price_for_margin'], {'0': '21.96', '0.5': '45.32'})
        self.assertEqual(scenario(result, '10')['price_for_margin'], {'0': '65.26', '0.5': '134.69'})
        comparison = result['comparison']
        self.assertEqual((comparison['monthly_usd'], comparison['price_ratio']), ('259', '0.3089'))
        self.assertAlmostEqual(Decimal(comparison['parity_multiple_for_margin']['0.5']['sui_cost_multiple']), Decimal('19.7383'), places=3)
        self.assertAlmostEqual(Decimal(comparison['parity_multiple_for_margin']['0']['sui_cost_multiple']), Decimal('41.3217'), places=3)
        del self.model['commercial']['comparison_monthly_usd']
        del self.policy['renewal_repricing_trigger_multiple']
        plain = calculate(self.model, self.policy)
        self.assertIsNone(plain['comparison'])
        self.assertIsNone(plain['renewal_repricing'])

    def test_unknown_required_inputs_refuse_a_quote(self):
        for key in ('sui_sensitive_usd', 'walrus_usd', 'services_usd'):
            model = copy.deepcopy(self.model)
            model['recurring_costs'][key] = None
            with self.subTest(key=key), self.assertRaises(ValueError):
                calculate(model, self.policy)
        for value in (None, ['Seal production provider']):
            model = copy.deepcopy(self.model)
            model['unquoted_costs'] = value
            with self.subTest(unquoted=value), self.assertRaises(ValueError):
                calculate(model, self.policy)
        for key, value in [('sui_usd', None), ('sui_usd', '0'), ('kind', 'guess'), ('sources', []), ('as_of', ' '),
                           ('reference_gas_price_mist', '100'), ('reference_gas_price_mist', 0)]:
            model = copy.deepcopy(self.model)
            model['basis'][key] = value
            with self.subTest(basis=key, value=value), self.assertRaises(ValueError):
                calculate(model, self.policy)

    def test_invalid_policy_and_workload_fail_closed(self):
        bad_policies = [
            dict(sui_cost_multiples=[]), dict(sui_cost_multiples=['0']), dict(sui_cost_multiples=[True]),
            dict(walrus_cost_multiples=['-1']), dict(payment_fee_fraction='NaN'), dict(payment_fee_fraction='Infinity'),
            dict(payment_fee_fraction='1'), dict(hedge={'term_sui_purchased_fraction': '1.5'}), dict(hedge=[]),
            dict(renewal_repricing_trigger_multiple='0.5'), dict(floors='3'), dict(floors=[{'sui_cost_multiple': '0.5', 'minimum_margin_fraction': '0.5'}]),
            dict(floors=[{'sui_cost_multiple': '3', 'minimum_margin_fraction': '0.98'}]),
            dict(floors=[{'sui_cost_multiple': '3'}]), dict(schema_version=1),
        ]
        for change in bad_policies:
            with self.subTest(change=change), self.assertRaises(ValueError):
                calculate(self.model, {**self.policy, **change})
        for value in (0, -1, True, 1.5, '1000'):
            with self.subTest(devices=value), self.assertRaises(ValueError):
                calculate({**self.model, 'devices': value}, self.policy)
        for change in [dict(annual_discount_fraction='1'), dict(list_monthly_usd='-1'), dict(monthly_minimum_usd=None)]:
            model = copy.deepcopy(self.model)
            model['commercial'].update(change)
            with self.subTest(commercial=change), self.assertRaises(ValueError):
                calculate(model, self.policy)
        model = copy.deepcopy(self.model)
        model['working_capital'] = {'float': '10'}
        with self.assertRaises(ValueError):
            calculate(model, self.policy)
        with self.assertRaises(ValueError):
            calculate(self.model, self.policy, annual='yes')

    def test_zero_cost_is_explicit_and_rounding_protects_the_floor(self):
        self.model['commercial'].update(list_monthly_usd='0', monthly_minimum_usd='0')
        self.model['recurring_costs'] = dict(sui_sensitive_usd='0', walrus_usd='0', services_usd='0')
        self.policy.update(payment_fixed_usd_per_invoice='0', payment_fee_fraction='0')
        result = calculate(self.model, self.policy)
        self.assertEqual(result['monthly_subscription_usd'], '0.00')
        self.assertIsNone(result['sensitivity'][0]['infrastructure_margin_fraction'])
        self.assertIsNone(result['breakeven']['sui_cost_multiple'])
        self.assertIsNone(result['sui_share_of_recurring_cost'])

    def test_floors_hold_their_margin_whenever_they_bind(self):
        generator = random.Random(152)
        for _ in range(400):
            model = copy.deepcopy(self.model)
            model['recurring_costs'] = dict(sui_sensitive_usd=str(generator.uniform(0, 500)),
                                            walrus_usd=str(generator.uniform(0, 200)), services_usd=str(generator.uniform(0, 300)))
            model['commercial'].update(list_monthly_usd=str(generator.uniform(0, 400)), monthly_minimum_usd=str(generator.uniform(0, 100)),
                                       annual_discount_fraction=str(generator.uniform(0, 0.5)))
            policy = copy.deepcopy(self.policy)
            policy['payment_fee_fraction'] = str(generator.uniform(0, 0.1))
            policy['floors'] = [dict(sui_cost_multiple=str(generator.uniform(1, 20)), walrus_cost_multiple=str(generator.uniform(1, 3)),
                                     minimum_margin_fraction=str(generator.uniform(0, 0.85))) for _ in range(generator.randint(0, 3))]
            policy['hedge'] = {'term_sui_purchased_fraction': str(generator.uniform(0, 1))}
            annual, hedged = generator.random() < 0.5, generator.random() < 0.5
            result = calculate(model, policy, annual=annual, hedged=hedged)
            price = Decimal(result['monthly_subscription_usd'])
            self.assertGreaterEqual(price, Decimal(result['risk_floor_monthly_usd']))
            self.assertGreaterEqual(price, Decimal(result['monthly_minimum_usd']))
            self.assertGreaterEqual(price, Decimal(result['commercial_after_discount_usd']))
            for floor in result['floors']:
                row = scenario(result, floor['sui_cost_multiple']) if floor['walrus_cost_multiple'] == '1' else None
                if row is None or not price:
                    continue
                margin = Decimal(row['hedged_margin_fraction' if hedged else 'infrastructure_margin_fraction'])
                self.assertGreaterEqual(margin, Decimal(floor['minimum_margin_fraction']) - Decimal('0.0002'))

    def test_cli_prints_json_or_table_and_refuses_unquoted_costs(self):
        command = [sys.executable, str(ROOT / 'scripts/subscription_pricing.py')]
        result = subprocess.run(command + [str(EXAMPLES['1k'])], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['kind'], 'subscription-planning-only')
        result = subprocess.run(command + [str(EXAMPLES['100k']), '--annual', '--hedged', '--table'],
                                check=True, capture_output=True, text=True)
        self.assertTrue(result.stdout.startswith('| SUI cost multiple |'))
        self.assertIn('| 1× SUI, 2× WAL |', result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'unknown.json'
            self.model['unquoted_costs'] = ['production RPC']
            path.write_text(json.dumps(self.model))
            result = subprocess.run(command + [str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(result.stdout)
            self.assertIn('unquoted', result.stderr)

    def test_documented_tables_match_the_calculator(self):
        document = (BUSINESS / 'SUBSCRIPTION_PRICING.md').read_text()
        cases = [('1k', False, False), ('10k', False, False), ('100k', False, False), ('100k', True, True)]
        for name, annual, hedged in cases:
            result = calculate(load(EXAMPLES[name]), self.policy, annual=annual, hedged=hedged)
            with self.subTest(example=name, annual=annual, hedged=hedged):
                self.assertIn(table(result), document)
                self.assertIn(f"${Decimal(result['monthly_subscription_usd']):,.2f}", document)


if __name__ == '__main__':
    unittest.main()
