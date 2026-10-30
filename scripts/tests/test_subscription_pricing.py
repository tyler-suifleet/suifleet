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


def shock(result, token_price, token_quantity):
    return next(row for row in result['combined_sensitivity']
                if (row['token_price_multiple'], row['token_quantity_multiple']) == (token_price, token_quantity))


def fixed_quantity(policy):
    """The policy with floors that assume the token quantity stays at the basis."""
    bounded = copy.deepcopy(policy)
    for floor in bounded['floors']:
        floor['max_token_quantity_multiple'] = '1'
    return bounded


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
        self.assertIsNone(result['breakeven']['hedged_token_price_multiple'])
        self.assertEqual(result['breakeven']['hedged_token_quantity_multiple'], result['breakeven']['sui_cost_multiple'])
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

    def test_hedge_fixes_token_price_but_not_token_quantity(self):
        result = calculate(self.model, self.policy, annual=True, hedged=True)
        # Unbounded, a floor's whole multiple may be a gas-price rise the hedge cannot cover.
        self.assertEqual([f['max_token_quantity_multiple'] for f in result['floors']], ['3', '10'])
        self.assertEqual([f['floor_hedged_usd'] for f in result['floors']], [f['floor_unhedged_usd'] for f in result['floors']])
        self.assertEqual([f['floor_hedged_usd'] for f in result['floors']], ['44.74', '64.98'])
        self.assertEqual(result['hedge_purchase_usd'], '72.00')
        ten = scenario(result, '10')
        self.assertEqual((ten['hedged_recurring_cost_token_price_usd'], ten['hedged_margin_token_price_fraction']), ('9.00', '0.8447'))
        self.assertEqual((ten['hedged_recurring_cost_token_quantity_usd'], ten['hedged_margin_token_quantity_fraction']), ('63.00', '0.0947'))
        self.assertEqual(ten['infrastructure_margin_fraction'], '0.0947')
        self.assertIsNone(result['breakeven']['hedged_token_price_multiple'])
        self.assertEqual(result['breakeven']['hedged_token_quantity_multiple'], result['breakeven']['sui_cost_multiple'])
        bounded = calculate(self.model, fixed_quantity(self.policy), annual=True, hedged=True)
        self.assertEqual([f['floor_hedged_usd'] for f in bounded['floors']], ['19.21', '9.31'])
        self.assertEqual(bounded['risk_floor_monthly_usd'], '19.21')
        self.policy['hedge']['term_sui_purchased_fraction'] = '0.5'
        partial = calculate(self.model, self.policy, annual=True, hedged=True)
        self.assertEqual(partial['hedge_purchase_usd'], '36.00')
        self.assertAlmostEqual(Decimal(partial['breakeven']['hedged_token_price_multiple']), Decimal('21.2717'), places=3)
        self.assertEqual(scenario(partial, '10')['hedged_recurring_cost_token_price_usd'], '36.00')
        self.assertEqual(scenario(partial, '10')['hedged_recurring_cost_token_quantity_usd'], '63.00')
        # Without --hedged the policy's hedge is still reported, but it does not move the floors.
        unhedged = calculate(self.model, fixed_quantity(self.policy), annual=True)
        self.assertFalse(unhedged['hedge_applied_to_floors'])
        self.assertEqual(unhedged['hedge_purchase_usd'], '36.00')
        self.assertEqual(scenario(unhedged, '10')['hedged_recurring_cost_token_price_usd'], '36.00')
        self.assertEqual(unhedged['risk_floor_monthly_usd'], '64.98')

    def test_token_only_gas_only_and_combined_shocks_against_the_hedge(self):
        model = load(EXAMPLES['100k'])
        result = calculate(model, fixed_quantity(self.policy), annual=True, hedged=True)
        self.assertEqual(result['monthly_subscription_usd'], '4770.00')
        # Token price 10x: the pre-bought SUI pays for the term and capital is revalued.
        token = shock(result, '10', '1')
        self.assertEqual((token['hedged_recurring_cost_usd'], token['hedged_margin_fraction']), ('1470.00', '0.6618'))
        self.assertEqual(token['sui_denominated_capital_value_usd'], '89220.00')
        # Gas price 10x at the same token price: the extra 9x of SUI is bought at market.
        gas = shock(result, '1', '10')
        self.assertEqual((gas['recurring_cost_usd'], gas['hedged_recurring_cost_usd']), ('6870.00', '6870.00'))
        self.assertEqual(gas['hedged_margin_fraction'], '-0.4703')
        self.assertEqual(gas['sui_denominated_capital_value_usd'], '8922.00')
        self.assertEqual(shock(result, '1', '5.5')['hedged_margin_fraction'], '0.0958')
        # Token 2x and gas 5x: 600 pre-bought plus 4 x 600 more at twice the price, plus 870 WAL and services.
        both = shock(result, '2', '5')
        self.assertEqual((both['sui_cost_multiple'], both['hedged_recurring_cost_usd']), ('10', '6270.00'))
        self.assertEqual((both['infrastructure_margin_fraction'], both['hedged_margin_fraction']), ('-0.4703', '-0.3445'))
        self.assertEqual(both['sui_denominated_capital_value_usd'], '17844.00')
        self.assertAlmostEqual(Decimal(result['breakeven']['hedged_token_quantity_multiple']), Decimal('6.2615'), places=3)
        self.assertIsNone(result['breakeven']['hedged_token_price_multiple'])
        # The policy as written therefore floors the hedged volume term at the unhedged price.
        floored = calculate(model, self.policy, annual=True, hedged=True)
        self.assertEqual((floored['monthly_subscription_usd'], floored['binding_constraint']), ('7082.50', 'risk_floor'))
        self.assertGreaterEqual(Decimal(shock(floored, '1', '10')['hedged_margin_fraction']), Decimal(0))
        # A floor bounded at 5x quantity covers the remaining 2x with the hedge.
        partial = copy.deepcopy(self.policy)
        partial['floors'][1]['max_token_quantity_multiple'] = '5'
        bounded = calculate(model, partial, annual=True, hedged=True)
        self.assertEqual(bounded['floors'][1]['floor_hedged_usd'], '6463.95')
        self.assertGreaterEqual(Decimal(shock(bounded, '2', '5')['hedged_margin_fraction']), Decimal(0))

    def test_hedge_share_is_measured_against_the_discounted_annual_invoice(self):
        cases = [('1k', self.policy, '72.00', '864.00', '0.0833'), ('10k', self.policy, '720.00', '8640.00', '0.0833'),
                 ('100k', fixed_quantity(self.policy), '7200.00', '57240.00', '0.1258'),
                 ('100k', self.policy, '7200.00', '84990.00', '0.0847')]
        for name, policy, purchase, due, share in cases:
            result = calculate(load(EXAMPLES[name]), policy, annual=True, hedged=True)
            with self.subTest(example=name, due=due):
                self.assertEqual((result['hedge_purchase_usd'], result['amount_due_usd'], result['hedge_share_of_invoice']),
                                 (purchase, due, share))
                self.assertEqual(Decimal(result['hedge_share_of_invoice']),
                                 (Decimal(purchase) / Decimal(due)).quantize(Decimal('0.0001')))

    def test_monthly_billing_refuses_hedged_floors(self):
        # A monthly price persists below the renewal trigger while a monthly hedge covers one month.
        with self.assertRaisesRegex(ValueError, 'prepaid annual'):
            calculate(self.model, self.policy, hedged=True)
        command = [sys.executable, str(ROOT / 'scripts/subscription_pricing.py'), str(EXAMPLES['100k']), '--hedged']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('prepaid annual', result.stderr)

    def test_unreachable_margin_reports_no_multiple(self):
        # A price that misses a margin even if SUI operations were free has no absorbable SUI move.
        self.model['commercial'].update(list_monthly_usd='1', monthly_minimum_usd='0', comparison_monthly_usd='1')
        self.policy['floors'] = []
        result = calculate(self.model, self.policy)
        self.assertEqual(result['monthly_subscription_usd'], '1.00')
        self.assertIsNone(result['breakeven']['sui_cost_multiple'])
        self.assertIsNone(result['breakeven']['implied_sui_usd'])
        self.assertEqual(result['headroom'], [dict(minimum_margin_fraction='0', sui_cost_multiple=None, implied_sui_usd=None)])
        self.assertEqual(result['comparison']['parity_multiple_for_margin'], {'0': dict(sui_cost_multiple=None, implied_sui_usd=None)})

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

    def test_volume_tier_floor_binds_unless_token_quantity_is_fixed(self):
        model = load(EXAMPLES['100k'])
        result = calculate(model, self.policy)
        self.assertEqual([f['floor_unhedged_usd'] for f in result['floors']], ['5681.49', '7082.79'])
        self.assertEqual((result['monthly_subscription_usd'], result['binding_constraint']), ('7082.79', 'risk_floor'))
        self.assertEqual(result['effective_per_device_month_usd'], '0.07083')
        self.assertGreaterEqual(Decimal(scenario(result, '10')['infrastructure_margin_fraction']), Decimal(0))
        self.assertGreaterEqual(Decimal(scenario(result, '3')['infrastructure_margin_fraction']), Decimal('0.5'))
        # With only a non-binding 1x floor, the $5,300 monthly list breaks even at about
        # 7.1x SUI cost and falls below 50% margin from about 2.7x.
        loose = [dict(sui_cost_multiple='1', minimum_margin_fraction='0.5')]
        unfloored = calculate(model, {**self.policy, 'floors': loose})
        self.assertEqual((unfloored['monthly_subscription_usd'], unfloored['binding_constraint']), ('5300.00', 'list_price'))
        self.assertAlmostEqual(Decimal(unfloored['breakeven']['sui_cost_multiple']), Decimal('7.1178'), places=3)
        headroom = {h['minimum_margin_fraction']: h for h in unfloored['headroom']}
        self.assertAlmostEqual(Decimal(headroom['0']['sui_cost_multiple']), Decimal('7.1178'), places=3)
        self.assertAlmostEqual(Decimal(headroom['0.5']['sui_cost_multiple']), Decimal('2.7012'), places=3)
        annual = calculate(model, fixed_quantity(self.policy), annual=True, hedged=True)
        self.assertEqual([f['floor_hedged_usd'] for f in annual['floors']], ['3127.72', '1515.49'])
        self.assertEqual((annual['monthly_subscription_usd'], annual['binding_constraint']), ('4770.00', 'list_price'))
        self.assertAlmostEqual(Decimal(annual['breakeven']['sui_cost_multiple']), Decimal('6.2615'), places=3)
        self.assertEqual(annual['hedge_purchase_usd'], '7200.00')
        unhedged_annual = calculate(model, self.policy, annual=True)
        self.assertEqual((unhedged_annual['monthly_subscription_usd'], unhedged_annual['binding_constraint']), ('7082.50', 'risk_floor'))

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
            dict(floors=[{'sui_cost_multiple': '3', 'minimum_margin_fraction': '0.5', 'max_token_quantity_multiple': '4'}]),
            dict(floors=[{'sui_cost_multiple': '3', 'minimum_margin_fraction': '0.5', 'max_token_quantity_multiple': '0.5'}]),
            dict(combined_shocks={}), dict(combined_shocks=[{'token_price_multiple': '2'}]),
            dict(combined_shocks=[{'token_price_multiple': '0', 'token_quantity_multiple': '1'}]),
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
            policy['floors'] = []
            for _ in range(generator.randint(0, 3)):
                multiple = generator.uniform(1, 20)
                floor = dict(sui_cost_multiple=str(multiple), walrus_cost_multiple=str(generator.uniform(1, 3)),
                             minimum_margin_fraction=str(generator.uniform(0, 0.85)))
                if generator.random() < 0.5:
                    floor['max_token_quantity_multiple'] = str(generator.uniform(1, multiple))
                policy['floors'].append(floor)
            policy['hedge'] = {'term_sui_purchased_fraction': str(generator.uniform(0, 1))}
            annual = generator.random() < 0.5
            hedged = annual and generator.random() < 0.5
            result = calculate(model, policy, annual=annual, hedged=hedged)
            price = Decimal(result['monthly_subscription_usd'])
            self.assertGreaterEqual(price, Decimal(result['risk_floor_monthly_usd']))
            self.assertGreaterEqual(price, Decimal(result['monthly_minimum_usd']))
            self.assertGreaterEqual(price, Decimal(result['commercial_after_discount_usd']))
            if not price:
                continue
            # Recompute independently over every token x quantity split each floor allows.
            costs = {key: Decimal(value) for key, value in model['recurring_costs'].items()}
            covered = Decimal(policy['hedge']['term_sui_purchased_fraction']) if hedged else Decimal(0)
            fee = Decimal(policy['payment_fee_fraction'])
            fixed = Decimal(policy['payment_fixed_usd_per_invoice']) / (12 if annual else 1)
            for floor in result['floors']:
                multiple = Decimal(floor['sui_cost_multiple'])
                bound = Decimal(floor['max_token_quantity_multiple'])
                for step in range(11):
                    quantity = 1 + (bound - 1) * step / 10
                    token = multiple / quantity
                    sui = (covered + max(quantity - covered, Decimal(0)) * token) * costs['sui_sensitive_usd']
                    recurring = sui + Decimal(floor['walrus_cost_multiple']) * costs['walrus_usd'] + costs['services_usd']
                    margin = (price - recurring - price * fee - fixed) / price
                    self.assertGreaterEqual(margin, Decimal(floor['minimum_margin_fraction']) - Decimal('1e-9'))

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
        bounded = fixed_quantity(self.policy)
        cases = [('1k', self.policy, False, False), ('10k', self.policy, False, False),
                 ('100k', self.policy, False, False), ('100k', bounded, True, True)]
        for name, policy, annual, hedged in cases:
            result = calculate(load(EXAMPLES[name]), policy, annual=annual, hedged=hedged)
            with self.subTest(example=name, annual=annual, hedged=hedged):
                self.assertIn(table(result), document)
                self.assertIn(f"${Decimal(result['monthly_subscription_usd']):,.2f}", document)



class PublicPricingTests(unittest.TestCase):
    def setUp(self):
        import site_pricing
        self.site = site_pricing
        self.prices = load(site_pricing.PRICE_TABLE)
        self.policy = load(site_pricing.POLICY)

    def test_generated_page_is_current(self):
        self.site.generate(check=True)

    def test_console_uses_shared_table(self):
        source = (ROOT / 'dapp/src/lib/fleet-pricing.ts').read_text()
        self.assertIn('import defaultPriceTable from "./fleet-price-table.json"', source)
        self.assertIn('validatePriceTable(defaultPriceTable)', source)

    def test_tariff_matches_business_plan(self):
        plan = self.site.PLAN.read_text()
        tiers = self.prices['base']['tiers']
        self.assertIn(f'| First {tiers[0]["upToDevices"]:,} | {self.site.usd(tiers[0]["usdPerDeviceMonth"])} |', plan)
        self.assertIn(f'| {tiers[0]["upToDevices"] + 1:,} to {tiers[1]["upToDevices"]:,} | {self.site.usd(tiers[1]["usdPerDeviceMonth"])} |', plan)
        self.assertIn(f'| Above {tiers[1]["upToDevices"]:,} | {self.site.usd(tiers[2]["usdPerDeviceMonth"])} |', plan)
        self.assertIn(f'| Above {tiers[-1]["upToDevices"]:,} | Custom, from {self.site.usd(self.prices["base"]["customFromUsdPerDeviceMonth"])} |', plan)
        self.assertIn(f'| Monthly minimum | ${self.prices["base"]["monthlyMinimumUsd"]} |', plan)
        self.assertIn(f'| Annual prepay | {self.prices["base"]["annualPrepayDiscountPercent"]}% discount |', plan)

    def test_public_offers_match_plan_and_render_from_policy(self):
        from unittest.mock import patch
        offers = self.policy['public_offers']
        managed, at_cost, partners = (offers[key] for key in ('managed', 'at_cost', 'design_partners'))
        plan = self.site.PLAN.read_text()
        self.assertIn(f'Managed plan: the platform price plus {self.site.usd(managed["premium_usd_per_device_month"])} per device per month, with a ${managed["monthly_minimum_usd"]} monthly minimum.', plan)
        self.assertIn(f'Up to {at_cost["max_devices"]:,} devices per organization', plan)
        self.assertIn(f'about {self.site.usd(at_cost["estimated_usd_per_device_month"])} per device per month', plan)
        self.assertIn(f'Up to {partners["max_devices"]:,} devices each at cost for {partners["term_months"]} months', plan)
        page = self.site.generated()
        for text in ('Managed plan', '$0.10 per device per month', '$199.00 monthly minimum',
                     '100 devices per organization', '$0.01', '5,000 devices each at cost for 12 months',
                     'community support only', 'Billed annually in advance', 'case study'):
            self.assertIn(text, page)
        # Each canonical offer field must affect the page and invalidate committed output.
        for group, values in offers.items():
            for key, value in values.items():
                policy = copy.deepcopy(self.policy)
                policy['public_offers'][group][key] = str(Decimal(str(value)) + 1) if isinstance(value, str) else value + 1
                changed = self.site.render(self.prices, policy, 'digest')
                self.assertNotEqual(self.site.render(self.prices, self.policy, 'digest'), changed)
                with patch.object(self.site, 'generated', return_value=changed):
                    with self.assertRaisesRegex(ValueError, 'stale'):
                        self.site.generate(check=True)

    def test_model_changes_invalidate_page(self):
        from unittest.mock import patch
        original = self.site.generated()
        for key in ('monthlyMinimumUsd', 'activationUsdPerDevice', 'annualPrepayDiscountPercent', 'customFromUsdPerDeviceMonth'):
            prices = copy.deepcopy(self.prices)
            prices['base'][key] += 1
            changed = self.site.render(prices, self.policy, 'changed')
            self.assertNotEqual(original, changed)
            with patch.object(self.site, 'generated', return_value=changed):
                with self.assertRaisesRegex(ValueError, 'stale'):
                    self.site.generate(check=True)
        prices = copy.deepcopy(self.prices)
        prices['base']['tiers'][0] = dict(upToDevices=12345, usdPerDeviceMonth=0.09)
        prices['settings']['checkIntervalMs'][0]['usdPerDeviceMonth'] = 0.77
        page = self.site.render(prices, self.policy, 'digest')
        for text in ('First 12,345', '12,346 to 100,000', '$0.09', '$0.77'):
            self.assertIn(text, page)
        policy = copy.deepcopy(self.policy)
        policy['renewal_repricing_trigger_multiple'] = '4'
        policy['renewal_notice_days'] = 45
        policy['hedge']['term_sui_purchased_fraction'] = '0.5'
        page = self.site.render(prices, policy, 'digest')
        for text in ('at least 4×', '45 days', '50% of the term'):
            self.assertIn(text, page)

    def test_policy_and_estimator_are_explicit(self):
        page = self.site.generated()
        for text in ('{{app:settings/}}', 'SUI pays', 'WAL pays', 'never change mid-term', 'at least 3×', '30 days', 'gas-price', 'not a binding tariff'):
            self.assertIn(text, page)


if __name__ == '__main__':
    unittest.main()
