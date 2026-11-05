#!/usr/bin/env python3
"""Create one isolated customer using a child funded from the retained SF-182 ledger.

Never publishes, changes an existing customer, resets allowance or retries a
pending operation. Child gas is bounded by its already charged funding.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

from live_common import Live, ROOT
from live_subscription_checkout import AGENT
from testnet_budget import Budget
import deployment_manifest as dm


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wallet', required=True)
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--private-dir', type=Path, required=True)
    p.add_argument('--resume-failed-create', action='store_true', help='Reconcile one effects-confirmed out-of-gas create, then bounded top-up')
    p.add_argument('--resume-unfunded', action='store_true', help='Reconcile a fixture stopped before funding/signing only')
    args = p.parse_args()
    require(os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'), 'machine lease required')
    state = json.loads(args.state.read_text())
    require(state['limit_mist'] == 100_000_000, 'Run allowance must remain 100000000 MIST')
    prior = state.get('isolated_fixture')
    require(not prior or args.resume_unfunded or args.resume_failed_create, 'reconcile existing isolated fixture; no replay')
    if prior and not args.resume_failed_create:
        require(not prior.get('pending_digest') and not prior['transactions'], 'Unfunded fixture has a pending or recorded transaction')
        require(len(state['transactions']) == 3, 'funding already attempted; reconcile instead')
    require(not state.get('pending_digest') or state['pending_digest'] in [t['digest'] for t in state['transactions']], 'Unknown pending parent transaction; reconcile first')
    live = Live(args.wallet, 'testnet', state['limit_mist'])
    live.preflight()
    require(live.chain == '4c78adac' and live.operator == AGENT, 'Dedicated testnet chain/account mismatch')
    m = json.loads(args.manifest.read_text()); dm.verify(m)
    require(m['chain_identifier'] == live.chain, 'Manifest chain mismatch')
    require(dm.object_id(live.owner(m['upgrade_cap_id'])) == AGENT, 'UpgradeCap owner mismatch')
    registry = m['shared_objects']['registry']['id']; package = m['call_package_id']
    require(dm.object_id(live.object(registry)['content']['admin']) == AGENT, 'Registry administrator mismatch')
    require(m['treasury_payee'] == json.loads((ROOT / 'deployments/testnet/treasury.json').read_text())['treasury_payee'], 'Pinned treasury mismatch')
    for t in state['transactions']:
        require(t['status'] == 'success', 'Prior parent transaction did not succeed')
        gas = live.executed(t['digest'])['effects']['gasUsed']
        require(t['gas_mist'] == max(0, int(gas['computationCost']) + int(gas['storageCost']) - int(gas['storageRebate'])), 'Recorded parent gas differs from executed effects')
    require(state['allowance_used_mist'] == sum(t['gas_mist'] + t.get('funding_mist', 0) for t in state['transactions']), 'Retained parent allowance does not reconcile')
    live.evidence['transactions'] = state['transactions']; live.ledger.evidence = state['transactions']
    live.ledger.used = state['allowance_used_mist']
    if prior:
        config = args.private_dir / "client.yaml"
        address = dm.object_id(live.client("active-address", json_output=False, config=config).stdout.strip().split()[-1])
        require(address == prior["address"], 'Child wallet address mismatch')
        if not args.resume_failed_create:
            require(live.balance(address) == 0, 'Unfunded child already has a balance')
        else:
            require(not prior.get('recovery_started') and not prior.get('customer'), 'Failed-create recovery already started or customer already recorded')
            require(len(prior['transactions']) == 1, 'Expected exactly one failed create')
            t = prior['transactions'][0]
            require(t['status'] == 'failure' and t['label'] == 'SF-182 isolated customer creation', 'Unexpected failed-create transaction')
            r = live.executed(t['digest']); gas = r['effects']['gasUsed']
            require(r['effects']['status'] == {'status': 'failure', 'error': 'InsufficientGas'}, 'Recovery requires effects-confirmed InsufficientGas')
            require(t['gas_mist'] == max(0, int(gas['computationCost']) + int(gas['storageCost']) - int(gas['storageRebate'])), 'Recorded child gas differs from executed effects')
            require(not live.created(r, '::customer::Customer'), 'Failed create unexpectedly produced a customer')
            require(prior['pending_digest'] == t['digest'], 'Child pending digest mismatch')
            require(prior['allowance_used_mist'] == t['gas_mist'], 'Child allowance does not reconcile')
            require(live.balance(address) == prior['funding_mist'] - t['gas_mist'], 'Child balance does not reconcile')
        fixture = prior
    else:
        args.private_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        address = subprocess.check_output(['node', '-e', '''
    const fs=require('fs'); const {appRequire}=require('./scripts/frontend_test_utils.cjs');
    const {Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519');
    const {decodeSuiPrivateKey}=appRequire('@mysten/sui/cryptography');
    const kp=new Ed25519Keypair(), seed=decodeSuiPrivateKey(kp.getSecretKey()).secretKey;
    fs.writeFileSync(process.argv[1]+'/sui.keystore',JSON.stringify([Buffer.concat([Buffer.from([0]),Buffer.from(seed)]).toString('base64')]),{mode:0o600});
    console.log(kp.toSuiAddress());''', str(args.private_dir)], cwd=ROOT, text=True).strip()
        config = args.private_dir / 'client.yaml'
        config.write_text(f'keystore:\n  File: {args.private_dir}/sui.keystore\nenvs:\n- alias: testnet\n  rpc: {live.rpc_url}\n  ws: null\n  basic_auth: null\nactive_env: testnet\nactive_address: "{address}"\n'); config.chmod(0o600)
        fixture = state['isolated_fixture'] = dict(address=address, funding_mist=15_000_000, transactions=[])
    child = Budget(fixture['funding_mist'], fixture['transactions'])
    child.used = fixture.get('allowance_used_mist', 0)
    def save():
        state['allowance_used_mist'] = live.ledger.used
        fixture['allowance_used_mist'] = child.used
        args.state.write_text(json.dumps(state, indent=2)+'\n'); args.state.chmod(0o600)
    original = live.signer
    def signer(config, sender):
        sign = original(config, sender)
        def durable(tx):
            live.assert_lock(); fixture['pending_digest'] = live.decode(tx)['digest']; save()
            return sign(tx)
        return durable
    live.signer = signer
    save()
    try:
        amount = 20_000_000 if args.resume_failed_create else 15_000_000
        if args.resume_failed_create:
            fixture['recovery_started'] = True
            save()
        live.transaction('SF-182 fund isolated fixture identity', '--split-coins', 'gas', f'[{amount}]', '--assign', 'funds', '--transfer-objects', '[funds.0]', '@'+address, funding=amount, budget=10_000_000)
        if args.resume_failed_create:
            fixture['funding_mist'] += amount
            child.limit += amount
        save()
        r = live.transaction('SF-182 isolated customer creation', '--move-call', f'{package}::customer::create', '@'+registry, '"SF-182 isolated checkout"', budget=23_000_000 if args.resume_failed_create else 10_000_000, sender=(config, child))
        fixture['customer'] = live.created(r, '::customer::Customer')[0]; save()
        live.transaction('SF-182 grant dedicated agent isolated customer administration', '--move-call', f'{package}::customer::set_credential', '@'+fixture['customer'], '@'+AGENT, 'true', 'true', budget=5_000_000, sender=(config, child))
        save()
        fixture['ready'] = True
        print(json.dumps(dict(customer=fixture['customer'], address=address, allowance_used_mist=live.ledger.used)))
    finally:
        save()

if __name__ == '__main__':
    main()
