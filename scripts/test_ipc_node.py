#!/usr/bin/env python3
"""SF-302 TypeScript/Node component SDK against the real daemon.

Run as root in a disposable, network-isolated container (make test-ipc-node):
root only creates real, distinct Unix peer UIDs for SO_PEERCRED. Local chain and
Walrus simulators; no keys, gas, public network, firmware or hardware.

The SDK under test is the packed npm tarball (--package) or, without it, the
built checkout package. Its runnable examples run unchanged as separate UIDs.
Covers configuration, health, admin install/status/logs, volatile and durable
pub/sub, streams, shadows, opaque-data refusal, default-deny authorization,
reconnects across a daemon restart, bounded buffering under daemon
backpressure, and wire interoperability with the Python SDK.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_ota
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for

ROOT = Path(__file__).resolve().parents[1]
SENSOR, CONSUMER, INBOX, APP, STRANGER = 64001, 64002, 64003, 64004, 64005
NAMES = {SENSOR: 'sensor', CONSUMER: 'consumer', INBOX: 'inbox', APP: 'app', STRANGER: 'stranger', 0: ''}
THING = 'dev1'
DELTA = f'$aws/things/{THING}/shadow/name/config/update/delta'
SPOOL = 16  # durable QoS 1 messages per subscription


def drop(uid):
    def credentials():
        if uid:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
    return credentials


def run(package=None):
    assert os.geteuid() == 0, 'run in a disposable root container (make test-ipc-node)'
    node = shutil.which('node')
    assert node, 'node is required'
    version = subprocess.run([node, '--version'], capture_output=True, text=True, check=True).stdout.strip()
    print(f'node {version}', flush=True)
    binary = ROOT / 'suifleet-edge/target/debug/suifleet'
    sim = Simulator()
    sim.allow_reports, sim.fail_execution, sim.truncate_next_artifact = True, False, False
    sim.add(test_ota.object_id('0x701'), event=dict(component_name='sensor'), recipe=dict(schemaVersion='1.2',
            lifecycle=dict(run='echo sensor-started; exec sleep 300'),
            configuration=dict(defaults=dict(interval_ms=0, unit='celsius'))))

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
    daemon = {}
    try:
        with tempfile.TemporaryDirectory(prefix='sf302-') as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            # Foreign UIDs get a readable copy of exactly the package under test.
            pkg = root / 'pkg'
            if package:
                with tarfile.open(package) as archive:
                    archive.extractall(root / 'unpacked', filter='data')
                shutil.copytree(root / 'unpacked/package', pkg)
            else:
                source = ROOT / 'sdk/typescript'
                pkg.mkdir()
                shutil.copy(source / 'package.json', pkg)
                shutil.copytree(source / 'dist', pkg / 'dist')
                shutil.copytree(source / 'examples', pkg / 'examples')
            shutil.copy(ROOT / 'sdk/typescript/test/scenario.mjs', pkg / 'scenario.mjs')
            pysdk = root / 'pysdk'
            pysdk.mkdir()
            shutil.copy(ROOT / 'sdk/python/suifleet_ipc.py', pysdk)
            for path in [root / 'unpacked', pkg, pysdk]:
                for item in [path, *path.rglob('*')] if path.exists() else []:
                    item.chmod(0o755 if item.is_dir() else 0o644)
            assert (pkg / 'dist/index.js').exists() and (pkg / 'dist/index.d.ts').exists(), 'package lacks built output'
            key = root / 'device.key'
            key.write_text('07' * 32)
            key.chmod(0o600)
            url = f'http://127.0.0.1:{server.server_port}'
            (root / 'deployment.json').write_text(json.dumps(dict(schema_version=1, protocol_version=1, network='localnet',
                chain_identifier=sim.chain, rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP,
                package_version='3', type_origins=TYPE_ORIGINS,
                shared_objects={'registry': {'id': REGISTRY, 'initial_shared_version': '1'}},
                supported_clients={'dapp': {'min': '0.0.1', 'max_exclusive': '0.1.0'}, 'edge': {'min': '0.1.0', 'max_exclusive': '0.3.0'}},
                walrus={'publisher_url': url, 'aggregator_url': url})))
            state, sock = root / 'state', root / 'ipc.sock'
            config = root / 'config.toml'
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state))}\n'
                'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 3\ncomponent_stop_timeout_seconds = 1\n'
                f'[ipc]\nsocket = {json.dumps(str(sock))}\nadministrators = [0]\n'
                f'[ipc.components.sensor]\nuid = {SENSOR}\n'
                'operations = ["config", "health", "status", "publish", "stream_create", "stream_append", "stream_read", '
                '"stream_describe", "stream_list", "data_put"]\n'
                'publish = ["sensor/events", "bulk/samples"]\nstreams = ["sensor-*"]\n'
                f'[ipc.components.consumer]\nuid = {CONSUMER}\noperations = ["subscribe", "receive", "ack", "unsubscribe"]\n'
                '[[ipc.components.consumer.pubsub_policies]]\noperations = ["aws.greengrass#SubscribeToTopic"]\nresources = ["sensor/#"]\n'
                f'[ipc.components.inbox]\nuid = {INBOX}\noperations = ["receive"]\nsubscribe = ["bulk/samples"]\n'
                f'[ipc.components.app]\nuid = {APP}\n'
                'operations = ["shadow_get", "shadow_update", "shadow_list", "subscribe", "receive", "ack"]\n'
                '[[ipc.components.app.shadow_policies]]\n'
                'operations = ["aws.greengrass#GetThingShadow", "aws.greengrass#UpdateThingShadow"]\n'
                f'resources = ["$aws/things/{THING}/shadow/name/config"]\nsections = ["reported"]\n'
                '[[ipc.components.app.pubsub_policies]]\n'
                f'operations = ["aws.greengrass#SubscribeToTopic"]\nresources = ["{DELTA}"]\n'
                '[streams]\nmax_streams = 4\nmax_total_bytes = 262144\n'
                f'[pubsub]\nmax_messages_per_subscription = {SPOOL}\n'
                f'[shadows]\nthing_name = "{THING}"\nmax_shadows = 4\n')
            check = subprocess.run([str(binary), '--check-config', str(config)], capture_output=True, text=True)
            assert check.returncode == 0, check.stderr
            log = (root / 'daemon.log').open('a')

            def start():
                daemon['p'] = subprocess.Popen([str(binary)], env={**os.environ, 'EDGE_CONFIG': str(config)}, stdout=log, stderr=log)
                wait_for(sock.exists, 'IPC socket did not appear', daemon['p'])

            def stop():
                daemon['p'].terminate()
                daemon['p'].wait(timeout=30)
                daemon.pop('p')
                assert not sock.exists(), 'socket not cleaned after graceful shutdown'

            def scenario(uid, spec, timeout=120):
                spec = {'socket': str(sock), 'component': NAMES[uid], **spec}
                result = subprocess.run([node, str(pkg / 'scenario.mjs'), json.dumps(spec)], preexec_fn=drop(uid),
                                        capture_output=True, text=True, timeout=timeout, cwd=pkg)
                assert result.returncode == 0, (spec, result.stdout, result.stderr)
                return json.loads(result.stdout.splitlines()[-1])

            def call(uid, operation, topic='', payload=None, **extra):
                return scenario(uid, dict(action='call', operation=operation, topic=topic, payload=payload, **extra))

            def ok(uid, operation, topic='', payload=None, **extra):
                result = call(uid, operation, topic, payload, **extra)
                assert result['ok'], (operation, result)
                return result['data']

            def denied(uid, operation, topic='', payload=None, code='request_failed', **extra):
                result = call(uid, operation, topic, payload, **extra)
                assert not result['ok'] and result['error']['name'] == 'IpcError' and result['error']['code'] == code, (uid, operation, result)

            def example(uid, name, env, timeout=120):
                environment = {'PATH': os.environ['PATH'], 'SUIFLEET_IPC_SOCKET': str(sock), 'SUIFLEET_COMPONENT': NAMES[uid], **env}
                result = subprocess.run([node, str(pkg / 'examples' / name)], preexec_fn=drop(uid), env=environment,
                                        capture_output=True, text=True, timeout=timeout, cwd=pkg)
                assert result.returncode == 0, (name, result.stdout, result.stderr)
                return json.loads(result.stdout.splitlines()[-1])

            def python(uid, operation, topic='', payload=None):
                """The unmodified Python SDK on the same daemon (wire interoperability)."""
                code = ('import json,sys; sys.path.insert(0, sys.argv[1]); from suifleet_ipc import Client; '
                        'print(json.dumps(Client(sys.argv[2], sys.argv[3]).request(sys.argv[4], topic=sys.argv[5], payload=json.loads(sys.argv[6]))))')
                result = subprocess.run([sys.executable, '-c', code, str(pysdk), str(sock), NAMES[uid], operation, topic,
                                         json.dumps(payload)], preexec_fn=drop(uid), capture_output=True, text=True, timeout=130)
                assert result.returncode == 0, (operation, result.stderr)
                return json.loads(result.stdout)

            start()
            # Administrator management through the Node SDK.
            assert ok(0, 'list')['installed'] == {}
            installed = ok(0, 'install', payload='0x701')
            assert installed['status'] == 1, installed
            assert ok(0, 'status', component='sensor')['runtime']['phase'] == 'running'
            assert 'sensor-started' in ok(0, 'logs', component='sensor')['tail']
            print('PASS: administrator install/list/status/logs through the Node SDK', flush=True)

            # Runnable examples, each as its own kernel-authenticated UID.
            assert ok(CONSUMER, 'subscribe', 'sensor/#', {'qos': 1}) == {'subscribed': 'sensor/#', 'qos': 1}
            sensor = example(SENSOR, 'sensor.mjs', {'SENSOR_COUNT': '5'})
            assert sensor['configuration'] == {'interval_ms': 0, 'unit': 'celsius'}, sensor
            assert (sensor['sent'], sensor['pending'], sensor['stream']['newest']) == (10, 0, 4), sensor
            assert ok(0, 'status', component='sensor')['reported_health']['state'] == 'healthy'
            readings = python(SENSOR, 'stream_read', 'sensor-readings', {'start': 0, 'min': 5, 'max': 5})['messages']
            assert [m['payload'] for m in readings] == [{'index': i, 'celsius': 20 + i / 10} for i in range(5)], readings
            consumed = example(CONSUMER, 'consumer.mjs', {'CONSUMER_COUNT': '5'})['received']
            assert [(m['topic'], m['sender'], m['payload']['index']) for m in consumed] == [('sensor/events', 'sensor', i) for i in range(5)]
            python(SENSOR, 'publish', 'sensor/events', {'from': 'python'})
            assert example(CONSUMER, 'consumer.mjs', {})['received'][0]['payload'] == {'from': 'python'}
            stats = [s for s in ok(0, 'messaging')['subscriptions'] if s['principal'] == 'consumer']
            assert [s['queued'] for s in stats] == [0], stats
            print('PASS: sensor/consumer examples: configuration, health, durable stream, QoS 1 pub/sub with ack; '
                  'Python SDK reads Node-written streams and Node consumes Python publications', flush=True)

            # Default-deny authorization by kernel UID; spoofed names do not help.
            denied(SENSOR, 'logs')
            denied(SENSOR, 'install', payload='0x701')
            denied(SENSOR, 'messaging')
            denied(SENSOR, 'config', component='consumer')
            denied(SENSOR, 'publish', 'consumer/private', 'x')
            denied(SENSOR, 'stream_append', 'other-stream', 1)
            denied(CONSUMER, 'publish', 'sensor/events', 'spoof', component='sensor')
            denied(CONSUMER, 'subscribe', 'sensor/+', {'qos': 1})
            denied(APP, 'shadow_update', 'config', {'state': {'desired': {'mode': 'x'}}})
            stranger = call(STRANGER, 'status', component='sensor')
            assert not stranger['ok'] and stranger['error']['name'] == 'TransportError' and stranger['error']['outcome'] == 'unknown', stranger
            assert ok(CONSUMER, 'receive', 'sensor/#') is None, 'a denied publication was delivered'
            # The packaged CLI uses the same client and authorization.
            cli = [node, str(pkg / 'dist/cli.js'), '--socket', str(sock), '--component', 'sensor']
            shown = subprocess.run([*cli, 'config'], preexec_fn=drop(SENSOR), capture_output=True, text=True, timeout=60)
            assert shown.returncode == 0 and json.loads(shown.stdout)['configuration']['effective']['unit'] == 'celsius', shown
            refused = subprocess.run([*cli, 'logs'], preexec_fn=drop(SENSOR), capture_output=True, text=True, timeout=60)
            assert refused.returncode == 1 and refused.stderr.strip() == 'rejected: request_failed', refused
            print('PASS: default-deny by peer UID: admin-only, cross-component, topic, stream, section and spoofed-name '
                  'requests refused without side effects; unmapped UID closed; packaged CLI allowed/refused', flush=True)

            # Bounded buffering against the daemon's real bounds.
            # (a) Volatile inbox: 32 messages per principal/topic, atomic backpressure.
            items = [dict(kind='publish', topic='bulk/samples', payload={'n': n}) for n in range(40)]
            buffered = subprocess.Popen([node, str(pkg / 'scenario.mjs'), json.dumps(dict(socket=str(sock), component='sensor',
                action='buffer', items=items, maxItems=36))], preexec_fn=drop(SENSOR), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=pkg)
            first = json.loads(buffered.stdout.readline())
            assert (first['overflow'], first['sent'], first['pending']) == (4, 32, 4), first
            assert first['error']['name'] == 'IpcError' and first['error']['code'] == 'request_failed', first
            drained = [python(INBOX, 'receive', 'bulk/samples')['payload']['n'] for _ in range(10)]
            buffered.stdin.write('drained\n')
            buffered.stdin.flush()
            second = json.loads(buffered.stdout.readline())
            assert buffered.wait(timeout=30) == 0, buffered.stderr.read()
            assert (second['sent'], second['pending'], second['error']) == (4, 0, None), second
            while (message := python(INBOX, 'receive', 'bulk/samples')) is not None:
                drained.append(message['payload']['n'])
            assert drained == list(range(36)), drained
            # (b) Durable QoS 1 spool bound (max_messages_per_subscription).
            items = [dict(kind='publish', topic='sensor/events', payload={'n': n}) for n in range(SPOOL + 4)]
            buffered = subprocess.Popen([node, str(pkg / 'scenario.mjs'), json.dumps(dict(socket=str(sock), component='sensor',
                action='buffer', items=items, maxItems=64))], preexec_fn=drop(SENSOR), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=pkg)
            first = json.loads(buffered.stdout.readline())
            assert (first['sent'], first['pending'], first['error']['code']) == (SPOOL, 4, 'request_failed'), first
            got = [m['payload']['n'] for m in example(CONSUMER, 'consumer.mjs', {'CONSUMER_COUNT': '10'})['received']]
            buffered.stdin.write('drained\n')
            buffered.stdin.flush()
            second = json.loads(buffered.stdout.readline())
            assert buffered.wait(timeout=30) == 0, buffered.stderr.read()
            assert (second['sent'], second['pending']) == (4, 0), second
            got += [m['payload']['n'] for m in example(CONSUMER, 'consumer.mjs', {'CONSUMER_COUNT': '10'})['received']]
            assert got == list(range(SPOOL + 4)), got
            # (c) RejectNewData stream at max_size: `full`, nothing dropped or reordered locally.
            ok(SENSOR, 'stream_create', 'sensor-small', {'max_size': 1024, 'strategy_on_full': 'RejectNewData'})
            items = [dict(kind='stream', stream='sensor-small', payload={'n': n, 'pad': 'p' * 90}) for n in range(40)]
            full = scenario(SENSOR, dict(action='buffer', items=items, maxItems=40, phases=1))
            assert full['error']['code'] == 'full' and 0 < full['sent'] < 40 and full['sent'] + full['pending'] == 40, full
            small = ok(SENSOR, 'stream_describe', 'sensor-small')
            assert (small['oldest'], small['newest'], small['messages']) == (0, full['sent'] - 1, full['sent']), small
            print('PASS: bounded buffer under real daemon backpressure: volatile inbox (32), durable spool '
                  f'({SPOOL}) and RejectNewData stream `full`; local overflow refused, delivery in order without loss or duplicates', flush=True)

            # Reconnect across a daemon restart. Only unsent requests are retried.
            python(SENSOR, 'publish', 'sensor/events', 'before-restart')
            stop()
            down = call(SENSOR, 'config')
            assert down['error']['name'] == 'TransportError' and down['error']['outcome'] == 'not_sent' \
                and down['error']['errno'] == 'ENOENT', down
            consumer = subprocess.Popen([node, str(pkg / 'examples/consumer.mjs')], preexec_fn=drop(CONSUMER), cwd=pkg,
                env={'PATH': os.environ['PATH'], 'SUIFLEET_IPC_SOCKET': str(sock), 'SUIFLEET_COMPONENT': 'consumer',
                     'CONSUMER_COUNT': '2', 'CONSUMER_TIMEOUT_MS': '90000'}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            waiting = subprocess.Popen([node, str(pkg / 'scenario.mjs'), json.dumps(dict(socket=str(sock), component='sensor',
                action='call', operation='config', topic='', payload=None, connectAttempts=60, connectRetryDelayMs=200))],
                preexec_fn=drop(SENSOR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=pkg)
            time.sleep(2)
            assert consumer.poll() is None and waiting.poll() is None, 'clients gave up while the daemon was down'
            start()
            out, err = waiting.communicate(timeout=60)
            resumed = json.loads(out.splitlines()[-1])
            assert resumed['ok'] and resumed['data']['configuration']['effective']['unit'] == 'celsius', (resumed, err)
            assert resumed['elapsed_ms'] >= 1500, resumed
            python(SENSOR, 'publish', 'sensor/events', 'after-restart')
            out, err = consumer.communicate(timeout=90)
            assert consumer.returncode == 0, err
            assert [m['payload'] for m in json.loads(out.splitlines()[-1])['received']] == ['before-restart', 'after-restart']
            print('PASS: reconnect: missing socket is not_sent (ENOENT); connect retries and the consumer iterator span a '
                  'daemon restart; the unacknowledged durable message is redelivered in order', flush=True)

            # Shadows: desired from an operator, the example reports it back.
            ok(APP, 'subscribe', DELTA, {'qos': 1})
            assert ok(0, 'shadow_update', 'config', {'state': {'desired': {'mode': 'active', 'rate': 5}}})['version'] == 1
            applied = example(APP, 'shadow-reporter.mjs', {'SHADOW_THING': THING, 'SHADOW_NAME': 'config'})['applied']
            assert applied == [{'delta': {'mode': 'active', 'rate': 5}, 'version': 2}], applied
            document = ok(0, 'shadow_get', 'config')
            assert document['state']['reported'] == {'mode': 'active', 'rate': 5} and not document['state'].get('delta'), document
            denied(APP, 'shadow_update', 'config', {'state': {'reported': {'mode': 'stale'}}, 'version': 1}, code='conflict')
            denied(0, 'shadow_get', 'missing', code='not-found')
            assert ok(APP, 'shadow_list')['shadows'] == []  # no ListNamedShadowsForThing grant matches
            print('PASS: shadow reporter example follows desired through delta notifications with conditional updates; '
                  'conflict and not-found protocol codes', flush=True)

            # Opaque data is refused when the operator did not enable it; the error keeps the resume point.
            refused = scenario(SENSOR, dict(action='put_data', stream='sensor-raw', size=30000))
            assert refused['error']['name'] == 'DataPutError' and refused['chunk'] == 0 and len(refused['upload']) == 64, refused
            assert refused['cause']['name'] == 'IpcError' and refused['cause']['code'] == 'request_failed', refused
            print('PASS: data_put refusal surfaces DataPutError with upload ID and chunk (opaque storage disabled)', flush=True)

            stop()
            assert not sim.errors, sim.errors
            print('PASS: graceful shutdown removed the socket; simulator saw no errors', flush=True)
    finally:
        if 'p' in daemon:
            daemon['p'].terminate()
            daemon['p'].wait(timeout=15)
        shim.terminate()
        shim.wait(timeout=10)
        server.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--package', help='packed @suifleet/component-ipc tarball to test (default: built checkout)')
    run(parser.parse_args().package)
