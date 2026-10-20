#!/usr/bin/env python3
"""Dedicated-wallet testnet handover against an already verified package fixture.
No publication, faucet, mainnet or hardware operations. Child wallet is disposable.
"""
import argparse
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile

import deployment_manifest as dm
from live_common import Live, plain


def run(args):
    if not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
        raise RuntimeError('machine-session network:suifleet-testnet lock is required')
    os.umask(0o077)
    live = Live(args.wallet, 'testnet', args.max_spend_mist, build_sui=args.build_sui, network_sui=args.network_sui)
    live.preflight()
    facts = dm.chain_facts(live.rpc_url, args.package, args.upgrade_cap, args.registry)
    cap = facts['objects'][dm.object_id(args.upgrade_cap)]
    assert dm.upgrade_cap(cap)[:2] == (dm.object_id(args.package), str(facts['objects'][dm.object_id(args.package)]['version']))
    assert live.owner(args.upgrade_cap) == live.operator, 'fixture UpgradeCap is not owned by dedicated source'
    origins = dm.origins(facts['objects'][dm.object_id(args.package)])
    assert 'fleet_handover::Handover' in origins, 'fixture lacks handover protocol'
    package, registry = dm.object_id(args.package), dm.object_id(args.registry)
    live.evidence.update(package=package, registry=registry, handover_origin=origins['fleet_handover::Handover'],
                         base_package=dm.object_id(args.base_package))
    root = Path(tempfile.mkdtemp(prefix='sf089-recipient-'))
    try:
        (root/'sui.keystore').write_text('[]')
        config = root/'client.yaml'
        config.write_text(f'keystore:\n  File: {root}/sui.keystore\nenvs:\n- alias: testnet\n  rpc: {live.rpc_url}\nactive_env: testnet\nactive_address: null\n')
        # Capture new-address output privately; never print key material.
        generated = subprocess.run([live.network_sui, 'client', '--client.config', str(config), 'new-address', 'ed25519', '--json'], capture_output=True, text=True, timeout=60)
        if generated.returncode:
            raise RuntimeError('temporary recipient key generation failed; output suppressed')
        try:
            child_address = json.loads(generated.stdout[generated.stdout.index('{'):])['address']
        except (ValueError, KeyError):
            raise RuntimeError('temporary recipient key response was invalid; output suppressed') from None
        live.client('switch','--address',child_address,config=config)
        funding = 40_000_000
        live.transaction('fund disposable handover recipient','--split-coins','gas',f'[{funding}]','--assign','fund',
            '--transfer-objects','[fund]',f'@{child_address}',budget=5_000_000,funding=funding)
        child = live.child(config,funding)
        call = lambda module, fn: f'{package}::{module}::{fn}'
        r=live.transaction('create disposable activated device','--move-call',call('device_registry','register_device'),
            f'@{registry}','"handover-test"','@0xcc','"arm"','@0x6','--assign','device',
            '--move-call',call('device_registry','activate_device'),f'@{registry}','device','--assign','cap',
            '--transfer-objects','[cap]',f'@{live.operator}',budget=10_000_000)
        device=live.created(r,'::device_registry::Device')[0];owner_cap=live.created(r,'::device_registry::DeviceOwnerCap')[0]
        r=live.transaction('create disposable group','--move-call',call('device_group','create_group'),'"handover fleet"','"testnet acceptance"','@0x6',budget=10_000_000)
        group=live.created(r,'::device_group::DeviceGroup')[0]
        r=live.transaction('populate group and desired native record','--move-call',call('device_registry','borrow_device'),f'@{registry}',f'@{owner_cap}','--assign','borrowed',
            '--move-call',call('device_group','add_device'),f'@{group}','borrowed.0',
            '--move-call',call('device_registry','return_device'),f'@{registry}','borrowed.0','borrowed.1',
            '--move-call',call('deployment_manager','new_deployment_verified'),'"handover-app"','"1.0.0"','"blob"','"'+'ab'*32+'"','"'+'cd'*32+'"',f'vector[@{group}]','vector[]','@0x6','--assign','record',
            '--move-call',call('deployment_manager','set_control'),'record','1','0','0','0','0','0',
            '--move-call',call('desired_state','begin'),f'@{group}','--assign','plan',
            '--move-call',call('desired_state','set_component'),'plan','record',
            '--move-call',call('desired_state','commit'),'plan',f'@{group}','@0x6',
            '--move-call',call('deployment_manager','share_deployment'),'record',budget=20_000_000)
        record=live.created(r,'::deployment_manager::DeploymentRecord')[0]
        expiry=int(plain(live.object('0x6')['content'])['timestamp_ms'])+3600000
        r=live.transaction('propose fixed inventory','--move-call',call('fleet_handover','propose'),f'@{child_address}',f'vector[@{owner_cap},@{group},@{record}]','vector[0u64,1u64,2u64]',str(expiry),'@0x6',budget=10_000_000)
        h=live.created(r,'::fleet_handover::Handover')[0]
        live.evidence.update(handover=h,recipient=child_address,device=device,owner_cap=owner_cap,group=group,record=record)
        live.attack('source cannot confirm for recipient','--move-call',call('fleet_handover','confirm'),f'@{h}','@0x6',module='fleet_handover',function='confirm',code=0,budget=5_000_000)
        live.transaction('recipient confirms inventory','--move-call',call('fleet_handover','confirm'),f'@{h}','@0x6',sender=child,budget=5_000_000)
        live.transaction('transfer cap as first resumable batch','--move-call',call('fleet_handover','device'),f'@{h}',f'@{owner_cap}','@0x6',budget=5_000_000)
        value=plain(live.object(h)['content'])
        assert value['completed']==[True,False,False]
        assert dm.object_id(live.owner(owner_cap))==dm.object_id(child_address)
        live.transaction('complete group and record authority','--move-call',call('fleet_handover','group'),f'@{h}',f'@{group}','@0x6',
            '--move-call',call('fleet_handover','deployment'),f'@{h}',f'@{record}','@0x6',budget=10_000_000)
        for pkg in {package,dm.object_id(args.base_package)}:
            live.attack('old owner cannot mutate migrated group '+pkg[:10],'--move-call',f'{pkg}::device_group::remove_device',f'@{group}',f'@{device}',module='device_group',function='remove_device',code=0,budget=5_000_000)
            live.attack('old owner cannot control migrated deployment '+pkg[:10],'--move-call',f'{pkg}::deployment_manager::set_control',f'@{record}','0','0','0','0','0','0',module='deployment_manager',function='set_control',code=2,budget=5_000_000)
        live.transaction('recipient revises existing desired state and controls','--move-call',call('desired_state','begin'),f'@{group}','--assign','plan',
            '--move-call',call('desired_state','set_component'),'plan',f'@{record}',
            '--move-call',call('desired_state','commit'),'plan',f'@{group}','@0x6',
            '--move-call',call('deployment_manager','set_control'),f'@{record}','0','0','0','0','0','0',
            '--move-call',call('fleet_handover','finish'),f'@{h}',sender=child,budget=5_000_000)
        assert int(plain(live.object(h)['content'])['state'])==3
        assert dm.object_id(plain(live.object(group)['content'])['admin'])==dm.object_id(child_address)
        assert dm.object_id(plain(live.object(record)['content'])['creator'])==dm.object_id(child_address)
        live.check('recipient confirmation, durable partial progress, unchanged device/cap identity, old/new-package old-owner rejection, recipient desired-state and controls')
        live.transaction('delete transient recipient fleet objects','--move-call',call('deployment_manager','delete_deployment'),f'@{record}',
            '--move-call',call('device_group','delete_group'),f'@{group}',
            '--move-call',call('device_registry','deregister_activated_device'),f'@{registry}',f'@{owner_cap}',sender=child,budget=5_000_000)
        live.execute('return disposable recipient gas',['pay-all-sui','--recipient',live.operator],gas=5_000_000,sender=child)
        live.evidence['limits']=['Handover audit object and revoked device identity tombstone retained; transient group/record/device/cap deleted.',
            'Native multisig signing covered on isolated chain, not this dedicated-wallet testnet run.',
            'No physical, mainnet, hosted-provider credential or Seal acceptance.']
        live.evidence['success']=True
    finally:
        if live.evidence.get('success'):
            shutil.rmtree(root)
        else:
            live.evidence['private_runtime_retained_for_cleanup'] = str(root)
        live.finish(args.evidence)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ['wallet','package','upgrade-cap','registry','base-package','evidence']:
        parser.add_argument('--'+flag,required=True)
    parser.add_argument('--max-spend-mist',type=int,default=150_000_000)
    parser.add_argument('--build-sui',default='sui')
    parser.add_argument('--network-sui',default='/opt/sui-network/sui')
    run(parser.parse_args())
