#!/usr/bin/env python3
"""Bounded canonical-USDC acceptance; dedicated agent and machine lease only.

Retains one non-replenishing ledger and verified publication across retries.
Unknown outcomes require reconciliation before another mutation. No faucet call.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import deployment_manifest as dm
from live_common import Live, ROOT, WALRUS_TESTNET, stage
from check_move_package_size import check_package_size
from sui_toolchain import compile_package

AGENT = '0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965'
USDC = '0xa1ec7fc00a6f40db9693ad1415d0c193ad3906494428cf252621037bd7117e29::usdc::USDC'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wallet', required=True)
    p.add_argument('--usdc-coin', required=True, help='verified dedicated-agent canonical USDC coin ID')
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True)
    args = p.parse_args()
    assert os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'), 'machine lease required'
    live = Live(args.wallet, 'testnet', 1_000_000_000)
    live.preflight()
    assert live.operator == AGENT
    treasury = json.loads((ROOT / 'deployments/testnet/treasury.json').read_text())['treasury_payee']
    coin_id = dm.object_id(args.usdc_coin)
    coin = live.object(coin_id)
    assert dm.same_type(coin['objType'], f'0x2::coin::Coin<{USDC}>')
    assert coin['owner'] == {'AddressOwner': AGENT}
    fingerprint = hashlib.sha256(b''.join(path.read_bytes() for path in sorted([
        *(ROOT / 'contracts/sources').glob('*.move'), ROOT / 'contracts/Move.toml', ROOT / 'contracts/Move.lock']))).hexdigest()
    state = json.loads(args.state.read_text()) if args.state.exists() else {'fingerprint': fingerprint, 'results': {}, 'transactions': []}
    assert state['fingerprint'] == fingerprint, 'reconcile previous fixture before changing bytecode'
    assert not any(t['status'] == 'unknown' for t in state['transactions']), 'unknown outcome requires reconciliation'
    live.evidence['transactions'] = state['transactions']
    live.ledger.evidence = state['transactions']
    live.ledger.used = state.get('allowance_used_mist', 0)
    state.setdefault('initial_balance_mist', live.start_balance + live.ledger.used)
    live.start_balance = state['initial_balance_mist']
    for entry in state['transactions']:
        if entry.get('digest'):
            assert live.executed(entry['digest'])['effects']['status']['status'] == entry['status']

    def save():
        state['allowance_used_mist'] = live.ledger.used
        args.state.parent.mkdir(parents=True, exist_ok=True)
        args.state.write_text(json.dumps(state, indent=2) + '\n')
        args.state.chmod(0o600)

    # Reserve durably before every PTB signature. A killed process cannot reset
    # allowance or replay ambiguous custody operations on a subsequent run.
    original_signer = live.signer
    def durable_signer(config, address):
        sign = original_signer(config, address)
        def sign_exact(tx_bytes):
            state['pending_transaction_digest'] = live.decode(tx_bytes)['digest']
            save()
            return sign(tx_bytes)
        return sign_exact
    live.signer = durable_signer

    def operation(label, submit):
        if label in state['results']:
            result = state['results'][label]
            read = live.executed(result['digest'])
            assert read['effects']['status']['status'] == 'success'
            return result
        try:
            result = submit()
            # Independent read, including actual signed gas and effects.
            read = live.executed(result['digest'])
            assert read['digest'] == result['digest'] and read['effects']['status']['status'] == 'success'
            state['results'][label] = read
            return read
        finally:
            save()

    with tempfile.TemporaryDirectory(prefix='subscription-testnet-') as directory:
        package_dir = Path(directory) / 'contracts'
        stage(ROOT / 'contracts', package_dir)
        package_size = check_package_size(compile_package(live.build_sui, package_dir))
        live.evidence['package_size_bytes'] = package_size
        def pending(identity):
            state['pending_publication'] = identity
            save()
        assert not state.get('pending_publication') or 'publish' in state['results'], 'reconcile pending publication first'
        r = operation('publish', lambda: live.publish('publish subscription fixture', package_dir, Path(directory) / 'Published.toml', persist=pending))
        package = live.published(r)
        registry = live.created(r, '::device_registry::DeviceRegistry')[0]
        cap = live.created(r, '::package::UpgradeCap')[0]
        assert dm.object_id(live.owner(cap)) == AGENT
        manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url, package=package,
            upgrade_cap=cap, registry=registry, network='testnet', publisher=WALRUS_TESTNET[0], aggregator=WALRUS_TESTNET[1],
            treasury_payee=treasury, subscription_coins={'USDC': {'coin_type': USDC, 'decimals': 6}}))
        assert all(manifest['type_origins'][t] == dm.object_id(package) for t in dm.SUBSCRIPTION_TYPES)
        args.manifest.write_text(json.dumps(manifest, indent=2) + '\n')
        def tx(label, *ptb):
            return operation(label, lambda: live.transaction(label, *ptb, budget=60_000_000))
        at = lambda value: '@' + value
        target = lambda name: f'{package}::subscription::{name}'
        r = tx('create customer and annual offer', '--move-call', f'{package}::customer::create', at(registry), '"USDC acceptance"',
               '--move-call', target('offer'), f'<{USDC}>', at(registry), at(treasury), '1', '100', '1000', '10000', 'true', '@0x6')
        customer = live.created(r, '::customer::Customer')[0]
        offer = live.created(r, f'::subscription::Offer<{USDC}>')[0]
        r = tx('open prepaid account', '--split-coins', at(coin_id), '[108000]', '--assign', 'payment',
               '--move-call', target('open'), f'<{USDC}>', at(customer), at(registry), at(offer), 'payment.0', '@0x6')
        account = live.created(r, f'::subscription::Account<{USDC}>')[0]
        tx('deposit surplus', '--split-coins', at(coin_id), '[1000]', '--assign', 'payment',
           '--move-call', target('deposit'), f'<{USDC}>', at(account), 'payment.0')
        tx('collect first period', '--move-call', target('collect'), f'<{USDC}>', at(account), at(customer), '@0x6')
        tx('cancel and refund', '--move-call', target('cancel'), f'<{USDC}>', at(account), at(customer), '@0x6',
           '--assign', 'refund', '--transfer-objects', '[refund]', at(AGENT))
        account_read = live.object(account)
        content = account_read['content']
        assert content['cancelled'] is True and int(content['balance']) == 0 and int(content['remaining']) == 0
        assert content['treasury'] == treasury and int(content['fee']) == 9000
        live.evidence.update(fingerprint=fingerprint, objects={'package': package, 'registry': registry, 'upgrade_cap': cap,
            'customer': customer, 'offer': offer, 'account': account}, account_read=account_read,
            coin_type=USDC, treasury_payee=treasury, usdc_open_base_units=108000, usdc_deposit_base_units=1000,
            usdc_fee_base_units=9000, usdc_refund_base_units=100000,
            limits=['Testnet only; 1 SUI non-replenishing allowance; no earlier public writes for this retained implementation.',
                    'Incoming Circle funds do not replenish gas allowance; no faucet or new signing identity.',
                    'Physical/mainnet/provider/manual-wallet acceptance remains unrun in #32/#159.'])
        live.evidence['effects'] = [{k: r[k] for k in ('digest', 'effects', 'objectChanges', 'balanceChanges') if k in r} for r in state['results'].values()]
        live.finish(args.evidence)
    print('PASS: canonical USDC deposit, bounded first collection and cancellation/refund')


if __name__ == '__main__':
    main()
