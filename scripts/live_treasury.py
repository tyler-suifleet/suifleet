#!/usr/bin/env python3
"""Bounded dedicated-wallet testnet 2-of-3 fixture; no Move publication.
Private disposable keys persist outside the repository for later payment tests.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

from live_common import Live
import sui_toolchain as toolchain

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    if not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
        raise RuntimeError('network:suifleet-testnet lock required')
    os.umask(0o077)
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.state.resolve().is_relative_to(ROOT):
        raise RuntimeError('private disposable key state must be outside checkout')
    if args.state.stat().st_mode & 0o077:
        raise RuntimeError('private state directory permissions must be 0700')
    state = args.state / 'testnet-only-keys.json'
    utility = ROOT / 'scripts/testnet_treasury.cjs'
    descriptor = json.loads(subprocess.check_output(['node', str(utility), 'create', str(state)], text=True))
    pinned = json.loads((ROOT / 'deployments/testnet/treasury.json').read_text())
    if any(pinned.get(key) != value for key, value in descriptor.items()):
        raise RuntimeError('disposable keys differ from reviewed pinned testnet descriptor; no funding permitted')
    address = descriptor['treasury_payee']
    live = Live(args.wallet, 'testnet', 30_000_000, build_sui=args.build_sui, network_sui=args.network_sui)
    live.evidence.update(descriptor)
    try:
        live.preflight()
        derived = subprocess.run([live.network_sui, 'keytool', 'multi-sig-address', '--pks',
            *[key['public_key'] for key in descriptor['public_keys']], '--weights', '1', '1', '1',
            '--threshold', '2', '--json'], capture_output=True, text=True, check=True, timeout=60)
        if json.loads(derived.stdout)['multisigAddress'] != address:
            raise RuntimeError('SDK/CLI multisig address mismatch; no funding permitted')
        live.evidence['independent_cli_address_verified'] = True
        funding = 10_000_000
        live.transaction('fund disposable 2-of-3 testnet treasury', '--split-coins', 'gas', f'[{funding}]', '--assign', 'fund',
                         '--transfer-objects', '[fund]', f'@{address}', budget=5_000_000, funding=funding)
        # Use the common exact-byte executor and independent CLI decoding/effects.
        original = live.signer
        def signer(config, sender):
            if toolchain.normal(sender) != address:
                return original(config, sender)
            def sign(tx_bytes):
                decoded = live.decode(tx_bytes)
                if toolchain.normal(decoded['sender']) != address or toolchain.normal(decoded['gas_owner']) != address:
                    raise toolchain.NotSubmitted('multisig sender/gas owner mismatch')
                result = subprocess.run(['node', str(utility), 'sign', str(state)], input=tx_bytes,
                                        text=True, capture_output=True, timeout=60)
                if result.returncode:
                    raise RuntimeError('testnet multisig signing failed; private output suppressed')
                return result.stdout.strip()
            return sign
        live.signer = signer
        submit = live.exact_ptb(['--sender', f'@{address}', '--split-coins', 'gas', '[1]', '--assign', 'spend',
                                 '--transfer-objects', '[spend]', f'@{live.operator}'], args.wallet)
        live.execute('2-of-3 multisig signed spend to dedicated agent', submit, gas=5_000_000,
                     sender=live.child(args.wallet, funding), require_full=True)
        live.evidence.update(descriptor, result='success', limits=[
            'Disposable software testnet keys only; not production hardware custody.',
            'No mainnet, SuiNS, production-provider or physical operations.'])
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(live.evidence, indent=2) + '\n')
    print('PASS: funded and spent from disposable testnet 2-of-3 multisig; effects in', args.output)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wallet', type=Path, required=True)
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--build-sui', default='sui')
    p.add_argument('--network-sui', default='/opt/sui-network/sui')
    run(p.parse_args())
