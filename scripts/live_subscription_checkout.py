#!/usr/bin/env python3
"""Testnet-only SF-182 real frontend checkout using a reusable SF-181 package.

Requires the dedicated agent wallet and global mutation lease. Uses a disposable
0.01 USDC test tariff; does not publish packages or change the public price table.
State is exclusive: interrupted/unknown runs require reconciliation, never replay.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

import deployment_manifest as dm
from live_common import Live, ROOT
import sui_toolchain as tools

AGENT = '0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wallet', required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--build-sui', default='sui')
    p.add_argument('--existing-account', help='Explicit verified disposable fixture account; exercise deposit/receipt only')
    p.add_argument('--offer', help='Explicit compatible disposable offer; required for a bound customer')
    p.add_argument('--inspect-only', action='store_true', help='Read fixture and offer eligibility without signing or acquiring a lease')
    args = p.parse_args()
    if not args.inspect_only and not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
        raise RuntimeError('global machine-session lease required')
    retained = json.loads(args.state.read_text()) if args.state.exists() else None
    if retained and (any(t.get('status') != 'success' for t in retained['transactions']) or retained.get('prepay_started')) and not args.inspect_only:
        raise RuntimeError('Interrupted or completed checkout requires reconciliation; refusing replay')
    if args.existing_account and retained and len(retained['transactions']) != 1:
        raise RuntimeError('Existing deposit run requires reconciliation; refusing replay')
    if retained and retained.get('pending_digest') not in (None, *[t['digest'] for t in retained['transactions']]):
        raise RuntimeError('Pending digest requires reconciliation; refusing replay')
    live = Live(args.wallet, 'testnet', 100_000_000, build_sui=args.build_sui)
    live.preflight()
    assert live.chain == '4c78adac' and live.operator == AGENT
    manifest = json.loads(args.manifest.read_text())
    dm.verify(manifest)
    assert manifest['chain_identifier'] == live.chain
    assert dm.object_id(live.owner(manifest['upgrade_cap_id'])) == AGENT
    registry = manifest['shared_objects']['registry']['id']
    assert dm.object_id(live.object(registry)['content']['admin']) == AGENT
    assert manifest['treasury_payee'] == json.loads((ROOT / 'deployments/testnet/treasury.json').read_text())['treasury_payee']
    usdc = manifest['subscription_coins']['USDC']['coin_type']
    assert usdc == '0xa1ec7fc00a6f40db9693ad1415d0c193ad3906494428cf252621037bd7117e29::usdc::USDC'
    selected_offer = args.offer or (retained or {}).get('offer')
    if not selected_offer and retained:
        for entry in retained['transactions']:
            if entry['label'] == 'SF-182 disposable customer and monthly USDC offer':
                offers = live.created(live.executed(entry['digest']), f'::subscription::Offer<{usdc}>')
                if len(offers) == 1:
                    selected_offer = offers[0]
    discovery = json.loads(subprocess.check_output([
        'node', str(ROOT / 'scripts/live_subscription_checkout.cjs'), str(args.manifest),
        '--discover', selected_offer or '-', AGENT], cwd=ROOT, text=True))
    if args.inspect_only or (discovery['reason'] and not args.existing_account):
        args.evidence.write_text(json.dumps({'fixture_preflight': discovery,
            'prior_allowance_used_mist': (retained or {}).get('allowance_used_mist', 0),
            'transactions_submitted': 0}, indent=2) + '\n')
        print(json.dumps(discovery))
        if discovery['reason'] and not args.inspect_only:
            raise RuntimeError('Fixture ineligible; see recorded preflight. No transaction submitted')
        return
    state = retained or {'transactions': live.evidence['transactions'], 'limit_mist': 100_000_000, 'manifest': str(args.manifest)}
    args.state.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents a second run from racing this ledger.
    if retained:
        # Reconcile all historical charges; storage refunds never replenish allowance.
        for entry in retained['transactions']:
            effects = live.executed(entry['digest'])['effects']
            assert effects['status']['status'] == entry['status']
            gas = effects['gasUsed']
            assert entry['gas_mist'] == max(0, int(gas['computationCost']) + int(gas['storageCost']) - int(gas['storageRebate']))
        live.evidence['transactions'] = retained['transactions']
        live.ledger.evidence = retained['transactions']
        assert retained['allowance_used_mist'] == sum(t['gas_mist'] + t.get('funding_mist', 0) for t in retained['transactions'])
        live.ledger.used = retained['allowance_used_mist']
    else:
        with args.state.open('x') as f:
            f.write(json.dumps(state) + '\n')
    args.state.chmod(0o600)

    def save():
        state['allowance_used_mist'] = live.ledger.used
        args.state.write_text(json.dumps(state, indent=2) + '\n')

    original = live.signer
    def signer(config, address):
        sign = original(config, address)
        def exact(tx):
            live.assert_lock()
            state['pending_digest'] = live.decode(tx)['digest']
            save()
            return sign(tx)
        return exact
    live.signer = signer
    package = manifest['call_package_id']
    node = None
    try:
        if args.existing_account:
            # Account-only mode never opens/renews a cancelled plan or claims it
            # active. The node bridge validates the typed account and treasury.
            node = subprocess.Popen(['node', str(ROOT / 'scripts/live_subscription_checkout.cjs'), str(args.manifest), '--existing-account', args.existing_account, AGENT], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            message = json.loads(node.stdout.readline())
            assert message['label'] == 'deposit'
            def submit_deposit(reserved):
                tx = message['tx']; data = tools.decode(tx)
                tools.verify_ptb_transaction(data, sender=AGENT, budget=reserved, calls=[f'{package}::subscription::deposit'])
                signature = live.signer(None, AGENT)(tx)
                return tools.bound(live.executor()('execute-signed-tx', '--tx-bytes', tx, '--signatures', signature), data['digest'])
            result = live.execute('SF-182 frontend payment-link USDC deposit', submit_deposit, gas=20_000_000, require_full=True)
            save()
            node.stdin.write(json.dumps({'digest': result['digest']}) + '\n'); node.stdin.flush()
            report = json.loads(node.stdout.readline())['result']
            assert node.wait(timeout=120) == 0
            account = report['account']
            assert account['id'] == dm.object_id(args.existing_account) and account['cancelled']
            # Only this run's 1,000-unit surplus was present; cancellation returns
            # it to its existing dedicated-agent administrator without new fees.
            refund = live.transaction('SF-182 refund explicitly selected cancelled fixture deposit', '--move-call', f'{package}::subscription::cancel', f'<{usdc}>', '@' + account['id'], '@' + account['customer_id'], '@0x6', '--assign', 'refund', '--transfer-objects', '[refund]', '@' + AGENT, budget=20_000_000)
            save()
            live.evidence.update(checkout=report, refund_digest=refund['digest'], manifest=str(args.manifest), limits=report['limits'])
            live.finish(args.evidence)
            print('PASS: frontend payment-link USDC escrow deposit, typed effects/event confirmation, receipts and full refund; existing cancelled fixture remains cancelled')
            return
        state['prepay_started'] = True
        save()
        customer, offer = discovery['customer'], selected_offer
        if not customer or not offer:
            calls = []
            if not customer:
                calls += ['--move-call', f'{package}::customer::create', '@' + registry, '"SF-182 checkout acceptance"']
            if not offer:
                calls += ['--move-call', f'{package}::subscription::offer', f'<{usdc}>', '@' + registry,
                          '@' + manifest['treasury_payee'], '1', '100', '1000', '10000', 'false', '@0x6']
            setup = live.transaction('SF-182 disposable customer and monthly USDC offer', *calls, budget=20_000_000)
            save()
            customer = customer or live.created(setup, '::customer::Customer')[0]
            offer = offer or live.created(setup, f'::subscription::Offer<{usdc}>')[0]
        state.update(customer=customer, offer=offer)
        save()
        node = subprocess.Popen(['node', str(ROOT / 'scripts/live_subscription_checkout.cjs'), str(args.manifest), customer, offer, AGENT],
                                cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        for step in (discovery['action'], 'deposit', 'collect'):
            message = json.loads(node.stdout.readline())
            assert message['label'] == step
            def submit(reserved, tx=message['tx'], step=step):
                data = tools.decode(tx)
                calls = [f'{package}::subscription::{step}']
                if step == 'renew' and any(c.get('function') == 'deposit' for c in data['commands']):
                    calls.insert(0, f'{package}::subscription::deposit')
                tools.verify_ptb_transaction(data, sender=AGENT, budget=reserved, calls=calls)
                # Persist exact digest and remaining allowance BEFORE signing.
                state['pending_digest'] = data['digest']
                save()
                signature = live.signer(None, AGENT)(tx)
                return tools.bound(live.executor()('execute-signed-tx', '--tx-bytes', tx, '--signatures', signature), data['digest'])
            r = live.execute(f'SF-182 frontend {step}', submit, gas=20_000_000, require_full=True)
            save()
            node.stdin.write(json.dumps({'digest': r['digest']}) + '\n')
            node.stdin.flush()
        report = json.loads(node.stdout.readline())['result']
        assert node.wait(timeout=120) == 0
        state['result'] = report
        save()
        # Refund only this explicitly created disposable account's surplus.
        refund = live.transaction('SF-182 cancel and refund disposable surplus', '--move-call', f'{package}::subscription::cancel',
            f'<{usdc}>', '@' + report['account']['id'], '@' + customer, '@0x6', '--assign', 'refund',
            '--transfer-objects', '[refund]', '@' + AGENT, budget=20_000_000)
        save()
        assert live.executed(refund['digest'])['effects']['status']['status'] == 'success'
        live.evidence.update(checkout=report, refund_digest=refund['digest'], manifest=str(args.manifest),
            limits=['Testnet only; reusable SF-181 fixture package; no publication or original app objects modified.',
                    '0.01 USDC disposable test tariff, 0.001 USDC surplus fully refunded; no production tariff or market conversion claimed.',
                    'Same dedicated signer exercises permissionless deposit; manual second-wallet/extension/Pi/mainnet/provider checks remain #32/#159.'])
        live.finish(args.evidence)
        print('PASS: actual frontend USDC prepay, link deposit, collection, typed confirmation, receipt CSV and refund')
    finally:
        save()
        if node and node.poll() is None:
            node.kill()
            node.wait()


if __name__ == '__main__':
    main()
