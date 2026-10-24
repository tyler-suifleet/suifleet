#!/usr/bin/env python3
"""SF-129 dedicated-Testnet reclaim run against the existing SF-082 fleet.

Upgrades the SF-082 fleet_data package in place (bytecode verified before
signing), checks type origins and that existing fleet objects are unchanged,
then exercises the on-chain expiry guard and, for archives whose Walrus
storage has actually ended, prunes them and records the executed rebate.
Never runs on mainnet; the caller holds network:suifleet-testnet. The run
re-reads its cache on chain before reusing an upgrade; identical code is never
republished.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from live_common import Live, ROOT, plain, stage
from live_fleet_budget import SYSTEM, net_gas, record_publication, unrelocate_module
import deployment_manifest as dm
import sui_toolchain as tc

FLEET = json.loads((ROOT / 'docs/validation/sf082-testnet-native.json').read_text())
CORE = '0x34978350b5be61a60b51f45edd0b7f3f29e267399cc56a60e16aa6bfecb2bec2'
CORE_CAP = '0x400dda2c3b0788a08841c0a81a4bdc444c6e399268d60f352eb37f04066d0983'


def run(args):
    assert os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'), 'machine mutation lock required'
    os.umask(0o077)
    live = Live(args.wallet, 'testnet', args.max_spend_mist, build_sui=args.build_sui, network_sui=args.network_sui)
    # At most one faucet request, only when the balance is below the ceiling.
    live.preflight(auto_fund=args.auto_fund)
    data, packages = FLEET['fleet_data'], FLEET['packages']
    base, cap, storage_package = packages['data']['package'], packages['data']['cap'], packages['storage']
    state = Path(args.cache)
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache = json.loads(state.read_text()) if state.exists() else {}
    head_source = (ROOT / 'contracts/fleet_data/sources/fleet_data.move').read_bytes()
    try:
        # Authority: this wallet holds the data UpgradeCap and the fleet's budget cap.
        facts = dm.chain_facts(live.rpc_url, cap, CORE_CAP, data['fleetId'], data['writerId'])
        assert dm.object_id(live.owner(cap)) == live.operator
        cap_package, cap_version = dm.upgrade_cap(facts['objects'][dm.object_id(cap)])[:2]
        # The CLI lists owned objects as raw Move BCS: BudgetOwnerCap is (id, budget_id).
        budget_caps = []
        for item in live.client('objects', live.operator):
            move = (item.get('data') or {}).get('Move') or {}
            kind = (move.get('type_') or {}).get('Other') or {}
            if (kind.get('module'), kind.get('name')) == ('fleet_budget', 'BudgetOwnerCap') and dm.object_id('0x' + kind['address'].removeprefix('0x')) == dm.object_id(CORE):
                raw = bytes(move['contents'])
                if dm.object_id('0x' + raw[32:64].hex()) == dm.object_id(data['budgetId']):
                    budget_caps.append(dm.object_id('0x' + raw[:32].hex()))
        assert len(budget_caps) == 1, 'expected exactly one owner cap for the SF-082 fleet budget'
        budget_cap = budget_caps[0]
        live.check('Testnet chain/account, data UpgradeCap and fleet budget owner cap verified')
        before = {name: plain(live.object(data[name])['content']) for name in ('fleetId', 'writerId')}
        with tempfile.TemporaryDirectory(prefix='sf129-live-') as directory:
            tree = Path(directory) / 'contracts'
            stage(ROOT / 'contracts', tree)
            record_publication(tree, live.chain, CORE, CORE_CAP)
            record_publication(tree / 'fleet_storage', live.chain, storage_package['package'], storage_package['cap'])
            record_publication(tree / 'fleet_data', live.chain, base, cap)
            built = tc.compile_package(live.build_sui, tree / 'fleet_data')
            key = hashlib.sha256(head_source + base.encode() + live.tools['build']['version'].encode()).hexdigest()
            fixture = cache.setdefault(key, {})

            def same_modules(package):
                raw = live.client('object', package, '--bcs')['data']['Package']
                return sorted(unrelocate_module(unrelocate_module(bytes(v), base), package) for v in raw['module_map'].values()) == sorted(
                    base64.b64decode(v) for v in built['modules'])
            if 'head' not in fixture:
                assert (dm.object_id(cap_package), cap_version) == (dm.object_id(base), '1'), 'unexpected data package version'
                record = live.evidence.setdefault('verified_package_transactions', {}).setdefault('upgrade fleet_data', {})

                def submit(budget):
                    pinned = (tree / 'fleet_data/Move.lock').read_bytes()
                    unsigned = live.client('upgrade', tree / 'fleet_data', '--upgrade-capability', cap,
                                           '--gas-budget', str(budget), '--serialize-unsigned-transaction', json_output=False)
                    assert (tree / 'fleet_data/Move.lock').read_bytes() == pinned
                    tx = unsigned.stdout.strip().splitlines()[-1]
                    record.update(tc.verify_package_transaction(tc.decode(tx), built, sender=live.operator, budget=budget,
                                                                upgrade={'cap': cap, 'package': base}))
                    return live.executor()('serialized-tx', tx)
                result = live.execute('compatible fleet_data upgrade adding reclaim', submit, gas=args.upgrade_budget)
                fixture['head'] = live.published(result)
                state.write_text(json.dumps(cache, indent=2) + '\n')
            head = fixture['head']
            assert same_modules(head), 'deployed head bytecode differs from this source'
            assert dm.upgrade_cap(dm.chain_facts(live.rpc_url, cap)['objects'][dm.object_id(cap)])[:2] == (dm.object_id(head), '2')
            origins = dm.origins(dm.chain_facts(live.rpc_url, head)['objects'][dm.object_id(head)])
            for name in ('FleetData', 'DeviceData', 'AnchorKey', 'Anchor'):
                assert dm.object_id(origins['fleet_data::' + name]) == dm.object_id(base), name
            for name in ('AnchorsPruned', 'ExpiredBlobsBurned'):
                assert dm.object_id(origins['fleet_data::' + name]) == dm.object_id(head), name
            after = {name: plain(live.object(data[name])['content']) for name in ('fleetId', 'writerId')}
            assert after == before, 'the upgrade must not change existing fleet objects'
            live.check('in-place upgrade: verified bytecode, base origins kept, new event origins, existing objects unchanged')

            # Candidates come from the dApp planner, the epoch from the pinned Walrus SDK.
            config = {k: data[k] for k in ('schema', 'chainIdentifier', 'registryId', 'packageId', 'typeOrigin', 'fleetId', 'budgetId', 'storageId')}
            planned = json.loads(subprocess.run(['node', str(ROOT / 'scripts/reclaim_plan.cjs'), live.rpc_url, json.dumps(config),
                                                 storage_package['package'], data['deviceId']], check=True, capture_output=True, text=True).stdout)
            count = int(after['writerId']['anchor_count'])
            expired = [(c['index'], c['endEpoch']) for c in planned['anchors'] if dm.object_id(c['writerId']) == dm.object_id(data['writerId'])]
            unexpired_index = next((i for i in range(count) if i not in {e[0] for e in expired}), None)
            live.evidence.update(walrus_epoch=planned['epoch'], plan=planned)
            prune = lambda indices: ['--move-call', f'{head}::fleet_data::prune_expired_anchors', '@' + data['fleetId'], '@' + data['writerId'],
                                     '@' + data['budgetId'], '@' + budget_cap, '@' + data['storageId'], '@' + SYSTEM,
                                     'vector[' + ','.join(f'{i}u64' for i, _ in indices) + ']']
            if unexpired_index is not None:
                live.attack('prune of an unexpired archive refused on chain', *prune([(unexpired_index, None)]),
                            module='fleet_data', function='burn_expired', code=8)
                live.check(f'on-chain guard refused pruning unexpired archive index {unexpired_index} at Walrus epoch {planned["epoch"]}')
            if expired and args.reclaim:
                result = live.transaction('prune expired archives and burn their blobs', *prune(expired), budget=50_000_000)
                gas = result['effects']['gasUsed']
                live.evidence['reclaim'] = {'indices': [i for i, _ in expired], 'net_returned_mist': -net_gas(result), 'gas': gas}
                live.check(f'pruned {len(expired)} expired archive(s); {-net_gas(result)} MIST returned net of fees')
            live.evidence.update(fixture=dict(fixture, base=base, cap=cap, budget_cap=budget_cap), origins=origins,
                                 source_sha256=hashlib.sha256(head_source).hexdigest(), compiled_digest=bytes(built['digest']).hex())
    finally:
        live.finish(args.evidence)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallet', required=True)
    parser.add_argument('--cache', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--auto-fund', action='store_true', help='One faucet request if the balance is below the ceiling')
    parser.add_argument('--reclaim', action='store_true', help='Also prune archives whose storage has ended')
    parser.add_argument('--max-spend-mist', type=int, default=150_000_000)
    parser.add_argument('--upgrade-budget', type=int, default=120_000_000)
    parser.add_argument('--build-sui', default='sui')
    parser.add_argument('--network-sui', default=tc.NETWORK_DEFAULT)
    run(parser.parse_args())
