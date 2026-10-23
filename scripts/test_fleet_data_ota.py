#!/usr/bin/env python3
"""Real daemon -> gRPC simulator -> real browser verifier, without an indexer.

This is simulator evidence. Move unit tests and isolated/public-chain evidence
remain separate; the simulator never pretends to verify a Walrus certificate.
"""
import argparse, base64, copy, hashlib, json, os, signal, struct, subprocess, sys, tempfile, threading, time
from contextlib import contextmanager
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import test_budget_ota as budget
import test_ota as ota
from deployment_manifest import object_id
FLEET, WRITER, STORAGE, PACKAGE = '0xf1','0xf2','0xf3','0xf0'
def addr(v): return bytes.fromhex(object_id(v)[2:])
def u64(v): return struct.pack('<Q',v)
def vec(v):
    n=len(v); out=bytearray()
    while n>=128: out.append((n&127)|128);n>>=7
    return bytes(out)+bytes([n])+v
def unvec(v):
    n=shift=offset=0
    for b in v:
        offset+=1;n|=(b&127)<<shift
        if b<128: break
        shift+=7
    assert len(v)==n+offset
    return v[offset:]
def raw(id,kind,fields,contents):
    return {'data':{'objectId':id,'owner':{'Shared':{'initial_shared_version':'1'}},'content':{'type':kind,'fields':fields},
        'bcs':{'dataType':'moveObject','type':kind,'bcsBytes':contents.hex()}}}
class Simulator(budget.BudgetSimulator):
    def __init__(self,compact=False):
        self.compact=compact;self.heads=[]
        super().__init__();self.gas=100_000_000;self.seq=0;self.at=0;self.message=b'';self.signature=b'';self.clock=int(time.time()*1000);self.health=[];self.fail_once=True;self.batchseq=0;self.batch_at=None;self.emergency_at=None;self.anchors=[];self.registered={};self.certified=set();self.tx_results={};self.uploads_paused=True;self.certify_outage=True;self.request_seq=0;self.upload_dir=None;self.key_objects={};self.gas_version=1;self.register_reply_outage=True;self.history_pruned=False;self.status_reads={};self.stale_certificate_rejections=0
    def fleet(self):
        return dict(id=object_id(FLEET),version='1',budget_id=object_id(budget.BUDGET),storage_id=object_id(STORAGE),registry_id=object_id(ota.REGISTRY),
            chain_identifier=self.chain,key_epoch='0',support=[],health_interval_ms='60000',batch_interval_ms='86400000',emergency_interval_ms='60000',device_count='1')
    def writer(self):
        return dict(id=object_id(WRITER),version='1',fleet_id=object_id(FLEET),device_id=object_id(ota.DEVICE),account_id=object_id(budget.ACCOUNT),
            health_seq=str(self.seq),health_at_ms=str(self.at),message=list(self.message),signature=list(self.signature),batch_seq=str(self.batchseq),batch_at_ms=None if self.batch_at is None else str(self.batch_at),emergency_at_ms=None if self.emergency_at is None else str(self.emergency_at),anchor_count=str(len(self.anchors)))
    def obj(self,id):
        if id==PACKAGE:return {'data':{'version':'2','bcs':{'dataType':'package','typeOriginTable':[{'module_name':'fleet_data','datatype_name':name,'package':origin} for name,origin in [('DiagnosticKey','0xf8'),('CompactHeadKey','0xf9'),('CompactHead','0xf9')]]}}}
        if id==FLEET:
            b=addr(id)+u64(1)+addr(budget.BUDGET)+addr(STORAGE)+addr(ota.REGISTRY)+vec(self.chain.encode())+u64(0)+b'\0'+u64(60000)+u64(86400000)+u64(60000)+u64(1)
            return raw(id,f'{PACKAGE}::fleet_data::FleetData',self.fleet(),b)
        if id==WRITER:
            b=addr(id)+u64(1)+addr(FLEET)+addr(ota.DEVICE)+addr(budget.ACCOUNT)+u64(self.seq)+u64(self.at)+vec(self.message)+vec(self.signature)+u64(self.batchseq)+(b'\0' if self.batch_at is None else b'\1'+u64(self.batch_at))+(b'\0' if self.emergency_at is None else b'\1'+u64(self.emergency_at))+u64(len(self.anchors))
            return raw(id,f'{PACKAGE}::fleet_data::DeviceData',self.writer(),b)
        if id=='0x6':return raw(id,'0x2::clock::Clock',{'timestamp_ms':str(self.clock)},addr(id)+u64(self.clock))
        if id in self.key_objects:return self.key_objects[id]
        return super().obj(id)
    def rpc(self,method,params):
        if method=='shim_height':return {'height':1 if self.history_pruned else 0,'lowest':1 if self.history_pruned else 0}
        if method=='shim_fields':return super().rpc(method,params)+[[FLEET,f'{PACKAGE}::fleet_data::DeviceKey',addr(ota.DEVICE).hex()],
            [WRITER,'0xf8::fleet_data::DiagnosticKey','00']]+([[WRITER,'0xf9::fleet_data::CompactHeadKey','00']] if self.compact else [])+[[WRITER,f'{PACKAGE}::fleet_data::AnchorKey',u64(i).hex()] for i in range(len(self.anchors))]
        if method=='suix_getDynamicFieldObject' and params[0]==WRITER:
            if params[1]['type'].endswith('::CompactHeadKey'):
                return {'data':{'content':{'fields':{'value':{'bcs_hex':self.heads[-1].hex()} if self.heads else None}}}}
            value=({'seq':str(self.request_seq),'at_ms':str(self.clock)} if self.request_seq else None) if params[1]['type'].endswith('::DiagnosticKey') else self.anchors[struct.unpack('<Q',bytes.fromhex(params[1]['value']))[0]]
            return {'data':{'content':{'fields':{'value':value}}}}
        if method=='test_blob':return self.registered[params[0]]
        if method=='shim_fleet_gas_version':return self.gas_version
        if method=='shim_fleet_transaction':
            digest=params[0];self.status_reads[digest]=self.status_reads.get(digest,0)+1
            # A registration commits between the first effects lookup and gas
            # reconciliation. A second effects read must find it, never buy twice.
            if digest in self.registered and self.status_reads[digest]==1:return None
            return self.tx_results.get(digest)

        if method=='suix_getDynamicFieldObject' and params[0]==FLEET:return {'data':{'content':{'fields':{'value':object_id(WRITER)}}}}
        if method=='unsafe_moveCall' and params[2] in ['fleet_data','fleet_storage'] and params[3]!='report_health':
            if self.uploads_paused:raise ConnectionError('prepaid storage unavailable')
            if params[3]=='certify_blob' and unvec(base64.b64decode(params[5][3]['bcs']))[0]==1:
                self.stale_certificate_rejections+=1
                raise ConnectionError('certificate lacks stake before signing; refresh same blob')
            return {}
        if method=='unsafe_moveCall' and params[2]=='fleet_data':
            assert params[1]==object_id(PACKAGE) and params[3]=='report_health'
            assert params[5][:6]==[FLEET,WRITER,budget.BUDGET,budget.ACCOUNT,ota.REGISTRY,ota.CAP]
            if self.identity['revoked']:raise ConnectionError('revoked')
            if self.seq and self.clock<self.at+60000:raise ConnectionError('cadence')
            return {}
        if method=='shim_fleet_execute':
            module,fn,args,sender,digest,gas_version=params
            assert sender==ota.ADDRESS
            if digest in self.tx_results:return self.tx_results[digest]
            assert gas_version==self.gas_version,"stale owned gas must be reconciled before resubmission"
            if fn!='report_health':
                if fn=='register_blob':
                    assert not self.uploads_paused
                    assert args[:6]==[STORAGE,budget.BUDGET,budget.ACCOUNT,ota.REGISTRY,ota.CAP,'0xf4']
                    self.registered[digest]={'blob_object_id':'0x'+hashlib.sha256(digest.encode()).hexdigest(),'end_epoch':100,'encoded_size':'66034000','write_fee':'328860'}
                elif fn=='certify_blob':
                    if self.certify_outage:raise ConnectionError('certification transport outage')
                    blob='0x'+base64.b64decode(args[2]['bcs']).hex();self.certified.add(blob)
                elif fn in ['anchor','anchor_compact']:
                    blob='0x'+base64.b64decode(args[7]['bcs']).hex();assert blob in self.certified
                    seq=struct.unpack('<Q',base64.b64decode(args[8]['bcs']))[0];kind=base64.b64decode(args[9]['bcs'])[0]
                    sha=unvec(base64.b64decode(args[10]['bcs']));assert sha==hashlib.sha256((self.upload_dir/(blob+'.bin')).read_bytes()).digest()
                    assert seq>self.batchseq
                    last=self.batch_at if kind==0 else self.emergency_at
                    assert last is None or self.clock>=last+(86400000 if kind==0 else 60000)
                    self.batchseq=seq
                    if kind==0:self.batch_at=self.clock
                    else:self.emergency_at=self.clock
                    self.anchors.append(dict(seq=str(seq),kind=kind,blob_object_id=blob,blob_id='2',ciphertext_sha256=list(sha),signer=ota.ADDRESS,at_ms=str(self.clock),end_epoch=100,key_epoch='0'))
                    if fn=='anchor_compact':
                        previous=hashlib.sha256(self.heads[-1]).digest() if self.heads else b''
                        assert unvec(base64.b64decode(args[11]['bcs']))==previous
                        value=u64(seq)+bytes([kind])+addr(blob)+(2).to_bytes(32,'little')+vec(sha)+addr(ota.ADDRESS)+u64(self.clock)+struct.pack('<I',100)+u64(0)
                        self.heads.append(u64(0)+u64(len(self.anchors)-1)+value+vec(previous))
                else:raise AssertionError('unrecognized archive write')
                result={'digest':digest,'effects':{'status':{'status':'success'}}};self.tx_results[digest]=result;self.gas_version+=1
                if fn=='register_blob' and self.register_reply_outage:self.register_reply_outage=False;raise ConnectionError('lost response after committed registration')
                return result
            assert module=='fleet_data' and fn=='report_health'
            if self.fail_once:self.fail_once=False;raise ConnectionError('transient submit outage')
            assert not self.identity['revoked']
            seq=struct.unpack('<Q',base64.b64decode(args[6]['bcs']))[0]
            assert seq>self.seq and (not self.seq or self.clock>=self.at+60000)
            self.seq=seq;self.at=self.clock;self.message=unvec(base64.b64decode(args[7]['bcs']));self.signature=unvec(base64.b64decode(args[8]['bcs']))
            assert len(self.message)<=16384 and len(self.signature)==97
            self.health.append(digest);self.gas_version+=1
            return {'digest':digest,'effects':{'status':{'status':'success'}}}
        return super().rpc(method,params)
def wait(predicate,seconds=20):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        if predicate():return
        time.sleep(.1)
    raise AssertionError('daemon condition timed out')
@contextmanager
def stopped_workspace(stop):
    # Stop the daemon before removing files it may still be flushing on failure.
    with tempfile.TemporaryDirectory(prefix='sf082-producer-') as temp:
        try:yield temp
        finally:stop()

def run(binary,shim_binary,compact=False):
    ota.SHIM=shim_binary;sim=Simulator(compact)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            r=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:answer={'result':sim.rpc(r['method'],r['params'])}
            except Exception as e:
                if not isinstance(e,ConnectionError):sim.errors.append(repr(e))
                answer={'error':{'code':-32000,'message':str(e)}}
            b=json.dumps({'jsonrpc':'2.0','id':r['id'],**answer}).encode();self.send_response(200);self.send_header('Content-Length',str(len(b)));self.end_headers();
            try:self.wfile.write(b)
            except (BrokenPipeError,ConnectionResetError):pass  # cancelled loopback request during daemon shutdown
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    shim,rpc=ota.start_grpc(f'http://127.0.0.1:{server.server_port}');process=None
    def cleanup():
        nonlocal process
        if process:
            if process.poll() is None:process.send_signal(signal.SIGINT)
            process.wait(timeout=10);process=None
    try:
        with stopped_workspace(cleanup) as temp:
            root=Path(temp);key=root/'device.key';key.write_text('07'*32);key.chmod(0o600)
            manifest=dict(schema_version=1,protocol_version=1,network='localnet',chain_identifier=sim.chain,rpc_url=rpc,call_package_id=ota.PACKAGE,upgrade_cap_id=ota.UPGRADE_CAP,package_version='3',type_origins=ota.TYPE_ORIGINS,
                shared_objects={'registry':{'id':ota.REGISTRY,'initial_shared_version':'1'}},supported_clients={'dapp':{'min':'0.0.1','max_exclusive':'0.3.0'},'edge':{'min':'0.1.0','max_exclusive':'0.3.0'}},walrus={'publisher_url':rpc,'aggregator_url':rpc})
            (root/'deployment.json').write_text(json.dumps(manifest));config=root/'config.toml'
            config.write_text(f'[sui]\nmanifest_path="deployment.json"\n[device]\nkeypair_path={json.dumps(str(key))}\ndevice_object_id="{ota.DEVICE}"\ndevice_cap_id="{ota.CAP}"\n[walrus]\naggregator_url="{rpc}"\n[apply]\ncommand="true"\n[runtime]\nstate_dir={json.dumps(str(root/"state"))}\npoll_seconds=1\n[fleet_data]\npackage_id="{PACKAGE}"\ntype_origin="{PACKAGE}"\nfleet_id="{FLEET}"\nbudget_id="{budget.BUDGET}"\nstorage_id="{STORAGE}"\nwriter_id="{WRITER}"\naccount_id="{budget.ACCOUNT}"\n')
            vectors=json.loads(Path('interfaces/seal-vectors.json').read_text());sim.upload_dir=root/'uploaded';sim.upload_dir.mkdir()
            for server_key in vectors['servers']:
                id=server_key['object_id'];content=addr(id)+u64(1)+u64(1)
                sim.key_objects[id]=raw(id,'0xf5::key_server::KeyServer',{},content)
                field=hashlib.blake2b(b'\xf0'+addr(id)+u64(8)+u64(1)+b'\x02',digest_size=32).digest();field_id='0x'+field.hex()
                contents=field+u64(1)+vec(b'fixture')+vec(b'https://fixture.invalid')+b'\0'+vec(bytes.fromhex(server_key['public_key']))
                sim.key_objects[field_id]=raw(field_id,'0x2::dynamic_field::Field<u64,0xf5::key_server::KeyServerV1>',{},contents)
            codec=root/'codec.py'
            codec.write_text("import sys,json,pathlib,urllib.request,base64,shutil,time\nr=json.loads(sys.stdin.readline())\n"
                "if r['operation']=='prepare': time.sleep(6); print(json.dumps({'blob_id':'2','root_hash':'1'}))\n"
                "elif r['operation']=='registered':\n"
                f" q=urllib.request.Request('http://127.0.0.1:{server.server_port}',json.dumps({{'jsonrpc':'2.0','id':1,'method':'test_blob','params':[r['digest']]}}).encode(),{{'Content-Type':'application/json'}})\n"
                " with urllib.request.urlopen(q) as response: print(json.dumps(json.load(response)['result']))\n"
                "elif r['operation']=='write':\n"
                f" shutil.copyfile(r['ciphertext_path'],str(pathlib.Path({str(sim.upload_dir)!r})/(r['blob_object_id']+'.bin')))\n"
                " p=pathlib.Path(r['ciphertext_path']).parent/'write-count'; n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))\n"
                " print(json.dumps({'signature':base64.b64encode(bytes([1 if n==1 else 2])+bytes(95)).decode(),'bitmap':'AQ==','message':'Zml4dHVyZQ=='}))\n")
            archive_config=(f'[components]\nconfiguration_overlay_dir={json.dumps(str(root/"state"/"overlays"))}\n[logs]\ninterval_seconds=10\nmax_lines_per_interval=128\nbuffer_max=16\n'
                f'[fleet_data.archives]\nstorage_package="0xf6"\nstorage_type_origin="0xf6"\nsystem_id="0xf4"\nwalrus_package="0xf7"\nkey_server_package="0xf5"\n'
                f'key_servers = '+json.dumps([{'object_id':s['object_id'],'weight':1} for s in vectors['servers']]).replace(': ', ' = ')+ '\n'
                f'threshold=2\ncompact_history={str(compact).lower()}\nnode={json.dumps(sys.executable)}\ncodec={json.dumps(str(codec))}\ncodec_sha256="{hashlib.sha256(codec.read_bytes()).hexdigest()}"\n')
            # Add source logs only after the startup reconciliation created its
            # state; the collection path is the shipped SF-020 redactor.
            with (root/'daemon.log').open('w') as log:
                def start():return subprocess.Popen([str(binary)],env={**os.environ,'EDGE_CONFIG':str(config)},stdout=log,stderr=log)
                def stop():
                    nonlocal process
                    process.send_signal(signal.SIGINT);process.wait(timeout=10);assert process.returncode==0;process=None
                process=start();wait(lambda:sim.seq==1);assert len(sim.health)==1 and not sim.fail_once
                time.sleep(2);assert len(sim.health)==1,'local polls must not reset chain cadence'
                stop();process=start();time.sleep(3);assert len(sim.health)==1,'restart must not reset sequence/cadence'
                sim.clock+=60000;wait(lambda:sim.seq==2)
                sim.identity['revoked']=True;sim.clock+=60000;time.sleep(3);assert sim.seq==2,'revoked device must not submit'
                stop();sim.identity['revoked']=False
                (root/'state'/'supervised'/'mqtt').mkdir(parents=True,exist_ok=True)
                (root/'state'/'overlays').mkdir(exist_ok=True)
                (root/'state'/'overlays'/'mqtt.json').write_text(json.dumps({'/password':'fixture-confidential-value'}))
                (root/'state'/'supervised'/'mqtt'/'component.log').write_text('login fixture-confidential-value succeeded\n')
                # SF-084: device shadows (the store's own durable file format)
                # travel only in the encrypted archive, never in public health.
                def shadow(name,version,document):
                    (root/'state'/'shadows').mkdir(parents=True,exist_ok=True)
                    (root/'state'/'shadows'/f's-{name.encode().hex()}.json').write_text(json.dumps({'name':name,'version':version,'document':document,'updated_ms':1000+version}))
                shadow('',2,{'desired':{'mode':'eco','fan':{'speed':2}},'reported':{'mode':'eco','fan':{'speed':1},'secret_key':'shadow-private-value'}})
                shadow('cfg',5,{'reported':{'firmware':'3.1'}})
                with config.open('a') as out:out.write(archive_config+'[shadows]\nthing_name="fixture"\n')
                process=start();wait(lambda:(root/'state'/'fleet-data'/'pending.json').exists())
                assert not sim.registered,'exhausted prepaid storage must not purchase'
                stop();sim.uploads_paused=False;process=start()
                wait(lambda:len(sim.registered)==1);wait(lambda:(root/'state'/'fleet-data'/'certify.bcs').exists())
                stop();sim.certify_outage=False;sim.clock+=60000;sim.history_pruned=True;process=start()
                time.sleep(3);assert not sim.anchors and len(sim.registered)==1,'pruned/unknown outcomes must retain the purchase reservation'
                sim.history_pruned=False;wait(lambda:len(sim.anchors)==1)
                wait(lambda:not (root/'state'/'fleet-data'/'pending.json').exists())
                assert len(sim.registered)==1,'restart must resume the existing purchase'
                assert sim.stale_certificate_rejections>=1,'rejected pre-sign certificate must be refreshed without a second registration'
                time.sleep(2);assert len(sim.anchors)==1,'periodic batch cadence persists'
                sim.request_seq=1;wait(lambda:len(sim.anchors)==2);assert sim.anchors[-1]['kind']==1
                wait(lambda:not (root/'state'/'fleet-data'/'pending.json').exists())
                time.sleep(2);assert len(sim.anchors)==2,'acknowledged diagnostics must not repeat'
                (root/'state'/'supervised'/'active.json').write_text(json.dumps({'fixture-crash':{'name':'fixture-crash','version':'1','deployment':'0xa1','run':'true','shutdown':None,'readiness':None,'restart':'on-failure'}}))
                (root/'state'/'supervised'/'status.json').write_text(json.dumps({'fixture-crash':{'version':'1','deployment':'0xa1','phase':'broken','pid':None,'restarts':3,'last_exit':'exit status: 1','since':1}}))
                time.sleep(2);assert len(sim.anchors)==2,'crashes share emergency cadence with diagnostics'
                sim.clock+=60000;wait(lambda:len(sim.anchors)==3);assert sim.anchors[-1]['kind']==2
                wait(lambda:not (root/'state'/'fleet-data'/'pending.json').exists())
                time.sleep(2);assert len(sim.anchors)==3,'one crash fingerprint must not reupload every poll'
                # A shadow change archives on the routine cadence, even without logs.
                stop();shadow('',3,{'desired':{'mode':'eco','fan':{'speed':2}},'reported':{'mode':'eco','fan':{'speed':2}}})
                process=start();time.sleep(3);assert len(sim.anchors)==3,'routine cadence applies to shadow changes'
                sim.clock+=86400000;wait(lambda:len(sim.anchors)==4);assert sim.anchors[-1]['kind']==0
                wait(lambda:not (root/'state'/'fleet-data'/'pending.json').exists())
                sim.clock+=86400000;time.sleep(3);assert len(sim.anchors)==4,'unchanged shadows must not reupload'
                stop()
                # Public health carries shadow counts only, never documents.
                assert b'shadow-private-value' not in sim.message and b'secret_key' not in sim.message
                assert json.loads(sim.message)['services']['shadows']['shadows']==2
                fixture=root/'fixture.json';fixture.write_text(json.dumps({'manifest':manifest,'config':dict(schema=1,chainIdentifier=sim.chain,registryId=object_id(ota.REGISTRY),packageId=object_id(PACKAGE),typeOrigin=object_id(PACKAGE),fleetId=object_id(FLEET),budgetId=object_id(budget.BUDGET),storageId=object_id(STORAGE)),
                    'compact_heads':[h.hex() for h in sim.heads],'anchors':sim.anchors,'ciphertexts':{a['blob_object_id']:base64.b64encode((sim.upload_dir/(a['blob_object_id']+'.bin')).read_bytes()).decode() for a in sim.anchors},'device':object_id(ota.DEVICE),'signer':ota.ADDRESS,'fleet':sim.fleet(),'writer':sim.writer(),'now':max(sim.clock,int(time.time()*1000)),
                    'shadows':[{'':2,'cfg':5}]*3+[{'':3,'cfg':5}]}))
                subprocess.run(['node','scripts/verify_native_fleet.cjs',str(fixture)],check=True)
                assert not sim.errors,sim.errors
                print('PASS real daemon signed health -> simulated Sui -> browser verification; health/bulk retry, exhaustion, cadence, restart, revocation and diagnostics; '
                      'SF-084 encrypted shadow documents archived on change and decrypted/verified in the browser library')
    finally:
        if process:process.terminate();process.wait(timeout=10)
        shim.terminate();shim.wait(timeout=10);server.shutdown();server.server_close()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--binary',type=Path,required=True);p.add_argument('--shim',type=Path,required=True);p.add_argument('--compact-history',action='store_true');a=p.parse_args();run(a.binary.resolve(),a.shim.resolve(),a.compact_history)
