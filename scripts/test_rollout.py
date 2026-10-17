#!/usr/bin/env python3
"""Three real daemons against shared rollout-grant simulator views.
Move tests/localnet cover coordinator transitions; this exercises device enforcement.
"""
import json
import base64
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from test_ota import Simulator, ARTIFACT, TYPE_ORIGINS, DEVICE, CAP, object_id, start_grpc, wait_for
from test_desired_state import manifest

ROOT = Path(__file__).resolve().parents[1]
RECORD = '0x701'
MEMBERS = ['0xd1', '0xd2', '0xd3']

def run():
    rollout = dict(state=0, members=MEMBERS, admitted_at=[0,0,0], outcomes=[0,0,0], skipped=[False]*3)
    fault = [False]
    class View(Simulator):
        def rpc(self, method, params):
            if method == 'shim_fields':
                return super().rpc(method, params) + [[RECORD, f"{TYPE_ORIGINS['deployment_manager::RolloutKey']}::deployment_manager::RolloutKey", '00']]
            if method == 'suix_getDynamicFieldObject' and params[1]['type'].endswith('::RolloutKey'):
                if fault[0]: raise ConnectionError('injected rollout read outage')
                return {'data':{'content':{'fields':{'value':{**rollout,'outcomes':base64.b64encode(bytes(rollout['outcomes'])).decode()}}}}}
            return super().rpc(method, params)
    handles = []
    try:
        with tempfile.TemporaryDirectory(prefix='suifleet-rollout-') as tmp:
            root = Path(tmp)
            for i, device in enumerate(MEMBERS):
                sim = View(device, f'0xc{i+2}')
                sim.allow_reports = True; sim.fail_execution = False; sim.truncate_next_artifact = False
                sim.add(RECORD, event={'target_devices':MEMBERS}); sim.schema_fields[RECORD] = {'schema_version':'3'}
                def handler(sim):
                    class Handler(BaseHTTPRequestHandler):
                        def log_message(self,*args): pass
                        def respond(self,data,status=200):
                            self.send_response(status); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
                        def do_POST(self):
                            req=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                            try: reply={'result':sim.rpc(req['method'],req['params'])}
                            except Exception as e:
                                if not isinstance(e,ConnectionError): sim.errors.append(repr(e))
                                reply={'error':{'message':str(e)}}
                            self.respond(json.dumps({'jsonrpc':'2.0','id':req['id'],**reply}).encode())
                        def do_GET(self):
                            blob=self.path.removeprefix('/v1/blobs/')
                            self.respond(ARTIFACT if blob=='artifact' else json.dumps(sim.recipes[blob]).encode())
                    return Handler
                server=ThreadingHTTPServer(('127.0.0.1',0),handler(sim)); threading.Thread(target=server.serve_forever,daemon=True).start()
                url=f'http://127.0.0.1:{server.server_port}'; shim,rpc=start_grpc(url)
                home=root/str(i); home.mkdir(); key=home/'device.key'; key.write_text('07'*32); key.chmod(0o600)
                (home/'deployment.json').write_text(json.dumps(manifest(sim,url,rpc)))
                runs=home/'runs'; updater=home/'apply.py'; updater.write_text(f"from pathlib import Path\np=Path({str(runs)!r})\np.write_text(p.read_text()+'apply\\n' if p.exists() else 'apply\\n')\n")
                cfg=home/'config.toml'; cfg.write_text('[sui]\nmanifest_path="deployment.json"\n'+f'[device]\nkeypair_path="{key}"\ndevice_object_id="{device}"\ndevice_cap_id="{sim.cap}"\n[apply]\ncommand="python3 {updater}"\ntimeout_seconds=10\n[runtime]\nstate_dir="{home}/state"\npoll_seconds=1\nreplay_history=true\n')
                log=(home/'daemon.log').open('w')
                handles.append(dict(sim=sim,server=server,shim=shim,home=home,runs=runs,cfg=cfg,log=log,process=None))
            def start(i):
                h=handles[i]; h['process']=subprocess.Popen([str(ROOT/'suifleet-edge/target/debug/suifleet')],env={**os.environ,'EDGE_CONFIG':str(h['cfg'])},stdout=h['log'],stderr=h['log'])
            def stop(i,crash=False):
                h=handles[i]
                if h['process']:
                    (h['process'].kill if crash else h['process'].terminate)(); h['process'].wait(timeout=10); h['process']=None
            def state(i):
                p=handles[i]['home']/'state/state.json'; return json.loads(p.read_text()) if p.exists() else {}
            def deferred(i,reason): return state(i).get('deferred',{}).get(RECORD,{}).get('reason')==reason
            def count(i):
                p=handles[i]['runs']; return len(p.read_text().splitlines()) if p.exists() else 0
            def wait(fn,msg):
                try: wait_for(fn,msg)
                except AssertionError:
                    for h in handles:
                        print((h['home']/'daemon.log').read_text()[-4000:])
                    raise
            start(0); start(1) # Third cohort member is offline.
            wait(lambda:all(deferred(i,'waiting for rollout eligibility') for i in (0,1)),'ungranted members not deferred')
            assert [count(i) for i in range(3)]==[0,0,0]
            # Coordinator grants only the canary, with both online devices polling.
            rollout['admitted_at'][0]=1
            wait(lambda:handles[0]['sim'].reports,'canary not applied/reported')
            assert [count(i) for i in range(3)]==[1,0,0]
            stop(1,True); start(1)
            wait(lambda:deferred(1,'waiting for rollout eligibility'),'restart lost deferred record')
            assert count(1)==0
            rollout['state']=1
            wait(lambda:deferred(1,'rollout paused'),'pause ignored')
            # Restart coordinator from a serialized chain-state snapshot. No local
            # browser/process state is needed to preserve grants or pause.
            saved=json.loads(json.dumps(rollout)); rollout.clear(); rollout.update(saved)
            stop(1,True); start(1)
            wait(lambda:deferred(1,'rollout paused'),'durable pause lost after crash')
            rollout['admitted_at'][1]=2; fault[0]=True; rollout['state']=0
            time.sleep(2); assert count(1)==0
            fault[0]=False
            wait(lambda:handles[1]['sim'].reports,'resumed grant did not execute')
            assert [count(i) for i in range(3)]==[1,1,0]
            rollout['state']=2; start(2)
            wait(lambda:deferred(2,'rollout aborted'),'offline member ignored abort')
            stop(2,True); start(2)
            wait(lambda:deferred(2,'rollout aborted'),'abort not durable')
            assert [count(i) for i in range(3)]==[1,1,0]
            assert all(not h['sim'].errors for h in handles),[h['sim'].errors for h in handles]
            print('PASS: three daemon cohorts, canary-only grants, offline member, crash/restart, pause/resume, fail-closed reads and durable abort; apply counts 1/1/0')
    finally:
        for h in handles:
            if h['process']:
                h['process'].terminate(); h['process'].wait(timeout=10)
            h['shim'].terminate(); h['shim'].wait(timeout=10); h['server'].shutdown(); h['log'].close()

if __name__=='__main__': run()
