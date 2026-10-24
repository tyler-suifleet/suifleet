#!/usr/bin/env python3
"""Compatible base->head upgrade, existing objects and old-package attack.
Only disposable network-none loopback nodes and temporary wallets are allowed.
"""
import json
import hashlib
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.request import Request, urlopen
from test_desired_state_localnet import wait_for
import deployment_manifest as dm
from types import SimpleNamespace
from test_ota import Simulator, ARTIFACT
from test_desired_state import serve

ROOT=Path(__file__).resolve().parents[1]

def run():
    if not Path('/.dockerenv').exists() or {p.name for p in Path('/sys/class/net').iterdir()}!={'lo'}:
        raise RuntimeError('requires disposable Docker --network none')
    rpc='http://127.0.0.1:9000'
    with tempfile.TemporaryDirectory(prefix='rollout-chain-') as tmp:
        root=Path(tmp); wallet=root/'wallet'; wallet.mkdir()
        (wallet/'sui.keystore').write_text('[]')
        (wallet/'client.yaml').write_text(f'keystore:\n  File: {wallet}/sui.keystore\nenvs:\n- alias: rollout\n  rpc: {rpc}\nactive_env: rollout\nactive_address: null\n')
        def client(*args, allow_failure=False):
            r=subprocess.run(['sui','client','--client.config',str(wallet/'client.yaml'),*map(str,args),'--json'],capture_output=True,text=True,timeout=300)
            if r.returncode and not allow_failure: raise RuntimeError(f'{args[0]} failed: {r.stderr[-1500:]}')
            if allow_failure and r.returncode and '{' not in r.stdout and '[' not in r.stdout:
                message = r.stdout+r.stderr
                match = re.search(r"Error executing transaction '([1-9A-HJ-NP-Za-km-z]+)'", message)
                if not match:
                    raise RuntimeError('expected submitted failure digest: '+message)
                effects = dm.rpc(rpc, 'sui_getTransactionBlock', [match[1], {'showEffects':True}])['effects']
                return {'effects':effects, 'digest':match[1]}
            return json.loads(r.stdout[min(i for i in (r.stdout.find('{'),r.stdout.find('[')) if i>=0):])
        owner=client('new-address','ed25519')['address'];client('switch','--address',owner)
        def fund():
            try:
                with urlopen(Request('http://127.0.0.1:9123/v2/gas',json.dumps({'FixedAmountRequest':{'recipient':owner}}).encode(),{'Content-Type':'application/json'}),timeout=30) as r:
                    return r.status == 200
            except OSError:
                return False
        wait_for(fund, 'local faucet did not become ready')
        wait_for(lambda:bool(client('gas')),'empty faucet')
        evidence=[]
        def success(r):
            assert r['effects']['status']['status']=='success',r['effects']['status']
            evidence.append(r['digest']);return r
        def tx(*args):return success(client('ptb',*args,'--gas-budget','100000000'))
        def created(r,suffix):return next(o['objectId'] for o in r['objectChanges'] if o['type']=='created' and o['objectType'].endswith(suffix))
        def published(r):return next(o['packageId'] for o in r['objectChanges'] if o['type']=='published')
        # Pin the actual pre-rollout parent. main already contains SF-017;
        # using its merge base would no longer exercise old-package attacks.
        base = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', '6640cd6^'], text=True).strip()
        staging=root/'base';staging.mkdir()
        archive=subprocess.check_output(['git','-C',str(ROOT),'archive',base,'contracts'])
        subprocess.run(['tar','-x','-C',str(staging)],input=archive,check=True)
        package=root/'package';shutil.copytree(staging/'contracts',package,ignore=shutil.ignore_patterns('build','Published.toml'))
        common=['--build-env','testnet','--pubfile-path',root/'Pub.toml','--gas-budget','400000000','--silence-warnings']
        first=success(client('test-publish',package,*common));old=published(first);cap=created(first,'::package::UpgradeCap');registry=created(first,'::device_registry::DeviceRegistry')
        devices=[];caps=[]
        for i in range(2):
            r=tx('--move-call',f'{old}::device_registry::register_device',f'@{registry}',f'"canary-{i}"',f'@{owner}','"linux"','@0x6','--assign','device','--move-call',f'{old}::device_cap::issue_cap','device','--move-call',f'{old}::device_registry::activate_device',f'@{registry}','device','--assign','owner','--transfer-objects','[owner]',f'@{owner}')
            devices.append(created(r,'::device_registry::Device'));caps.append(created(r,'::device_cap::DeviceCap'))
        digest='"'+'a'*64+'"';recipe='"'+'b'*64+'"';members='vector['+','.join('@'+d for d in devices)+']'
        legacy=created(tx('--move-call',f'{old}::deployment_manager::create_deployment_verified','"legacy"','"1"','"blob"',digest,recipe,'vector[]',members,'@0x6'),'::deployment_manager::DeploymentRecord')
        shutil.rmtree(package);shutil.copytree(ROOT/'contracts',package,ignore=shutil.ignore_patterns('build','Published.toml'))
        head=success(client('test-upgrade',package,'--upgrade-capability',cap,*common));new=published(head)
        facts=dm.chain_facts(rpc,new,cap);origins=dm.origins(facts['objects'][dm.object_id(new)])
        assert origins['deployment_manager::RolloutKey']==dm.object_id(new)
        assert origins['deployment_manager::DeploymentRecord']==dm.object_id(old)
        # Pre-existing record still accepts an active identity through new API.
        tx('--move-call',f'{new}::deployment_manager::report_verified_status',f'@{legacy}',f'@{registry}',f'@{caps[0]}','1')
        record=created(tx('--move-call',f'{new}::deployment_manager::rollout_policy','2','2','100','100','2','5000','3600000','--assign','policy','--move-call',f'{new}::deployment_manager::new_rollout_verified','"app"','"1"','"blob"',digest,recipe,'vector[]',members,'vector[0u64,0u64]','policy','@0x6','--assign','rollout','--move-call',f'{new}::deployment_manager::share_deployment','rollout'),'::deployment_manager::DeploymentRecord')
        def read():return dm.rpc(rpc,'suix_getDynamicFieldObject',[record,{'type':f'{new}::deployment_manager::RolloutKey','value':{'dummy_field':False}}])['data']['content']['fields']['value']['fields']
        def report(pkg,i,status,ok=True):
            r=client('ptb','--move-call',f'{pkg}::deployment_manager::report_verified_status',f'@{record}',f'@{registry}',f'@{caps[i]}',str(status),'--gas-budget','100000000',allow_failure=not ok)
            if ok:success(r)
            else:
                assert r['effects']['status']['status']=='failure';evidence.append(r['digest'])
        report(old,0,1) # Still callable: ordinary receipts cannot settle grants.
        assert [int(n) for n in read()['outcomes']]==[0,0]
        report(new,0,1,False) # Current entry point rejects ungranted report.
        def advance(healthy='vector[]'):
            tx('--move-call',f'{new}::deployment_manager::advance_rollout',f'@{record}',members,healthy,'vector[]','@0x6')
        advance();assert all(int(n)>0 for n in read()['admitted_at'])
        advance();assert [int(n) for n in read()['outcomes']]==[0,0] # old Applied remains excluded after grant
        report(new,0,2);advance();assert int(read()['state'])==0 # minimum sample not met
        report(new,1,1);advance('vector[@'+devices[1]+']');assert int(read()['state'])==2
        # A fresh invocation observes durable abort. No coordinator memory.
        again=read();assert int(again['state'])==2 and [int(n) for n in again['outcomes']]==[2,1]
        tx('--move-call',f'{new}::deployment_manager::delete_rollout',f'@{record}','--move-call',f'{new}::deployment_manager::delete_deployment',f'@{legacy}')
        # Real daemon over indexed gRPC: new-origin rollout JSON, final grant
        # recheck, authenticated recipe/artifact and effects-checked status report.
        key=root/'device.key';key.write_text('07'*32);key.chmod(0o600)
        daemon=ROOT/'suifleet-edge/target/debug/suifleet'
        signer=subprocess.check_output([str(daemon),'--derive-address',str(key)],text=True).strip()
        with urlopen(Request('http://127.0.0.1:9123/v2/gas',json.dumps({'FixedAmountRequest':{'recipient':signer}}).encode(),{'Content-Type':'application/json'}),timeout=30) as response: assert response.status==200
        registered=tx('--move-call',f'{new}::device_registry::register_device',f'@{registry}','"daemon"',f'@{signer}','"linux"','@0x6','--assign','device','--move-call',f'{new}::device_cap::issue_cap','device','--move-call',f'{new}::device_registry::activate_device',f'@{registry}','device','--assign','owner','--transfer-objects','[owner]',f'@{owner}')
        device=created(registered,'::device_registry::Device');device_cap=created(registered,'::device_cap::DeviceCap')
        sim=Simulator();sim.add('0x999');recipe=sim.recipes.pop('recipe-0x999');recipe['componentName']='live-app';sim.recipes['rollout-recipe']=recipe
        server=serve(sim)
        try:
            aggregator=f'http://127.0.0.1:{server.server_port}'
            manifest=dm.resolve(SimpleNamespace(chain_identifier=facts['chain_identifier'],rpc=rpc,package=new,upgrade_cap=cap,registry=registry,network='localnet',publisher=aggregator,aggregator=aggregator))
            (root/'deployment.json').write_text(json.dumps(manifest))
            artifact_hash=json.dumps(hashlib.sha256(ARTIFACT).hexdigest());recipe_hash=json.dumps(hashlib.sha256(json.dumps(recipe).encode()).hexdigest())
            live=created(tx('--move-call',f'{new}::deployment_manager::rollout_policy','1','1','100','0','1','10000','3600000','--assign','policy','--move-call',f'{new}::deployment_manager::new_rollout_verified','"live-app"','"1.0.0"','"rollout-recipe"',artifact_hash,recipe_hash,'vector[]',f'vector[@{device}]','vector[0u64]','policy','@0x6','--assign','rollout','--move-call',f'{new}::deployment_manager::share_deployment','rollout'),'::deployment_manager::DeploymentRecord')
            applied=root/'applied';updater=root/'apply.py';updater.write_text(f"from pathlib import Path\nPath({str(applied)!r}).touch()\n")
            cfg=root/'config.toml';cfg.write_text('[sui]\nmanifest_path="deployment.json"\n'+f'[device]\nkeypair_path="{key}"\ndevice_object_id="{device}"\ndevice_cap_id="{device_cap}"\n[apply]\ncommand="python3 {updater}"\ntimeout_seconds=10\n[runtime]\nstate_dir="{root}/state"\nreplay_history=true\n')
            def state():
                path=root/'state/state.json';return json.loads(path.read_text()) if path.exists() else {}
            with (root/'daemon.log').open('w') as log:
                process=subprocess.Popen([str(daemon)],env={**os.environ,'EDGE_CONFIG':str(cfg)},stdout=log,stderr=log)
                try:
                    wait_for(lambda:state().get('deferred',{}).get(live,{}).get('reason')=='waiting for rollout eligibility','real-chain ungranted admission not deferred',process)
                    assert not applied.exists()
                    tx('--move-call',f'{new}::deployment_manager::advance_rollout',f'@{live}',f'vector[@{device}]','vector[]','vector[]','@0x6')
                    wait_for(lambda:state().get('deployments',{}).get(live,{}).get('reported'),'real-chain grant did not apply/report',process,timeout=100)
                    assert applied.exists() and state()['deployments'][live]['status']==1
                    def live_outcomes():
                        field=dm.rpc(rpc,'suix_getDynamicFieldObject',[live,{'type':f'{new}::deployment_manager::RolloutKey','value':{'dummy_field':False}}])
                        return [int(n) for n in field['data']['content']['fields']['value']['fields']['outcomes']]
                    wait_for(lambda:live_outcomes()==[1], 'live rollout outcome not settled: '+str(live_outcomes()), process)
                    receipt=dm.rpc(rpc,'suix_getDynamicFieldObject',[live,{'type':f"{origins['deployment_manager::ReceiptsKey']}::deployment_manager::ReceiptsKey",'value':{'dummy_field':False}}])
                    previous=dm.rpc(rpc,'sui_getObject',[receipt['data']['objectId'],{'showPreviousTransaction':True}])['data']['previousTransaction']
                    assert dm.rpc(rpc,'sui_getTransactionBlock',[previous,{'showEffects':True}])['effects']['status']['status']=='success'
                    evidence.append(previous)
                finally:
                    process.terminate();process.wait(timeout=10)
            tx('--move-call',f'{new}::deployment_manager::delete_rollout',f'@{live}')
        finally:
            server.shutdown()
        print(json.dumps({'result':'PASS','base':base,'old_package':old,'new_package':new,'record_origin':origins['deployment_manager::DeploymentRecord'],'rollout_origin':origins['deployment_manager::RolloutKey'],'digests':evidence,'checks':['compatible upgrade','existing device/cap/deployment','old-package pre-grant receipt ignored before/after grant','ungranted report rejected','minimum sample','inclusive threshold abort','durable state','real gRPC daemon ungranted/granted apply/report with independent effects','cleanup']}))

if __name__=='__main__':run()
