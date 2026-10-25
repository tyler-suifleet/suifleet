#!/usr/bin/env python3
"""Actual daemon: authenticated platform selection, failure cursor and restart/report durability.

The device platform is the daemon binary's own ELF architecture, so the same
test selects `x86` manifests for a 32-bit i686 build (SF-076) and `x86_64`
ones for a 64-bit build; the x86/x86_64 sibling is never selected."""
import hashlib,json,os,signal,subprocess,tempfile,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import test_ota as ota
from deployment_manifest import CATALOG_TYPES
from test_component_ota import ComponentSimulator
import test_component as component

# Distinct later introduction origin, authenticated by the shim package table.
ota.TYPE_ORIGINS.update({n:ota.object_id('0x4') for n in CATALOG_TYPES})
DAEMON=Path(__file__).resolve().parents[1]/'suifleet-edge/target/debug/suifleet'
def daemon_arch(binary):
    """Rust `std::env::consts::ARCH` of an ELF binary, from its e_machine."""
    header=Path(binary).read_bytes()[:20]
    return {3:'x86',62:'x86_64',183:'aarch64'}[int.from_bytes(header[18:20],'big' if header[5]==2 else 'little')]
ARCH=daemon_arch(DAEMON)
SIBLING={'x86':'x86_64','x86_64':'x86'}[ARCH]
class CatalogSimulator(ComponentSimulator):
    def __init__(self):
        super().__init__();self.versions={};self.bindings={};self.downloads=[]
    def deploy(self,id,version,mode='valid'):
        artifact=component.build(version,'catalog-sim')
        recipe=component.recipe(artifact,'artifact-'+id)
        self.blobs['artifact-'+id]=artifact;self.blobs['recipe-'+id]=recipe
        version_id=ota.object_id(hex(int(id,16)+0x1000));self.bindings[id]=version_id
        value=dict(catalog_id='0xcafe',publisher=ota.OPERATOR,component_name=component.DEFAULT_COMPONENT,version=version,schema_version='1',
            platform_os=['linux','linux'],platform_arch=['aarch64',ARCH],
            recipe_blob_ids=['never-download','recipe-'+id],recipe_sha256s=['a'*64,hashlib.sha256(recipe).hexdigest()],artifact_sha256s=['b'*64,hashlib.sha256(artifact).hexdigest()])
        if mode=='unsupported':value['platform_arch']=['aarch64','arm']
        if mode=='sibling':value['platform_arch']=['aarch64',SIBLING]
        if mode=='invalid_unselected':value['recipe_sha256s'][0]='bad'
        self.versions[version_id]=(value,mode)
        event=dict(deployment_id=id,component_name=component.DEFAULT_COMPONENT,version=version,recipe_blob_id='',artifact_sha256='',target_groups=[],target_devices=[ota.DEVICE],creator=ota.OPERATOR)
        self.events.append(dict(id={'txDigest':f'event-{len(self.events)}','eventSeq':'0'},parsedJson=event))
        self.schema_fields[id]={'schema_version':'2'}
    def obj(self,id):
        if id in self.versions:
            value,mode=self.versions[id]
            return {'data':{'objectId':id,'owner':{'Shared':{'initial_shared_version':'1'}} if mode=='mutable' else 'Immutable',
                'content':{'type':f'{ota.TYPE_ORIGINS["component_catalog::ComponentVersion"]}::component_catalog::'+('ComponentCatalog' if mode=='wrong_type' else 'ComponentVersion'),'fields':value}}}
        return super().obj(id)
    def rpc(self,method,params):
        if method=='shim_fields':
            key=f'{ota.TYPE_ORIGINS["component_catalog::CatalogBindingKey"]}::component_catalog::CatalogBindingKey'
            return super().rpc(method,params)+[[parent,key,'00'] for parent in self.bindings]
        if method=='suix_getDynamicFieldObject' and params[1]['type'].endswith('CatalogBindingKey'):
            return {'data':{'content':{'fields':{'value':{'version_id':self.bindings[params[0]]}}}}}
        return super().rpc(method,params)

def run():
    sim=CatalogSimulator()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,body,status=200):
            self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:response={'id':request['id'],'result':sim.rpc(request['method'],request['params'])}
            except Exception as error:
                
                if not isinstance(error,ConnectionError): sim.errors.append(repr(error))
                response={'id':request['id'],'error':{'code':-32000,'message':str(error)}}
            self.respond(json.dumps(response).encode())
        def do_GET(self):
            sim.downloads.append(self.path);body=sim.blobs.get(self.path.removeprefix('/v1/blobs/'));self.respond(body or b'not found',200 if body else 404)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    shim,url=ota.start_grpc(f'http://127.0.0.1:{server.server_port}');process=None
    try:
        with tempfile.TemporaryDirectory(prefix='catalog-ota-') as temp:
            root=Path(temp);key=root/'device.key';key.write_text('07'*32);key.chmod(0o600)
            aggregator=f'http://127.0.0.1:{server.server_port}'
            manifest=dict(schema_version=1,protocol_version=1,network='localnet',chain_identifier=sim.chain,rpc_url=url,call_package_id=ota.PACKAGE,upgrade_cap_id=ota.UPGRADE_CAP,package_version='3',type_origins=ota.TYPE_ORIGINS,
                shared_objects={'registry':{'id':ota.REGISTRY,'initial_shared_version':'1'}},supported_clients={'edge':{'min':'0.2.0','max_exclusive':'0.3.0'}},walrus={'publisher_url':aggregator,'aggregator_url':aggregator})
            (root/'deployment.json').write_text(json.dumps(manifest));config=root/'config.toml'
            config.write_text(f'[sui]\nmanifest_path="deployment.json"\n[device]\nkeypair_path={json.dumps(str(key))}\ndevice_object_id="{ota.DEVICE}"\ndevice_cap_id="{ota.CAP}"\n[apply]\ncommand={json.dumps("/bin/sh "+str(component.UPDATER)+" --root "+str(root/"components"))}\n[runtime]\nstate_dir={json.dumps(str(root/"state"))}\npoll_seconds=1\nevent_discovery_seconds=1\n')
            def state():
                p=root/'state/state.json';return json.loads(p.read_text()) if p.exists() else {}
            with (root/'daemon.log').open('w') as log:
                def start():return subprocess.Popen([str(DAEMON)],env={**os.environ,'EDGE_CONFIG':str(config)},stdout=log,stderr=log)
                process=start();ota.wait_for(lambda:state().get('initialized'),'enrollment',process)
                sim.allow_reports=False
                for i,mode in enumerate(['unsupported','sibling','invalid_unselected','mutable','wrong_type','valid','valid']):sim.deploy(hex(0x301+i),f'{i+1}.0.0',mode)
                ota.wait_for(lambda:len(state().get('deployments',{}))==7 and state().get('cursor')==ota.scanned(7),'selection/failure cursor',process)
                outcomes=state()['deployments'];assert [outcomes[hex(0x301+i)]['status'] for i in range(7)]==[2,2,2,2,2,1,1],outcomes
                assert f'linux/{ARCH}' in outcomes['0x302'].get('error',''),outcomes['0x302']
                assert not any('never-download' in p for p in sim.downloads)
                for i in range(5):
                    id=hex(0x301+i)
                    assert not any(id in p for p in sim.downloads),sim.downloads
                    assert not (root/'state/artifacts'/hashlib.sha256(id.encode()).hexdigest()).exists()
                    assert outcomes[id].get('firmware_request') is None
                history=root/'components'/component.DEFAULT_COMPONENT/'history.jsonl'
                assert [json.loads(l)['version'] for l in history.read_text().splitlines()]==['6.0.0','7.0.0']
                process.send_signal(signal.SIGINT);process.wait(timeout=10);process=None
                sim.allow_reports=True;process=start()
                ota.wait_for(lambda:all(o['reported'] for o in state()['deployments'].values()),'restart receipts',process)
                assert len(history.read_text().splitlines())==2
                process.send_signal(signal.SIGINT);process.wait(timeout=10);process=None
                assert len(sim.reports)==7 and not sim.errors,sim.errors
                # A complete legacy manifest remains valid, but cannot resolve schema2.
                manifest['type_origins']={n:o for n,o in ota.TYPE_ORIGINS.items() if n not in CATALOG_TYPES}
                (root/'deployment.json').write_text(json.dumps(manifest))
                before=list(sim.downloads)
                sim.deploy('0x308','8.0.0')
                process=start()
                ota.wait_for(lambda:state().get('deployments',{}).get('0x308',{}).get('reported'),'missing origin failure receipt',process)
                assert state()['deployments']['0x308']['status']==2
                assert 'missing catalog type origin' in state()['deployments']['0x308']['error']
                assert sim.downloads==before
                process.send_signal(signal.SIGINT);process.wait(timeout=10);process=None
                assert len(history.read_text().splitlines())==2
            print(f'PASS: schema2 actual-daemon ({ARCH}) different manifests; unsupported, {SIBLING}-only, invalid unselected hash, mutable/wrong type rejected before downloads; cursor/restart/durable reports, no duplicate apply')
    finally:
        if process and process.poll() is None:process.kill();process.wait()
        shim.kill();shim.wait();server.shutdown();server.server_close()
if __name__=='__main__':run()
