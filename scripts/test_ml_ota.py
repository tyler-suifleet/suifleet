#!/usr/bin/env python3
"""Generated ML recipes through the real daemon: persistence, replacement, restart.

Local gRPC/Walrus simulation only; no chain or hardware acceptance claim.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ml_component
import ml_runner
from test_ota import (CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP,
                      Simulator, start_grpc, wait_for)


def run(binary, wheels):
    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False
    blobs = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:
                response = {'jsonrpc': '2.0', 'id': request['id'],
                            'result': sim.rpc(request['method'], request['params'])}
            except Exception as error:
                sim.errors.append(repr(error))
                response = {'jsonrpc': '2.0', 'id': request['id'],
                            'error': {'code': -32000, 'message': str(error)}}
            self.respond(json.dumps(response).encode())

        def do_GET(self):
            name = self.path.removeprefix('/v1/blobs/')
            if name in blobs:
                body = blobs[name]
                if self.headers['Range']:
                    start, _, end = self.headers['Range'].removeprefix('bytes=').partition('-')
                    start = int(start)
                    end = min(int(end) if end else len(body) - 1, len(body) - 1)
                    if start > end or start >= len(body):
                        self.respond(b'range outside blob', 416)
                        return
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes {start}-{end}/{len(body)}')
                    self.send_header('Content-Length', str(end - start + 1))
                    self.end_headers()
                    self.wfile.write(body[start:end + 1])
                else:
                    self.respond(body)
            elif name in sim.recipes:
                self.respond(json.dumps(sim.recipes[name]).encode())
            else:
                self.respond(b'not found', 404)

        def respond(self, body, status=200):
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shim, rpc_url = start_grpc(f'http://127.0.0.1:{server.server_port}')
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix='suifleet-ml-ota-') as temporary:
            root = Path(temporary)
            state = root / 'state'
            key = root / 'device.key'
            key.write_text('07' * 32)
            key.chmod(0o600)
            url = f'http://127.0.0.1:{server.server_port}'
            manifest = dict(schema_version=1, protocol_version=1, network='localnet', chain_identifier=sim.chain,
                rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version='3', type_origins=TYPE_ORIGINS,
                shared_objects={'registry': {'id': REGISTRY, 'initial_shared_version': '1'}},
                supported_clients={'dapp': {'min': '0.0.1', 'max_exclusive': '0.1.0'},
                                   'edge': {'min': '0.1.0', 'max_exclusive': '0.3.0'}},
                walrus={'publisher_url': url, 'aggregator_url': url})
            (root / 'deployment.json').write_text(json.dumps(manifest))
            config = root / 'config.toml'
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 20\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state))}\npoll_seconds = 1\n'
                'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 15\ncomponent_stop_timeout_seconds = 3\n')

            def journal():
                path = state / 'state.json'
                return json.loads(path.read_text()) if path.exists() else {}

            def deploy(id, task):
                raw = ml_component.build(task, ml_runner.target(), wheels)
                blob = 'ml-' + task
                blobs[blob] = raw
                recipe = ml_component.recipe(raw, blob)
                sim.add(id, event=dict(component_name=recipe['componentName'], version=recipe['componentVersion'],
                                      artifact_sha256=hashlib.sha256(raw).hexdigest()), recipe=recipe)
                wait_for(lambda: journal().get('deployments', {}).get(id, {}).get('reported'),
                         'ML deployment did not finish', process)
                assert journal()['deployments'][id]['status'] == 1, journal()
                # main.rs cleanup must have completed; a one-shot recipe loses its runtime here.
                wait_for(lambda: not any((state / 'artifacts').iterdir()), 'temporary deployment workspace survived', process)

            def infer(task):
                active = json.loads((state / 'supervised' / 'active.json').read_text())['suifleet-ml-' + task]
                tag = hashlib.blake2b(active['deployment'].encode(), digest_size=32).hexdigest()[:8]
                workspace = state / 'supervised' / ('suifleet-ml-' + task) / 'versions' / (active['version'] + '-' + tag)
                result = subprocess.run([sys.executable, '-I', '-B', str(workspace / 'artifact'), 'infer',
                                         '--state', str(workspace / 'ml-runtime'), '--fixture', 'marker'],
                                        capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, result.stderr
                value = json.loads(result.stdout)
                assert value['scores'] == ([.75, .25] if task == 'classifier' else [1., 0., 0., 0.]), value
                return workspace

            with (root / 'daemon.log').open('w') as log:
                def start():
                    return subprocess.Popen([str(binary)], env={**os.environ, 'EDGE_CONFIG': str(config)}, stdout=log, stderr=log)

                process = start()
                wait_for(lambda: journal().get('initialized'), 'daemon did not initialize', process)
                for id, task in [('0x701', 'classifier'), ('0x702', 'detector')]:
                    deploy(id, task)
                    infer(task)
                old = infer('classifier')
                deploy('0x703', 'classifier')
                assert infer('classifier') != old, 'replacement reused old deployment workspace'
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                process = start()
                def running():
                    path = state / 'supervised' / 'status.json'
                    values = json.loads(path.read_text()) if path.exists() else {}
                    return all(values.get('suifleet-ml-' + task, {}).get('phase') == 'running'
                               for task in ('classifier', 'detector'))
                wait_for(running, 'ML workers did not resume on daemon restart', process)
                for task in ('classifier', 'detector'):
                    infer(task)
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                process = None
                assert not sim.errors, sim.errors
                print('PASS: generated classifier/detector recipes retain executable inference after daemon cleanup, replacement and clean restart')
    finally:
        if process and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=20)
        shim.terminate()
        shim.wait(timeout=10)
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheels', required=True, type=Path)
    parser.add_argument('--binary', type=Path, default=Path(__file__).resolve().parents[1] / 'suifleet-edge/target/debug/suifleet')
    args = parser.parse_args()
    run(args.binary.resolve(), args.wheels)
