#!/usr/bin/env python3
"""Build an explicitly incomplete scenario from the SF-054 frozen observations."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/business/sf054"


def build(devices, releases):
    facts = json.loads((DATA / "network-quotes.json").read_text())
    blobs = json.loads((DATA / "blob-rate-calculations.json").read_text())
    lines = []
    for kind, quote in zip(('artifact', 'recipe'), blobs):
        for charge, category in [('storage', 'walrus_storage' if kind == 'artifact' else 'recipe_storage'),
                                 ('write', 'walrus_write')]:
            lines.append(dict(name=f'{kind} gross {charge}', category=category, kind='estimated',
                network='mainnet', unit='FROST', count=releases, amount=quote[f'{charge}_frost'],
                source='docs/business/sf054/blob-rate-calculations.json; gross rate arithmetic, subsidy unquoted'))
    # These are extrapolations of historical effects, not 4,000 measured reports
    # and not predictions of current Mainnet gas/computation/storage buckets.
    selected = [('Gr1L1HSdPH9PnXSNLWrUwzcLYZFotFZ26LEArs8mdauq', 'deployment_targets', devices * releases),
                ('5sSF3GovFFNf76vF1WpKRdtag5p95Ft6ahPHXWNTGVyV', 'receipts', devices * releases),
                ('8TzCZE7jezULePUFK2Q34LGuxtm3pjGjrbDXK2PSPztU', 'cleanup', devices * releases)]
    for digest, category, count in selected:
        tx = next(t for t in facts['historical_testnet_transactions'] if t['digest'] == digest)
        lines.append(dict(name='historical Testnet extrapolation: ' + tx['label'], category=category,
            kind='estimated', network='testnet', unit='MIST', count=count,
            effects=tx['effects']['gasEffects']['gasSummary'], status='success',
            source='docs/business/sf054/network-quotes.json#' + digest))
    walrus = json.loads((DATA / 'walrus-testnet.json').read_text())
    for tx in walrus['transactions']:
        lines.append(dict(name=tx['kind'] + ' sponsored Testnet Walrus operation', category='walrus_sui',
            kind='estimated', network='testnet', unit='MIST', count=releases, status='success',
            effects=tx['effects']['gasEffects']['gasSummary'], source='docs/business/sf054/walrus-testnet.json#' + tx['digest']))
    for category, explanation in {
        'onboarding': 'current-package device enrollment/rotation not measured; historical base fixture differs',
        'policy': 'policy/grant/group batches depend on selected scopes; historical samples retained separately',
        'walrus_sui': 'Mainnet reserve/register/certify gas and object deposits not simulated or executed',
        'renewal': 'no renewal in base case; future extension gas, subsidy and owner access unquoted',
        'retries': 'base case has zero retries; additional failure/unknown-outcome exposure must be supplied',
        'seal_provider': 'production independent key providers, threshold, quotas and availability unselected',
        'relay': 'app uses HTTP publisher; relay is an optional alternative, never automatically added',
        'gateway': 'production publisher/aggregator delivery and bandwidth SLA unquoted',
        'rpc': 'production reads/submissions/rate limits and SLA unquoted',
        'indexer': 'production receipt history retention/availability unquoted',
    }.items():
        lines.append(dict(name=explanation, category=category, kind='unquoted', network='mainnet', unit='USD',
                          count=1, amount=None, source='docs/business/FLEET_COST_MODEL.md'))
    quotes = facts['token_market_quotes']
    return {'schema_version': 1,
        'workload': {'devices': devices, 'shared_releases': releases, 'targets_per_release': devices,
                     'target_mode': 'one device-target deployment per device (group alternatives require separate measurement)',
                     'reports_per_device_per_release': 1, 'retries': 0,
                     'retention_epochs': 2, 'renewals': 0,
                     'artifact_plaintext_bytes': 1048576, 'recipe_plaintext_bytes': 317,
                     'limits': 'Sui per-deployment rows must scale with device targets, not shared blob count. Historical sample is one target.'},
        'token_prices': {symbol: {'usd': quotes[key]['usd'], 'observed_at': quotes[key]['last_updated_at'],
                                'source': facts['sources']['token_market_quotes']}
                         for symbol, key in [('SUI', 'sui'), ('WAL', 'walrus-2')]},
        'blob_quotes': [], 'lines': lines,
        'comparison': {'matched_scope': None, 'fixed_usd': None, 'fleet_per_device_usd': None,
                       'aws_per_device_usd': None,
                       'reason': 'Production availability, delivery, retention and key-provider costs unquoted on both matched paths.'}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--devices', type=int, default=1000)
    parser.add_argument('--releases', type=int, default=4)
    args = parser.parse_args()
    if min(args.devices, args.releases) < 1:
        parser.error('positive workload required')
    model = build(args.devices, args.releases)
    print(json.dumps(model, indent=2))
