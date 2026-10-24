#!/usr/bin/env python3
"""SF139 real daemon IPC -> durable ciphertext -> simulated certification/index.
Run as root in a disposable network-none container for real UID-authenticated IPC.
The simulator does not validate Walrus quorum signatures; SDK crypto is real.
"""
import argparse,base64,hashlib,json,os,signal,struct,subprocess,sys,tempfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import test_fleet_data_ota as fleet
import test_ota as ota
import test_budget_ota as budget
sys.path.insert(0,str(Path('sdk/python').resolve()))
from suifleet_ipc import Client,IpcError

def run(binary,shim_binary):
    assert os.geteuid()==0,'run disposable root container for IPC admin fixture'
    ota.SHIM=shim_binary;sim=fleet.Simulator();process=None
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            r=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:answer={'result':sim.rpc(r['method'],r['params'])}
            except Exception as e:
                if not isinstance(e,ConnectionError):sim.errors.append(repr(e))
                answer={'error':{'code':-32000,'message':str(e)}}
            b=json.dumps({'jsonrpc':'2.0','id':r['id'],**answer}).encode();self.send_response(200);self.end_headers()
            try:self.wfile.write(b)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    shim,rpc=ota.start_grpc(f'http://127.0.0.1:{server.server_port}')
    try:
        with tempfile.TemporaryDirectory(prefix='sf139-ota-') as temp:
            root=Path(temp);state=root/'state';key=root/'device.key';key.write_text('07'*32);key.chmod(0o600)
            manifest=dict(schema_version=1,protocol_version=1,network='localnet',chain_identifier=sim.chain,rpc_url=rpc,call_package_id=ota.PACKAGE,upgrade_cap_id=ota.UPGRADE_CAP,package_version='3',type_origins=ota.TYPE_ORIGINS,
                shared_objects={'registry':{'id':ota.REGISTRY,'initial_shared_version':'1'}},supported_clients={'dapp':{'min':'0.0.1','max_exclusive':'0.3.0'},'edge':{'min':'0.1.0','max_exclusive':'0.3.0'}},walrus={'publisher_url':rpc,'aggregator_url':rpc})
            (root/'deployment.json').write_text(json.dumps(manifest));config=root/'config.toml';socket=root/'ipc.sock'
            vectors=json.loads(Path('interfaces/seal-vectors.json').read_text());sim.upload_dir=root/'uploaded';sim.upload_dir.mkdir()
            for s in vectors['servers']:
                id=s['object_id'];sim.key_objects[id]=fleet.raw(id,'0xf5::key_server::KeyServer',{},fleet.addr(id)+fleet.u64(1)+fleet.u64(1))
                field=hashlib.blake2b(b'\xf0'+fleet.addr(id)+fleet.u64(8)+fleet.u64(1)+b'\x02',digest_size=32).digest()
                contents=field+fleet.u64(1)+fleet.vec(b'fixture')+fleet.vec(b'https://fixture.invalid')+b'\0'+fleet.vec(bytes.fromhex(s['public_key']))
                sim.key_objects['0x'+field.hex()]=fleet.raw('0x'+field.hex(),'0x2::dynamic_field::Field<u64,0xf5::key_server::KeyServerV1>',{},contents)
            codec=root/'codec.py'
            codec.write_text("import sys,json,pathlib,urllib.request,base64,shutil\nr=json.loads(sys.stdin.readline())\n"
                "if r['operation']=='prepare': print(json.dumps({'blob_id':'2','root_hash':'1','size':pathlib.Path(r['ciphertext_path']).stat().st_size+sum(i['size'] for i in r.get('items',[]))}))\n"
                "elif r['operation']=='registered':\n"
                f" q=urllib.request.Request('http://127.0.0.1:{server.server_port}',json.dumps({{'jsonrpc':'2.0','id':1,'method':'test_blob','params':[r['digest']]}}).encode(),{{'Content-Type':'application/json'}})\n"
                " with urllib.request.urlopen(q) as response: print(json.dumps(json.load(response)['result']))\n"
                "elif r['operation']=='write':\n"
                f" shutil.copyfile(r['ciphertext_path'],str(pathlib.Path({str(sim.upload_dir)!r})/(r['blob_object_id']+'.bin')))\n"
                " items=[{**{k:v for k,v in item.items() if k!='path'},'ciphertext':base64.b64encode(pathlib.Path(item['path']).read_bytes()).decode()} for item in r.get('items',[])]\n"
                f" pathlib.Path({str(sim.upload_dir)!r},r['blob_object_id']+'-items.json').write_text(json.dumps(items))\n"
                " print(json.dumps({'signature':base64.b64encode(bytes([2])+bytes(95)).decode(),'bitmap':'AQ==','message':'Zml4dHVyZQ=='}))\n")
            config.write_text(f'[sui]\nmanifest_path="deployment.json"\n[device]\nkeypair_path={json.dumps(str(key))}\ndevice_object_id="{ota.DEVICE}"\ndevice_cap_id="{ota.CAP}"\n[walrus]\naggregator_url="{rpc}"\n[apply]\ncommand="true"\n[runtime]\nstate_dir={json.dumps(str(state))}\npoll_seconds=1\n'
                f'[ipc]\nsocket={json.dumps(str(socket))}\nadministrators=[0]\n'
                f'[fleet_data]\npackage_id="{fleet.PACKAGE}"\ntype_origin="{fleet.PACKAGE}"\nfleet_id="{fleet.FLEET}"\nbudget_id="{budget.BUDGET}"\nstorage_id="{fleet.STORAGE}"\nwriter_id="{fleet.WRITER}"\naccount_id="{budget.ACCOUNT}"\n'
                f'[logs]\ninterval_seconds=10\nmax_lines_per_interval=128\nbuffer_max=16\n[fleet_data.archives]\nstorage_package="0xf6"\nstorage_type_origin="0xf6"\nsystem_id="0xf4"\nwalrus_package="0xf7"\nkey_server_package="0xf5"\n'
                'key_servers = '+json.dumps([{'object_id':s['object_id'],'weight':1} for s in vectors['servers']]).replace(': ',' = ')+'\n'
                f'threshold=2\nquilt=true\nopaque=true\nwalrus_streams=["durable"]\nnode={json.dumps(sys.executable)}\ncodec={json.dumps(str(codec))}\ncodec_sha256="{hashlib.sha256(codec.read_bytes()).hexdigest()}"\n'
                '[streams]\n[[streams.definitions]]\nname="durable"\nmax_size=1048576\nstrategy_on_full="RejectNewData"\npersistence="File"\ndiscard_exported=true\nwalrus_export=true\n')
            with (root/'daemon.log').open('w') as log:
                def start():return subprocess.Popen([str(binary)],env={**os.environ,'EDGE_CONFIG':str(config)},stdout=log,stderr=log)
                def stop():
                    nonlocal process
                    process.send_signal(signal.SIGINT);process.wait(timeout=10);assert process.returncode==0;process=None
                process=start();fleet.wait(lambda:socket.exists());client=Client(str(socket))
                binary_data=bytes(range(256))*200;text='opaque ☃\ntext'.encode()
                stream_payload={'raw':'durable bytes','value':17}
                stream_results=[client.request('stream_append',topic='durable',payload=stream_payload) for _ in range(6)]
                stream_result=stream_results[-1]
                stream_expected=json.dumps(stream_payload,separators=(',',':'),sort_keys=True).encode()
                for stream,data,upload in [('sensor',binary_data,'aa'*32),('text',text,'bb'*32)]:
                    result=client.put_data(stream,'sample',data,metadata={'10':'ten','2':'two',**{f'm{n}':'x'*200 for n in range(14)}},upload=upload)
                    assert all(not c['storage_acknowledged'] for c in result['chunks'])
                pending=state/'fleet-data/pending.json';queue=state/'fleet-data/opaque/index.json'
                fleet.wait(lambda:pending.exists());stop();before=json.loads(pending.read_text());assert len(before['opaque_items'])>=1
                assert json.loads(queue.read_text())['entries'],'failed prepaid storage preserves custody'
                log_source=state/'supervised/service/component.log';log_source.parent.mkdir(parents=True,exist_ok=True);log_source.write_text('routine log must not starve\n')
                original={p.name:p.read_bytes() for p in pending.parent.glob('data-*.bin')}
                sim.uploads_paused=False;process=start();fleet.wait(lambda:bool(sim.registered));time.sleep(2);stop()
                assert json.loads(queue.read_text())['entries'] and not sim.anchors,'certification outage must retain queue'
                for name,bytes_ in original.items():assert (pending.parent/name).read_bytes()==bytes_
                sim.certify_outage=False;process=start();fleet.wait(lambda:len(sim.anchors)==1);fleet.wait(lambda:not pending.exists());stop()
                assert len(sim.registered)==1,'restart reuses purchase'
                # A second batch may contain items enqueued after first formation.
                while json.loads(queue.read_text())['entries']:
                    count=len(sim.anchors);sim.clock+=86400000;process=start();fleet.wait(lambda:len(sim.anchors)==count+1);fleet.wait(lambda:not pending.exists());stop()
                fixture=root/'opaque.json';fixture.write_text(json.dumps({'manifest':manifest,'config':dict(schema=1,chainIdentifier=sim.chain,registryId=fleet.object_id(ota.REGISTRY),packageId=fleet.object_id(fleet.PACKAGE),typeOrigin=fleet.object_id(fleet.PACKAGE),fleetId=fleet.object_id(fleet.FLEET),budgetId=fleet.object_id(budget.BUDGET),storageId=fleet.object_id(fleet.STORAGE)),
                    'device':fleet.object_id(ota.DEVICE),'anchors':sim.anchors,'index':{f'{seq}/{n}':b.hex() for (seq,n),b in sim.opaque_items.items()},'patches':{a['blob_object_id']:json.loads((sim.upload_dir/(a['blob_object_id']+'-items.json')).read_text()) for a in sim.anchors},'expected':{'sensor':base64.b64encode(binary_data).decode(),'text':base64.b64encode(text).decode(),'durable':base64.b64encode(stream_expected).decode()}}))
                subprocess.run(['node','scripts/verify_opaque_native.cjs',str(fixture)],check=True)
                process=start();fleet.wait(lambda:socket.exists());description=Client(str(socket)).request('stream_describe',topic='durable');stop()
                assert description['exported']==stream_result['sequence'],description
                durable_batches={}
                for (seq,n),value in sim.opaque_items.items():
                    # Public BCS starts with anchor_index + sequence + stream string.
                    if value[17:24]==b'durable':durable_batches[seq]=durable_batches.get(seq,0)+1
                assert sum(durable_batches.values())==6,durable_batches
                assert max(durable_batches.values())>1,'multiple stream messages must share a batch'
                assert any(not any(p['identifier'].startswith('data-') for p in json.loads((sim.upload_dir/(a['blob_object_id']+'-items.json')).read_text())) for a in sim.anchors),'routine logs get a batch between opaque batches'
                assert not sim.errors,sim.errors
                print('PASS opaque real IPC chunk intake, durable storage/certification outage, restart exact ciphertext/purchase reuse, verified index retirement, independent SDK binary/text retrieval')
    finally:
        if process and process.poll() is None:process.terminate();process.wait(timeout=10)
        shim.terminate();shim.wait(timeout=10);server.shutdown();server.server_close()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--binary',type=Path,required=True);p.add_argument('--shim',type=Path,required=True);a=p.parse_args();run(a.binary.resolve(),a.shim.resolve())
