#!/usr/bin/env python3
"""Cost-indexed public prices (prototype): list prices are the minimum, SUI-cost floors can raise them.

For each public configuration the published per-device price is

    max(list price, policy floor at the current network-cost basis)

where the floor is the subscription policy's risk floor (scripts/subscription_pricing.py,
docs/business/subscription-policy.json) for a reference fleet whose SUI-sensitive cost is
scaled from the quote basis to the current SUI price and reference gas price.

    python3 scripts/cost_indexed_pricing.py                 # report current prices and headroom
    python3 scripts/cost_indexed_pricing.py --set-basis --sui-usd 1.42 --rgp 100 --as-of 2026-11-25 \\
        --source "CoinGecko SUI/USD close 2026-11-25; Mainnet RGP epoch 1320"
    python3 scripts/cost_indexed_pricing.py --set-basis --fetch-sui-price --rgp 100
    python3 scripts/subscription_pricing.py --generate-site # publish the recalculated page

Prices change only when the basis file is updated and the site regenerated. No wallets,
invoices, chain writes or live tariffs are touched.
"""
import argparse
from datetime import date
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from subscription_pricing import calculate, POLICY  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COSTS = ROOT / 'docs/business/cost-indexed-pricing.json'
CENT = Decimal('0.01')
COINGECKO = 'https://api.coingecko.com/api/v3/simple/price?ids=sui&vs_currencies=usd'


def load(path=COSTS):
    costs = json.loads(Path(path).read_text())
    if costs.get('schema_version') != 1:
        raise ValueError('unsupported cost-indexed pricing schema')
    return costs


def sui_scale(costs):
    """How far SUI-sensitive cost has moved from the quote basis (token price × reference gas price)."""
    quote, current = costs['quote_basis'], costs['current_basis']
    scale = Decimal(current['sui_usd']) / Decimal(quote['sui_usd'])
    if current.get('reference_gas_price_mist') and quote.get('reference_gas_price_mist'):
        scale *= Decimal(current['reference_gas_price_mist']) / Decimal(quote['reference_gas_price_mist'])
    return scale


def floor_per_device(costs, policy, key, list_rate, minimum, scale=None):
    """The policy's risk floor per device per month at `scale` (default: the current basis)."""
    scale = sui_scale(costs) if scale is None else Decimal(scale)
    config = costs['configurations'][key]
    devices = int(costs['reference_devices'])
    basis = dict(costs['current_basis'])
    model = dict(
        schema_version=2, devices=devices, profile=key, basis=basis, unquoted_costs=[],
        recurring_costs=dict(
            sui_sensitive_usd=str(Decimal(config['sui_sensitive_usd_per_device']) * devices * scale),
            walrus_usd=str(Decimal(config['walrus_usd_per_device']) * devices),
            services_usd='0'),
        commercial=dict(list_monthly_usd=str(Decimal(str(list_rate)) * devices),
                        monthly_minimum_usd=str(minimum), annual_discount_fraction='0'),
        working_capital={})
    result = calculate(model, policy)
    return Decimal(result['risk_floor_monthly_usd']) / devices


def published_rate(list_rate, floor):
    """List price is the minimum; a binding floor rounds up to the next cent so margin never erodes."""
    list_rate = Decimal(str(list_rate))
    floor = floor.quantize(CENT, rounding=ROUND_CEILING)
    return max(list_rate, floor), floor > list_rate


def headroom(costs, policy, key, list_rate, minimum):
    """SUI cost multiple of the quote basis at which the floor would first exceed the list price."""
    low, high = Decimal(0), Decimal(1000)
    if floor_per_device(costs, policy, key, list_rate, minimum, high) <= Decimal(str(list_rate)):
        return None
    for _ in range(60):
        mid = (low + high) / 2
        if floor_per_device(costs, policy, key, list_rate, minimum, mid) > Decimal(str(list_rate)):
            high = mid
        else:
            low = mid
    return low


def fetch_sui_price():
    request = urllib.request.Request(COINGECKO, headers={'User-Agent': 'suifleet-pricing/1', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        price = json.load(response)['sui']['usd']
    return str(Decimal(str(price)))


def set_basis(costs, sui_usd, rgp, as_of, sources):
    price = Decimal(str(sui_usd))
    if not price.is_finite() or price <= 0:
        raise ValueError('SUI/USD price must be a positive number')
    if rgp is not None and (type(rgp) is not int or rgp <= 0):
        raise ValueError('reference gas price must be a positive whole number of MIST')
    if not sources or not all(isinstance(text, str) and text.strip() for text in sources):
        raise ValueError('a dated source is required for the basis')
    current = costs['current_basis']
    current.update(kind='estimated', as_of=as_of, sui_usd=str(Decimal(sui_usd)))
    if rgp is not None:
        current['reference_gas_price_mist'] = int(rgp)
    current['sources'] = sources
    return costs


def report(costs, policy, prices):
    import site_pricing
    rows = []
    minimum = prices['base']['monthlyMinimumUsd']
    for config in site_pricing.CONFIGURATIONS:
        key = config['cost']
        list_rate = site_pricing.list_rate(prices, config)
        floor = floor_per_device(costs, policy, key, list_rate, minimum)
        rate, binding = published_rate(list_rate, floor)
        room = headroom(costs, policy, key, list_rate, minimum)
        rows.append(dict(configuration=config['name'], list_usd=str(list_rate), floor_usd=str(floor.quantize(Decimal('0.0001'))),
                         published_usd=str(rate), floor_binds=binding,
                         list_holds_until_sui_cost_multiple=None if room is None else str(room.quantize(Decimal('0.01')))))
    return dict(basis=costs['current_basis'], sui_cost_multiple_of_quote=str(sui_scale(costs).quantize(Decimal('0.0001'))),
                configurations=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--set-basis', action='store_true', help='record a new current basis, then report')
    parser.add_argument('--sui-usd', help='SUI/USD price for the new basis')
    parser.add_argument('--fetch-sui-price', action='store_true', help='read SUI/USD from the public CoinGecko API')
    parser.add_argument('--rgp', type=int, help='Mainnet reference gas price in MIST (kept when omitted)')
    parser.add_argument('--as-of', default=date.today().isoformat())
    parser.add_argument('--source', action='append', default=[], help='dated source for the basis (repeatable)')
    args = parser.parse_args()
    costs = load()
    if args.set_basis:
        if bool(args.sui_usd) == bool(args.fetch_sui_price):
            parser.error('--set-basis needs exactly one of --sui-usd or --fetch-sui-price')
        sui_usd = fetch_sui_price() if args.fetch_sui_price else args.sui_usd
        sources = args.source or [f'SUI/USD {sui_usd} ({"CoinGecko simple price" if args.fetch_sui_price else "operator supplied"}) on {args.as_of}']
        set_basis(costs, sui_usd, args.rgp, args.as_of, sources)
        COSTS.write_text(json.dumps(costs, indent=2, ensure_ascii=False) + '\n')
    prices = json.loads((ROOT / 'dapp/src/lib/fleet-price-table.json').read_text())
    print(json.dumps(report(costs, json.loads(POLICY.read_text()), prices), indent=2))
    if args.set_basis:
        print('Basis saved. Publish with: python3 scripts/subscription_pricing.py --generate-site', file=sys.stderr)


if __name__ == '__main__':
    main()
