#!/usr/bin/env python3
"""SF-128 dedicated-testnet v2->v3 storage upgrade; no Walrus or customer writes.

Cache a verified (base, head, core) package pair and a disposable empty storage.
Reuse packages only after on-chain bytecode, UpgradeCap and origins checks.
Publication/upgrade bytes are verified before signing; every effect is checked.
The caller must hold network:suifleet-testnet. Never runs on mainnet.
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
from live_fleet_budget import publish_storage, record_publication, unrelocate_module
import deployment_manifest as dm
import sui_toolchain as tc


def run(args):
    assert os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'), 'machine mutation lock required'
    os.umask(0o077)
    live = Live(args.wallet, 'testnet', args.max_spend_mist,
                build_sui=args.build_sui, network_sui=args.network_sui)
    live.preflight(auto_fund=False)
    state = Path(args.cache)
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache = json.loads(state.read_text()) if state.exists() else {}
    base_source = subprocess.check_output(['git', 'show',
        args.base + ':contracts/fleet_storage/sources/fleet_storage.move'], cwd=ROOT)
    head_source = (ROOT / 'contracts/fleet_storage/sources/fleet_storage.move').read_bytes()
    assert b'const VERSION: u64 = 2;' in base_source
    assert b'const VERSION: u64 = 3;' in head_source
    with tempfile.TemporaryDirectory(prefix='sf128-upgrade-') as directory:
        core = Path(directory) / 'contracts'
        stage(ROOT / 'contracts', core)
        storage = core / 'fleet_storage'
        source = storage / 'sources/fleet_storage.move'
        core_build = tc.compile_package(live.build_sui, core)
        key = hashlib.sha256(base_source + head_source + json.dumps(core_build, sort_keys=True).encode()
                             + live.tools['build']['version'].encode()).hexdigest()
        fixture = cache.setdefault(key, {})
        def save():
            state.write_text(json.dumps(cache, indent=2) + '\n')
        def same_modules(package, built, original=None):
            raw = live.client('object', package, '--bcs')['data']['Package']
            return sorted(unrelocate_module(unrelocate_module(bytes(v), original or package), package) for v in raw['module_map'].values()) == sorted(
                base64.b64decode(v) for v in built['modules'])
        def owned_cap(cap, package, version):
            assert dm.object_id(live.owner(cap)) == live.operator
            facts = dm.chain_facts(live.rpc_url, cap)
            assert dm.upgrade_cap(facts['objects'][dm.object_id(cap)])[:2] == (dm.object_id(package), str(version))
        try:
            if 'core' not in fixture:
                # Reuse identical immutable core packages across storage pairs.
                for prior in cache.values():
                    if prior.get('core') and same_modules(prior['core'], core_build):
                        fixture.update(core=prior['core'], core_cap=prior['core_cap'], registry=prior['registry'])
                        break
                else:
                    candidates = json.loads(Path(args.core_fixtures).read_text()).get('fixtures', {}) if args.core_fixtures else {}
                    for prior in candidates.values():
                        if prior.get('package') and same_modules(prior['package'], core_build):
                            fixture.update(core=prior['package'], core_cap=prior['upgrade_cap'], registry=prior['registry'])
                            break
                if 'core' not in fixture:
                    result = live.publish('publish isolated core fixture', core, Path(directory) / 'Pub.toml')
                    fixture.update(core=live.published(result),
                                   core_cap=live.created(result, '::package::UpgradeCap')[0],
                                   registry=live.created(result, '::device_registry::DeviceRegistry')[0])
                save()
            assert same_modules(fixture['core'], core_build)
            owned_cap(fixture['core_cap'], fixture['core'], 1)
            record_publication(core, live.chain, fixture['core'], fixture['core_cap'])
            source.write_bytes(base_source)
            base_build = tc.compile_package(live.build_sui, storage)
            if 'base' not in fixture:
                result = publish_storage(live, storage, 'publish v2 storage fixture')
                fixture.update(base=live.published(result), cap=live.created(result, '::package::UpgradeCap')[0])
                save()
            assert same_modules(fixture['base'], base_build)
            record_publication(storage, live.chain, fixture['base'], fixture['cap'])
            if 'budget' not in fixture:
                core_package, package = fixture['core'], fixture['base']
                result = live.transaction('create isolated budget',
                    '--split-coins', 'gas', '[1000000u64]', '--assign', 'funds',
                    '--move-call', f'{core_package}::fleet_budget::new_limits', '10u64', '1000u64', '100u64', '10u64', '0u64',
                    '--assign', 'limits', '--move-call', f'{core_package}::fleet_budget::create',
                    '@' + fixture['registry'], 'funds.0', 'limits', '1u64', '--assign', 'owner',
                    # create returns its owner cap; the shared budget ID is read
                    # from effects, so storage is created in the next PTB.
                    '--transfer-objects', '[owner]', '@' + live.operator)
                fixture.update(budget=live.created(result, '::fleet_budget::FleetBudget')[0],
                               owner=live.created(result, '::fleet_budget::BudgetOwnerCap')[0])
                save()
            if 'storage' not in fixture:
                package = fixture['base']
                result = live.transaction('create v2 storage',
                    '--move-call', f'{package}::fleet_storage::new_quota', '1000u64', '100u64', '10u64', '10u64', '100u64',
                    '--assign', 'quota', '--move-call', f'{package}::fleet_storage::create',
                    '@' + fixture['budget'], '@' + fixture['owner'], 'quota')
                fixture['storage'] = live.created(result, '::fleet_storage::FleetStorage')[0]
                save()
            if not fixture.get('configured'):
                package = fixture['base']
                live.transaction('configure legacy vector on v2 object', '--move-call',
                    f'{package}::fleet_storage::configure_retention', '@' + fixture['storage'],
                    '@' + fixture['budget'], '@' + fixture['owner'], '@' + live.operator,
                    '2u32', '100u64', '10u64', '0u64')
                fixture['configured'] = True
                save()
            source.write_bytes(head_source)
            head_build = tc.compile_package(live.build_sui, storage)
            live.evidence.update(source_sha256=hashlib.sha256(head_source).hexdigest(),
                                 compiled_digest=bytes(head_build['digest']).hex())
            if 'head' not in fixture:
                owned_cap(fixture['cap'], fixture['base'], 1)
                record = live.evidence.setdefault('verified_package_transactions', {}).setdefault('upgrade storage', {})
                def submit(budget):
                    pinned = (storage / 'Move.lock').read_bytes()
                    unsigned = live.client('upgrade', storage, '--upgrade-capability', fixture['cap'],
                        '--gas-budget', str(budget), '--serialize-unsigned-transaction', json_output=False)
                    assert (storage / 'Move.lock').read_bytes() == pinned
                    data = unsigned.stdout.strip().splitlines()[-1]
                    record.update(tc.verify_package_transaction(tc.decode(data), head_build,
                        sender=live.operator, budget=budget,
                        upgrade={'cap': fixture['cap'], 'package': fixture['base']}))
                    return live.executor()('serialized-tx', data)
                result = live.execute('compatible v2 to v3 storage upgrade', submit, gas=200_000_000)
                fixture['head'] = live.published(result)
                save()
            assert same_modules(fixture['head'], head_build, fixture['base'])
            owned_cap(fixture['cap'], fixture['head'], 2)
            facts = dm.chain_facts(live.rpc_url, fixture['head'])
            origins = dm.origins(facts['objects'][dm.object_id(fixture['head'])])
            assert dm.object_id(origins['fleet_storage::FleetStorage']) == dm.object_id(fixture['base'])
            assert dm.object_id(origins['fleet_storage::RenewalKey']) == dm.object_id(fixture['base'])
            assert dm.object_id(origins['fleet_storage::FullyPaidArchiveKey']) == dm.object_id(fixture['head'])
            fields = plain(live.object(fixture['storage'])['content'])
            if int(fields['version']) == 2:
                live.transaction('migrate existing v2 retention storage', '--move-call',
                    f"{fixture['head']}::fleet_storage::migrate", '@' + fixture['storage'],
                    '@' + fixture['budget'], '@' + fixture['owner'])
            assert int(plain(live.object(fixture['storage'])['content'])['version']) == 3
            live.attack('v2 owner mutation rejected after migration', '--move-call',
                f"{fixture['base']}::fleet_storage::clear_device_retention",
                '@' + fixture['storage'], '@' + fixture['budget'], '@' + fixture['owner'],
                module='fleet_storage', function='assert_owner', code=0)
            live.evidence.update(fixture=fixture, source_pair=key, origins=origins)
            live.check('compatible v2/v3 upgrade, existing-object migration, independent type origins and retired old entry point')
        finally:
            live.finish(args.evidence)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallet', required=True)
    parser.add_argument('--base', default='e19970e045c42aad2452d5a67294fe1b770133f9')
    parser.add_argument('--core-fixtures', help='Existing SF083 public fixture cache to reverify/reuse')
    parser.add_argument('--cache', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--max-spend-mist', type=int, default=500_000_000)
    parser.add_argument('--build-sui', default='sui')
    parser.add_argument('--network-sui', default=tc.NETWORK_DEFAULT)
    run(parser.parse_args())
