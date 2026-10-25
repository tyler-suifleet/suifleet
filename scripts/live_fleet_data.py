#!/usr/bin/env python3
"""Dedicated-Testnet SF-082 device-to-browser acceptance, with strict allowance.

Uses the already verified SF-083 core fixture at source 011ee1c (the core inputs
must remain byte-for-byte equal), publishes only missing storage/data packages,
then uses the real native producer, Walrus storage nodes and browser verifier.
No mainnet, production provider credentials, portal deployment or hardware.
"""
import argparse, hashlib, json, os, secrets, shutil, signal, subprocess, tempfile, time, threading
from pathlib import Path
from types import SimpleNamespace
import deployment_manifest as dm
import sui_toolchain as toolchain
from live_common import Live, ROOT, DAEMON, CLOCK, WALRUS_TESTNET, stage, key_wallet, pending_key, recover_key, sweep, plain, wait_for
from live_fleet_budget import record_publication, publish_storage, event, net_gas, SYSTEM, WALRUS_PACKAGE, EXCHANGE_PACKAGE, EXCHANGE
CORE_SOURCE='011ee1c'
CORE='0x34978350b5be61a60b51f45edd0b7f3f29e267399cc56a60e16aa6bfecb2bec2'
REGISTRY='0xd3afc908876326367294ba95d13cbac5775dd1370a23fb3ba8ba29b25daeaf41'
CORE_CAP='0x400dda2c3b0788a08841c0a81a4bdc444c6e399268d60f352eb37f04066d0983'
KEY_PACKAGE='0x0f16e84a49dec8425e6900cfdfe3730aaf1e8bc608d9f0500fcfa2c2267abfb4'
SERVERS=['0x73d05d62c18d9374e3ea529e8e0ed6161da1a141a94d3f76ae3fe4e99356db75','0xf5d14a81a982144ae441cd7d64b09027f116a468bd36e7eca494f750591623c8']
DEPOSIT,FLOAT,RESERVE,SWAP=250_000_000,80_000_000,100_000_000,20_000_000
ENCODED=66_034_000

def source_key(directory, dependency=''):
    h=hashlib.sha256(dependency.encode())
    for p in sorted([directory/'Move.toml',directory/'Move.lock',*(directory/'sources').glob('*.move')]):
        h.update(p.name.encode()+b'\0'+p.read_bytes()+b'\0')
    return h.hexdigest()
def run(args):
    if not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):raise RuntimeError('network:suifleet-testnet lock required')
    os.umask(0o077)
    args.codec=args.codec.resolve(strict=True)
    assert args.codec.is_file(), 'codec artifact must exist before wallet operations'
    live=Live(args.wallet,'testnet',args.max_spend_mist);live.preflight()
    # No republishing an identical core: these are the source/lock inputs of
    # the effects-verified publication in sf083-testnet.json, freshly re-read.
    changed=subprocess.check_output(['git','-C',str(ROOT),'diff',CORE_SOURCE,'--','contracts/sources','contracts/Move.toml','contracts/Move.lock'],text=True)
    assert not changed,'core fixture inputs changed; do not guess or silently adopt another API'
    facts=dm.chain_facts(live.rpc_url,CORE,CORE_CAP,REGISTRY)
    assert dm.object_id(live.owner(CORE_CAP))==live.operator
    assert dm.upgrade_cap(facts['objects'][dm.object_id(CORE_CAP)])[:2]==(CORE,'1')
    live.check('reused immutable SF083 core, exact source/lock/compiler inputs; chain, account and UpgradeCap reverified')
    prior=json.loads(args.resume_evidence.read_text()) if args.resume_evidence else None
    if prior:
        assert prior['chain']==live.chain and prior['operator']==live.operator
        live.ledger.grant(prior['allowance_used_mist'])
        live.evidence['carried_allowance_mist']=prior['allowance_used_mist']
        live.evidence['setup_transactions']=prior.get('setup_transactions',prior['transactions'])
    root=Path(prior['runtime']) if prior else Path(tempfile.mkdtemp(prefix='sf082-live-',dir=args.runtime_dir))
    daemon=None;watch_stop=threading.Event();health_receipts={};funding_receipts={}
    state_path=live.wallet.parent/'sf082-state.json';cache=json.loads(state_path.read_text()) if state_path.exists() else {'chain':live.chain,'packages':{}}
    assert cache['chain']==live.chain
    save=lambda:state_path.write_text(json.dumps(cache,indent=2)+'\n')
    def package(directory,label,key):
        old=cache['packages'].get(key)
        if old:
            current=dm.chain_facts(live.rpc_url,old['package'],old['cap'])
            assert current['objects'][dm.object_id(old['package'])]['kind']=='package'
            assert dm.object_id(live.owner(old['cap']))==live.operator
            assert dm.upgrade_cap(current['objects'][dm.object_id(old['cap'])])[:2]==(dm.object_id(old['package']),'1')
            live.check('reused '+label);return old
        r=publish_storage(live,directory,label=label)
        result={'package':live.published(r),'cap':live.created(r,'::package::UpgradeCap')[0]}
        cache['packages'][key]=result;save();return result
    key_path=live.wallet.parent/'sf082-device.key'
    if not prior:recover_key(live,key_path,root)
    try:
        if prior:
            manifest=prior['manifest'];dm.verify(manifest)
            storage_package=prior['packages']['storage'];data_package=prior['packages']['data']
            assert cache['packages'][source_key(ROOT/'contracts/fleet_storage',CORE)]==storage_package
            assert cache['packages'][source_key(ROOT/'contracts/fleet_data',storage_package['package'])]==data_package
            for selected in (storage_package,data_package):
                facts=dm.chain_facts(live.rpc_url,selected['package'],selected['cap'])
                assert dm.object_id(live.owner(selected['cap']))==live.operator
                assert dm.upgrade_cap(facts['objects'][dm.object_id(selected['cap'])])[:2]==(dm.object_id(selected['package']),'1')
            data_pkg=data_package['package'];storage_pkg=storage_package['package']
            config={k:prior['fleet_data'][k] for k in ('schema','chainIdentifier','registryId','packageId','typeOrigin','fleetId','budgetId','storageId')}
            saved=cache['last_objects'];budget=saved['budget'];budget_cap=saved['budget_cap'];account=saved['account']
            storage=saved['storage'];fleet=saved['fleet'];writer=saved['writer'];device_id=saved['device']
            assert (budget,storage,fleet)==(config['budgetId'],config['storageId'],config['fleetId'])
            seed=(root/'device/test.key').read_text().strip();assert seed==key_path.read_text().strip()
            device_config=root/'device/client.yaml';address=live.client('active-address',json_output=False,config=device_config).stdout.strip()
            available=live.balance(address);live.ledger.grant(available);device=live.child(device_config,available)
            state_dir=root/'state';release=root/'release'
            live.evidence.update(manifest=manifest,packages=prior['packages'],fleet_data=prior['fleet_data'],runtime=str(root),resumed_from=str(args.resume_evidence),precharged_cached_device_mist=available)
            old_health=state_dir/'fleet-data/health.json'
            if old_health.exists():
                receipt=json.loads(old_health.read_text());health_receipts[int(receipt['seq'])]=receipt
            call=lambda module,fn,pkg=CORE:f'{pkg}::{module}::{fn}'
        else:
            tree=root/'contracts';stage(ROOT/'contracts',tree);record_publication(tree,live.chain,CORE,CORE_CAP)
            storage_package=package(tree/'fleet_storage','publish SF082 current fleet_storage',source_key(ROOT/'contracts/fleet_storage',CORE))
            record_publication(tree/'fleet_storage',live.chain,storage_package['package'],storage_package['cap'])
            data_package=package(tree/'fleet_data','publish SF082 fleet_data',source_key(ROOT/'contracts/fleet_data',storage_package['package']))
            data_pkg=data_package['package'];storage_pkg=storage_package['package']
            manifest=dm.resolve(SimpleNamespace(chain_identifier=live.chain,rpc=live.rpc_url,package=CORE,upgrade_cap=CORE_CAP,registry=REGISTRY,network='testnet',publisher=WALRUS_TESTNET[0],aggregator=WALRUS_TESTNET[1]))
            live.evidence.update(core_source=CORE_SOURCE,manifest=manifest,packages=dict(storage=storage_package,data=data_package),runtime=str(root))
            call=lambda module,fn,pkg=CORE:f'{pkg}::{module}::{fn}'
            seed=secrets.token_hex(32);pending_key(key_path,seed);device_config,address=key_wallet(live,seed,root,'device')
            r=live.transaction('register SF082 device','--move-call',call('device_registry','register_device'),f'@{REGISTRY}','"sf082-native-producer"',f'@{address}','"x86_64"',CLOCK,'--assign','device',
                '--move-call',call('device_cap','issue_cap'),'device','--move-call',call('device_registry','activate_device'),f'@{REGISTRY}','device','--assign','owner','--transfer-objects','[owner]',f'@{live.operator}',budget=40_000_000)
            device_id=live.created(r,'::device_registry::Device')[0];cap=live.created(r,'::device_cap::DeviceCap')[0];owner_cap=live.created(r,'::device_registry::DeviceOwnerCap')[0]
            r=live.transaction('create SF082 prepaid budget','--split-coins','gas',f'[{DEPOSIT}u64]','--assign','deposit','--move-call',call('fleet_budget','new_limits'),f'{FLOAT}u64','86400000u64','80000000u64','4u64','0u64','--assign','limits',
                '--move-call',call('fleet_budget','create'),f'@{REGISTRY}','deposit','limits','10000000u64','--assign','cap','--transfer-objects','[cap]',f'@{live.operator}',budget=30_000_000,funding=DEPOSIT)
            budget=live.created(r,'::fleet_budget::FleetBudget')[0];budget_cap=live.created(r,'::fleet_budget::BudgetOwnerCap')[0]
            r=live.transaction('enroll SF082 device float and reserve','--move-call',call('fleet_budget','enroll_device'),f'@{budget}',f'@{budget_cap}',f'@{REGISTRY}',f'@{owner_cap}',f'{FLOAT}u64',f'{RESERVE}u64',CLOCK,budget=20_000_000)
            account=live.created(r,'::fleet_budget::GasAccount')[0];device=live.child(device_config,FLOAT)
            r=live.transaction('create SF082 fleet storage','--move-call',call('fleet_storage','new_quota',storage_pkg),'86400000u64',f'{ENCODED*2}u64','2u64','4194304u64','2000000u64','--assign','quota',
                '--move-call',call('fleet_storage','create',storage_pkg),f'@{budget}',f'@{budget_cap}','quota',budget=20_000_000)
            storage=live.created(r,'::fleet_storage::FleetStorage')[0]
            rate=plain(live.object(EXCHANGE)['content'])['rate'];minimum=SWAP*int(rate['wal'])//int(rate['sui'])
            quote=json.loads(subprocess.check_output(['node','scripts/quote_fleet_data.cjs'],text=True,timeout=180))
            assert quote['chain']==live.chain and dm.object_id(quote['system_id'])==dm.object_id(SYSTEM)
            end_epoch=int(quote['walrus_epoch'])+3;live.evidence['walrus_quote']=quote
            r=live.transaction('SUI-only prepaid storage purchase','--split-coins','gas',f'[{SWAP}u64]','--assign','sui','--move-call',f'{EXCHANGE_PACKAGE}::wal_exchange::exchange_all_for_wal',f'@{EXCHANGE}','sui','--assign','wal',
                '--move-call',call('fleet_storage','deposit_wal_at_least',storage_pkg),f'@{storage}',f'@{budget}',f'@{budget_cap}','wal',f'{minimum}u64',
                '--move-call',call('fleet_storage','refill_pool',storage_pkg),f'@{storage}',f'@{budget}',f'@{budget_cap}',f'@{SYSTEM}',f'{ENCODED*2}u64',f'{end_epoch}u32',budget=40_000_000,funding=SWAP)
            live.evidence['storage_purchase']={'digest':r['digest'],'events':r.get('events',[]),'net_gas_mist':net_gas(r)}
            live.transaction('owner approves finite retained device archives',
                '--move-call',call('fleet_storage','configure_retention',storage_pkg),f'@{storage}',f'@{budget}',f'@{budget_cap}',f'@{live.operator}','4u32','10000000u64','5000000u64','1000000u64',
                '--move-call',call('fleet_storage','withdraw_wal',storage_pkg),f'@{storage}',f'@{budget}',f'@{budget_cap}','10000000u64','--assign','renewal_wal',
                '--split-coins','gas','[5000000u64]','--assign','renewal_gas',
                '--move-call',call('fleet_storage','fund_retention',storage_pkg),f'@{storage}','renewal_wal','renewal_gas',
                '--move-call',call('fleet_storage','configure_device_retention',storage_pkg),f'@{storage}',f'@{budget}',f'@{budget_cap}',f'{end_epoch+1}u32','2000000u64',budget=40000000,funding=5000000)
            live.evidence['retention_policy']={'target':end_epoch+1,'initial_end':end_epoch,'max_fee_frost':2000000,'executor':live.operator}
            r=live.transaction('create fleet data index','--move-call',call('fleet_data','create',data_pkg),f'@{budget}',f'@{budget_cap}',f'@{storage}',f'"{live.chain}"',budget=20_000_000)
            fleet=live.created(r,'::fleet_data::FleetData')[0]
            r=live.transaction('enroll indexed device writer','--move-call',call('fleet_data','enroll',data_pkg),f'@{fleet}',f'@{budget}',f'@{budget_cap}',f'@{account}',
                '--move-call',call('fleet_data','configure',data_pkg),f'@{fleet}',f'@{budget}',f'@{budget_cap}','60000u64','86400000u64','60000u64',budget=20_000_000)
            writer=live.created(r,'::fleet_data::DeviceData')[0]
            config=dict(schema=1,chainIdentifier=live.chain,registryId=REGISTRY,packageId=data_pkg,typeOrigin=data_pkg,fleetId=fleet,budgetId=budget,storageId=storage)
            cache['last_objects']=dict(budget=budget,budget_cap=budget_cap,account=account,storage=storage,fleet=fleet,writer=writer,device=device_id,owner_cap=owner_cap);save()
            live.evidence['fleet_data']=dict(config,writerId=writer,deviceId=device_id,accountId=account,storagePackage=storage_pkg)
        if not (root/'release'/'config.toml').exists():
            if prior:
                setup=live.evidence['setup_transactions'];registration=next(t['digest'] for t in setup if t['label']=='register SF082 device')
                cap=live.created(live.executed(registration),'::device_cap::DeviceCap')[0]
            state_dir=root/'state';overlays=root/'overlays';overlays.mkdir(exist_ok=True);(overlays/'fixture.json').write_text(json.dumps({'/password':'sf082-private-fixture-value'}))
            (state_dir/'supervised'/'fixture').mkdir(parents=True,exist_ok=True);(state_dir/'supervised'/'fixture'/'component.log').write_text('fixture sf082-private-fixture-value must be redacted\n')
            template=root/'template.toml';template.write_text(f'[device]\nkeypair_path={json.dumps(str(root/"device/test.key"))}\ndevice_object_id="{device_id}"\ndevice_cap_id="{cap}"\n[apply]\ncommand="/bin/false"\n[runtime]\nstate_dir={json.dumps(str(state_dir))}\npoll_seconds=3\nevent_discovery_seconds=1\n'
                f'[components]\nconfiguration_overlay_dir={json.dumps(str(overlays))}\n[logs]\ninterval_seconds=10\nbuffer_max=32\n[fleet_data]\npackage_id="{data_pkg}"\ntype_origin="{data_pkg}"\nfleet_id="{fleet}"\nbudget_id="{budget}"\nstorage_id="{storage}"\nwriter_id="{writer}"\naccount_id="{account}"\n'
                f'[fleet_data.archives]\nstorage_package="{storage_pkg}"\nstorage_type_origin="{storage_pkg}"\nsystem_id="{SYSTEM}"\nwalrus_package="{WALRUS_PACKAGE}"\nkey_server_package="{KEY_PACKAGE}"\n'
                'key_servers=['+','.join('{object_id="'+s+'",weight=1}' for s in SERVERS)+']\nthreshold=2\n'
                f'node="/usr/bin/node"\ncodec={json.dumps(str(args.codec))}\ncodec_sha256="{hashlib.sha256(Path(args.codec).read_bytes()).hexdigest()}"\n')
            full=template.read_text();base,extensions=full.split('[components]',1);template.write_text(base)
            release=root/'release';dm.generate(manifest,release,template)
            with (release/'config.toml').open('a') as out:out.write('[components]'+extensions)
        # Gas draws mutate only this device's account; archive/health calls
        # keep fleet policy/budget read-only. Reserve all remaining draw funds.
        reserve=plain(live.object(account)['content'])['reserve']
        reserve=int(reserve['value'] if isinstance(reserve,dict) else reserve)
        live.ledger.grant(reserve);live.evidence['precharged_reserve_mist']=reserve
        config_file=release/'config.toml'
        if '[budget]' not in config_file.read_text():
            with config_file.open('a') as out:out.write(f'\n[budget]\nbudget_id="{budget}"\nlow_water_mist=45000000\ndraw_mist=30000000\n')
        completed_file=state_dir/'fleet-data/completed.json'
        periodic_file=root/'periodic-receipt.json'
        prior_completed=json.loads(completed_file.read_text()) if completed_file.exists() else {}
        if prior_completed.get('seq')==1 and not periodic_file.exists():periodic_file.write_text(json.dumps(prior_completed))
        pending_file=state_dir/'fleet-data/pending.json'
        prior_pending=json.loads(pending_file.read_text()) if pending_file.exists() else {}
        has_request=max(prior_completed.get('request_seq',0),prior_pending.get('request_seq',0))>0
        live.evidence['resumed_diagnostic_request']=has_request
        before={id:str(live.object(id)['version']) for id in [budget,fleet]}
        def watch_health():
            while not watch_stop.wait(1):
                try:
                    r=json.loads((state_dir/'fleet-data/health.json').read_text());health_receipts[int(r['seq'])]=r
                except FileNotFoundError:pass
                try:
                    funding=json.loads((state_dir/'state.json').read_text()).get('funding') or {}
                    if funding.get('last_draw_digest'):funding_receipts[funding['last_draw_digest']]=funding.get('last_draw_mist')
                except FileNotFoundError:pass
        threading.Thread(target=watch_health,daemon=True).start()
        log=(root/'daemon.log').open('w');live.assert_lock();daemon=subprocess.Popen([str(DAEMON)],env={**os.environ,'EDGE_CONFIG':str(release/'config.toml')},stdout=log,stderr=log)
        completed=lambda:json.loads((state_dir/'fleet-data/completed.json').read_text()) if (state_dir/'fleet-data/completed.json').exists() else {}
        health=lambda:json.loads((state_dir/'fleet-data/health.json').read_text()) if (state_dir/'fleet-data/health.json').exists() else {}
        wait_for(lambda:completed().get('seq',0)>=1,'native periodic archive did not certify/anchor',daemon,timeout=300)
        if not periodic_file.exists():
            assert completed().get('seq')==1,'missing periodic receipt for resumed fixture'
            periodic_file.write_text(json.dumps(completed()))
        periodic=json.loads(periodic_file.read_text());health_first=health();health_receipts[int(health_first['seq'])]=health_first
        daemon.terminate();daemon.wait(timeout=30);daemon=None
        live.assert_lock();daemon=subprocess.Popen([str(DAEMON)],env={**os.environ,'EDGE_CONFIG':str(release/'config.toml')},stdout=log,stderr=log)
        time.sleep(5)
        if not has_request:assert completed()['seq']==1
        if not has_request:live.transaction('request native diagnostics','--move-call',call('fleet_data','request_diagnostics',data_pkg),f'@{fleet}',f'@{writer}',f'@{budget}',f'@{budget_cap}',CLOCK,budget=15_000_000)
        wait_for(lambda:completed().get('seq')==2,'native diagnostic archive did not certify/anchor',daemon,timeout=300)
        diagnostic=completed();wait_for(lambda:health().get('seq',0)>int(health_first['seq']),'second direct health summary missing',daemon,timeout=120);health_second=health()
        daemon.terminate();daemon.wait(timeout=30);daemon=None
        assert before[fleet]==str(live.object(fleet)['version']),'device writes must not mutate fleet policy'
        if not funding_receipts:assert before[budget]==str(live.object(budget)['version']),'archive writes must not mutate the fleet budget'
        health_receipts[int(health_second['seq'])]=health_second;watch_stop.set()
        assert sorted(health_receipts)==list(range(min(health_receipts),int(health_second['seq'])+1)),'missed a native health transaction receipt'
        receipts=[]
        for label,digest in [*[(f'funding-{i}',digest) for i,digest in enumerate(funding_receipts)],*[(f'health-{seq}',r['digest']) for seq,r in sorted(health_receipts.items())],*[(f'periodic-{k}',periodic[k]) for k in ['registration','certification','anchor']],*[(f'diagnostic-{k}',diagnostic[k]) for k in ['registration','certification','anchor']]]:
            r=live.executed(digest);assert r['effects']['status']['status']=='success'
            receipts.append(dict(label=label,digest=digest,net_gas_mist=net_gas(r),gas=r['effects']['gasUsed']))
        total=sum(max(0,r['net_gas_mist']) for r in receipts);live.ledger.grant(total)
        live.evidence['native_transactions']=receipts;live.evidence['precharged_device_allowance_mist']=FLOAT
        live.check('native configured-cadence health path and prepaid encrypted logs/diagnostics certified and anchored; restart made no duplicate upload')
        # Apply both prepaid finite renewals with the explicitly authorized
        # executor; keep original anchor epochs to exercise live-record reads.
        renewal_record=prior.get('retention_policy') if prior else live.evidence.get('retention_policy')
        if renewal_record:
            live.evidence['retention_policy']=renewal_record
            current_end=renewal_record['initial_end'];target=renewal_record['target']
            renewal_args=['--move-call',call('fleet_storage','execute_renewal',storage_pkg),f'@{storage}',f'@{SYSTEM}',f'@{event(live.executed(periodic["registration"]),"::fleet_storage::UploadRegistered")["blob_object_id"]}',f'{current_end}u32','2u64',
                  '--move-call',call('fleet_storage','execute_renewal',storage_pkg),f'@{storage}',f'@{SYSTEM}',f'@{event(live.executed(diagnostic["registration"]),"::fleet_storage::UploadRegistered")["blob_object_id"]}',f'{current_end}u32','2u64']
            marker=root/'renewal-execution.json'
            if marker.exists():
                saved=json.loads(marker.read_text());assert saved.get('status')=='success','reconcile unknown owner renewal effects before resuming'
                r=live.executed(saved['digest']);assert r and r['effects']['status']['status']=='success'
            else:
                marker.write_text(json.dumps({'status':'unknown'}))
                r=live.transaction('apply finite prepaid archive renewals',*renewal_args,budget=30000000)
                marker.write_text(json.dumps({'status':'success','digest':r['digest']}))
            live.evidence['retention_execution']={'digest':r['digest'],'events':r.get('events',[]),'target':target}
        # Native bridge signs only a bounded owner Seal personal-message
        # session from the explicit profile; no key bytes reach Node.
        request=root/'browser.json';request.write_text(json.dumps(dict(manifest=manifest,config=config,device=device_id,budgetCap=budget_cap,servers=SERVERS,codec=str(args.codec))))
        run_browser(live,request,root)
        live.transaction('remove test device account','--move-call',call('fleet_budget','remove_device'),f'@{budget}',f'@{budget_cap}',f'@{account}',budget=20_000_000)
        value=plain(live.object(budget)['content'])['balance'];remaining=int(value['value'] if isinstance(value,dict) else value)
        if remaining:live.transaction('withdraw unused test budget','--move-call',call('fleet_budget','withdraw'),f'@{budget}',f'@{budget_cap}',f'{remaining}u64','--assign','back','--transfer-objects','[back]',f'@{live.operator}',budget=20_000_000)
        live.evidence['device_swept_mist']=sweep(live,seed,root,device[1]);key_path.unlink()
        live.evidence['limits']=['Testnet fixture and open Testnet Seal providers only; no production-provider availability/fees/recovery acceptance.','No manual extension/IdP wallet, physical target or Mainnet acceptance.','Certified non-deletable blobs retain purchased epochs; owner metadata remains for readback.']
    finally:
        watch_stop.set()
        if daemon and daemon.poll() is None:daemon.kill();daemon.wait()
        live.finish(args.evidence)

def run_browser(live,request,root):
    import base64,re
    node=subprocess.Popen(['node','scripts/live_fleet_data.cjs',str(request)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=(root/'browser.log').open('w'),text=True,bufsize=1)
    try:
        for line in node.stdout:
            r=json.loads(line)
            if 'personal_message' in r:
                message=base64.b64decode(r['personal_message'],validate=True);assert 0<len(message)<8192
                paths=re.findall(r'^\s+File:\s*(\S.*?)\s*$',live.wallet.read_text(),re.MULTILINE)
                assert len(paths)==1,'explicit wallet must have one file keystore'
                keystore=json.loads(paths[0]) if paths[0].startswith(chr(34)) else paths[0]
                assert Path(keystore).is_absolute(),'explicit absolute file keystore required'
                signed=subprocess.run([str(ROOT/'suifleet-edge/target/debug/examples/sign_seal_session')],
                    input=json.dumps(dict(wallet=str(live.wallet),keystore=keystore,expected_address=live.operator,
                                          namespace=json.loads(request.read_text())['config']['typeOrigin'],message=r['personal_message'])),
                    capture_output=True,text=True,timeout=60)
                assert signed.returncode==0,'native bridge refused selected owner Seal session'
                signature=json.loads(signed.stdout)['signature']
                node.stdin.write(json.dumps({'signature':signature})+'\n');node.stdin.flush()
            elif 'result' in r:live.evidence['browser']=r['result']
            else:raise RuntimeError('unexpected browser bridge request')
        assert node.wait(timeout=60)==0,'browser verified/decrypt check failed; private log retained'
        assert live.evidence.get('browser',{}).get('verified') is True
        live.check('real browser modules discovered and verified/decrypted certified native archives with owner signed Seal session')
    finally:
        if node.poll() is None:node.kill();node.wait()
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--wallet',required=True);p.add_argument('--max-spend-mist',type=int,default=800_000_000);p.add_argument('--runtime-dir',type=Path);p.add_argument('--codec',type=Path,required=True);p.add_argument('--evidence',required=True);p.add_argument('--resume-evidence',type=Path,help='resume the exact private fixture of an interrupted run without republishing or refunding the allowance');run(p.parse_args())
