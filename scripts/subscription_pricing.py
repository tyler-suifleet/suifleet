#!/usr/bin/env python3
"""Offline subscription planning under SUI cost changes; no wallets, invoices or live tariff changes.

The model prices a fixed USD subscription for a bounded monthly workload and
reports what happens to cost, gross profit and margin when the USD cost of
SUI-denominated operations moves, how far the price can absorb such a move, and
what a term hedge (buying the term's SUI when the invoice is paid) changes.

The USD cost multiple is the product of a token price multiple and a token
quantity multiple (reference gas price, storage price or gas units per operation).
A hedge buys a quantity of SUI at the basis price, so it removes token price
exposure for that quantity only; additional tokens required by a quantity rise
are bought at the shocked token price.
"""
import argparse
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_EVEN, localcontext
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'docs/business/subscription-policy.json'
CENT = Decimal('0.01')
ZERO = Decimal(0)
ONE = Decimal(1)
BASIS_KINDS = ('illustrative', 'estimated', 'measured')


def amount(value, name, minimum=ZERO, maximum=Decimal('1e12')):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f'{name}: a known amount is required')
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f'{name}: invalid amount') from error
    if not result.is_finite() or result < minimum or result > maximum:
        raise ValueError(f'{name}: expected a finite amount from {minimum} to {maximum}')
    return result


def multiples(values, name, minimum):
    if not isinstance(values, list) or not values:
        raise ValueError(f'{name}: a non-empty list of cost multiples is required')
    return sorted({amount(v, name, minimum=minimum, maximum=Decimal(1000)) for v in values})


def money(value):
    """Prices round up to the cent so rounding never erodes the target margin."""
    return str(value.quantize(CENT, rounding=ROUND_CEILING))


def cents(value):
    return str(value.quantize(CENT, rounding=ROUND_HALF_EVEN))


def fraction(value, places=4):
    return None if value is None else str(value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_EVEN))


def plain(value):
    text = format(value.normalize(), 'f')
    return text if text else '0'


def calculate(model, policy, annual=False, hedged=False):
    """Price a bounded monthly workload; amounts are USD for the whole fleet."""
    with localcontext() as context:
        context.prec = 50
        return _calculate(model, policy, annual, hedged)


def _validate_basis(basis):
    if (not isinstance(basis, dict) or basis.get('kind') not in BASIS_KINDS
            or not isinstance(basis.get('as_of'), str) or not basis['as_of'].strip()
            or not isinstance(basis.get('sources'), list) or not basis['sources']
            or any(not isinstance(s, str) or not s.strip() for s in basis['sources'])):
        raise ValueError('cost basis kind, observation date and sources are required')
    sui_usd = amount(basis.get('sui_usd'), 'basis.sui_usd', minimum=Decimal('1e-9'))
    rgp = basis.get('reference_gas_price_mist')
    if rgp is not None and (type(rgp) is not int or rgp <= 0):
        raise ValueError('basis.reference_gas_price_mist must be a positive whole number of MIST')
    return sui_usd


def _validate_floors(floors, payment):
    if not isinstance(floors, list):
        raise ValueError('policy.floors must be a list')
    result = []
    for index, floor in enumerate(floors):
        if not isinstance(floor, dict):
            raise ValueError(f'policy.floors[{index}] must be an object')
        name = f'policy.floors[{index}]'
        sui_multiple = amount(floor.get('sui_cost_multiple'), f'{name}.sui_cost_multiple', minimum=ONE, maximum=Decimal(1000))
        wal_multiple = amount(floor.get('walrus_cost_multiple', '1'), f'{name}.walrus_cost_multiple', minimum=ONE, maximum=Decimal(1000))
        margin = amount(floor.get('minimum_margin_fraction'), f'{name}.minimum_margin_fraction', maximum=ONE)
        if margin + payment >= ONE:
            raise ValueError(f'{name}: minimum margin plus payment fee fraction must be less than one')
        # Unless the policy bounds it, the whole cost multiple may be a token quantity rise.
        quantity = amount(floor.get('max_token_quantity_multiple', sui_multiple), f'{name}.max_token_quantity_multiple',
                          minimum=ONE, maximum=sui_multiple)
        result.append((sui_multiple, wal_multiple, margin, quantity))
    return result


def _shocks(shocks):
    if not isinstance(shocks, list):
        raise ValueError('policy.combined_shocks must be a list')
    result = []
    for index, shock in enumerate(shocks):
        if not isinstance(shock, dict):
            raise ValueError(f'policy.combined_shocks[{index}] must be an object')
        name = f'policy.combined_shocks[{index}]'
        result.append(tuple(amount(shock.get(key), f'{name}.{key}', minimum=Decimal('0.01'), maximum=Decimal(1000))
                            for key in ('token_price_multiple', 'token_quantity_multiple')))
    return result


def _capital(capital, sui_usd):
    if not isinstance(capital, dict):
        raise ValueError('working_capital must be an object')
    unknown = set(capital) - {'sui_denominated', 'usd_denominated'}
    if unknown:
        raise ValueError(f'working_capital: unknown groups {sorted(unknown)}; use sui_denominated or usd_denominated')
    groups = {}
    for group in ('sui_denominated', 'usd_denominated'):
        entries = capital.get(group, {})
        if not isinstance(entries, dict):
            raise ValueError(f'working_capital.{group} must be an object')
        groups[group] = {key: amount(value, f'working_capital.{group}.{key}') for key, value in entries.items()}
    sui_total = sum(groups['sui_denominated'].values(), ZERO)
    return groups, sui_total, sui_total / sui_usd


def _calculate(model, policy, annual, hedged):
    if not isinstance(model, dict) or not isinstance(policy, dict):
        raise ValueError('model and policy must be objects')
    if model.get('schema_version') != 2 or policy.get('schema_version') != 2:
        raise ValueError('unsupported planning schema')
    for flag, name in ((annual, 'annual'), (hedged, 'hedged')):
        if type(flag) is not bool:
            raise ValueError(f'{name} must be boolean')
    if hedged and not annual:
        # Below the renewal trigger a monthly price persists, but a monthly hedge covers one month.
        raise ValueError('hedged floors apply only to prepaid annual terms; price monthly billing unhedged')
    devices = model.get('devices')
    if type(devices) is not int or not 1 <= devices <= 1_000_000_000:
        raise ValueError('devices must be a positive whole number up to 1 billion')
    basis = model.get('basis')
    sui_usd = _validate_basis(basis)
    unknown = model.get('unquoted_costs')
    if not isinstance(unknown, list) or unknown:
        raise ValueError('required costs remain unquoted; supply all costs before pricing')
    costs = model.get('recurring_costs')
    if not isinstance(costs, dict):
        raise ValueError('recurring_costs must be an object')
    sui, walrus, services = (amount(costs.get(key), key) for key in
                            ('sui_sensitive_usd', 'walrus_usd', 'services_usd'))
    total = sui + walrus + services

    payment = amount(policy.get('payment_fee_fraction'), 'payment_fee_fraction', maximum=ONE)
    fixed = amount(policy.get('payment_fixed_usd_per_invoice'), 'payment_fixed_usd_per_invoice')
    if payment >= ONE:
        raise ValueError('payment_fee_fraction must be less than one')
    sui_multiples = multiples(policy.get('sui_cost_multiples'), 'sui_cost_multiples', Decimal('0.01'))
    wal_multiples = multiples(policy.get('walrus_cost_multiples', ['1']), 'walrus_cost_multiples', Decimal('0.01'))
    floors = _validate_floors(policy.get('floors'), payment)
    shocks = _shocks(policy.get('combined_shocks', []))
    hedge = policy.get('hedge', {})
    if not isinstance(hedge, dict):
        raise ValueError('policy.hedge must be an object')
    covered = amount(hedge.get('term_sui_purchased_fraction', '0'), 'hedge.term_sui_purchased_fraction', maximum=ONE)
    trigger = policy.get('renewal_repricing_trigger_multiple')
    if trigger is not None:
        trigger = amount(trigger, 'renewal_repricing_trigger_multiple', minimum=ONE, maximum=Decimal(1000))

    terms = model.get('commercial')
    if not isinstance(terms, dict):
        raise ValueError('commercial must be an object')
    list_price = amount(terms.get('list_monthly_usd'), 'list_monthly_usd')
    minimum = amount(terms.get('monthly_minimum_usd'), 'monthly_minimum_usd')
    discount = amount(terms.get('annual_discount_fraction'), 'annual_discount_fraction', maximum=ONE)
    if discount >= ONE:
        raise ValueError('annual_discount_fraction must be less than one')
    comparison = terms.get('comparison_monthly_usd')
    if comparison is not None:
        comparison = amount(comparison, 'comparison_monthly_usd')
    groups, sui_capital, sui_capital_sui = _capital(model.get('working_capital', {}), sui_usd)

    term = 12 if annual else 1
    # An annual card charge incurs one fixed fee, amortized over 12 months.
    fee = fixed / term
    # The policy's hedge is always reported in the hedged_* fields; it is applied to
    # the floors, and therefore to the price, only when `hedged` is requested.

    def cost(token_price, token_quantity, wal_multiple, locked=ZERO):
        """Recurring USD cost when the SUI token price and the SUI quantity the workload
        requires are at the given multiples of the basis.

        `locked` is the fraction of the basis quantity bought at the basis price. It pays
        for that quantity whatever the token does (unused purchased SUI earns no credit);
        any additional quantity is bought at the shocked token price."""
        sui_multiple = locked + max(token_quantity - locked, ZERO) * token_price
        return sui_multiple * sui + wal_multiple * walrus + services

    def price_for(margin, recurring):
        return (recurring + fee) / (ONE - margin - payment)

    floor_rows, applied = [], []
    for sui_multiple, wal_multiple, margin, quantity in floors:
        unhedged_floor = price_for(margin, cost(sui_multiple, ONE, wal_multiple))
        # Hedged cost rises with the quantity share of the move, so the bound is the worst case.
        hedged_floor = price_for(margin, cost(sui_multiple / quantity, quantity, wal_multiple, covered))
        floor_rows.append(dict(sui_cost_multiple=plain(sui_multiple), walrus_cost_multiple=plain(wal_multiple),
                               max_token_quantity_multiple=plain(quantity),
                               minimum_margin_fraction=plain(margin), floor_unhedged_usd=money(unhedged_floor),
                               floor_hedged_usd=money(hedged_floor)))
        applied.append(hedged_floor if hedged else unhedged_floor)
    risk_floor = max(applied) if applied else ZERO
    offered = list_price * (ONE - discount) if annual else list_price
    candidates = dict(list_price=offered, monthly_minimum=minimum, risk_floor=risk_floor)
    monthly = Decimal(money(max(candidates.values())))
    binding = next(name for name, value in candidates.items() if Decimal(money(value)) == monthly)
    payment_cost = monthly * payment + fee
    margins = sorted({margin for _, _, margin, _ in floors} | {ZERO})

    def margin_of(recurring):
        return fraction((monthly - recurring - payment_cost) / monthly) if monthly else None

    def target_multiple(margin):
        """SUI-sensitive cost, in multiples of the basis, at which the margin falls to `margin`."""
        return (monthly * (ONE - margin - payment) - fee - walrus - services) / sui

    def multiple_for(margin):
        """Unhedged SUI cost multiple at which the margin falls to `margin`; None when no multiple reaches it."""
        if sui == ZERO:
            return None
        multiple = target_multiple(margin)
        return multiple if multiple >= ZERO else None

    def hedged_price_multiple_for(margin):
        """Token price multiple, at the basis token quantity, at which the hedged margin falls to `margin`."""
        if sui == ZERO or covered >= ONE:
            return None
        multiple = (target_multiple(margin) - covered) / (ONE - covered)
        return multiple if multiple >= ZERO else None

    def hedged_quantity_multiple_for(margin):
        """Token quantity multiple, at the basis token price, at which the hedged margin falls to `margin`.

        Additional tokens cost what they did unhedged, so a quantity rise erodes the
        margin exactly as an unhedged move does once it exceeds the purchased quantity."""
        if sui == ZERO:
            return None
        multiple = target_multiple(margin)
        return multiple if multiple >= covered else None

    def row(sui_multiple, wal_multiple):
        recurring = cost(sui_multiple, ONE, wal_multiple)
        hedged_token = cost(sui_multiple, ONE, wal_multiple, covered)
        hedged_quantity = cost(ONE, sui_multiple, wal_multiple, covered)
        profit = monthly - recurring - payment_cost
        return dict(
            sui_cost_multiple=plain(sui_multiple), walrus_cost_multiple=plain(wal_multiple),
            implied_sui_usd=fraction(sui_multiple * sui_usd),
            recurring_cost_usd=cents(recurring),
            hedged_recurring_cost_token_price_usd=cents(hedged_token),
            hedged_recurring_cost_token_quantity_usd=cents(hedged_quantity),
            payment_cost_usd=cents(payment_cost), gross_profit_usd=cents(profit),
            infrastructure_margin_fraction=margin_of(recurring),
            hedged_margin_token_price_fraction=margin_of(hedged_token),
            hedged_margin_token_quantity_fraction=margin_of(hedged_quantity),
            # Capital is a token position: this is its value if the move is in the token price.
            sui_denominated_capital_value_usd=cents(sui_capital * sui_multiple),
            price_for_margin={plain(margin): money(price_for(margin, recurring)) for margin in margins})

    def combined(token_price, token_quantity):
        recurring = cost(token_price, token_quantity, ONE)
        hedged_recurring = cost(token_price, token_quantity, ONE, covered)
        return dict(
            token_price_multiple=plain(token_price), token_quantity_multiple=plain(token_quantity),
            sui_cost_multiple=plain(token_price * token_quantity), implied_sui_usd=fraction(token_price * sui_usd),
            recurring_cost_usd=cents(recurring), hedged_recurring_cost_usd=cents(hedged_recurring),
            infrastructure_margin_fraction=margin_of(recurring), hedged_margin_fraction=margin_of(hedged_recurring),
            sui_denominated_capital_value_usd=cents(sui_capital * token_price))

    scenarios = sorted(set(sui_multiples) | {m for m, _, _, _ in floors} | ({trigger} if trigger is not None else set()))
    rows = [row(m, ONE) for m in scenarios]
    rows += [row(ONE, w) for w in wal_multiples if w != ONE]
    combined_rows = [combined(t, q) for t, q in shocks]

    breakeven = multiple_for(ZERO)
    hedged_price_breakeven = hedged_price_multiple_for(ZERO)
    hedged_quantity_breakeven = hedged_quantity_multiple_for(ZERO)
    headroom = []
    for margin in margins:
        multiple = multiple_for(margin)
        headroom.append(dict(minimum_margin_fraction=plain(margin), sui_cost_multiple=fraction(multiple),
                             implied_sui_usd=fraction(multiple * sui_usd) if multiple is not None else None))
    repricing = None
    if trigger is not None:
        at_trigger = cost(trigger, ONE, ONE)
        repricing = dict(trigger_multiple=plain(trigger), implied_sui_usd=fraction(trigger * sui_usd),
                         infrastructure_margin_fraction=fraction((monthly - at_trigger - payment_cost) / monthly) if monthly else None,
                         price_for_margin={plain(margin): money(price_for(margin, at_trigger)) for margin in margins})
    comparison_result = None
    if comparison is not None:
        parity = {}
        for margin in margins:
            if sui != ZERO:
                multiple = (comparison * (ONE - margin - payment) - fee - walrus - services) / sui
                if multiple < ZERO:
                    # The comparison price misses this margin even if SUI operations were free.
                    parity[plain(margin)] = dict(sui_cost_multiple=None, implied_sui_usd=None)
                else:
                    parity[plain(margin)] = dict(sui_cost_multiple=fraction(multiple), implied_sui_usd=fraction(multiple * sui_usd))
        comparison_result = dict(label=terms.get('comparison_label'), monthly_usd=plain(comparison),
                                 price_ratio=fraction(monthly / comparison) if comparison else None,
                                 parity_multiple_for_margin=parity)

    return dict(
        schema_version=2, kind='subscription-planning-only', basis=basis, devices=devices,
        profile=model.get('profile'), policy=policy, annual=annual, hedged=hedged,
        hedge_applied_to_floors=hedged, term_months=term,
        recurring_costs_usd=dict(sui_sensitive=plain(sui), walrus=plain(walrus), services=plain(services), total=plain(total)),
        sui_share_of_recurring_cost=fraction(sui / total) if total else None,
        list_monthly_usd=plain(list_price), commercial_after_discount_usd=cents(offered),
        monthly_minimum_usd=plain(minimum), floors=floor_rows, risk_floor_monthly_usd=money(risk_floor),
        binding_constraint=binding, monthly_subscription_usd=money(monthly),
        effective_per_device_month_usd=fraction(monthly / devices, 5), amount_due_usd=money(monthly * term),
        term_sui_spend_usd=cents(sui * term), hedge_purchase_usd=cents(covered * sui * term),
        hedge_share_of_invoice=fraction(covered * sui * term / (monthly * term)) if monthly else None,
        breakeven=dict(sui_cost_multiple=fraction(breakeven),
                       implied_sui_usd=fraction(breakeven * sui_usd) if breakeven is not None else None,
                       hedged_token_price_multiple=fraction(hedged_price_breakeven),
                       hedged_implied_sui_usd=fraction(hedged_price_breakeven * sui_usd) if hedged_price_breakeven is not None else None,
                       hedged_token_quantity_multiple=fraction(hedged_quantity_breakeven)),
        headroom=headroom, renewal_repricing=repricing, comparison=comparison_result,
        working_capital_usd=dict(sui_denominated={k: plain(v) for k, v in groups['sui_denominated'].items()},
                                 usd_denominated={k: plain(v) for k, v in groups['usd_denominated'].items()},
                                 sui_denominated_total_usd=cents(sui_capital), sui_denominated_total_sui=fraction(sui_capital_sui, 2)),
        sensitivity=rows, combined_sensitivity=combined_rows)


def usd(text):
    value = Decimal(text)
    sign = '-' if value < 0 else ''
    return f'{sign}${abs(value).quantize(CENT, rounding=ROUND_HALF_EVEN):,.2f}'


def percent(text):
    return 'n/a' if text is None else f'{(Decimal(text) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)}%'


def table(result):
    """Markdown sensitivity table at the priced subscription."""
    lines = ['| SUI cost multiple | Implied SUI price | Recurring cost | Gross profit | Margin '
             '| Hedged margin, token price move | Hedged margin, gas/quantity move | SUI-denominated capital |',
             '|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in result['sensitivity']:
        label = f"{row['sui_cost_multiple']}×"
        if row['walrus_cost_multiple'] != '1':
            label += f" SUI, {row['walrus_cost_multiple']}× WAL"
        lines.append('| ' + ' | '.join([
            label, usd(row['implied_sui_usd']), usd(row['recurring_cost_usd']), usd(row['gross_profit_usd']),
            percent(row['infrastructure_margin_fraction']), percent(row['hedged_margin_token_price_fraction']),
            percent(row['hedged_margin_token_quantity_fraction']), usd(row['sui_denominated_capital_value_usd'])]) + ' |')
    if result['combined_sensitivity']:
        lines += ['', '| Token price × quantity | SUI cost multiple | Recurring cost | Margin | Hedged margin | SUI-denominated capital |',
                  '|---:|---:|---:|---:|---:|---:|']
        for row in result['combined_sensitivity']:
            lines.append('| ' + ' | '.join([
                f"{row['token_price_multiple']}× × {row['token_quantity_multiple']}×", f"{row['sui_cost_multiple']}×",
                usd(row['recurring_cost_usd']), percent(row['infrastructure_margin_fraction']),
                percent(row['hedged_margin_fraction']), usd(row['sui_denominated_capital_value_usd'])]) + ' |')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', type=Path)
    parser.add_argument('--policy', type=Path, default=POLICY)
    parser.add_argument('--annual', action='store_true', help='price a prepaid 12-month term')
    parser.add_argument('--hedged', action='store_true',
                        help="assume the policy's fraction of the term's SUI spend is bought when the invoice is paid")
    parser.add_argument('--table', action='store_true', help='print the Markdown sensitivity table instead of JSON')
    args = parser.parse_args()
    try:
        result = calculate(json.loads(args.model.read_text()), json.loads(args.policy.read_text()),
                           annual=args.annual, hedged=args.hedged)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        parser.exit(2, f'Pricing refused: {error}\n')
    print(table(result) if args.table else json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
