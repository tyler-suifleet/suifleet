#!/usr/bin/env python3
"""SF-027 real-daemon IPC integration. Run as root in a disposable container.
Uses root only to exercise SO_PEERCRED across real, distinct Unix users.
No public network, real identities, firmware installation or hardware.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_ota
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))
from suifleet_ipc import Client, IpcError


def run():
    assert os.geteuid() == 0, 'run in a disposable root container to test distinct peer UIDs'
    binary = Path(__file__).resolve().parents[1] / 'suifleet-edge/target/debug/suifleet'
    sim = Simulator()
    sim.allow_reports, sim.fail_execution, sim.truncate_next_artifact = True, False, False
    # All fixtures predate the initial cursor. Only local install may run them.
    sim.add(test_ota.object_id('0x701'), event=dict(component_name='svc'), recipe=dict(schemaVersion='1.2',
            lifecycle=dict(install='echo install >> {artifacts}/installs', run='echo hello; exec sleep 300'),
            configuration=dict(defaults=dict(answer=42))))
    sim.add(test_ota.object_id('0x702'), direct=False, event=dict(component_name='untargeted'))
    sim.add(test_ota.object_id('0x703'), bad_hash=True, event=dict(component_name='tampered'))
    sim.add(test_ota.object_id('0x704'), event=dict(component_name='managed'))
    sim.managed.add(test_ota.object_id('0x704'))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, data, status=200):
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:
                result = dict(jsonrpc='2.0', id=request['id'], result=sim.rpc(request['method'], request['params']))
            except Exception as error:
                sim.errors.append(repr(error))
                result = dict(jsonrpc='2.0', id=request['id'], error=dict(code=-32000, message=str(error)))
            self.respond(json.dumps(result).encode())

        def do_GET(self):
            blob = self.path.removeprefix('/v1/blobs/')
            if blob == 'artifact':
                self.respond(test_ota.ARTIFACT)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b'not found', 404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shim, rpc_url = start_grpc(f'http://127.0.0.1:{server.server_port}')
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix='sf027-') as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            sdk = root / 'sdk'
            sdk.mkdir(mode=0o755)
            (sdk / 'suifleet_ipc.py').write_bytes((Path(__file__).resolve().parents[1] / 'sdk/python/suifleet_ipc.py').read_bytes())
            (sdk / 'suifleet_ipc.py').chmod(0o644)
            key = root / 'device.key'
            key.write_text('07' * 32)
            key.chmod(0o600)
            url = f'http://127.0.0.1:{server.server_port}'
            manifest = dict(schema_version=1, protocol_version=1, network='localnet', chain_identifier=sim.chain,
                rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version='3', type_origins=TYPE_ORIGINS,
                shared_objects={'registry': {'id': REGISTRY, 'initial_shared_version': '1'}},
                supported_clients={'dapp': {'min': '0.0.1', 'max_exclusive': '0.1.0'}, 'edge': {'min': '0.1.0', 'max_exclusive': '0.3.0'}},
                walrus={'publisher_url': url, 'aggregator_url': url})
            (root / 'deployment.json').write_text(json.dumps(manifest))
            state = root / 'state'
            path = root / 'ipc.sock'
            config = root / 'config.toml'
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state))}\n'
                'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 3\ncomponent_stop_timeout_seconds = 1\n'
                f'[ipc]\nsocket = {json.dumps(str(path))}\nadministrators = [0]\n'
                '[ipc.components.svc]\nuid = 61001\noperations = ["config", "health", "status", "publish", "receive"]\n'
                'publish = ["svc/events"]\nsubscribe = ["svc/events"]\n'
                '[ipc.components.other]\nuid = 61002\noperations = ["receive"]\nsubscribe = ["svc/events"]\n')
            with (root / 'daemon.log').open('w') as log:
                process = subprocess.Popen([str(binary)], env={**os.environ, 'EDGE_CONFIG': str(config)}, stdout=log, stderr=log)
                def initialized():
                    try:
                        return json.loads((state / 'state.json').read_text()).get('initialized')
                    except (OSError, ValueError):
                        return False
                wait_for(initialized, 'daemon did not initialize', process)
                client = Client(str(path), 'svc')
                assert client.list()['installed'] == {}
                installed = client.install('0x701')
                assert installed['status'] == 1, installed
                assert client.status()['runtime']['phase'] == 'running'
                assert client.configuration()['effective'] == dict(answer=42)
                assert 'hello' in client.logs()
                # Control bytes expand sixfold when JSON encoded. Even a full
                # binary/control-character tail must fit the response frame.
                (state / 'supervised/svc/component.log').write_bytes(b'\x01' * (16 * 1024))
                assert client.logs() == '\x01' * (8 * 1024)
                before = client.status()['runtime']['pid']
                client.stop()
                assert client.status()['runtime']['phase'] == 'stopped'
                client.start()
                assert client.status()['runtime']['pid'] != before
                client.restart()
                assert client.status()['runtime']['phase'] == 'running'
                client.install(test_ota.object_id('0x701'))
                assert next((state / 'supervised/svc/versions').glob('*/installs')).read_text().splitlines() == ['install']
                try:
                    client.install('0x702')
                    raise AssertionError('untargeted install accepted')
                except IpcError:
                    pass
                try:
                    client.install('0x704')
                    raise AssertionError('desired-state install bypass accepted')
                except IpcError:
                    pass
                assert client.install('0x703')['status'] == 2, 'unauthenticated artifact installed'
                assert 'tampered' not in client.list()['installed']
                try:
                    wait_for(lambda: {test_ota.object_id(id) for id, _ in sim.reports} ==
                        {test_ota.object_id('0x701'), test_ota.object_id('0x703')},
                        'local installs did not promptly report at the default 30-minute cadence', process)
                except AssertionError:
                    print('Reports:', sim.reports, flush=True)
                    print((root/'daemon.log').read_text(), flush=True)
                    raise
                print('PASS: local install/list/start/stop/restart/status/logs/config; no replay, wrong-target and digest rejection')

                # Each SDK process drops all groups and changes UID before connecting.
                def as_uid(uid, operation, component='svc', topic='', payload=None, success=True):
                    code = ('import sys; sys.path.insert(0, sys.argv[1]); from suifleet_ipc import Client; import json; '
                            'print(json.dumps(Client(sys.argv[2], sys.argv[3]).request(sys.argv[4], topic=sys.argv[5], payload=json.loads(sys.argv[6]))))')
                    def credentials():
                        os.setgroups([])
                        os.setgid(uid)
                        os.setuid(uid)
                    result = subprocess.run([sys.executable, '-c', code, str(sdk),
                        str(path), component, operation, topic, json.dumps(payload)], preexec_fn=credentials,
                        capture_output=True, text=True, timeout=130)
                    assert (result.returncode == 0) == success, (operation, result.stdout, result.stderr)
                    return json.loads(result.stdout) if success else None
                assert as_uid(61001, 'config')['configuration']['effective'] == dict(answer=42)
                as_uid(61001, 'health', payload='healthy')
                assert client.status()['reported_health']['state'] == 'healthy'
                as_uid(61001, 'logs', success=False)
                as_uid(61001, 'restart', success=False)
                as_uid(61001, 'install', payload='0x702', success=False)
                as_uid(61001, 'config', component='other', success=False)
                as_uid(61001, 'publish', topic='other/events', payload='private', success=False)
                as_uid(61003, 'status', success=False)
                assert as_uid(61001, 'publish', topic='svc/events', payload={'event': 7})['delivered'] == 2
                assert as_uid(61002, 'receive', component='other', topic='svc/events') == dict(sender='svc', payload={'event': 7})
                assert as_uid(61002, 'receive', component='other', topic='svc/events') is None
                client.publish('svc/events', 'operator message')
                assert as_uid(61002, 'receive', component='other', topic='svc/events')['sender'] == '@operator/0'
                print('PASS: kernel-authenticated component SDK config/health/messaging, unknown UID, cross-component and topic denial')
                # Idle peers from an unmapped UID or a mapped component must
                # not consume the operator's admission capacity. The child keeps
                # every socket open until the parent completes an admin request.
                for uid in (61003, 61001):
                    code = ('import os,socket,sys; os.setgroups([]); '
                            'os.setgid(int(sys.argv[2])); os.setuid(int(sys.argv[2])); '
                            'peers=[socket.socket(socket.AF_UNIX) for _ in range(40)]; '
                            '[s.connect(sys.argv[1]) for s in peers]; '
                            'print("ready", flush=True); sys.stdin.readline()')
                    idle = subprocess.Popen([sys.executable, '-c', code, str(path), str(uid)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    try:
                        assert idle.stdout.readline().strip() == 'ready'
                        assert client.status()['runtime']['phase'] == 'running'
                        # All admitted idle connections expire in three seconds.
                        time.sleep(4)
                        assert as_uid(61001, 'config')['configuration']['effective'] == dict(answer=42)
                    finally:
                        idle.communicate('done\n', timeout=10)
                    assert idle.returncode == 0
                print('PASS: unmapped and mapped idle floods preserve administrator access; framing timeout releases UID slots')
                # Spoofed caller fields and future versions cannot reach handlers.
                for request in [dict(version=1, operation='list', uid=0), dict(version=2, operation='list')]:
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.connect(str(path))
                        connection.sendall(json.dumps(request).encode() + b'\n')
                        assert json.loads(connection.recv(65536))['ok'] is False
                process.terminate()
                process.wait(timeout=15)
                process = None
                assert not path.exists(), 'socket not cleaned after shutdown'
                assert not sim.errors, sim.errors
                print('PASS: malformed/version rejection and graceful socket cleanup')
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=15)
        shim.terminate()
        shim.wait(timeout=10)
        server.shutdown()


if __name__ == '__main__':
    run()
