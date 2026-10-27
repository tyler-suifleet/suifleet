#!/usr/bin/env python3
"""SF-171 dedicated-wallet testnet batch membership.

Publishes the checked-out contracts once as an ephemeral package (cached by
contracts tree, re-verified before reuse), then: a customer fleet with four
devices, a signed on-chain architecture refusal, one bounded batch adding
the devices pinned to their reviewed architectures, one batch removal and
cleanup of the group and devices. No mainnet, faucet or hardware.
"""
import argparse
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

import deployment_manifest as dm
from live_common import Live, ROOT, plain, stage


def run(args):
    if not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
        raise RuntimeError('machine-session network:suifleet-testnet lock is required')
    os.umask(0o077)
    live = Live(args.wallet, 'testnet', args.max_spend_mist, build_sui=args.build_sui, network_sui=args.network_sui)
    # At most one faucet request, only when the wallet holds less than the ceiling.
    live.preflight(auto_fund=args.auto_fund)
    state_path = Path(args.state)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    tree = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD:contracts'], text=True).strip()
    live.evidence.update(contracts_tree=tree)
    try:
        cached = state.get('publication') if state.get('contracts_tree') == tree and state.get('chain') == live.chain else None
        if cached:
            # Free reads before reuse: the package, registry and our UpgradeCap still exist.
            facts = dm.chain_facts(live.rpc_url, cached['package'], cached['upgrade_cap'], cached['registry'])
            assert dm.upgrade_cap(facts['objects'][dm.object_id(cached['upgrade_cap'])])[0] == dm.object_id(cached['package'])
            assert live.owner(cached['upgrade_cap']) == live.operator, 'cached UpgradeCap is not owned by the dedicated wallet'
            package, registry = cached['package'], cached['registry']
            live.check('reused verified cached publication of identical contracts')
        else:
            work = Path(tempfile.mkdtemp(prefix='sf171-publish-'))
            stage(ROOT / 'contracts', work / 'contracts')
            result = live.publish('publish head contracts', work / 'contracts', work / 'Pub.testnet.toml')
            package = live.published(result)
            registry = live.created(result, '::device_registry::DeviceRegistry')[0]
            upgrade_cap = live.created(result, '::package::UpgradeCap')[0]
            state = {'contracts_tree': tree, 'chain': live.chain,
                     'publication': {'package': package, 'registry': registry, 'upgrade_cap': upgrade_cap}}
            state_path.write_text(json.dumps(state, indent=2) + '\n')
        facts = dm.chain_facts(live.rpc_url, package)
        origins = dm.origins(facts['objects'][dm.object_id(package)])
        assert origins.get('customer::GroupBatchApplied') == dm.object_id(package), 'package lacks the batch entry points'
        live.evidence.update(package=package, registry=registry, group_batch_origin=origins['customer::GroupBatchApplied'])
        call = lambda fn: f'{package}::customer::{fn}'

        if 'customer' not in state:
            r = live.transaction('create customer and default fleet', '--move-call', call('create'), f'@{registry}', '"SF-171 batch"', budget=60_000_000)
            state['customer'] = {'customer': live.created(r, '::customer::Customer')[0], 'fleet': live.created(r, '::customer::Fleet')[0],
                                 'budget': live.created(r, '::fleet_budget::FleetBudget')[0]}
            state_path.write_text(json.dumps(state, indent=2) + '\n')
        customer, fleet, budget = (state['customer'][k] for k in ('customer', 'fleet', 'budget'))
        # Entitlement periods only advance; each run starts a fresh one.
        state['period'] = state.get('period', 0) + 1
        state_path.write_text(json.dumps(state, indent=2) + '\n')
        live.transaction('entitle four devices', '--move-call', call('set_entitlement'), f'@{customer}', f'@{registry}', '4', '10000000',
                         str(state['period']), budget=10_000_000)
        archs = ['x86_64', 'x86_64', 'x86_64', 'aarch64']
        register = []
        for index, arch in enumerate(archs):
            register += ['--move-call', call('register_device'), f'@{customer}', f'@{fleet}', f'@{budget}', f'@{registry}',
                         f'"sf171-{index}"', f'@0x{secrets.token_hex(32)}', f'"{arch}"', '@0x6']
        r = live.transaction('register four fleet devices', *register, budget=200_000_000)
        devices = live.created(r, '::device_registry::Device')
        accounts = live.created(r, '::fleet_budget::GasAccount')
        assert len(devices) == 4 and len(accounts) == 4
        by_arch = {}
        for d in devices:
            by_arch.setdefault(plain(live.object(d)['content'])['arch'], []).append(d)
        assert sorted(len(v) for v in by_arch.values()) == [1, 3]
        x86, arm = by_arch['x86_64'], by_arch['aarch64']
        r = live.transaction('create managed group', '--move-call', call('create_group'), f'@{customer}', f'@{fleet}', '"SF-171 canary"', '""', '@0x6', budget=20_000_000)
        group = live.created(r, '::device_group::DeviceGroup')[0]
        live.evidence.update(customer=customer, fleet=fleet, group=group, devices={'x86_64': x86, 'aarch64': arm})
        vec = lambda ids: 'vector[' + ','.join(f'@{i}' for i in ids) + ']'
        live.attack('architecture mismatch refuses the whole batch', '--move-call', call('add_many_to_group'), f'@{customer}', f'@{fleet}', f'@{group}',
                    f'@{registry}', vec(x86[:2] + arm), 'some("x86_64")', module='customer', function='add_many_to_group', code=8, budget=20_000_000)
        r = live.transaction('batch add four devices pinned to reviewed architectures',
                             '--move-call', call('add_many_to_group'), f'@{customer}', f'@{fleet}', f'@{group}', f'@{registry}', vec(x86), 'some("x86_64")',
                             '--move-call', call('add_many_to_group'), f'@{customer}', f'@{fleet}', f'@{group}', f'@{registry}', vec(arm), 'some("aarch64")',
                             budget=40_000_000)
        events = [e for e in r.get('events', []) if e['type'].endswith('::customer::GroupBatchApplied')]
        assert sorted(int(e['parsedJson']['count']) for e in events) == [1, 3], events
        members = [dm.object_id(m) for m in plain(live.object(group)['content'])['device_ids']]
        assert sorted(members) == sorted(dm.object_id(d) for d in devices)
        live.check('one transaction added four devices (3 x86_64 + 1 aarch64) with on-chain architecture checks')
        r = live.transaction('batch remove four devices', '--move-call', call('remove_many_from_group'), f'@{customer}', f'@{fleet}', f'@{group}',
                             vec(devices), budget=40_000_000)
        assert plain(live.object(group)['content'])['device_ids'] == []
        live.check('one transaction removed all four members')
        account_of = {dm.object_id(plain(live.object(a)['content'])['device_id']): a for a in accounts}
        cleanup = ['--move-call', call('delete_group'), f'@{customer}', f'@{fleet}', f'@{group}']
        for d in devices:
            cleanup += ['--move-call', call('deregister_device'), f'@{customer}', f'@{fleet}', f'@{budget}', f'@{account_of[dm.object_id(d)]}', f'@{registry}', f'@{d}']
        live.transaction('delete group and deregister devices', *cleanup, budget=60_000_000)
        live.check('emptied group deleted and formerly grouped devices deregistered')
        live.evidence['limits'] = [
            'Calls are the CLI equivalents of the dApp builder output; the builder itself ran against a real upgraded package on an isolated localnet.',
            'The ephemeral package, customer and fleet remain (package storage is not refundable); group and devices were deleted.',
            'No browser wallet, mainnet, hardware or production provider acceptance.']
        live.evidence['success'] = True
    finally:
        live.finish(args.evidence)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallet', required=True)
    parser.add_argument('--state', required=True, help='private cache of the ephemeral publication')
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--max-spend-mist', type=int, default=800_000_000)
    parser.add_argument('--auto-fund', action='store_true')
    parser.add_argument('--build-sui', default='sui')
    parser.add_argument('--network-sui', default='/opt/sui-network/sui')
    run(parser.parse_args())
