#!/usr/bin/env python3
"""Bounded SF139 testnet fixture using existing fleet storage and the shared codec.
Requires an explicit isolated fixture, dedicated wallet and external mutation lease.
Never use customer objects. Cache exact source package/publication and every effect.
"""
import argparse,base64,hashlib,json,os,shutil,subprocess
from pathlib import Path
from live_common import Live,ROOT,CLOCK
from live_fleet_budget import publish_storage,record_publication,event,net_gas,SYSTEM,WALRUS_PACKAGE,u8_vector,unrelocate_module
from live_fleet_data import SERVERS
import deployment_manifest as dm
import sui_toolchain as tc

def run(args):
    assert args.read_only or os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'),'mutation lease required'
    os.umask(0o077);directory=args.directory.resolve();directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    evidence=directory/'evidence.json';prior=json.loads(evidence.read_text()) if evidence.exists() else None
    if args.read_only:assert prior and prior.get('result')=='PASS' and 'package' in prior,'read-only requires an existing successful fixture'
    class WalletLive(Live):
        # Keep the explicitly selected client.yaml, whose keystore is container-mapped.
        def client(self,*command,json_output=True,check=True,config=None):
            assert config is None
            wallet=self.wallet.parent
            full=['docker','run','--rm','--user',f'{os.getuid()}:{os.getgid()}',
                '-v',f'{directory}/wallet-profile:{wallet}','-v',f'{wallet}:/w/agent:ro','-v',f'{directory}:{directory}',
                '-v',f'{Path.home()/".move"}:/home/dev/.move','--entrypoint','/opt/sui-network/sui',args.image,
                'client','--client.config',str(self.wallet),*map(str,command)]
            if json_output:full.append('--json')
            result=subprocess.run(full,capture_output=True,text=True,timeout=180)
            if check and result.returncode:raise RuntimeError('explicit testnet CLI '+str(command[0])+' failed: '+result.stderr[-1000:]+result.stdout[-1000:])
            if not json_output or result.returncode:return result
            out=result.stdout;return json.loads(out[min(i for i in (out.find('{'),out.find('[')) if i>=0):])
    image_id=subprocess.check_output(['docker','image','inspect',args.image,'--format','{{.Id}}'],text=True).strip()
    binary_hash=subprocess.check_output(['docker','run','--rm','--entrypoint','sha256sum',image_id,'/opt/sui-network/sui'],text=True).split()[0]
    assert binary_hash==tc.NETWORK['binary_sha256'],'container transaction CLI checksum mismatch'
    args.image=image_id
    profile=directory/'wallet-profile';profile.mkdir(mode=0o700,exist_ok=True)
    if not (profile/'client.yaml').exists():shutil.copyfile(args.wallet,profile/'client.yaml')
    live=WalletLive(args.wallet,'testnet',args.max_spend_mist,build_sui=args.build_sui,network_sui=args.network_sui)
    live.preflight(auto_fund=False)
    if prior:
        assert prior['chain']==live.chain and prior['operator']==live.operator
        live.evidence=prior;live.ledger.evidence=prior['transactions'];live.ledger.used=prior['allowance_used_mist']
    fixture=json.loads(args.fixture.read_text());objects=fixture['objects'];base=fixture['fixture']
    budget=objects['budget'];cap=objects['budget_cap'];account=objects['account'];storage=objects['finite_storage'];devicecap=objects['device_cap']
    device=fixture['measured']['finite_register_0']['upload']['device_id'];storagepkg=base['storage_package'];registry=base['registry']
    assert live.owner(cap)==live.operator and live.owner(devicecap)==live.operator,'fixture authority mismatch'
    def save():
        live.evidence.update(allowance_used_mist=live.ledger.used,chain=live.chain,operator=live.operator)
        evidence.write_text(json.dumps(live.evidence,indent=2)+'\n')
    def renew():
        if args.read_only:
            subprocess.run(['ticket','--project','suifleet','heartbeat','SF-139','--token',os.environ['PR_COORDINATOR_CLAIM_TOKEN']],check=True,stdout=subprocess.DEVNULL)
            return
        for operation in [('lock-renew','network:suifleet-testnet','--lock-token',os.environ['SUIFLEET_TESTNET_LOCK_TOKEN']),('heartbeat','SF-139','--token',os.environ['PR_COORDINATOR_CLAIM_TOKEN'])]:
            subprocess.run(['ticket','--project','suifleet',*operation],check=True,stdout=subprocess.DEVNULL)
        live.assert_lock()
    def tx(label,*ptb,budget=20_000_000):
        renew()
        for old in live.evidence['transactions']:
            if old['label']==label:
                if old['status']=='success':return live.executed(old['digest'])
                if old['status']!='not_submitted':raise RuntimeError('reconcile previous failed/unknown effect before retry')
        try:return live.transaction(label,*ptb,budget=budget)
        finally:save()
    def node(operation,params):
        renew();file=directory/'request.json';file.write_text(json.dumps({**params,'operation':operation}))
        with (directory/(operation+'.log')).open('w') as error:
            process=subprocess.Popen([args.node,'scripts/live_opaque_runtime.cjs',str(file)],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=error,text=True)
            answer=None
            try:
                for line in process.stdout:
                    value=json.loads(line)
                    if 'personal_message' in value:
                        request=dict(wallet=str(live.wallet),keystore=str(Path(args.wallet).parent/'sui.keystore'),expected_address=live.operator,namespace=params['config']['typeOrigin'],message=value['personal_message'],fleet=params['config']['fleetId'],device=device)
                        signed=subprocess.run([str(args.signer)],input=json.dumps(request),capture_output=True,text=True,timeout=30)
                        if signed.returncode:raise RuntimeError('synthetic fixture signer refused')
                        process.stdin.write(signed.stdout);process.stdin.flush()
                    else:answer=value
                if process.wait(timeout=30):raise RuntimeError('opaque SDK '+operation+' failed; private diagnostic retained')
                assert answer;return answer
            finally:
                if process.poll() is None:process.kill();process.wait()
    source=(ROOT/'contracts/fleet_data/sources/fleet_data.move').read_bytes();source_hash=hashlib.sha256(source).hexdigest()
    live.evidence.setdefault('source_sha256',source_hash);assert live.evidence['source_sha256']==source_hash,'source changed; preserve fixture and reconcile before new run'
    try:
        if 'package' not in live.evidence:
            tree=directory/'contracts'
            if not tree.exists():shutil.copytree(args.dependency_tree,tree,ignore=shutil.ignore_patterns('build'))
            data_tree=tree/'fleet_data';(data_tree/'sources/fleet_data.move').write_bytes(source)
            # Verify all reused immutable dependency bytecode before publication.
            for path,package in [(tree,base['core']),(tree/'fleet_storage',storagepkg)]:
                built=tc.compile_package(live.build_sui,path)
                raw=live.client('object',package,'--bcs')['data']['Package']
                actual=sorted(unrelocate_module(bytes(v),package) for v in raw['module_map'].values())
                assert actual==sorted(base64.b64decode(v) for v in built['modules']),'reused dependency bytecode differs'
            renew()
            try:
                result=publish_storage(live,data_tree,'SF139 publish isolated scoped-data fixture')
                live.evidence.update(package=live.published(result),upgrade_cap=live.created(result,'::package::UpgradeCap')[0])
            finally:save()
        package=live.evidence['package']
        built=tc.compile_package(live.build_sui,directory/'contracts/fleet_data')
        raw=live.client('object',package,'--bcs')['data']['Package']
        assert sorted(unrelocate_module(bytes(v),package) for v in raw['module_map'].values())==sorted(base64.b64decode(v) for v in built['modules']),'cached fixture package bytecode differs'
        live.evidence['verified_source_digest']=bytes(built['digest']).hex()
        assert live.owner(live.evidence['upgrade_cap'])==live.operator
        facts=dm.chain_facts(live.rpc_url,live.evidence['upgrade_cap'])
        assert dm.upgrade_cap(facts['objects'][dm.object_id(live.evidence['upgrade_cap'])])[:2]==(package,'1')
        if args.read_only:
            assert prior and prior.get('result')=='PASS'
            params=json.loads((directory/'request.json').read_text())
            assert params['config']==prior['config'] and params['device']==device and params['signer']==live.operator
            live.evidence['read_only_revalidation']=node('read-denied',params)
            save();print('PASS read-only final scoped reader/index/bytecode revalidation; no chain mutation');return
        call=lambda fn:f'{package}::fleet_data::{fn}'
        r=tx('SF139 create isolated data fleet','--move-call',call('create'),f'@{budget}',f'@{cap}',f'@{storage}',f'"{live.chain}"');fleet=live.created(r,'::fleet_data::FleetData')[0]
        r=tx('SF139 enroll writer and legacy support','--move-call',call('enroll'),f'@{fleet}',f'@{budget}',f'@{cap}',f'@{account}','--move-call',call('set_support'),f'@{fleet}',f'@{budget}',f'@{cap}',f'@{live.operator}','true');writer=live.created(r,'::fleet_data::DeviceData')[0]
        cfg=dict(schema=1,chainIdentifier=live.chain,registryId=registry,packageId=package,typeOrigin=package,fleetId=fleet,budgetId=budget,storageId=storage)
        manifest=json.loads((ROOT/'manifests/fixture.localnet.json').read_text());manifest.update(network='testnet',chain_identifier=live.chain,rpc_url=live.rpc_url,call_package_id=base['core'],upgrade_cap_id='0x400dda2c3b0788a08841c0a81a4bdc444c6e399268d60f352eb37f04066d0983',walrus={'publisher_url':'https://publisher.walrus-testnet.walrus.space','aggregator_url':'https://aggregator.walrus-testnet.walrus.space'})
        manifest['shared_objects']['registry']['id']=registry;manifest['type_origins']={k:base['core'] for k in manifest['type_origins']}
        params=dict(manifest=manifest,config=cfg,device=device,signer=live.operator,servers=SERVERS,budgetCap=cap,directory=str(directory))
        prepared_file=directory/'prepared.json'
        if prepared_file.exists():prepared=json.loads(prepared_file.read_text())
        else:prepared=node('prepare',params)['prepared'];prepared_file.write_text(json.dumps(prepared))
        request=dict(chain=live.chain,rpc_url=live.rpc_url,ciphertext_path=str(directory/'ciphertext.bin'),items=prepared['items'],system_id=SYSTEM,walrus_package=WALRUS_PACKAGE,storage_package=storagepkg,storage_type_origin=storagepkg,storage_id=storage,device_id=device)
        def codec(operation,extra=None):
            renew();r=subprocess.run([args.node,'dapp/scripts/walrus-device-codec.mjs'],cwd=ROOT,input=json.dumps({**request,'operation':operation,**(extra or {})})+'\n',capture_output=True,text=True,timeout=310)
            if r.returncode:raise RuntimeError('shared codec '+operation+' failed: '+r.stderr[-500:])
            return json.loads(r.stdout)
        enc=codec('prepare');live.evidence['encoding']=enc;save()
        r=tx('SF139 register opaque batch','--move-call',f'{storagepkg}::fleet_storage::register_blob',f'@{storage}',f'@{budget}',f'@{account}',f'@{registry}',f'@{devicecap}',f'@{SYSTEM}',f'{enc["blob_id"]}u256',f'{enc["root_hash"]}u256',f'{enc["size"]}u64','1u8',CLOCK,budget=40_000_000)
        blob=event(r,'::fleet_storage::UploadRegistered')['blob_object_id']
        cert_file=directory/'certificate.json'
        if cert_file.exists():cert=json.loads(cert_file.read_text())
        else:cert=codec('write',dict(blob_id=enc['blob_id'],root_hash=enc['root_hash'],blob_object_id=blob));cert_file.write_text(json.dumps(cert))
        tx('SF139 certify opaque batch','--move-call',f'{storagepkg}::fleet_storage::certify_blob',f'@{storage}',f'@{SYSTEM}',f'@{blob}',u8_vector(base64.b64decode(cert['signature']).hex()),u8_vector(base64.b64decode(cert['bitmap']).hex()),u8_vector(base64.b64decode(cert['message']).hex()),budget=10_000_000)
        vector=lambda bytes_:'vector['+','.join(str(b)+'u8' for b in bytes_)+']'
        indexes='vector['+','.join(vector(bytes.fromhex(s)) for s in prepared['indexes'])+']'
        anchor=tx('SF139 anchor two independent items','--move-call',call('anchor_items'),f'@{fleet}',f'@{writer}',f'@{budget}',f'@{account}',f'@{registry}',f'@{devicecap}',f'@{storage}',f'@{blob}',f'{prepared["seq"]}u64','0u8',vector(bytes.fromhex(prepared['ciphertext_sha256'])),indexes,CLOCK,budget=20_000_000)
        stream=hashlib.sha256(b'public-binary').digest()
        tx('SF139 grant binary stream only','--move-call',call('set_read_grant'),f'@{fleet}',f'@{budget}',f'@{cap}',f'@{device}',vector(stream),f'@{live.operator}','true',budget=10_000_000)
        read=node('read',params)
        tx('SF139 revoke binary stream','--move-call',call('set_read_grant'),f'@{fleet}',f'@{budget}',f'@{cap}',f'@{device}',vector(stream),f'@{live.operator}','false',budget=10_000_000)
        denied=node('read-denied',params)
        live.evidence.update(result='PASS',config=cfg,writer=writer,device=device,blob=blob,readback=read,revoked=denied,
            anchor_gas=anchor['effects']['gasUsed'],anchor_net_gas_mist=net_gas(anchor),item_index_bcs_bytes=[len(bytes.fromhex(s))+8 for s in prepared['indexes']])
        # Read every effects record independently, including reused checkpoints.
        for entry in live.evidence['transactions']:
            if entry.get('digest'):
                result=live.executed(entry['digest']);assert result['effects']['status']['status']=='success'
        save();print('PASS SF139 bounded public-testnet opaque roundtrip, scoped stream refusal/revocation; charged allowance',live.ledger.used)
    finally:save()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--read-only',action='store_true');p.add_argument('--wallet',required=True);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--dependency-tree',type=Path,required=True);p.add_argument('--directory',type=Path,required=True);p.add_argument('--node',required=True);p.add_argument('--signer',type=Path,required=True);p.add_argument('--build-sui',required=True);p.add_argument('--network-sui',required=True);p.add_argument('--image',default='suifleet-dev:sf037');p.add_argument('--max-spend-mist',type=int,default=500_000_000);run(p.parse_args())
