#!/usr/bin/env python3
"""Effects-checked SF201 native quorum fixture; strictly dedicated testnet/localnet.
Private ephemeral multisig keys live outside the checkout. No real wallet claims.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from live_common import Live, CLOCK
from live_package_upgrade import contracts_digest
import deployment_manifest as dm
import sui_toolchain as tc

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    if args.network == 'testnet' and not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
        raise RuntimeError('network:suifleet-testnet lock required')
    os.umask(0o077)
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.state.resolve().is_relative_to(ROOT) or args.state.stat().st_mode & 0o077:
        raise RuntimeError('private state must be outside checkout with mode 0700')
    utility = ROOT / 'scripts/testnet_treasury.cjs'
    secrets = args.state / 'keys.json'
    descriptor = json.loads(subprocess.check_output(['node', str(utility), 'create', str(secrets)], text=True))
    address = descriptor['treasury_payee']
    # Public keys only are passed to the Move verifier.
    import base64
    keys = 'vector[' + ','.join('vector[' + ','.join(str(b)+'u8' for b in base64.b64decode(k['public_key'])) + ']' for k in descriptor['public_keys']) + ']'
    weights = 'vector[1u8,1u8,1u8]'
    live = Live(args.wallet, args.network, args.max_spend_mist)
    live.evidence.update(multisig=descriptor, success=False)
    cache = args.state / 'fixture.json'
    state = json.loads(cache.read_text()) if cache.exists() else {}
    def save(): cache.write_text(json.dumps(state, indent=2)+'\n')
    if state.get('run_started') or state.get('completed'):
        raise RuntimeError('fixture already started/completed; reconcile recorded effects rather than replaying setup or funding')
    try:
        live.preflight(auto_fund=args.auto_fund)
        digest = contracts_digest(Path('contracts'))
        identity = [live.chain, live.operator, digest]
        if state.get('pending'): raise RuntimeError('uncertain fixture mutation; reconcile recorded digest before retry')
        fixture = state.get('fixture')
        if fixture:
            assert fixture['identity'] == identity, 'cache code/chain/account mismatch; do not republish identical code'
            package, registry = fixture['package'], fixture['registry']
            cap = live.object(fixture['cap'])
            assert live.owner(fixture['cap']) == live.operator
            assert dm.object_id(cap['content']['package']) == package
            facts = dm.chain_facts(live.rpc_url, package)
            assert dm.origins(facts['objects'][package]) == fixture['origins']
        else:
            state['pending'] = 'publish'; save()
            def checkpoint(data): state['pending'] = data; save()
            r = live.publish('publish isolated multisig fixture', Path('contracts'), args.state/'published.toml', persist=checkpoint)
            package = live.published(r); registry = live.created(r, '::device_registry::DeviceRegistry')[0]
            cap = live.created(r, '::package::UpgradeCap')[0]
            facts = dm.chain_facts(live.rpc_url, package)
            fixture = dict(identity=identity, package=package, registry=registry, cap=cap, origins=dm.origins(facts['objects'][package]))
            state['fixture'] = fixture; del state['pending']; save()
        descriptor['network'], descriptor['chain_identifier'] = args.network, live.chain
        live.evidence['fixture'] = fixture
        state['run_started'] = True; save()
        assert fixture['origins']['customer::Customer'] == package
        assert fixture['origins']['security_policy::Action'] == package
        assert live.owner(fixture['cap']) == live.operator and live.owner(registry) == 'shared'
        r = live.transaction('create disposable organization', '--move-call', f'{package}::customer::create', f'@{registry}', '"SF201 native quorum"', budget=35_000_000)
        c = live.created(r, '::customer::Customer')[0]
        live.transaction('initialize policy and register quorum credential', '--move-call', f'{package}::customer::initialize_security', f'@{c}',
                         '--move-call', f'{package}::customer::set_credential', f'@{c}', f'@{address}', 'true', 'true', budget=20_000_000)
        funding = 80_000_000
        live.transaction('fund disposable quorum gas', '--split-coins', 'gas', f'[{funding}]', '--assign', 'fund', '--transfer-objects', '[fund]', f'@{address}', budget=5_000_000, funding=funding)
        child = live.child(args.wallet, funding)
        original = live.signer
        def signer(config, sender):
            if tc.normal(sender) != address: return original(config, sender)
            def sign(tx_bytes):
                decoded = live.decode(tx_bytes)
                if tc.normal(decoded['sender']) != address or tc.normal(decoded['gas_owner']) != address:
                    raise tc.NotSubmitted('quorum sender/gas owner mismatch')
                signed = subprocess.run(['node', str(utility), 'sign', str(secrets)], input=tx_bytes, text=True, capture_output=True, timeout=60)
                if signed.returncode: raise RuntimeError('quorum signature failed; private output suppressed')
                return signed.stdout.strip()
            return sign
        live.signer = signer
        def quorum(label, *ptb):
            return live.transaction(label, '--sender', f'@{address}', *ptb, sender=child, budget=15_000_000)
        quorum('native 2-of-3 enrolls authenticated configuration', '--move-call', f'{package}::multisig_admin::enroll', f'@{c}', keys, weights, '2')
        on, off = 'vector[1u8,1u8,1u8,1u8,1u8,1u8,1u8]', 'vector[0u8,0u8,0u8,0u8,0u8,0u8,0u8]'
        quorum('activate optional organization policy', '--move-call', f'{package}::customer::set_security', f'@{c}', on)
        live.attack('ordinary administrator refused policy downgrade', '--move-call', f'{package}::customer::set_security', f'@{c}', off,
                    module='security_policy', function='check', code=1, budget=5_000_000)
        expiry = str(int(time.time()*1000)+3600000)
        r = quorum('quorum prepares exact recovery intent', '--move-call', f'{package}::multisig_admin::prepare_policy_change', f'@{c}', off, expiry, CLOCK)
        a = live.created(r, '::security_policy::Action')[0]
        live.attack('ordinary administrator cannot manufacture quorum approval', '--move-call', f'{package}::multisig_admin::approve', f'@{c}', f'@{a}', keys, weights, '2', CLOCK,
                    module='multisig_admin', function='approve', code=1, budget=5_000_000)
        quorum('surviving quorum approves and executes recovery', '--move-call', f'{package}::multisig_admin::approve', f'@{c}', f'@{a}', keys, weights, '2', CLOCK,
               '--move-call', f'{package}::customer::set_security_secured', f'@{c}', off, f'@{a}', CLOCK)
        live.transaction('recovered ordinary administrator can update credential', '--move-call', f'{package}::customer::set_credential', f'@{c}', f'@{address}', 'true', 'false', budget=10_000_000)
        live.check('native quorum enrollment, active policy, direct refusal and exact surviving-quorum recovery')
        state['completed'] = True; del state['run_started']; save()
        live.evidence.update(success=True, customer=c, action=a, limits=['Software keys only; no manual Slush/extension/hardware acceptance, mainnet or production operations.', 'Fresh disposable package; isolated existing-object/old-package upgrade evidence is separate.'])
    finally:
        live.finish(args.evidence)


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wallet', required=True); p.add_argument('--network', choices=['testnet','localnet'], required=True)
    p.add_argument('--state', type=Path, required=True); p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--max-spend-mist', type=int, default=1_050_000_000); p.add_argument('--auto-fund', action='store_true')
    run(p.parse_args())
