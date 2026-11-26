"""Cost-indexed public prices: list is the minimum, SUI-cost floors can raise it, nothing else moves it."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cost_indexed_pricing as cip  # noqa: E402
import site_pricing  # noqa: E402
from subscription_pricing import calculate  # noqa: E402


class CostIndexedPricingTests(unittest.TestCase):
    def setUp(self):
        self.costs = cip.load()
        self.policy = json.loads(cip.POLICY.read_text())
        self.prices = json.loads(site_pricing.PRICE_TABLE.read_text())

    def rates(self, costs):
        return [site_pricing.configuration_rate(self.prices, c, costs, self.policy) for c in site_pricing.CONFIGURATIONS]

    def at(self, sui_usd, rgp=None):
        return cip.set_basis(copy.deepcopy(self.costs), sui_usd, rgp, 'test', ['test'])

    def test_every_public_configuration_has_a_cost_basis(self):
        self.assertEqual({c['cost'] for c in site_pricing.CONFIGURATIONS}, set(self.costs['configurations']))
        for key, config in self.costs['configurations'].items():
            self.assertTrue(config['sources'], key)

    def test_list_prices_hold_at_the_quote_basis(self):
        self.assertEqual(cip.sui_scale(self.costs), Decimal(1))
        self.assertEqual(self.rates(self.costs), [Decimal('0.08'), Decimal('0.18'), Decimal('0.20')])

    def test_list_price_is_the_minimum_when_sui_falls(self):
        self.assertEqual(self.rates(self.at('0.10')), [Decimal('0.08'), Decimal('0.18'), Decimal('0.20')])

    def test_rising_sui_cost_raises_prices_through_the_policy_floor(self):
        rates = self.rates(self.at('1.50'))
        self.assertEqual(rates, [Decimal('0.10'), Decimal('0.25'), Decimal('0.26')])
        # Gas price counts like token price: half the token price at twice the gas price is the same cost.
        self.assertEqual(self.rates(self.at('0.75', 200)), rates)

    def test_floor_matches_the_subscription_calculator(self):
        costs = self.at('2.00')
        scale = cip.sui_scale(costs)
        model = dict(schema_version=2, devices=1000, profile='check', basis=costs['current_basis'], unquoted_costs=[],
                     recurring_costs=dict(sui_sensitive_usd=str(Decimal('6.0') * scale), walrus_usd='3.0', services_usd='0'),
                     commercial=dict(list_monthly_usd='80', monthly_minimum_usd='49', annual_discount_fraction='0'),
                     working_capital={})
        expected = Decimal(calculate(model, self.policy)['risk_floor_monthly_usd']) / 1000
        self.assertEqual(cip.floor_per_device(costs, self.policy, 'essentials', '0.08', 49), expected)
        rate, binds = cip.published_rate('0.08', expected)
        self.assertTrue(binds)
        self.assertEqual(rate, expected.quantize(Decimal('0.01'), rounding='ROUND_CEILING'))

    def test_headroom_reports_where_list_stops_holding(self):
        room = cip.headroom(self.costs, self.policy, 'essentials', '0.08', 49)
        self.assertGreater(room, Decimal(1))
        self.assertLessEqual(cip.floor_per_device(self.costs, self.policy, 'essentials', '0.08', 49, room), Decimal('0.08'))
        self.assertGreater(cip.floor_per_device(self.costs, self.policy, 'essentials', '0.08', 49, room + Decimal('0.01')), Decimal('0.08'))

    def test_page_uses_published_rates_and_basis_date(self):
        costs = self.at('1.50')
        costs['current_basis']['as_of'] = '2027-01-15'
        page = site_pricing.render(self.prices, self.policy, 'digest', costs)
        for rate in ('$0.10', '$0.25', '$0.26'):
            self.assertIn(f'<div class="amount">{rate} <small>', page)
        self.assertIn('Network costs as of 2027-01-15', page)
        # 1,000 devices on Essentials at the raised rate.
        self.assertIn('<tr><td>1,000</td><td>$100</td>', page)

    def test_basis_change_makes_the_committed_page_stale(self):
        changed = site_pricing.render(self.prices, self.policy, 'digest', self.at('1.50'))
        with patch.object(site_pricing, 'generated', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'stale'):
                site_pricing.generate(check=True)

    def test_set_basis_refuses_bad_inputs(self):
        for sui, rgp, sources in (('0', None, ['x']), ('-1', None, ['x']), ('NaN', None, ['x']), ('1', 0, ['x']), ('1', None, [])):
            with self.subTest(sui=sui, rgp=rgp, sources=sources), self.assertRaises(ValueError):
                cip.set_basis(copy.deepcopy(self.costs), sui, rgp, 'test', sources)


if __name__ == '__main__':
    unittest.main()
