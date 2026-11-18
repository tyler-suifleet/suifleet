#!/usr/bin/env python3
"""Bounded Testnet simulated purchase -> actual frontend prepay, no provider.

Creates one isolated identity/customer; never replays an existing state directory.
Unknown outcomes retain the private exact digest/allowance for reconciliation.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

import deployment_manifest as dm
from live_common import Live, ROOT
from live_subscription_checkout import AGENT
from testnet_budget import Budget
import sui_toolchain as tools


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wallet', required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--offer', required=True)
    p.add_argument('--private-dir', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--build-sui', default='sui')
    p.add_argument('--resume-ready', action='store_true', help='Resume only a funded purchase whose prepay was refused before signing')
    p.add_argument('--resume-failed-create', action='store_true', help='Reconcile one effects-confirmed insufficient-gas create, then use the same allowance')
    p.add_argument('--resume-unfunded', action='store_true', help='Only reconcile one explicitly not-submitted funding attempt; never replay signed/unknown operations')
    args = p.parse_args()
    assert os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'), 'machine mutation lease required'
    prior = json.loads((args.private_dir / 'state.json').read_text()) if args.resume_unfunded or args.resume_failed_create or args.resume_ready else None
    if prior:
        assert prior['limit_mist'] == 100_000_000
        if args.resume_ready:
            assert prior.get('customer') and not prior.get('account') and prior.get('purchase')
            assert prior['child_transactions'][-1]['status'] == 'not_submitted'
            assert prior['child_transactions'][-1]['label'] == 'SF-183 frontend prepay after simulated arrival'
            assert not prior.get('resumed_ready')
        elif args.resume_unfunded:
            assert prior['allowance_used_mist'] == 0 and not prior['child_transactions']
            assert len(prior['transactions']) == 1 and prior['transactions'][0]['status'] == 'not_submitted'
            assert not prior.get('resumed_unfunded'), 'Unfunded recovery already attempted'
        else:
            assert not prior.get('customer') and not prior.get('recovered_create')
            assert len(prior['child_transactions']) == 1
            assert prior['child_transactions'][0]['status'] == 'failure'
    else:
        assert not args.private_dir.exists(), 'Existing run requires reconciliation, not replay'
    live = Live(args.wallet, 'testnet', 100_000_000, build_sui=args.build_sui)
    live.preflight()
    assert live.chain == '4c78adac' and live.operator == AGENT
    m = json.loads(args.manifest.read_text()); dm.verify(m)
    assert m['network'] == 'testnet' and m['chain_identifier'] == live.chain
    assert dm.object_id(live.owner(m['upgrade_cap_id'])) == AGENT
    assert dm.object_id(live.object(m['shared_objects']['registry']['id'])['content']['admin']) == AGENT
    assert m['treasury_payee'] == json.loads((ROOT / 'deployments/testnet/treasury.json').read_text())['treasury_payee']
    usdc = m['subscription_coins']['USDC']['coin_type']
    assert usdc == '0xa1ec7fc00a6f40db9693ad1415d0c193ad3906494428cf252621037bd7117e29::usdc::USDC'
    # verifyDeployment/readOffer verifies all type origins and the canonical
    # test tariff BEFORE funding. No published customer price is changed.
    discovery = json.loads(subprocess.check_output(['node', str(ROOT / 'scripts/live_subscription_checkout.cjs'), str(args.manifest), '--discover', args.offer, AGENT], cwd=ROOT, text=True))
    assert discovery['offer']['fee'] == '10000' and discovery['offer']['periods'] == '1'
    if prior:
        address = prior['address']
        if args.resume_unfunded: assert live.balance(address) == 0, 'Unfunded identity already has gas'
    else:
        args.private_dir.mkdir(mode=0o700, parents=True)
        address = subprocess.check_output(['node', '-e', '''
    const fs=require('fs'),{appRequire}=require('./scripts/frontend_test_utils.cjs');
    const {Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519'),{decodeSuiPrivateKey}=appRequire('@mysten/sui/cryptography');
    const k=new Ed25519Keypair(),seed=decodeSuiPrivateKey(k.getSecretKey()).secretKey;
    fs.writeFileSync(process.argv[1]+'/sui.keystore',JSON.stringify([Buffer.concat([Buffer.from([0]),Buffer.from(seed)]).toString('base64')]),{mode:0o600});console.log(k.toSuiAddress());
    ''', str(args.private_dir)], cwd=ROOT, text=True).strip()
    config = args.private_dir / 'client.yaml'
    config.write_text(f'keystore:\n  File: {args.private_dir}/sui.keystore\nenvs:\n- alias: testnet\n  rpc: {live.rpc_url}\n  ws: null\n  basic_auth: null\nactive_env: testnet\nactive_address: "{address}"\n'); config.chmod(0o600)
    child = Budget(prior.get('child_funding_mist', 45_000_000) if prior else 45_000_000, prior['child_transactions'] if prior else [])
    if prior:
        for t in prior['transactions'] + prior['child_transactions']:
            if t['status'] == 'not_submitted': continue
            r = live.executed(t['digest']); gas = r['effects']['gasUsed']
            assert t['status'] == r['effects']['status']['status']
            assert t['gas_mist'] == max(0, int(gas['computationCost']) + int(gas['storageCost']) - int(gas['storageRebate']))
            if t['status'] == 'failure':
                assert r['effects']['status']['error'] == 'InsufficientGas' and not live.created(r, '::customer::Customer')
        live.ledger.used = prior['allowance_used_mist']; child.used = prior['child_used_mist']
        assert live.ledger.used == sum(t.get('gas_mist', 0)+t['funding_mist'] for t in prior['transactions'] if t['status'] != 'not_submitted')
        assert child.used == sum(t.get('gas_mist', 0) for t in child.evidence)
        if args.resume_failed_create: assert live.balance(address) == child.limit-child.used
    if prior:
        live.evidence['transactions'].extend(prior['transactions'])
    state = dict(address=address, transactions=live.evidence['transactions'], child_transactions=child.evidence, limit_mist=100_000_000, resumed_unfunded=bool(prior), recovered_create=args.resume_failed_create, resumed_ready=args.resume_ready)
    if args.resume_ready:
        state.update(customer=prior['customer'], purchase=prior['purchase'])
    def save():
        state.update(allowance_used_mist=live.ledger.used, child_used_mist=child.used, child_funding_mist=child.limit)
        target = args.private_dir / 'state.json'; target.write_text(json.dumps(state, indent=2)+'\n'); target.chmod(0o600)
    original = live.signer
    def signer(conf, sender):
        sign = original(conf, sender)
        def durable(tx):
            live.assert_lock(); state['pending_digest'] = live.decode(tx)['digest']; save()
            return sign(tx)
        return durable
    live.signer = signer
    save(); node = None
    package, registry = m['call_package_id'], m['shared_objects']['registry']['id']
    try:
        if not args.resume_failed_create and not args.resume_ready:
            live.transaction('SF-183 fund isolated test identity', '--split-coins', 'gas', '[45000000]', '--assign', 'funds', '--transfer-objects', '[funds.0]', '@'+address, funding=45_000_000, budget=10_000_000); save()
        if args.resume_ready:
            customer = prior['customer']
        else:
            created = live.transaction('SF-183 isolated customer', '--move-call', package+'::customer::create', '@'+registry, '"SF-183 simulated purchase"', budget=35_000_000, sender=(config, child)); save()
            customer = live.created(created, '::customer::Customer')[0]; state['customer'] = customer; save()
        node = subprocess.Popen(['node', str(ROOT / 'scripts/live_subscription_card.cjs'), str(args.manifest), address, args.offer], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        def ask(**message):
            node.stdin.write(json.dumps(message)+'\n'); node.stdin.flush()
            line = node.stdout.readline()
            if not line: raise RuntimeError('Card bridge disconnected; reconcile retained ledger')
            return json.loads(line)
        start = ask(action='start', customer=customer, **({'purchase': prior['purchase']['purchase']} if args.resume_ready else {}))
        assert start['state']['state'] == ('ready' if args.resume_ready else 'waiting') and start['purchase']['startBalance'] == '0'
        state['purchase'] = start
        def submit(tx, label, sender, budget, calls):
            conf, ledger = (config, child) if sender == address else (None, live.ledger)
            def exact(reserved):
                data = tools.decode(tx)
                tools.verify_ptb_transaction(data, sender=sender, budget=reserved, calls=calls)
                signature = live.signer(conf, sender)(tx)
                return tools.bound(live.executor(conf)('execute-signed-tx', '--tx-bytes', tx, '--signatures', signature), data['digest'])
            r = live.execute(label, exact, gas=budget, sender=(conf, ledger), require_full=True); save(); return r
        observations = []
        for amount, expected in ([] if args.resume_ready else [('1000', 'partial'), ('9000', 'ready')]):
            message = ask(action='transfer', sender=AGENT, amount=amount)
            submit(message['tx'], 'SF-183 simulated purchase transfer '+amount, AGENT, 10_000_000, [])
            seen = ask(action='observe'); assert seen['state'] == expected; observations.append(seen); state['observations'] = observations; save()
        if child.remaining < 20_000_000:
            live.transaction('SF-183 bounded child gas top-up', '--split-coins', 'gas', '[10000000]', '--assign', 'extra', '--transfer-objects', '[extra.0]', '@'+address, funding=10_000_000, budget=5_000_000)
            child.limit += 10_000_000; state['child_funding_mist'] = child.limit; save()
        if args.resume_ready:
            observations.append(ask(action='observe'))
        tx = ask(action='prepay')['tx']
        opened = submit(tx, 'SF-183 frontend prepay after simulated arrival', address, 20_000_000, ['0x'+('2'.zfill(64))+'::coin::destroy_zero', package+'::subscription::open'])
        verified = ask(action='verify', digest=opened['digest'])
        account = verified['account']; state['account'] = account; save()
        # Refund the entire uncollected term to the isolated customer, then
        # return all test USDC and remaining SUI to the dedicated funder.
        live.transaction('SF-183 cancel and refund isolated term', '--move-call', package+'::subscription::cancel', '<'+usdc+'>', '@'+account['id'], '@'+customer, '@0x6', '--assign', 'refund', '--transfer-objects', '[refund]', '@'+AGENT, budget=10_000_000, sender=(config, child)); save()
        remaining = live.balance(address)
        if remaining > 2_000_000:
            live.transaction('SF-183 return remaining child gas', '--split-coins', 'gas', f'[{remaining-2_000_000}]', '--assign', 'return', '--transfer-objects', '[return.0]', '@'+AGENT, budget=2_000_000, sender=(config, child)); save()
        state['completed'] = True
        live.evidence.update(simulated_purchase=start, observations=observations, prepay=verified, child_transactions=child.evidence, child_used_mist=child.used, child_funding_mist=child.limit, child_remaining_mist=live.balance(address), limits=['Provider-independent Testnet transfer simulation, no real card/provider/wallet-extension acceptance.', 'Disposable 0.01 USDC test tariff; existing package and offer reused, no original customer/app objects modified.', 'Full uncollected subscription refunded to dedicated agent. Child funding is charged in full; gas/refunds do not replenish the 0.1 SUI ceiling.', 'Remaining child gas (if any) retained privately; no key material in evidence.'])
        live.finish(args.evidence)
        print('PASS: actual Testnet partial/complete USDC arrival, frontend prepay, typed PlanChanged confirmation, full refund')
    finally:
        save()
        if node:
            node.stdin.close()
            try: node.wait(timeout=5)
            except subprocess.TimeoutExpired: node.kill(); node.wait()


if __name__ == '__main__':
    main()
