#!/usr/bin/env python3
"""SF-123: real-daemon default-cadence deadlines and ordered gRPC faults."""
import json
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from test_ota import Simulator, ARTIFACT, DEVICE, CAP, OWNER_CAP, start_grpc, wait_for
from test_desired_state import manifest

ROOT = Path(__file__).resolve().parents[1]


def run(faults=(429, 408, 503, 'wrong-chain', 'discovery-outage', 'completed-window', 'receipt-outage')):
    for fault in faults:
        sim = Simulator()
        sim.allow_reports = True
        sim.fail_execution = False
        sim.truncate_next_artifact = False
        now = int(time.time() * 1000)
        sim.add('0x901')
        sim.controls['0x901'] = dict(revision=1, state=0, not_before_ms=now+8000,
            expires_ms=0, window_start_ms=now+8000, window_end_ms=now+18000, notice_ms=0)
        counts = [0, 0]
        outages = [0]
        injection = dict(armed=False, scanned=False, done=False)
        if fault == 'completed-window':
            sim.controls['0x901']['window_end_ms'] = now + 180000
        applied = None
        if fault == 'receipt-outage':
            sim.add('0x900')
        if fault == 'discovery-outage':
            sim.add('0x902')
            sim.controls['0x902'] = dict(revision=1, state=0, not_before_ms=now+86400000,
                expires_ms=0, window_start_ms=0, window_end_ms=0, notice_ms=0)

        def handler(primary):
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def respond(self, body, status=200):
                    self.send_response(status)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except BrokenPipeError:
                        pass  # The daemon may stop while reconstructing its timer.
                def do_POST(self):
                    req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    counts[0 if primary else 1] += 1
                    if (fault == 'receipt-outage' and req['method'] == 'unsafe_moveCall'
                            and int(req['params'][5][0], 16) == 0x900):
                        outages[0] += 1
                        reply = {'error': {'message': 'device is no longer targeted'}}
                    elif (fault == 'discovery-outage' and applied is not None and applied.exists()
                            and req['method'] == 'sui_getObject'
                            and int(req['params'][0], 16) == 0x902):
                        outages[0] += 1
                        reply = {'error': {'code': 503, 'message': 'later discovery unavailable'}}
                    elif primary and fault == 'wrong-chain' and req['method'] == 'sui_getChainIdentifier':
                        reply = {'result': 'deadbeef'}
                    elif primary and isinstance(fault, int) and req['method'] != 'sui_getChainIdentifier' and req['method'] != 'shim_height':
                        reply = {'error': {'code': fault, 'message': 'injected endpoint failure'}}
                    else:
                        try:
                            reply = {'result': sim.rpc(req['method'], req['params'])}
                        except Exception as error:
                            sim.errors.append(repr(error))
                            reply = {'error': {'message': str(error)}}
                    # Publish the next record after this poll's discovery, at
                    # the exact read used to reconcile its operator. It cannot
                    # be picked up until a subsequent check.
                    if fault == 'completed-window' and injection['armed']:
                        if req['method'] == 'shim_events' and req['params'][0].endswith('::DeploymentCreated'):
                            injection['scanned'] = True
                        if (injection['scanned'] and req['method'] == 'sui_getObject'
                                and int(req['params'][0], 16) == int(OWNER_CAP, 16)):
                            sim.add('0x902')
                            injection.update(armed=False, done=True)
                    self.respond(json.dumps({'jsonrpc': '2.0', 'id': req['id'], **reply}).encode())
                def do_GET(self):
                    blob = self.path.removeprefix('/v1/blobs/')
                    self.respond(ARTIFACT if blob == 'artifact' else json.dumps(sim.recipes[blob]).encode())
            return Handler

        servers = [ThreadingHTTPServer(('127.0.0.1', 0), handler(primary)) for primary in (True, False)]
        shims = []
        process = None
        def stop_daemon():
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        try:
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                shims.append(start_grpc(f'http://127.0.0.1:{server.server_port}'))
            with ExitStack() as resources:
                tmp = resources.enter_context(tempfile.TemporaryDirectory(prefix='cadence-ota-'))
                # Stop the current (possibly restarted) writer before deleting
                # its state, including when an assertion fails.
                resources.callback(stop_daemon)
                root = Path(tmp)
                key = root/'device.key'
                key.write_text('07'*32)
                key.chmod(0o600)
                url = f'http://127.0.0.1:{servers[1].server_port}'
                deployment = manifest(sim, url, shims[0][1])
                deployment['rpc_urls'] = [item[1] for item in shims]
                (root/'deployment.json').write_text(json.dumps(deployment))
                applied = root/'applied'
                updater = root/'apply.py'
                updater.write_text(f'from pathlib import Path\nPath({str(applied)!r}).touch()\n')
                # All cases keep the default 30-minute normal cadence. The
                # completion case uses configurable 2-second fast checks.
                cfg = root/'config.toml'
                cfg.write_text('[sui]\nmanifest_path="deployment.json"\n'+
                    f'[device]\nkeypair_path="{key}"\ndevice_object_id="{DEVICE}"\ndevice_cap_id="{CAP}"\n'+
                    f'[apply]\ncommand="python3 {updater}"\ntimeout_seconds=10\n'+
                    f'[runtime]\nstate_dir="{root}/state"\nreplay_history=true\n'+
                    ('rollout_poll_seconds=2\n' if fault == 'completed-window' else ''))
                with (root/'daemon.log').open('w') as log:
                    process = subprocess.Popen([str(ROOT/'suifleet-edge/target/debug/suifleet')],
                        env={**os.environ, 'EDGE_CONFIG': str(cfg)}, stdout=log, stderr=log)
                    def state():
                        path = root/'state/state.json'
                        return json.loads(path.read_text()) if path.exists() else {}
                    if fault == 'wrong-chain':
                        process.wait(timeout=15)
                        assert process.returncode != 0
                        assert counts[1] == 0 and not applied.exists(), counts
                        assert 'chain identifier mismatch' in (root/'daemon.log').read_text()
                    else:
                        wait_for(lambda: '0x901' in state().get('deferred', {}), 'schedule not discovered', process)
                        assert not applied.exists()
                        # Reconstruct the timer from the persisted record on restart.
                        process.terminate()
                        process.wait(timeout=10)
                        if fault == 'receipt-outage':
                            saved = state()
                            saved.setdefault('deployments', {})['0x900'] = dict(status=1, reported=False)
                            (root/'state/state.json').write_text(json.dumps(saved))
                        process = subprocess.Popen([str(ROOT/'suifleet-edge/target/debug/suifleet')],
                            env={**os.environ, 'EDGE_CONFIG': str(cfg)}, stdout=log, stderr=log)
                        try:
                            wait_for(lambda: state().get('deployments', {}).get('0x901', {}).get('reported'),
                                'default-cadence short window did not apply/report through fallback', process)
                        except AssertionError:
                            print((root/'daemon.log').read_text(), flush=True)
                            print(state(), flush=True)
                            raise
                        if fault == 'discovery-outage':
                            wait_for(lambda: outages[0] > 0 and counts[1] > 0,
                                'later discovery outage did not exercise fallback', process)
                        assert applied.exists() and sim.reports and (fault == 'completed-window' or counts[1] > 0)
                        assert not sim.errors, sim.errors
                        if fault == 'completed-window':
                            process.terminate()
                            process.wait(timeout=10)
                            injection['armed'] = True
                            process = subprocess.Popen([str(ROOT/'suifleet-edge/target/debug/suifleet')],
                                env={**os.environ, 'EDGE_CONFIG': str(cfg)}, stdout=log, stderr=log)
                            wait_for(lambda: injection['done'], 'next record not injected after discovery', process)
                            try:
                                wait_for(lambda: state().get('deployments', {}).get('0x902', {}).get('reported'),
                                    'completion/restart lost the still-active fast-check window', process)
                                assert state()['deployments']['0x902']['status'] == 1
                            except AssertionError:
                                print((root/'daemon.log').read_text(), flush=True)
                                print(state(), flush=True)
                                raise
                        if fault == 'receipt-outage':
                            assert outages[0] >= 2, 'receipt must remain queued across retries'
                            assert not state()['deployments']['0x900']['reported']
                            assert 'Sync failed' not in (root/'daemon.log').read_text()
                        if fault == 'discovery-outage':
                            assert outages[0] > 0, 'report must survive a later read failure'
                    print(f'PASS: endpoint {fault}; chain rejection or default-cadence timer/restart/apply/report')
        finally:
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            for shim, _ in shims:
                shim.terminate()
                shim.wait(timeout=10)
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    run()
