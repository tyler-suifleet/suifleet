#!/usr/bin/env python3
"""Offline subscription planning; no wallets, invoices or live tariff changes."""
import argparse
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'docs/business/subscription-policy.json'
CENT = Decimal('0.01')


def amount(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f'{name}: a known nonnegative amount is required')
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f'{name}: invalid amount') from error
    if not result.is_finite() or result < 0 or result > Decimal('1e12'):
        raise ValueError(f'{name}: expected finite amount from 0 to 1e12')
    return result


def money(value):
    return str(value.quantize(CENT, rounding=ROUND_CEILING))


def calculate(model, policy, annual=False):
    """Price a bounded monthly workload; amounts are USD for the whole fleet."""
    with localcontext() as context:
        context.prec = 50
        return _calculate(model, policy, annual)


def _calculate(model, policy, annual):
    if model.get('schema_version') != 1 or policy.get('schema_version') != 1:
        raise ValueError('unsupported planning schema')
    if type(annual) is not bool:
        raise ValueError('annual must be boolean')
    devices = model.get('devices')
    if type(devices) is not int or not 1 <= devices <= 1_000_000_000:
        raise ValueError('devices must be a positive whole number up to 1 billion')
    basis = model.get('basis', {})
    if (basis.get('kind') not in ('illustrative', 'estimated', 'measured')
            or not isinstance(basis.get('as_of'), str) or not basis['as_of'].strip()
            or not isinstance(basis.get('sources'), list) or not basis['sources']
            or any(not isinstance(s, str) or not s.strip() for s in basis['sources'])):
        raise ValueError('cost basis, observation date and sources are required')
    unknown = model.get('unquoted_costs')
    if not isinstance(unknown, list) or unknown:
        raise ValueError('required costs remain unquoted; supply all costs before pricing')
    costs = model.get('recurring_costs', {})
    sui, walrus, services = (amount(costs.get(key), key) for key in
                            ('sui_sensitive_usd', 'walrus_usd', 'services_usd'))
    multiple = amount(policy.get('sui_cost_multiple'), 'sui_cost_multiple')
    walrus_multiple = amount(policy.get('walrus_cost_multiple'), 'walrus_cost_multiple')
    if multiple < 1 or walrus_multiple < 1:
        raise ValueError('cost stress multiples must be at least one')
    margin = amount(policy.get('stressed_margin_fraction'), 'stressed_margin_fraction')
    payment = amount(policy.get('payment_fee_fraction'), 'payment_fee_fraction')
    fixed = amount(policy.get('payment_fixed_usd_per_invoice'), 'payment_fixed_usd_per_invoice')
    if margin + payment >= 1:
        raise ValueError('margin plus payment fee fraction must be less than one')
    terms = model.get('commercial', {})
    candidate = amount(terms.get('candidate_monthly_usd'), 'candidate_monthly_usd')
    minimum = amount(terms.get('monthly_minimum_usd'), 'monthly_minimum_usd')
    discount = amount(terms.get('annual_discount_fraction'), 'annual_discount_fraction')
    if discount >= 1:
        raise ValueError('annual_discount_fraction must be less than one')
    # An annual card charge incurs one fixed fee, amortized over 12 months.
    fee = fixed / (12 if annual else 1)
    stressed = multiple * sui + walrus_multiple * walrus + services
    floor = (stressed + fee) / (1 - margin - payment)
    offered = candidate * (1 - discount if annual else 1)
    monthly = Decimal(money(max(floor, minimum, offered)))
    projections = []
    for s, w in ((1, 1), (2, 1), (multiple, walrus_multiple), (5, 1), (1, 2)):
        s, w = Decimal(s), Decimal(w)
        cost = s * sui + w * walrus + services
        payment_cost = monthly * payment + fee
        actual_margin = None if not monthly else str((monthly - cost - payment_cost) / monthly)
        projections.append(dict(sui_cost_multiple=str(s), walrus_cost_multiple=str(w),
            recurring_cost_usd=str(cost), payment_cost_usd=str(payment_cost),
            infrastructure_margin_fraction=actual_margin))
    # Capital is reported separately and never deducted as recurring consumption.
    capital = model.get('working_capital', {})
    if not isinstance(capital, dict):
        raise ValueError('working_capital must be an object')
    capital = {k: str(amount(v, k)) for k, v in capital.items()}
    return dict(schema_version=1, kind='subscription-planning-only', basis=basis,
        devices=devices, policy=policy, annual=annual,
        baseline_recurring_usd=str(sui + walrus + services), stressed_recurring_usd=str(stressed),
        risk_floor_monthly_usd=money(floor), commercial_after_discount_usd=str(offered),
        monthly_minimum_usd=str(minimum), monthly_subscription_usd=money(monthly),
        effective_per_device_month_usd=str(monthly / devices),
        amount_due_usd=money(monthly * (12 if annual else 1)),
        working_capital_usd=capital, sensitivity=projections)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', type=Path)
    parser.add_argument('--policy', type=Path, default=POLICY)
    parser.add_argument('--annual', action='store_true')
    args = parser.parse_args()
    try:
        result = calculate(json.loads(args.model.read_text()), json.loads(args.policy.read_text()), args.annual)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        parser.exit(2, f'Pricing refused: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
