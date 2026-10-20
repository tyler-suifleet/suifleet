#!/usr/bin/env python3
"""SF-081: real-daemon service deployment transaction and UID authorization.
Run as root in a disposable network-none container; all endpoints are loopback.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_ota
from test_ota import Simulator, start_grpc, wait_for, CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP
from test_streams import Receiver

ROOT = Path(__file__).resolve().parents[1]
UID = 63001


class Mqtt:
    """Loopback MQTT protocol peer: CONNACK, QoS1 PUBLISH/PUBACK and ping.
    Independently observe destination topics across bridge replacement.
    """
    def __init__(self):
        self.messages = []
        self.lock = threading.Lock()
        peer = self

        class Handler(socketserver.BaseRequestHandler):
            def read(self, count):
                data = b''
                while len(data) < count:
                    part = self.request.recv(count - len(data))
                    if not part:
                        raise EOFError
                    data += part
                return data

            def handle(self):
                self.request.settimeout(10)
                try:
                    while True:
                        header = self.read(1)[0]
                        size, multiplier = 0, 1
                        for _ in range(4):
                            byte = self.read(1)[0]
                            size += (byte & 127) * multiplier
                            if not byte & 128:
                                break
                            multiplier *= 128
                        body = self.read(size)
                        kind = header >> 4
                        if kind == 1:
                            self.request.sendall(b'\x20\x02\x00\x00')
                        elif kind == 3:
                            length = int.from_bytes(body[:2], 'big')
                            topic = body[2:2 + length].decode()
                            offset = 2 + length
                            qos = (header >> 1) & 3
                            packet_id = body[offset:offset + 2] if qos else b''
                            payload = json.loads(body[offset + (2 if qos else 0):])
                            with peer.lock:
                                peer.messages.append((topic, payload))
                            if qos == 1:
                                self.request.sendall(b'\x40\x02' + packet_id)
                        elif kind == 12:
                            self.request.sendall(b'\xd0\x00')
                        elif kind == 14:
                            break
                        else:
                            raise AssertionError(f'unexpected MQTT packet {kind}')
                except (EOFError, ConnectionError, TimeoutError, OSError):
                    pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
        self.server = Server(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def received(self, topic, payload):
        with self.lock:
            return (topic, payload) in self.messages


def run():
    assert os.geteuid() == 0, 'use a disposable root container'
    binary = ROOT / 'suifleet-edge/target/debug/suifleet'
    sim = Simulator()
    sim.allow_reports, sim.fail_execution, sim.truncate_next_artifact = True, False, False

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
    receiver = None
    mqtt = Mqtt()
    try:
        with tempfile.TemporaryDirectory(prefix='sf081-') as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            key = root / 'device.key'
            key.write_text('07' * 32)
            key.chmod(0o600)
            address = subprocess.run([str(binary), '--derive-address', str(key)], text=True, capture_output=True, check=True).stdout.strip()
            receiver = Receiver(address)
            receiver.mode = "ok"
            token = root / 'export.token'
            token.write_text('export-secret\n')
            token.chmod(0o600)
            url = f'http://127.0.0.1:{server.server_port}'
            (root / 'deployment.json').write_text(json.dumps(dict(schema_version=1, protocol_version=1, network='localnet',
                chain_identifier=sim.chain, rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version='3',
                type_origins=TYPE_ORIGINS, shared_objects={'registry': {'id': REGISTRY, 'initial_shared_version': '1'}},
                supported_clients={'dapp': {'min': '0.0.1', 'max_exclusive': '0.1.0'}, 'edge': {'min': '0.1.0', 'max_exclusive': '0.3.0'}},
                walrus={'publisher_url': url, 'aggregator_url': url})))
            state, sock = root / 'state', root / 'ipc.sock'
            config = root / 'config.toml'
            config.write_text('[sui]\nmanifest_path="deployment.json"\n'
                f'[device]\nkeypair_path="{key}"\ndevice_object_id="{DEVICE}"\ndevice_cap_id="{CAP}"\n'
                f'[walrus]\naggregator_url="{url}"\n[apply]\ncommand="/bin/false"\nallow_recipe_commands=true\ntimeout_seconds=30\n'
                f'[runtime]\nstate_dir="{state}"\npoll_seconds=1\ncomponent_startup_seconds=1\ncomponent_ready_timeout_seconds=2\ncomponent_stop_timeout_seconds=1\ncomponent_max_restarts=0\n'
                '[components.recovery_commands]\napp="true"\n'
                f'[ipc]\nsocket="{sock}"\nadministrators=[0]\n[ipc.components.app]\nuid={UID}\n')
            log = (root / 'daemon.log').open('a')

            def journal():
                return json.loads((state / 'state.json').read_text()) if (state / 'state.json').exists() else {}

            def outcome(id):
                return journal().get('deployments', {}).get(id, {}).get('status')

            def start():
                nonlocal process
                process = subprocess.Popen([str(binary)], env={**os.environ, 'EDGE_CONFIG': str(config)}, stdout=log, stderr=log)
                wait_for(lambda: journal().get('initialized') and sock.exists(), 'daemon startup', process)

            def stop(kill=False):
                (process.kill if kill else process.terminate)()
                process.wait(timeout=30)
                if sock.exists():
                    sock.unlink()

            def ipc(op, topic='', payload=None, admin=False):
                request = dict(version=1, operation=op, component='app', topic=topic, payload=payload)
                code = ('import os,socket,sys,json; uid=int(sys.argv[1]); uid and (os.setgid(uid),os.setuid(uid)); '
                        's=socket.socket(socket.AF_UNIX); s.settimeout(15); s.connect(sys.argv[2]); '
                        's.sendall(sys.argv[3].encode()+b"\\n"); print(s.makefile().readline())')
                result = subprocess.run([sys.executable, '-c', code, '0' if admin else str(UID), str(sock), json.dumps(request)], capture_output=True, text=True, check=True)
                return json.loads(result.stdout)

            def service(stream, topic):
                return dict(version=1, grants={'app': dict(operations=['config', 'publish', 'stream_append', 'stream_read', 'stream_describe'], streams=[stream],
                    pubsub_policies=[dict(operations=['aws.greengrass#PublishToTopic'], resources=[topic])])},
                    pubsub=dict(bridge=dict(broker=f'127.0.0.1:{mqtt.server.server_address[1]}', mappings=[dict(source='Pubsub', target='LocalMqtt', topic=topic, target_topic_prefix=stream + '/')])),
                    streams=dict(max_streams=8, max_total_bytes=65536, definitions=[dict(name=stream, max_size=4096, strategy_on_full='RejectNewData')]))

            def deploy(id, version, services=None, **lifecycle):
                recipe = dict(schemaVersion='1.3', lifecycle=lifecycle or dict(run='exec sleep 300'))
                if services is not None:
                    recipe['services'] = services
                sim.add(id, event=dict(component_name='app', version=version), recipe=recipe)

            try:
                start()
                assert not ipc('publish', 'old')['ok']
                first = service('a', 'old')
                deploy('0x801', '1', first)
                wait_for(lambda: outcome('0x801') == 1, 'initial services deployment', process)
                assert ipc('publish', 'old', {'value': 1})['ok']
                wait_for(lambda: mqtt.received('a/old', {'value': 1}), 'initial bridge forwarding', process)
                assert ipc('stream_append', 'a', 'retained')['ok']
                assert not ipc('publish', 'new')['ok']
                summary = ipc('config')['data']['services']
                assert summary['streams']['definitions'][0]['name'] == 'a'
                assert summary['pubsub']['bridge_mappings'][0]['topic'] == 'old'
                print('PASS: fleet-created streams, live pubsub/bridge configuration and UID grants', flush=True)

                second = service('b', 'new')
                second['streams']['definitions'][0]['export'] = dict(url=f'http://127.0.0.1:{receiver.server.server_port}/ingest',
                    bearer_token_file=str(token), batch_interval_seconds=1, max_backoff_seconds=2)
                deploy('0x802', '2', second)
                wait_for(lambda: outcome('0x802') == 1, 'upgraded services', process)
                assert not ipc('publish', 'old')['ok'] and ipc('publish', 'new', 2)['ok']
                wait_for(lambda: mqtt.received('b/new', 2), 'replacement bridge forwarding', process)
                assert not ipc('stream_append', 'a', 'denied')['ok']
                assert ipc('stream_append', 'b', 'export-me')['ok']
                wait_for(lambda: receiver.received('b'), 'live fleet export', process)
                public = json.dumps(ipc('config')['data']['services'])
                assert str(token) not in public and 'export-secret' not in public and '/ingest' not in public
                print('PASS: upgrade revokes old grants, activates new mapping/export with independent signature verification', flush=True)

                deploy('0x8021', '2.1', service('c', 'temporary'), run='exec sleep 300', restart='on-failure')
                wait_for(lambda: outcome('0x8021') == 1, 'runtime-recoverable generation did not apply', process)
                assert ipc('publish', 'temporary', 1)['ok'] and not ipc('publish', 'new')['ok']
                runtime_pid = json.loads((state / 'supervised/status.json').read_text())['app']['pid']
                os.kill(runtime_pid, signal.SIGKILL)
                wait_for(lambda: journal()['components']['app']['deployment_id'] == '0x802' and
                         journal()['services']['app']['deployment'] == '0x802', 'runtime recovery did not restore service generation', process)
                assert ipc('publish', 'new', 8)['ok'] and not ipc('publish', 'temporary')['ok']
                assert not ipc('stream_append', 'c', 'denied')['ok']
                wait_for(lambda: mqtt.received('b/new', 8), 'restored bridge did not forward after runtime rollback', process)
                assert ipc('stream_append', 'b', 'after-rollback')['ok']
                wait_for(lambda: 'after-rollback' in receiver.received('b').values(), 'restored export did not deliver after runtime rollback', process)
                assert outcome('0x8021') == 1, 'runtime rollback rewrote historical command completion'
                print('PASS: recovery after an applied generation restores prior grants, stream declarations and live bridge/export configuration', flush=True)

                bad = service('c', 'bad')
                bad['streams']['max_total_bytes'] = 1024
                marker = root / 'must-not-run'
                deploy('0x803', '3', bad, install=f'touch {marker}', run='exec sleep 300')
                wait_for(lambda: outcome('0x803') == 2, 'quota preflight rejection', process)
                assert not marker.exists()
                assert ipc('publish', 'new', 3)['ok'] and ipc('stream_read', 'a', admin=True)['data']['messages'][0]['payload'] == 'retained'
                deploy('0x804', '4', service('c', 'failed'), run='exit 1')
                wait_for(lambda: outcome('0x804') == 2, 'failed lifecycle rollback', process)
                assert ipc('publish', 'new', 4)['ok'] and not ipc('publish', 'failed')['ok']
                print('PASS: quota rejection runs no commands; failed replacement restores previous grants and preserves stream data', flush=True)

                stop()
                start()
                wait_for(lambda: ipc('publish', 'new', 5)['ok'], 'restart services recovery', process)
                assert ipc('config')['data']['services']['pubsub']['bridge_mappings'][0]['topic'] == 'new'
                assert (state / 'services-effective.json').exists()
                print('PASS: daemon restart restores committed policies, stream declarations and bridge generation', flush=True)

                interrupted = root / 'install-started'
                deploy('0x805', '5', service('c', 'interrupted'), install=f'echo $$ > {interrupted}; exec sleep 60', run='exec sleep 300')
                wait_for(interrupted.exists, 'interrupted install did not start', process)
                install_pid = int(interrupted.read_text())
                running_pid = json.loads((state / 'supervised/status.json').read_text())['app']['pid']
                stop(kill=True)
                # This plain container has no sealed service cgroup. Preserve
                # the supervisor's fail-closed gate, then explicitly clear this
                # fixture's two exec-only process groups before operator clearance.
                process = subprocess.Popen([str(binary)], env={**os.environ, 'EDGE_CONFIG': str(config)}, stdout=log, stderr=log)
                assert process.wait(timeout=30) != 0, 'unsealed successor bypassed containment gate'
                from test_supervisor import alive
                for pid in (install_pid, running_pid):
                    if not alive(pid):
                        continue
                    try:
                        command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                        assert command[:1] == [b'sleep'], command
                        os.killpg(pid, signal.SIGKILL)
                    except FileNotFoundError:
                        pass
                wait_for(lambda: not alive(install_pid) and not alive(running_pid), 'fixture processes survived clearance')
                (state / 'supervised/instance.json').unlink()
                if sock.exists():
                    sock.unlink()
                start()
                wait_for(lambda: ipc('publish', 'new', 6)['ok'], 'old grants after inspected interrupted install', process)
                assert not ipc('publish', 'interrupted')['ok']
                assert outcome('0x805') is None and journal()['deployments']['0x805']['indeterminate']
                print('PASS: SIGKILL retains old generation; unsealed successor fails closed, inspected restart never replays work', flush=True)

                deploy('0x806', '6', None)
                wait_for(lambda: outcome('0x806') == 1, 'remove service requirements', process)
                assert not ipc('publish', 'new')['ok'] and not ipc('stream_append', 'b', 'denied')['ok']
                summary = ipc('config', admin=True)['data']['services']
                assert not summary['grants']['app']['operations'] and summary['pubsub'] is None and summary['streams'] is None
                print('PASS: removing requirements revokes grants and stops exports/bridge while retaining stream files', flush=True)
                stop()
                start()
                deploy('0x807', '7', service('b', 'restored'))
                wait_for(lambda: outcome('0x807') == 1, 're-enable persisted service', process)
                assert ipc('stream_read', 'b')['data']['messages'][0]['payload'] == 'export-me'
                assert ipc('publish', 'restored', 7)['ok']
                wait_for(lambda: mqtt.received('b/restored', 7), 're-enabled bridge forwarding', process)
                print('PASS: re-enabling disabled services after restart preserves persisted records and activates the new generation', flush=True)
                assert not sim.errors, sim.errors
            except Exception:
                log.flush()
                (ROOT / 'ci-logs').mkdir(exist_ok=True)
                (ROOT / 'ci-logs/sf081-daemon.log').write_text((root / 'daemon.log').read_text())
                raise
            finally:
                if process and process.poll() is None:
                    stop()
                log.close()
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        mqtt.server.shutdown()
        mqtt.server.server_close()
        if receiver:
            receiver.server.shutdown()
        shim.terminate()
        shim.wait(timeout=10)
        server.shutdown()


if __name__ == '__main__':
    run()
