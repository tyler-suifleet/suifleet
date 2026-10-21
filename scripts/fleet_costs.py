#!/usr/bin/env python3
"""Offline encrypted fleet cost accounting. No wallet access or transactions.

Amounts are integer MIST/FROST or decimal USD strings. A missing cost is unknown,
never zero. Effects rebates remain signed; budgets are exposure, not expenses.
"""
import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import re

CATEGORIES = {
    "onboarding", "policy", "deployment_targets", "receipts", "walrus_storage",
    "walrus_write", "walrus_sui", "recipe_storage", "renewal", "cleanup",
    "retries", "seal_provider", "relay", "gateway", "rpc", "indexer",
}
KINDS = {"measured", "simulated", "estimated", "unquoted", "excluded"}


def integer(value):
    if isinstance(value, bool) or str(value) != str(int(value)) or int(value) < 0:
        raise ValueError(f"expected nonnegative integer: {value!r}")
    return int(value)


def effects_cost(gas):
    # nonRefundableStorageFee is informational: it is already reflected in
    # storageCost minus storageRebate. Adding it again double counts it.
    return (integer(gas["computationCost"]) + integer(gas["storageCost"])
            - integer(gas["storageRebate"]))


def reconcile(flow):
    """Consolidate operator + device balances; internal funding cancels out."""
    start, end, incoming, outgoing, consumed = (int(flow[k]) for k in
        ("start", "end", "external_in", "external_out", "consumed"))
    if min(start, end, incoming, outgoing) < 0:
        raise ValueError("negative token balances/transfers")
    return start + incoming - outgoing - consumed - end


def walrus_quote(quote):
    """Use exact client output for ciphertext, not 4.5x + 64MB arithmetic."""
    for key in ("client_version", "ciphertext_sha256", "source", "network"):
        if not quote.get(key):
            raise ValueError(f"missing quote provenance: {key}")
    size = integer(quote["encoded_bytes"])
    start, end = integer(quote["start_epoch"]), integer(quote["end_epoch"])
    if end <= start or not re.fullmatch(r'[0-9a-f]{64}', quote["ciphertext_sha256"]):
        raise ValueError("invalid expiry interval or ciphertext hash")
    # The exact client purchase quote is authoritative (including subsidy and
    # rounding). Raw rate arithmetic is diagnostic, never a replacement quote.
    storage = integer(quote["storage_frost"])
    write = integer(quote["write_frost"])
    subsidy = integer(quote["subsidy_frost"])
    if subsidy > storage:
        raise ValueError("subsidy exceeds storage charge")
    return {"encoded_bytes": size, "epoch_interval": [start, end],
            "net_frost": storage + write - subsidy}


def calculate(model):
    if model["schema_version"] != 1:
        raise ValueError("unsupported schema")
    workload = model["workload"]
    devices, releases = integer(workload["devices"]), integer(workload["shared_releases"])
    if not devices or not releases:
        raise ValueError("devices and releases must be positive")
    totals = defaultdict(int)
    exposure = defaultdict(int)
    rows, missing, seen, digests = [], [], set(), set()
    for row in model["lines"]:
        category, kind = row["category"], row["kind"]
        if category not in CATEGORIES or kind not in KINDS or not row.get("source"):
            raise ValueError("unknown category/kind or absent evidence source")
        seen.add(category)
        unit = row["unit"]
        if unit not in {"MIST", "FROST", "USD"}:
            raise ValueError("unsupported currency")
        count = integer(row["count"])
        if row["network"] not in {"mainnet", "testnet", "localnet"}:
            raise ValueError("unknown network")
        exposure[f'{row["network"]}:budget_MIST'] += integer(row.get("budget_mist", 0)) * count
        exposure[f'{row["network"]}:unknown_MIST'] += integer(row.get("unknown_reserved_mist", 0)) * count
        if kind in {"unquoted", "excluded"}:
            if row.get("amount") is not None:
                raise ValueError("unquoted/excluded line cannot carry an amount")
            missing.append(row["name"])
            rows.append({"name": row["name"], "kind": kind, "total": None, "unit": unit})
            continue
        if row.get("effects") is not None:
            if kind not in {"measured", "simulated", "estimated"} or unit != "MIST":
                raise ValueError("effects require measured/simulated/estimated MIST")
            if row.get("status") not in {"success", "failure"}:
                raise ValueError("unknown effects cannot be booked as settled cost")
            if kind == "measured":
                digest = row.get("digest")
                if not digest or digest in digests or count != 1:
                    raise ValueError("measured transaction needs unique digest and count=1")
                digests.add(digest)
            amount = effects_cost(row["effects"])
        else:
            amount = Decimal(str(row["amount"]))
            if not amount.is_finite() or amount < 0 or (unit != "USD" and amount != int(amount)):
                raise ValueError("invalid amount")
        total = amount * count
        # Keep networks and evidence classes separate: testnet is not a mainnet invoice.
        bucket = f'{row["network"]}:{kind}:{unit}'
        totals[bucket] += total
        rows.append({"name": row["name"], "kind": kind, "total": str(total), "unit": unit,
                     "effects": row.get("effects"),
                     "per_release": str(Decimal(total) / releases),
                     "per_device": str(Decimal(total) / devices)})
    missing += sorted(CATEGORIES - seen)
    quotes = [walrus_quote(q) for q in model.get("blob_quotes", [])]
    # A complete invoice requires matching production measurements, quotes and
    # no omitted categories. Estimates/simulations are useful, but not invoices.
    complete = (not missing and bool(quotes) and
                all(q['network'] == 'mainnet' for q in model.get('blob_quotes', [])) and
                not any(value for key, value in exposure.items() if key.endswith(':unknown_MIST')) and
                all(r["kind"] == "measured" and r["network"] == "mainnet" for r in model["lines"]))
    reconciliations = {f['name']: reconcile(f) for f in model.get('token_flows', [])}
    if not reconciliations or any(reconciliations.values()):
        complete = False
    converted = {}
    prices = model.get('token_prices', {})
    for bucket, value in totals.items():
        network, kind, unit = bucket.split(':')
        if network != 'mainnet' or unit == 'USD':
            continue
        quote = prices.get({'MIST': 'SUI', 'FROST': 'WAL'}[unit])
        if quote is None:
            continue
        if not quote.get('source') or not quote.get('observed_at'):
            raise ValueError('missing FX provenance')
        price = Decimal(str(quote['usd']))
        if not price.is_finite() or price <= 0:
            raise ValueError('invalid token price')
        converted[bucket] = str(Decimal(value) / 1000000000 * price)
    comparison = model.get('comparison')
    comparison_result = {'status': 'unquoted', 'break_even_devices': None}
    if comparison and comparison.get('matched_scope') and all(comparison.get(k) is not None for k in
        ('fixed_usd', 'fleet_per_device_usd', 'aws_per_device_usd')):
        comparison_result = {'status': 'conditional_estimate', 'scope': comparison['matched_scope'],
            'break_even_devices': break_even(comparison['fixed_usd'], comparison['fleet_per_device_usd'],
                                            comparison['aws_per_device_usd'])}
    return {"complete_mainnet_bill": complete, "missing_or_excluded": missing,
            "accounting_basis": "Net token cashflows include refundable object deposits. Budgets/funding are not fees; future rebates are not booked until executed.",
            "subtotals": {k: str(v) for k, v in sorted(totals.items())},
            "exposure_not_cost": dict(exposure), "lines": rows, "blob_quotes": quotes,
            "token_reconciliation_residuals": reconciliations,
            "mainnet_usd_subtotals_not_bill": converted, "comparison": comparison_result}


def break_even(fixed_usd, fleet_per_device_usd, aws_per_device_usd):
    """Matched scope only: F + N*v = N*a; caller supplies all service costs."""
    fixed, variable, aws = map(Decimal, map(str, (fixed_usd, fleet_per_device_usd, aws_per_device_usd)))
    if not all(x.is_finite() and x >= 0 for x in (fixed, variable, aws)):
        raise ValueError("invalid comparison inputs")
    return None if aws <= variable else str(fixed / (aws - variable))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    result = calculate(json.loads(args.model.read_text()))
    print(json.dumps(result, indent=2))
    if args.require_complete and not result["complete_mainnet_bill"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
