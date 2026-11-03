#!/usr/bin/env python3
"""Cost experiment over recorded effects; never a transaction or acceptance proof.

The deliberately optimistic scenario permits every observed rebate to offset
every observed charge, even across package versions and lifecycle boundaries.
It also erases computation in a second scenario. Neither scenario is executable:
the real ledger still clamps each transaction at zero and reserves full budgets.
"""
import argparse
import hashlib
import json
from pathlib import Path


def analyze(receipt):
    runs = {run['phase']: run for run in receipt['runs']}
    fresh, retained = (runs[name] for name in ('pair-0-fresh', 'pair-0-retained'))
    for key in ('base', 'head', 'chain', 'operator'):
        if fresh[key] != retained[key]:
            raise ValueError(f'cannot combine different {key}')
    if fresh['success'] or not retained['success']:
        raise ValueError('requires the measured failed fresh and successful retained continuation')
    computation = storage = funding = ledger_charge = refunds_discarded = 0
    for run in (fresh, retained):
        run_charge = 0
        for tx in run['transactions']:
            if tx['status'] != 'success' or not tx.get('digest'):
                raise ValueError('requires known successful execution effects')
            gas = tx['gas_used']
            compute, cost, rebate = (int(gas[name]) for name in
                                     ('computationCost', 'storageCost', 'storageRebate'))
            grant = tx['funding_mist']
            if min(compute, cost, rebate, grant) < 0:
                raise ValueError('negative gas/funding field')
            signed = compute + cost - rebate
            charged = max(0, signed)
            if tx['gas_mist'] != charged:
                raise ValueError('effects disagree with ledger gas')
            computation += compute
            storage += cost - rebate
            funding += grant
            refunds_discarded += max(0, -signed)
            run_charge += charged + grant
        if run_charge != run['allowance_used_mist']:
            raise ValueError('unexplained grants or ledger charges')
        ledger_charge += run_charge
    limit = fresh['max_spend_mist']
    return dict(base=fresh['base'], head=fresh['head'], chain=fresh['chain'],
        limit_mist=limit, measured_ledger_charge_mist=ledger_charge,
        measured_deficit_mist=ledger_charge - limit,
        computation_mist=computation, signed_storage_mist=storage,
        funding_mist=funding, discarded_negative_net_mist=refunds_discarded,
        hypothetical_global_net_mist=computation + storage + funding,
        hypothetical_global_net_deficit_mist=computation + storage + funding - limit,
        hypothetical_zero_computation_deficit_mist=storage + funding - limit,
        acceptance_complete=False,
        limits=[
            'Observed effects are previous local receipts, not new executions.',
            'Global rebate netting and erased computation are non-executable optimistic scenarios, not ledger changes.',
            'Grouping can change actual computation and storage effects; this is not an impossibility proof for arbitrary regrouping or source reduction.',
            'Retained success cannot certify fresh acceptance; future temporal operations and public exact-head acceptance remain unproved.',
        ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt', type=Path)
    args = parser.parse_args()
    raw = args.receipt.read_bytes()
    result = analyze(json.loads(raw))
    result['receipt_sha256'] = hashlib.sha256(raw).hexdigest()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
