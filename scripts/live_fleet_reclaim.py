#!/usr/bin/env python3
"""SF-129 dedicated-Testnet reclaim run against the existing SF-082 fleet.

Upgrades the SF-082 fleet_data package in place from the latest version its
UpgradeCap names (bytecode verified before signing), checks that every type
keeps the origin of the version that introduced it and that existing fleet
objects are unchanged, then exercises the on-chain expiry guard and, for
archives whose Walrus storage has actually ended, prunes them and records the
executed rebate.
Never runs on mainnet; the caller holds network:suifleet-testnet. The
UpgradeCap, not the cache, decides reuse: a latest version whose bytecode
matches this source is reused, so identical code is never republished.
`--source-revision` builds the contracts of a committed revision instead of
the working tree, so a version deployed from that revision is verified
byte for byte and reused when the working tree no longer links against the
fixture's published dependencies.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

from live_common import Live, ROOT, plain, stage
from live_fleet_budget import SYSTEM, net_gas, record_publication, unrelocate_module
import deployment_manifest as dm
import sui_toolchain as tc

FLEET = json.loads((ROOT / 'docs/validation/sf082-testnet-native.json').read_text())
CORE = '0x34978350b5be61a60b51f45edd0b7f3f29e267399cc56a60e16aa6bfecb2bec2'
CORE_CAP = '0x400dda2c3b0788a08841c0a81a4bdc444c6e399268d60f352eb37f04066d0983'
RECLAIM_EVENTS = ('fleet_data::AnchorsPruned', 'fleet_data::ExpiredBlobsBurned')


def fixture_step(base, cap_package, cap_version, matches):
    """('reuse', latest) when the cap's latest version already carries this
    source's bytecode, otherwise ('upgrade', latest): upgrade from the version
    the cap actually names, whatever earlier source produced it."""
    latest = dm.object_id(cap_package)
    if not cap_version.isdigit() or int(cap_version) < 1 or (latest == dm.object_id(base)) != (cap_version == '1'):
        raise AssertionError(f'inconsistent data UpgradeCap: {latest} at version {cap_version}')
    return ('reuse' if matches(latest) else 'upgrade'), latest


def check_origins(head, base, head_origins, base_origins, introduced, previous=None):
    """Base types keep the base origin; every later type keeps the package
    that introduced it, which names itself as that type's origin. After an
    upgrade, all types of the previous version keep their origins and only
    the new version's new types originate in it. Returns head_origins."""
    head, base = dm.object_id(head), dm.object_id(base)
    for name, origin in base_origins.items():
        assert dm.object_id(origin) == base and dm.object_id(head_origins.get(name, '0x0')) == base, f'{name} lost its base origin'
    if previous is not None:
        for name, origin in previous.items():
            assert dm.object_id(head_origins.get(name, '0x0')) == dm.object_id(origin), f'{name} changed origin in the upgrade'
    for name in RECLAIM_EVENTS:
        assert name in head_origins and dm.object_id(head_origins[name]) != base, f'{name} is missing or has the base origin'
    for name, origin in head_origins.items():
        origin = dm.object_id(origin)
        if name in base_origins:
            continue
        if previous is not None and name not in previous:
            assert origin == head, f'{name} is new in this upgrade but originates in {origin}'
        assert origin == head or dm.object_id(introduced(origin).get(name, '0x0')) == origin, f'{name} origin {origin} did not introduce it'
    return head_origins


def contracts_at(revision, directory):
    """The committed contracts tree of `revision`, extracted under `directory`."""
    archive = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', revision, 'contracts'],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(directory, filter='data')
    return Path(directory) / 'contracts'


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
    source_dir = tempfile.TemporaryDirectory(prefix='sf129-source-')
    contracts = contracts_at(args.source_revision, source_dir.name) if args.source_revision else ROOT / 'contracts'
    head_source = (contracts / 'fleet_data/sources/fleet_data.move').read_bytes()
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
            stage(contracts, tree)
            record_publication(tree, live.chain, CORE, CORE_CAP)
            record_publication(tree / 'fleet_storage', live.chain, storage_package['package'], storage_package['cap'])
            record_publication(tree / 'fleet_data', live.chain, base, cap, published_at=cap_package, version=cap_version)
            built = tc.compile_package(live.build_sui, tree / 'fleet_data')
            key = hashlib.sha256(head_source + base.encode() + live.tools['build']['version'].encode()).hexdigest()
            fixture = cache.setdefault(key, {})
            package_origins = lambda package: dm.origins(dm.chain_facts(live.rpc_url, package)['objects'][dm.object_id(package)])

            def same_modules(package):
                raw = live.client('object', package, '--bcs')['data']['Package']
                return sorted(unrelocate_module(unrelocate_module(bytes(v), base), package) for v in raw['module_map'].values()) == sorted(
                    base64.b64decode(v) for v in built['modules'])
            step, latest = fixture_step(base, cap_package, cap_version, same_modules)
            previous = None
            if step == 'reuse':
                head, version = latest, cap_version
                live.check(f'data package version {version} {head} already carries this source; not republished')
            else:
                previous = package_origins(latest)
                record = live.evidence.setdefault('verified_package_transactions', {}).setdefault('upgrade fleet_data', {})

                def submit(budget):
                    pinned = (tree / 'fleet_data/Move.lock').read_bytes()
                    unsigned = live.client('upgrade', tree / 'fleet_data', '--upgrade-capability', cap,
                                           '--gas-budget', str(budget), '--serialize-unsigned-transaction', json_output=False)
                    assert (tree / 'fleet_data/Move.lock').read_bytes() == pinned
                    tx = unsigned.stdout.strip().splitlines()[-1]
                    record.update(tc.verify_package_transaction(tc.decode(tx), built, sender=live.operator, budget=budget,
                                                                upgrade={'cap': cap, 'package': latest}))
                    return live.executor()('serialized-tx', tx)
                result = live.execute(f'compatible fleet_data upgrade from version {cap_version}', submit, gas=args.upgrade_budget)
                head, version = live.published(result), str(int(cap_version) + 1)
                fixture.update(head=head, upgraded_from=latest, version=version)
                state.write_text(json.dumps(cache, indent=2) + '\n')
            assert same_modules(head), 'deployed head bytecode differs from this source'
            assert dm.upgrade_cap(dm.chain_facts(live.rpc_url, cap)['objects'][dm.object_id(cap)])[:2] == (dm.object_id(head), version)
            origins = check_origins(head, base, package_origins(head), package_origins(base), package_origins, previous)
            after = {name: plain(live.object(data[name])['content']) for name in ('fleetId', 'writerId')}
            assert after == before, 'the upgrade must not change existing fleet objects'
            live.check('in-place upgrade: verified bytecode, introduction origins kept, existing objects unchanged')

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
            live.evidence.update(fixture=dict(fixture, head=head, version=version, base=base, cap=cap, budget_cap=budget_cap), origins=origins,
                                 source_sha256=hashlib.sha256(head_source).hexdigest(), compiled_digest=bytes(built['digest']).hex(),
                                 source_revision=args.source_revision or 'working tree')
    finally:
        source_dir.cleanup()
        live.finish(args.evidence)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallet', required=True)
    parser.add_argument('--cache', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--auto-fund', action='store_true', help='One faucet request if the balance is below the ceiling')
    parser.add_argument('--reclaim', action='store_true', help='Also prune archives whose storage has ended')
    parser.add_argument('--source-revision', help='Build contracts from this committed revision instead of the working tree')
    parser.add_argument('--max-spend-mist', type=int, default=150_000_000)
    parser.add_argument('--upgrade-budget', type=int, default=120_000_000)
    parser.add_argument('--build-sui', default='sui')
    parser.add_argument('--network-sui', default=tc.NETWORK_DEFAULT)
    run(parser.parse_args())
