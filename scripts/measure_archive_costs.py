#!/usr/bin/env python3
"""Controlled dedicated-testnet archive registration/retention/cleanup costs.

No mainnet, device deployment or production account. Uses an explicit verified
existing fixture; publishes no packages. Synthetic equal-size payloads are stored,
certified and read back. Owner releases/burns only these disposable blobs; this
measures rebates, NOT implemented automatic expired-archive cleanup. WAL is not
refunded and early burn forfeits renewal control. Setup costs remain separate.
"""
import argparse, json, os, subprocess, tempfile, select
from pathlib import Path
import deployment_manifest as dm
from live_common import Live, CLOCK, ROOT, plain
from live_fleet_budget import event, net_gas, u8_vector, SYSTEM, WALRUS_PACKAGE, EXCHANGE, EXCHANGE_PACKAGE


def run(args, live_factory=Live):
    if not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
        raise RuntimeError('Acquire network:suifleet-testnet before running')
    live = live_factory(args.wallet, 'testnet', args.max_spend_mist,
                        build_sui=args.build_sui, network_sui=args.network_sui)
    live.preflight(auto_fund=False)
    if args.resume_evidence:
        previous=json.loads(Path(args.resume_evidence).read_text())
        assert previous['chain']==live.chain and previous['operator']==live.operator
        assert previous['max_spend_mist']==args.max_spend_mist
        expected=dict(core=args.core, registry=args.registry, storage_package=args.storage_package)
        if previous.get('fixture') != expected:
            raise RuntimeError('Resume fixture differs from the original controlled experiment')
        current=live.evidence
        live.evidence=previous
        live.evidence.setdefault('resume_preflights',[]).append({k:current[k] for k in ['started','start_balance_mist','head']})
        live.ledger.evidence=live.evidence['transactions']
        live.ledger.used=previous['allowance_used_mist']
    def save():
        live.evidence['allowance_used_mist']=live.ledger.used
        Path(args.evidence).write_text(json.dumps(live.evidence,indent=2)+'\n')
    def transaction(label,*args,**kwargs):
        previous=[r for r in live.evidence['transactions'] if r['label']==label]
        for old in previous:
            if old['status']=='success':
                result=live.executed(old['digest'])
                assert result['effects']['status']['status']=='success'
                return result
            if old['status'] not in ['not_submitted']:
                raise RuntimeError('Uncertain prior transaction requires effects reconciliation before resuming: '+label)
        try:return live.transaction(label,*args,**kwargs)
        finally:save()
    call = lambda module, fn, package=args.core: f'{package}::{module}::{fn}'
    facts = dm.chain_facts(live.rpc_url, args.core, args.registry, args.upgrade_cap, args.storage_package)
    assert dm.object_id(live.owner(args.upgrade_cap)) == live.operator
    assert dm.upgrade_cap(facts['objects'][dm.object_id(args.upgrade_cap)])[0] == dm.object_id(args.core)
    # Binding the registry type to this core is checked by the deployed budget
    # and storage contracts, and recorded here before mutating any fixture.
    registry = facts['objects'][dm.object_id(args.registry)]
    assert registry['type'].endswith('::device_registry::DeviceRegistry')
    live.evidence.update(scope='synthetic matched-size per-device archives; no confidential-data or automatic-cleanup acceptance',
                         fixture=dict(core=args.core, registry=args.registry, storage_package=args.storage_package),
                         measured=live.evidence.get('measured',{}), limitations=[
                             'Dedicated testnet effects, not a mainnet price.',
                             'Owner release/burn is immediate on disposable synthetic archives, not unattended expiry cleanup.',
                             'WAL storage/write payments are consumed; Sui storage rebates do not refund WAL.',
                             'No SuiFleet data anchors are created, so this is storage-registration/certification/cleanup only.',
                             'One-off setup, remaining fleet/device objects, deposits and reserve funds are separate from per-upload costs.'])
    def record(label, result):
        value=dict(digest=result['digest'], gas=result['effects']['gasUsed'], net_gas_mist=net_gas(result), object_changes=result.get('objectChanges',[]), events=result.get('events',[]))
        live.evidence['measured'][label]=value
        save()
        return value
    try:
        r=transaction('cost probe: isolated device owned by operator',
            '--move-call',call('device_registry','register_device'),f'@{args.registry}','"archive-cost-probe"',f'@{live.operator}','"x86_64"',CLOCK,'--assign','device',
            '--move-call',call('device_cap','issue_cap'),'device',
            '--move-call',call('device_registry','activate_device'),f'@{args.registry}','device','--assign','owner',
            '--transfer-objects','[owner]',f'@{live.operator}',budget=30_000_000)
        device_cap=live.created(r,'::device_cap::DeviceCap')[0];owner_cap=live.created(r,'::device_registry::DeviceOwnerCap')[0]
        r=transaction('cost probe: isolated prepaid budget',
            '--split-coins','gas','[20000000u64]','--assign','deposit',
            '--move-call',call('fleet_budget','new_limits'),'10000000u64','86400000u64','10000000u64','4u64','0u64','--assign','limits',
            '--move-call',call('fleet_budget','create'),f'@{args.registry}','deposit','limits','0u64','--assign','cap',
            '--transfer-objects','[cap]',f'@{live.operator}',budget=30_000_000,funding=20_000_000)
        budget=live.created(r,'::fleet_budget::FleetBudget')[0];cap=live.created(r,'::fleet_budget::BudgetOwnerCap')[0]
        r=transaction('cost probe: enroll operator device', '--move-call',call('fleet_budget','enroll_device'),f'@{budget}',f'@{cap}',f'@{args.registry}',f'@{owner_cap}','0u64','0u64',CLOCK,budget=20_000_000)
        account=live.created(r,'::fleet_budget::GasAccount')[0]
        live.evidence.setdefault('objects',{}).update(budget=budget,budget_cap=cap,account=account,device_cap=device_cap)
        with tempfile.TemporaryDirectory(prefix='suifleet-archive-cost-') as temporary:
            root=Path(temporary)
            for profile,retained,epochs in [('finite',False,3),('retained',True,3),('finite_upfront',False,4)]:
                if all(f'{profile}_cleanup_{i}' in live.evidence['measured'] for i in range(2)):
                    continue  # Already burned: never rewrite an acknowledged completed profile.
                r=transaction(f'cost probe: {profile} storage',
                    '--move-call',call('fleet_storage','new_quota',args.storage_package),'86400000u64','264136000u64','4u64','4096u64','2000000u64','--assign','quota',
                    '--move-call',call('fleet_storage','create',args.storage_package),f'@{budget}',f'@{cap}','quota',budget=20_000_000)
                storage=live.created(r,'::fleet_storage::FleetStorage')[0]
                live.evidence['objects'][profile+'_storage']=storage
                rate=plain(live.object(EXCHANGE)['content'])['rate'];minimum=20_000_000*int(rate['wal'])//int(rate['sui'])
                r=transaction(f'cost probe: {profile} funded pool',
                    '--split-coins','gas','[20000000u64]','--assign','sui',
                    '--move-call',f'{EXCHANGE_PACKAGE}::wal_exchange::exchange_all_for_wal',f'@{EXCHANGE}','sui','--assign','wal',
                    '--move-call',call('fleet_storage','deposit_wal_at_least',args.storage_package),f'@{storage}',f'@{budget}',f'@{cap}','wal',f'{minimum}u64',
                    '--move-call',call('fleet_storage','buy_storage',args.storage_package),f'@{storage}',f'@{budget}',f'@{cap}',f'@{SYSTEM}','264136000u64',f'{epochs}u32',
                    budget=40_000_000,funding=20_000_000)
                end=int(event(r,'::fleet_storage::StorageDeposited')['end_epoch'])
                live.evidence.setdefault('retention_intervals',{})[profile]={'initial_end_epoch':end,'target_end_epoch':end+1 if retained else end,'initial_epochs':epochs,'renewal_prepaid':retained}
                if retained:
                    transaction('cost probe: enable and prepay retained policy',
                        '--move-call',call('fleet_storage','configure_retention',args.storage_package),f'@{storage}',f'@{budget}',f'@{cap}',f'@{live.operator}','4u32','10000000u64','5000000u64','1000000u64',
                        '--move-call',call('fleet_storage','withdraw_wal',args.storage_package),f'@{storage}',f'@{budget}',f'@{cap}','10000000u64','--assign','wal',
                        '--split-coins','gas','[5000000u64]','--assign','renewal_gas',
                        '--move-call',call('fleet_storage','fund_retention',args.storage_package),f'@{storage}','wal','renewal_gas',
                        '--move-call',call('fleet_storage','configure_device_retention',args.storage_package),f'@{storage}',f'@{budget}',f'@{cap}',f'{end+1}u32','2000000u64',
                        budget=40_000_000,funding=5_000_000)
                blobs=[]
                for sample in range(2):
                    file=root/f'{profile}-{sample}.bin';file.write_bytes(bytes([sample+({'finite':0,'retained':2,'finite_upfront':4}[profile])+1])*2048)
                    log=(root/f'{profile}-{sample}.stderr').open('w')
                    upload=subprocess.Popen(['node',str(ROOT/'scripts/live_walrus_upload.mjs'),str(args.sdk),live.rpc_url,str(file),WALRUS_PACKAGE],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True)
                    def read_upload():
                        if not select.select([upload.stdout],[],[],180)[0]:
                            raise RuntimeError('Synthetic Walrus uploader timed out; no transaction outcome inferred')
                        line=upload.stdout.readline()
                        if not line:
                            log.flush()
                            raise RuntimeError('Keyless synthetic uploader failed: '+Path(log.name).read_text()[-2000:])
                        return json.loads(line)
                    try:
                        encoded=read_upload();assert encoded['size']==2048 and encoded['n_shards']==1000
                        r=transaction(f'cost probe: {profile} register {sample}',
                            '--move-call',call('fleet_storage','register_blob',args.storage_package),f'@{storage}',f'@{budget}',f'@{account}',f'@{args.registry}',f'@{device_cap}',f'@{SYSTEM}',
                            f'{encoded["blob_id_u256"]}u256',f'{encoded["root_hash_u256"]}u256','2048u64','1u8',CLOCK,budget=40_000_000)
                        record(f'{profile}_register_{sample}',r);registered=event(r,'::fleet_storage::UploadRegistered');blob=registered['blob_object_id'];blobs.append(blob)
                        live.evidence['measured'][f'{profile}_register_{sample}']['upload']=registered
                        upload.stdin.write(blob+'\n');upload.stdin.flush();certificate=read_upload()
                        vec=u8_vector
                        r=transaction(f'cost probe: {profile} certify {sample}',
                            '--move-call',call('fleet_storage','certify_blob',args.storage_package),f'@{storage}',f'@{SYSTEM}',f'@{blob}',vec(certificate['signature']),vec(certificate['signers_bitmap']),vec(certificate['message']),budget=10_000_000)
                        record(f'{profile}_certify_{sample}',r)
                        upload.stdin.write('read\n');upload.stdin.flush();read=read_upload();assert read['read_matches'] and upload.wait(timeout=60)==0
                    finally:
                        if upload.poll() is None:upload.kill();upload.wait()
                        log.close()
                for sample,blob in enumerate(blobs):
                    r=transaction(f'cost probe: owner cleanup {profile} {sample}',
                        '--move-call',call('fleet_storage','release_blob',args.storage_package),f'@{storage}',f'@{budget}',f'@{cap}',f'@{blob}','--assign','released',
                        '--move-call',f'{WALRUS_PACKAGE}::blob::burn','released',budget=30_000_000)
                    record(f'{profile}_cleanup_{sample}',r)
                # Leave unused fleet reserves explicitly recorded; do not guess
                # a withdrawal amount after actual renewal purchases/refunds.
        assert live.evidence['retention_intervals']['finite_upfront']['target_end_epoch']==live.evidence['retention_intervals']['retained']['target_end_epoch'], 'Epoch rollover: do not claim an equal-retention comparison'
        live.evidence['result']='passed'
    finally:
        save()


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['wallet','core','registry','upgrade-cap','storage-package','sdk','evidence']:p.add_argument('--'+name,required=True)
    p.add_argument('--build-sui',default='sui');p.add_argument('--network-sui',default='/opt/sui-network/sui')
    p.add_argument('--max-spend-mist',type=int,default=500_000_000)
    p.add_argument('--resume-evidence',type=Path,help='Re-read exact prior effects and retain the original allowance; unknown outcomes refuse.')
    return p
if __name__=='__main__':run(parser().parse_args())
