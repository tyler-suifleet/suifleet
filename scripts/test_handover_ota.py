#!/usr/bin/env python3
"""Real daemon: staged authority migration, old-owner rejection and restart.
Chain mutations are simulator inputs; Move/live tests establish their authorization.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from test_ota import Simulator, OWNER_CAP, OPERATOR, DEVICE, CAP, GROUP, wait_for
from test_desired_state import serve, manifest
from test_ota import start_grpc

NEW_OWNER = '0xbb'

class Fleet(Simulator):
    operator = OPERATOR
    on_group_read = None
    hook_lock = threading.Lock()
    def obj(self, id):
        # Publish chain changes at an exact point of the daemon's poll: just
        # before it next reads the group, i.e. after its operator read.
        with self.hook_lock:
            hook, self.on_group_read = (self.on_group_read, None) if id == GROUP else (None, self.on_group_read)
        if hook:
            hook()
        result = super().obj(id)
        if id == OWNER_CAP:
            result['data']['owner'] = {'AddressOwner': self.operator}
        return result

def run(binary):
    sim = Fleet()
    sim.truncate_next_artifact = False
    sim.allow_reports = True
    sim.fail_execution = False
    sim.memberships.append((GROUP, DEVICE))
    server = serve(sim)
    shim, rpc_url = start_grpc(f'http://127.0.0.1:{server.server_port}')
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix='sf089-ota-') as tmp, ExitStack() as cleanup:
            # Stop the writer before TemporaryDirectory removes its journal,
            # including when an assertion fails or after the restart below.
            def stop_daemon():
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            cleanup.callback(stop_daemon)
            root = Path(tmp)
            key = root/'device.key'; key.write_text('07'*32); key.chmod(0o600)
            applied = root/'applied'
            updater = root/'apply.py'; updater.write_text(f'from pathlib import Path\np=Path({str(applied)!r})\np.write_text(p.read_text()+"run\\n" if p.exists() else "run\\n")\n')
            url = f'http://127.0.0.1:{server.server_port}'
            (root/'deployment.json').write_text(json.dumps(manifest(sim, url, rpc_url)))
            cfg = root/'config.toml'
            cfg.write_text('[sui]\nmanifest_path="deployment.json"\n'+
                f'[device]\nkeypair_path="{key}"\ndevice_object_id="{DEVICE}"\ndevice_cap_id="{CAP}"\n'+
                f'[apply]\ncommand="{sys.executable} {updater}"\ntimeout_seconds=10\n[runtime]\nstate_dir="{root}/state"\npoll_seconds=1\n')
            def state():
                p=root/'state/state.json'
                return json.loads(p.read_text()) if p.exists() else {}
            def outcomes(): return state().get('deployments',{})
            def runs(): return len(applied.read_text().splitlines()) if applied.exists() else 0
            def settle():
                for _ in range(3):
                    checks=sim.chain_checks
                    wait_for(lambda: sim.chain_checks>checks, 'daemon stopped polling', process)
                time.sleep(.3)
            def deploy(id, creator, managed=False):
                sim.add(id,creator=creator,groups=[GROUP],direct=False,event={'component_name':'app'})
                if managed: sim.managed.add(id)
            log=(root/'daemon.log').open('w')
            def start(): return subprocess.Popen([str(binary)],env={**os.environ,'EDGE_CONFIG':str(cfg)},stdout=log,stderr=log)
            deploy('0x901',OPERATOR,True)
            sim.desired[GROUP]=(1,{'app':'0x901'})
            process=start()
            wait_for(lambda: outcomes().get('0x901',{}).get('reported'), 'initial desired state failed',process)
            assert runs()==1
            # Transfer capability first. The source's records and groups are
            # not silently inherited during this partial migration. The
            # transfer and the old owner's new desired state land between the
            # daemon's operator read and its group read, so authority read
            # before the desired state must be confirmed after it.
            def transfer():
                sim.operator=NEW_OWNER
                deploy('0x902',OPERATOR)
                deploy('0x903',OPERATOR,True)
                sim.desired[GROUP]=(2,{'app':'0x903'})
            sim.on_group_read=transfer
            wait_for(lambda: sim.on_group_read is None, 'daemon did not read its group', process)
            settle()
            assert runs()==1, 'partially migrated group admitted work'
            # Complete group authority. Current signed registry identity and
            # owner-cap ownership suffice; the device key/config never changes.
            sim.groups[GROUP]['admin']=NEW_OWNER
            sim.schema_fields['0x903']={'creator':NEW_OWNER}
            wait_for(lambda: outcomes().get('0x903',{}).get('reported'), 'recipient desired state failed',process)
            assert runs()==2
            deploy('0x904',OPERATOR)
            settle(); assert runs()==2, 'old owner deployed after handover'
            process.terminate();process.wait(timeout=10);process=start()
            settle();assert runs()==2, 'handover restart replayed updater'
            deploy('0x905',NEW_OWNER)
            wait_for(lambda: outcomes().get('0x905',{}).get('reported'), 'recipient could not deploy after restart',process)
            assert runs()==3
            assert key.read_text()=='07'*32
            assert not sim.errors,sim.errors
            print('PASS: populated desired fleet, interrupted transfer, old-owner rejection, unchanged device identity and restart deduplication')
    finally:
        if process and process.poll() is None: process.terminate();process.wait(timeout=10)
        shim.terminate();shim.wait(timeout=10);server.shutdown()

if __name__=='__main__':
    run(Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parents[1]/'suifleet-edge/target/debug/suifleet')
