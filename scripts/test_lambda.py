#!/usr/bin/env python3
"""SF-069 function manager with the real daemon. Run as root in a disposable,
network-isolated container (make test-lambda): root only creates the real,
distinct component UIDs that SO_PEERCRED authenticates. Local chain simulator;
no keys, gas, public network, AWS or hardware.

The manager runs as its own component UID with only CAP_SETUID/SETGID/CHOWN
(ambient), and starts functions as a separate UID with no capabilities.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts/tests'))
import test_ota
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for
from test_lambda_manager import HANDLER, alive, wait_for as wait_until

SENSOR, MANAGER, CONSUMER, INTRUDER, OTHER, FUNCTION = 64001, 64002, 64003, 64004, 64005, 64006


def run(evidence=None):
    assert os.geteuid() == 0, 'run in a disposable root container (make test-lambda)'
    passed = []

    def ok(text):
        passed.append(text)
        print(f'PASS: {text}', flush=True)

    binary = ROOT / 'suifleet-edge/target/debug/suifleet'
    test_ota.SHIM = ROOT / 'suifleet-edge/target/debug/examples/grpc_sim_shim'
    sim = Simulator()
    sim.allow_reports, sim.fail_execution, sim.truncate_next_artifact = True, False, False

    class Chain(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:
                result = dict(jsonrpc='2.0', id=request['id'], result=sim.rpc(request['method'], request['params']))
            except Exception as error:
                result = dict(jsonrpc='2.0', id=request['id'], error=dict(code=-32000, message=str(error)))
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    chain = ThreadingHTTPServer(('127.0.0.1', 0), Chain)
    threading.Thread(target=chain.serve_forever, daemon=True).start()
    shim, rpc_url = start_grpc(f'http://127.0.0.1:{chain.server_port}')
    with tempfile.TemporaryDirectory(prefix='sf069-') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        # Installed read-only code, accessible to the component UIDs.
        code = root / 'code'
        code.mkdir(mode=0o755)
        (code / 'handler.py').write_text(HANDLER)
        for name in ('suifleet_ipc.py', 'suifleet_function.py'):
            (code / name).write_bytes((ROOT / 'sdk/python' / name).read_bytes())
        (code / 'lambda_manager.py').write_text((ROOT / 'scripts/lambda_manager.py').read_text()
                                                .replace("sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))",
                                                         'sys.path.insert(0, str(Path(__file__).resolve().parent))'))
        for path in code.iterdir():
            path.chmod(0o644)
        marks = root / 'marks'
        marks.mkdir()
        marks.chmod(0o1777)
        key = root / 'device.key'
        key.write_text('07' * 32)
        key.chmod(0o600)
        url = f'http://127.0.0.1:{chain.server_port}'
        (root / 'deployment.json').write_text(json.dumps(dict(schema_version=1, protocol_version=1, network='localnet',
            chain_identifier=sim.chain, rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version='3',
            type_origins=TYPE_ORIGINS, shared_objects={'registry': {'id': REGISTRY, 'initial_shared_version': '1'}},
            supported_clients={'dapp': {'min': '0.0.1', 'max_exclusive': '0.1.0'}, 'edge': {'min': '0.1.0', 'max_exclusive': '0.3.0'}},
            walrus={'publisher_url': url, 'aggregator_url': url})))
        run_dir = root / 'run'
        run_dir.mkdir(mode=0o755)
        socket_path = run_dir / 'ipc.sock'
        publish = '[[ipc.components.{0}.pubsub_policies]]\noperations = ["aws.greengrass#PublishToTopic"]\nresources = [{1}]\n'
        subscribe = '[[ipc.components.{0}.pubsub_policies]]\noperations = ["aws.greengrass#SubscribeToTopic"]\nresources = [{1}]\n'
        config = root / 'config.toml'
        config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
            f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
            f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = "/bin/false"\n'
            f'[runtime]\nstate_dir = "{root / "state"}"\npoll_seconds = 1\nevent_discovery_seconds = 1\n'
            f'[ipc]\nsocket = "{socket_path}"\nadministrators = [0]\n'
            f'[ipc.components.sensor]\nuid = {SENSOR}\noperations = ["publish"]\n' + publish.format('sensor', '"sensors/*"') +
            f'[ipc.components.other]\nuid = {OTHER}\noperations = ["publish"]\n' + publish.format('other', '"sensors/*"') +
            f'[ipc.components.intruder]\nuid = {INTRUDER}\noperations = ["publish"]\n' + publish.format('intruder', '"intruder/*"') +
            f'[ipc.components.functions]\nuid = {MANAGER}\noperations = ["subscribe", "receive", "ack", "unsubscribe", "publish"]\n'
            + subscribe.format('functions', '"sensors/temp", "sensors/count"')
            + publish.format('functions', '"functions/results"') +
            f'[ipc.components.consumer]\nuid = {CONSUMER}\noperations = ["subscribe", "receive", "ack", "unsubscribe"]\n'
            + subscribe.format('consumer', '"functions/results"') +
            '[pubsub]\nmax_messages_per_subscription = 100\n')
        manager_root = root / 'manager'
        manager_root.mkdir(mode=0o755)
        os.chown(manager_root, MANAGER, MANAGER)
        functions_config = root / 'functions.json'
        handler = [sys.executable, str(code / 'handler.py')]
        common = dict(command=handler, result_topic='functions/results', run_as={'uid': FUNCTION, 'gid': FUNCTION},
                      environment={'MARK_DIR': str(marks)})
        functions_config.write_text(json.dumps(dict(
            version=1, socket=str(socket_path), component='functions', state_dir=str(manager_root / 'state'),
            work_dir=str(manager_root / 'work'),
            functions={'enrich': dict(common, mode='on-demand', timeout_ms=1500, max_concurrency=2),
                       'counter': dict(common, mode='long-lived', timeout_ms=1500)},
            subscriptions=[dict(source='sensor', subject='sensors/temp', target='enrich'),
                           dict(source='sensor', subject='sensors/count', target='counter')])))
        functions_config.chmod(0o644)
        log = (root / 'daemon.log').open('a')
        processes = {}

        def start_daemon():
            processes['daemon'] = subprocess.Popen([str(binary)], env={**os.environ, 'EDGE_CONFIG': str(config)}, stdout=log, stderr=log)
            wait_for(lambda: socket_path.exists(), 'IPC socket did not appear', processes['daemon'])

        def stop_daemon(kill=False):
            (processes['daemon'].kill if kill else processes['daemon'].terminate)()
            processes['daemon'].wait(timeout=30)
            if socket_path.exists():
                socket_path.unlink()

        def start_manager():
            processes['manager'] = subprocess.Popen(
                ['setpriv', f'--reuid={MANAGER}', f'--regid={MANAGER}', '--clear-groups',
                 '--inh-caps=+setuid,+setgid,+chown', '--ambient-caps=+setuid,+setgid,+chown',
                 sys.executable, str(code / 'lambda_manager.py'), '--config', str(functions_config)],
                stdout=log, stderr=log, cwd='/')

        def stop_manager(kill=False):
            processes['manager'].send_signal(signal.SIGKILL if kill else signal.SIGTERM)
            processes['manager'].wait(timeout=30)

        def ipc(uid, operation, component, topic='', payload=None):
            program = ('import os,sys,json; uid=int(sys.argv[1]); uid and (os.setgid(uid), os.setuid(uid)); '
                       'sys.path.insert(0, sys.argv[7]); from suifleet_ipc import Client, IpcError\n'
                       "try: print(json.dumps({'ok': True, 'data': Client(sys.argv[2], component=sys.argv[3]).request(sys.argv[4], topic=sys.argv[5], payload=json.loads(sys.argv[6]))}))\n"
                       "except IpcError as e: print(json.dumps({'ok': False, 'error': str(e)}))")
            out = subprocess.run([sys.executable, '-c', program, str(uid), str(socket_path), component, operation, topic,
                                  json.dumps(payload), str(code)], text=True, capture_output=True)
            assert out.returncode == 0, out.stderr
            return json.loads(out.stdout)

        received = []

        def results(count, timeout=30):
            """The next `count` result envelopes, received and acknowledged as the consumer."""
            start = len(received)

            def poll():
                while True:
                    message = ipc(CONSUMER, 'receive', 'consumer', 'functions/results')['data']
                    if not message:
                        return len(received) >= start + count
                    assert message['sender'] == 'functions', message
                    received.append(message['payload'])
                    assert ipc(CONSUMER, 'ack', 'consumer', 'functions/results', message['id'])['ok']
            wait_until(poll, f'expected {count} results, got {len(received) - start}', timeout=timeout)
            assert len(received) == start + count, received[start:]
            return received[start:]

        def invokes():
            path = marks / 'invoke'
            return [json.loads(line.split(' ', 2)[2]) for line in path.read_text().splitlines()] if path.exists() else []

        try:
            start_daemon()
            assert ipc(CONSUMER, 'subscribe', 'consumer', 'functions/results', {'qos': 1})['ok']
            start_manager()
            wait_for(lambda: (marks / 'start').exists(), 'long-lived function did not start with the manager')

            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/temp', {'op': 'echo', 'value': 42})['ok']
            [echo] = results(1)
            assert echo['status'] == 'ok' and echo['result']['payload'] == {'op': 'echo', 'value': 42}, echo
            assert (echo['source'], echo['subject'], echo['function'], echo['attempt']) == ('sensor', 'sensors/temp', 'enrich', 1), echo
            ok('on-demand function: real daemon durable pub/sub -> journaled invocation -> function process -> '
               'versioned result envelope delivered to an authenticated subscriber')

            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/temp', {'op': 'env'})['ok']
            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/temp', {'op': 'caps'})['ok']
            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/temp',
                       {'op': 'impersonate', 'socket': str(socket_path), 'component': 'functions', 'topic': 'functions/results'})['ok']
            env, caps, forged = sorted(results(3), key=lambda r: r['message_id'])
            assert (env['result']['uid'], env['result']['gid'], env['result']['groups']) == (FUNCTION, FUNCTION, []), env
            assert caps['result']['CapEff'] == caps['result']['CapPrm'] == caps['result']['CapAmb'] == '0000000000000000', caps
            assert caps['result']['NoNewPrivs'] == '1', caps
            assert forged['status'] == 'ok' and forged['result'].startswith('denied: '), forged
            ok('isolation: function runs as its own UID without groups/capabilities (no-new-privs) and cannot use '
               'the manager IPC identity (peer UID refused)')

            refused = ipc(INTRUDER, 'publish', 'intruder', 'sensors/temp', {'op': 'echo'})
            assert refused == {'ok': False, 'error': 'request_failed'}, refused
            before = len(invokes())
            assert ipc(OTHER, 'publish', 'other', 'sensors/temp', {'op': 'echo', 'tag': 'unrouted'})['ok']
            time.sleep(1.5)
            assert len(invokes()) == before, 'an unsubscribed source invoked a function'
            ok('permissions: daemon ACL refuses an unauthorized publisher; an authorized but unsubscribed source is not routed')

            for n in range(2):
                assert ipc(SENSOR, 'publish', 'sensor', 'sensors/count', {'op': 'echo', 'n': n})['ok']
            first, second = sorted(results(2), key=lambda r: r['message_id'])
            assert (first['result']['count'], second['result']['count']) == (1, 2), (first, second)
            assert first['result']['pid'] == second['result']['pid']
            ok('long-lived function: started with the manager, one process keeps state across invocations')

            def sleeper(tag):
                return int(next(line for line in (marks / 'sleeping').read_text().splitlines() if line.endswith(' ' + tag)).split()[0])

            # The manager may not signal the function UID directly; deadlines must still hold
            # for functions that would outlive the whole test.
            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/temp', {'op': 'raise'})['ok']
            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/temp', {'op': 'sleep', 'seconds': 600, 'tag': 'od'})['ok']
            error, timeout = sorted(results(2, timeout=10), key=lambda r: r['message_id'])
            assert error['status'] == 'error' and error['error'] == {'type': 'Validation', 'message': 'bad input'}, error
            assert timeout['status'] == 'timeout' and 'result' not in timeout and timeout['duration_ms'] < 3000, timeout
            wait_until(lambda: not alive(sleeper('od')), 'timed-out on-demand function survived', timeout=5)
            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/temp', {'op': 'escape'})['ok']
            [escaped] = results(1, timeout=10)
            escapee = int((marks / 'escaped').read_text())
            assert escaped['status'] == 'ok', escaped
            wait_until(lambda: not alive(escapee), 'escaped function descendant survived', timeout=5)
            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/count', {'op': 'sleep', 'seconds': 600, 'tag': 'll'})['ok']
            [timeout] = results(1, timeout=10)
            assert timeout['status'] == 'timeout' and timeout['duration_ms'] < 3000, timeout
            wait_until(lambda: not alive(sleeper('ll')), 'timed-out long-lived function survived', timeout=5)
            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/temp', {'op': 'echo'})['ok']
            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/count', {'op': 'echo'})['ok']
            after = sorted(results(2, timeout=15), key=lambda r: r['message_id'])
            assert [r['status'] for r in after] == ['ok', 'ok'] and after[1]['result']['count'] == 1, after
            assert processes['manager'].poll() is None
            ok('error and timeout envelopes: handler error type/message; on-demand and long-lived functions of another '
               'UID that outlive their deadline are killed promptly with escaped descendants; later invocations '
               'run (long-lived restarted)')

            # Manager SIGKILL during an invocation: the function dies with it and runs again.
            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/temp', {'op': 'sleep', 'seconds': 1, 'tag': 'restart'})['ok']
            wait_for(lambda: any(i.get('tag') == 'restart' for i in invokes()), 'interrupted invocation did not start')
            stop_manager(kill=True)
            # Daemon SIGKILL while messages wait in the manager's durable subscription.
            assert ipc(SENSOR, 'publish', 'sensor', 'sensors/temp', {'op': 'echo', 'tag': 'queued'})['ok']
            stop_daemon(kill=True)
            start_daemon()
            start_manager()
            replayed, queued = sorted(results(2), key=lambda r: r['message_id'])
            assert (replayed['status'], replayed['attempt'], replayed['result']) == ('ok', 2, 'slept'), replayed
            assert (queued['status'], queued['attempt']) == ('ok', 1), queued
            assert [i['attempt'] for i in invokes() if i.get('tag') == 'restart'] == [1, 2]
            assert [i['attempt'] for i in invokes() if i.get('tag') == 'queued'] == [1]
            time.sleep(1)
            assert results(0) == [] and ipc(CONSUMER, 'receive', 'consumer', 'functions/results')['data'] is None
            ok('restart: manager and daemon SIGKILL; interrupted invocation re-run once with attempt 2, '
               'durably queued message invoked once; no duplicate result')

            stop_manager()
            assert processes['manager'].returncode == 0
            ok('graceful manager stop')
            if evidence:
                head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True, capture_output=True).stdout.strip()
                Path(evidence).write_text(json.dumps({'ticket': 'SF-069', 'head': head or None, 'checks': passed}, indent=2) + '\n')
        except Exception:
            print((root / 'daemon.log').read_text()[-6000:])
            raise
        finally:
            for name in ('manager', 'daemon'):
                process = processes.get(name)
                if process and process.poll() is None:
                    process.kill()
                    process.wait()
            shim.terminate()


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
