"""SF-069 function manager: real manager and function processes over a native IPC v1 socket.

The local IPC endpoint implements the durable QoS 1 subscribe/receive/ack/publish
subset (head until acknowledged, exact topics). The real daemon path is
scripts/test_lambda.py; these tests drive failure timing that needs control.
"""
import ctypes
import json
import os
from pathlib import Path
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'sdk/python'))
import greengrass_roadmap
import lambda_manager as lm
import suifleet_function

FUNCTION_UID = 61001

HANDLER = r'''
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from suifleet_function import FunctionError, serve
MARK = os.environ['MARK_DIR']
state = {'count': 0}
RAW = {'nan': b'"status":"ok","result":NaN}', 'infinity': b'"status":"ok","result":[-Infinity]}',
       'surrogate': b'"status":"ok","result":"\\ud800"}',
       'error-surrogate': b'"status":"error","error":{"type":"T","message":"\\udc80"}}',
       'bad-utf8': b'"status":"ok","result":"\xff"}',
       'too-deep': b'"status":"ok","result":' + b'[' * 64 + b']' * 64 + b'}',
       'deep': b'"status":"ok","result":' + b'[' * 30000 + b']' * 30000 + b'}',
       'max-deep': b'"status":"ok","result":' + b'[' * 63 + b']' * 63 + b'}'}

def mark(kind, text=''):
    with open(os.path.join(MARK, kind), 'a') as out:
        out.write(f'{os.getpid()} {time.time()} {text}\n')

mark('start')

def handler(payload, context):
    state['count'] += 1
    op = payload.get('op')
    mark('invoke', json.dumps({'op': op, 'attempt': context['attempt'], 'tag': payload.get('tag')}))
    if op == 'echo':
        return {'payload': payload, 'pid': os.getpid(), 'count': state['count'], 'context': context}
    if op == 'env':
        return {'env': dict(os.environ), 'cwd': os.getcwd(), 'uid': os.getuid(), 'gid': os.getgid(),
                'groups': os.getgroups()}
    if op == 'caps':
        status = dict(line.split(':\t', 1) for line in open('/proc/self/status').read().splitlines() if ':\t' in line)
        return {k: status[k].strip() for k in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb', 'NoNewPrivs')}
    if op == 'impersonate':
        from suifleet_ipc import Client, IpcError
        try:
            Client(payload['socket'], component=payload['component']).publish(payload['topic'], {'forged': True})
            return 'published'
        except IpcError as error:
            return 'denied: ' + str(error)
        except OSError as error:      # the daemon drops unknown peer UIDs
            return 'denied: ' + type(error).__name__
    if op == 'read':
        try:
            open(payload['path'], 'rb').close()
            return 'readable'
        except OSError as error:
            return type(error).__name__
    if op == 'raise':
        raise FunctionError('Validation', 'bad input')
    if op == 'value-error':
        raise ValueError('boom')
    if op == 'alloc':
        return len(bytearray(256 << 20))
    if op == 'sleep':
        mark('sleeping', payload.get('tag', ''))
        time.sleep(payload['seconds'])
        mark('woke', payload.get('tag', ''))
        return 'slept'
    if op == 'crash':
        os._exit(3)
    if op == 'big':
        return 'x' * 5000
    if op == 'garbage':
        sys.stdout.write('not json\n'); sys.stdout.flush()
        time.sleep(30)
    if op == 'version2':
        sys.stdout.write(json.dumps({'version': 2, 'invocation_id': context['invocation_id'], 'status': 'ok',
                                     'result': 1}) + '\n'); sys.stdout.flush()
        time.sleep(30)
    if op in RAW:   # responses outside strict JSON, written directly
        head = '{"version":1,"invocation_id":%s,' % json.dumps(context['invocation_id'])
        sys.stdout.buffer.write(head.encode() + RAW[op] + b'\n'); sys.stdout.flush()
        time.sleep(30)
    if op == 'flood':
        sys.stdout.write('x' * 70000); sys.stdout.flush()
        time.sleep(30)
    if op == 'escape':
        child = os.fork()
        if child == 0:
            os.setsid()
            if os.fork():
                os._exit(0)
            with open(os.path.join(MARK, 'escaped'), 'w') as out:
                out.write(str(os.getpid()))
            time.sleep(60)
            os._exit(0)
        os.waitpid(child, 0)
        while not os.path.exists(os.path.join(MARK, 'escaped')):
            time.sleep(0.01)
        return 'escaped'
    return 'unknown'

serve(handler)
'''


class Ipc(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class FakeDaemon:
    """Durable QoS 1 subset of native IPC v1; the caller's component is trusted here."""

    def __init__(self, path, publishers):
        self.publishers, self.lock = publishers, threading.Lock()
        self.queues, self.published, self.seq = {}, [], 0
        self.fail_publish = self.fail_ack = 0
        daemon = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                try:
                    data = daemon.serve(request['component'], request['operation'], request['topic'], request['payload'])
                    reply = dict(version=1, ok=True, data=data)
                except LookupError as error:
                    reply = dict(version=1, ok=False, error=str(error.args[0]))
                self.wfile.write(json.dumps(reply).encode() + b'\n')

        self.server = Ipc(str(path), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def serve(self, component, operation, topic, payload):
        with self.lock:
            if operation == 'subscribe':
                self.queues.setdefault((component, topic), [])
                return {}
            if operation == 'receive':
                queue = self.queues.get((component, topic))
                return queue[0] if queue else None
            if operation == 'ack':
                if self.fail_ack:
                    self.fail_ack -= 1
                    raise LookupError('unavailable')
                queue = self.queues[(component, topic)]
                if queue and queue[0]['id'] == payload:
                    queue.pop(0)
                return {}
            if operation == 'publish':
                if topic not in self.publishers.get(component, ()):
                    raise LookupError('request_failed')
                if self.fail_publish and component == 'functions':
                    self.fail_publish -= 1
                    raise LookupError('backpressure')
                self.seq += 1
                message = dict(id=self.seq, topic=topic, sender=component, payload=payload, published_ms=0)
                self.published.append(message)
                for (_, subject), queue in self.queues.items():
                    if subject == topic:
                        queue.append(message)
                return {}
            raise LookupError('request_failed')

    def inject(self, sender, topic, payload):
        return self.serve(sender, 'publish', topic, payload)

    def results(self, topic='functions/results'):
        with self.lock:
            return [m['payload'] for m in self.published if m['topic'] == topic and m['sender'] == 'functions']

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def wait_for(predicate, message, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(message)


def alive(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return False
    return stat.rpartition(')')[2].split()[0] != 'Z'


class Fixture:
    def __init__(self, root, functions, subscriptions, publishers=None):
        self.root = root
        root.chmod(0o755)
        self.marks = root / 'marks'
        self.marks.mkdir()
        self.marks.chmod(0o1777)
        code = root / 'code'
        code.mkdir(mode=0o755)
        (code / 'handler.py').write_text(HANDLER)
        (code / 'suifleet_function.py').write_bytes((ROOT / 'sdk/python/suifleet_function.py').read_bytes())
        for path in code.iterdir():
            path.chmod(0o644)
        self.handler = [sys.executable, str(code / 'handler.py')]
        for fn in functions.values():
            if os.geteuid() == 0:   # a root manager refuses functions without run_as
                fn.setdefault('run_as', {'uid': FUNCTION_UID, 'gid': FUNCTION_UID})
            fn.setdefault('command', self.handler)
            fn.setdefault('environment', {})['MARK_DIR'] = str(self.marks)
        self.socket = root / 'ipc.sock'
        self.daemon = FakeDaemon(self.socket, publishers or {
            'sensor': {'sensors/temp', 'sensors/other'}, 'other': {'sensors/temp'},
            'functions': {'functions/results', 'functions/second'}})
        self.config = dict(version=1, socket=str(self.socket), component='functions', state_dir=str(root / 'state'),
                           work_dir=str(root / 'work'), functions=functions, subscriptions=subscriptions)
        self.config_path = root / 'functions.json'
        self.write()
        self.process, self.log = None, root / 'manager.log'

    def write(self):
        self.config_path.write_text(json.dumps(self.config))

    def start(self, env=None):
        with self.log.open('a') as log:
            self.process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/lambda_manager.py'), '--config',
                                             str(self.config_path)], stderr=log, env={**os.environ, **(env or {})})
        # Startup subscribes; publications before that would not be queued.
        wait_for(lambda: all(('functions', s['subject']) in self.daemon.queues for s in self.config['subscriptions'])
                 or self.process.poll() is not None, 'manager did not subscribe')
        assert self.process.poll() is None, self.log.read_text()

    def stop(self, kill=False):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL if kill else signal.SIGTERM)
            self.process.wait(30)

    def marks_of(self, kind):
        path = self.marks / kind
        return [line.split(' ', 2) for line in path.read_text().splitlines()] if path.exists() else []

    def result(self, count=1, topic='functions/results', timeout=20):
        return wait_for(lambda: len(self.daemon.results(topic)) >= count and self.daemon.results(topic), 'no result', timeout)

    def close(self):
        self.stop()
        self.daemon.close()


def on_demand(**extra):
    return {**dict(mode='on-demand', timeout_ms=5000, result_topic='functions/results'), **extra}


SUBSCRIBE = [dict(source='sensor', subject='sensors/temp', target='fn')]


class ManagerTests(unittest.TestCase):
    def fixture(self, functions=None, subscriptions=None, start=True):
        temporary = tempfile.TemporaryDirectory(prefix='sf069-')
        self.addCleanup(temporary.cleanup)
        fixture = Fixture(Path(temporary.name), functions or {'fn': on_demand()}, subscriptions or SUBSCRIBE)
        self.addCleanup(fixture.close)
        if start:
            fixture.start()
        return fixture

    def test_reference_subcontract_vectors(self):
        cases = [c for c in json.loads((ROOT / 'interfaces/compat/remaining-roadmap-cases.json').read_text())['cases']
                 if c['scope'] == 'lambda']
        self.assertGreaterEqual(len(cases), 9)
        for case in cases:
            self.assertEqual(lm.reference_decision(case['request']), case['expected'], case['id'])
            self.assertEqual(greengrass_roadmap.evaluate('lambda', case['request']), case['expected'], case['id'])

    def test_contract_matches_runtime(self):
        contract = json.loads((ROOT / 'interfaces/compat/lambda-v1.json').read_text())
        self.assertEqual((contract['version'], contract['ticket'], contract['implementation']), (1, 'SF-069', 'scripts/lambda_manager.py'))
        self.assertEqual(tuple(contract['statuses']), lm.STATUSES)
        self.assertEqual(tuple(contract['modes']), lm.MODES)
        self.assertEqual(contract['limits'], dict(response_line_bytes=lm.MAX_RESPONSE, response_nesting_depth=lm.MAX_DEPTH,
                                                  result_envelope_bytes=lm.MAX_ENVELOPE, timeout_ms=[1, 900000],
                                                  log_bytes_per_function=2 * lm.LOG_BYTES,
                                                  cleanup_wait_ms=int(lm.CLEANUP_WAIT * 1000)))
        self.assertTrue(all(r['url'].startswith('https://docs.aws.amazon.com/greengrass/v2/') and r['version']
                            for r in contract['references']))

    def test_admission_and_routing_decisions(self):
        self.assertEqual([lm.admit(r, w, 2, 1) for r, w in ((0, 0), (1, 0), (2, 0), (2, 1))],
                         ['invoke', 'invoke', 'queue', 'busy'])
        self.assertTrue(lm.timed_out(100, 100) and not lm.timed_out(99, 100))
        subs = [dict(source='a', subject='t', target='y'), dict(source='a', subject='t', target='x'),
                dict(source='a', subject='t', target='x'), dict(source='b', subject='t', target='z')]
        self.assertEqual(lm.route(subs, 'a', 't'), ['x', 'y'])
        self.assertEqual(lm.route(subs, 'a', 'u'), [])

    def test_configuration_rejections(self):
        good = dict(version=1, socket='/run/s.sock', component='functions', state_dir='/var/lib/f', work_dir='/var/lib/w',
                    functions={'fn': dict(mode='on-demand', command=['/bin/true'], timeout_ms=10)},
                    subscriptions=[dict(source='sensor', subject='a/b', target='fn')])
        lm.load_config(json.loads(json.dumps(good)))
        mutations = [
            lambda c: c.update(version=2), lambda c: c.update(version=True), lambda c: c.update(extra=1),
            lambda c: c['functions']['fn'].update(mode='shell'),
            lambda c: c['functions']['fn'].update(mode='long-lived', max_concurrency=2),
            lambda c: c['functions']['fn'].update(command=['true']),
            lambda c: c['functions']['fn'].update(timeout_ms=0),
            lambda c: c['functions']['fn'].update(max_queue=10001),
            lambda c: c['functions']['fn'].update(environment={'SUIFLEET_FUNCTION': 'x'}),
            lambda c: c['functions']['fn'].update(run_as={'uid': 0, 'gid': 0}),
            lambda c: c['functions']['fn'].update(memory_bytes=1),
            lambda c: c['subscriptions'][0].update(subject='a/+'),
            lambda c: c['subscriptions'][0].update(subject='a/#'),
            lambda c: c['subscriptions'][0].update(target='missing'),
            lambda c: c['subscriptions'][0].update(source='functions'),
            lambda c: c['subscriptions'][0].update(source='bad source'),
            lambda c: c.update(subscriptions=[]),
        ]
        for mutate in mutations:
            config = json.loads(json.dumps(good))
            mutate(config)
            with self.assertRaises(ValueError, msg=str(config)):
                lm.load_config(config)

    def test_sdk_handler_responses(self):
        def handler(payload, context):
            if payload == 'fail':
                raise suifleet_function.FunctionError('Bad', 'no')
            return [payload, context['attempt']]
        request = dict(version=1, invocation_id='i', attempt=1, payload='p')
        self.assertEqual(lm.parse_response(suifleet_function.handle(handler, json.dumps(request)), request),
                         ('ok', {'result': ['p', 1]}))
        self.assertEqual(lm.parse_response(suifleet_function.handle(handler, json.dumps(dict(request, payload='fail'))), request),
                         ('error', {'error': {'type': 'Bad', 'message': 'no'}}))
        self.assertEqual(lm.parse_response(json.dumps(dict(version=1, invocation_id='other', status='ok', result=1)), request),
                         ('invalid-response', {}))
        self.assertEqual(lm.parse_response(json.dumps(dict(version=1, invocation_id='i', status='ok', result=1, x=2)), request),
                         ('invalid-response', {}))

    def test_strict_json_responses(self):
        request = dict(version=1, invocation_id='i')
        head = b'{"version":1,"invocation_id":"i",'
        for body in (b'"status":"ok","result":NaN}', b'"status":"ok","result":Infinity}', b'"status":"ok","result":"\\udfff"}',
                     b'"status":"error","error":{"type":"\\ud800","message":""}}', b'"status":"ok","result":"\xc3"}',
                     b'"status":"ok","result":' + b'[' * 64 + b']' * 64 + b'}'):
            self.assertEqual(lm.parse_response(head + body, request), ('invalid-response', {}), body[:40])
        self.assertEqual(lm.parse_response(b'[' * 60000, request), ('invalid-response', {}))
        self.assertEqual(lm.parse_response(json.dumps(dict(request, status='ok', result='\u00e9')).encode('utf-16'), request),
                         ('invalid-response', {}), 'only UTF-8')
        nested = head + b'"status":"ok","result":' + b'[' * 63 + b']' * 63 + b'}'
        self.assertEqual(lm.parse_response(nested, request)[0], 'ok')

    def test_malformed_json_responses_are_contained(self):
        f = self.fixture({'fn': on_demand(), 'other': on_demand(result_topic='functions/second')},
                         SUBSCRIBE + [dict(source='sensor', subject='sensors/other', target='other')])
        ops = ['nan', 'infinity', 'surrogate', 'error-surrogate', 'bad-utf8', 'too-deep', 'deep', 'max-deep']
        for op in ops:
            f.daemon.inject('sensor', 'sensors/temp', {'op': op})
        f.daemon.inject('sensor', 'sensors/other', {'op': 'echo', 'tag': 'unrelated'})
        results = sorted(f.result(len(ops)), key=lambda r: r['message_id'])
        self.assertEqual({op: r['status'] for op, r in zip(ops, results)},
                         dict.fromkeys(ops[:-1], 'invalid-response') | {'max-deep': 'ok'})
        self.assertEqual(f.result(1, 'functions/second')[0]['status'], 'ok', 'unrelated function continues')
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'echo'})
        self.assertEqual(f.result(len(ops) + 1)[-1]['status'], 'ok')
        self.assertIsNone(f.process.poll(), 'a malformed response must not stop the manager')

    def spawner(self):
        temporary = tempfile.TemporaryDirectory(prefix='sf069-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        root.chmod(0o755)
        (root / 'logs').mkdir()
        (root / 'work/fn').mkdir(parents=True)
        if os.geteuid() == 0:
            os.chown(root / 'work/fn', FUNCTION_UID, FUNCTION_UID)
        spawner = lm.Spawner(root / 'logs', root / 'work')
        self.addCleanup(ctypes.CDLL(None).prctl, 36, 0)     # this test process is not a subreaper
        fn = dict(mode='on-demand', command=['/bin/sleep', '300'], environment={})
        if os.geteuid() == 0:
            fn['run_as'] = {'uid': FUNCTION_UID, 'gid': FUNCTION_UID}
        return spawner, spawner.spawn('fn', fn)

    def test_denied_cleanup_fails_promptly(self):
        spawner, process = self.spawner()
        self.addCleanup(lambda: process.poll() is None and (os.killpg(process.pid, signal.SIGKILL), process.wait()))
        started = time.monotonic()
        with mock.patch.object(lm.os, 'killpg', side_effect=PermissionError), \
                mock.patch.object(lm, 'KILLER', 'import sys; sys.exit(1)'), self.assertRaises(lm.CleanupError):
            spawner.release(process)
        self.assertLess(time.monotonic() - started, 3, 'denied cleanup is reported, not waited out')
        self.assertTrue(spawner.failed)
        self.assertIsNone(process.poll())

    @unittest.skipUnless(os.geteuid() == 0, 'run_as needs a root manager (make test-lambda)')
    def test_denied_signal_is_sent_as_the_function_user(self):
        spawner, process = self.spawner()
        started = time.monotonic()
        with mock.patch.object(lm.os, 'killpg', side_effect=PermissionError):
            spawner.release(process)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertIsNone(spawner.failed)

    def test_on_demand_invocation_fresh_isolated_process(self):
        f = self.fixture()
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'echo', 'value': 42})
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'env'})
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'caps'})
        first, second, caps = f.result(3)
        self.assertEqual((caps['result']['CapEff'], caps['result']['CapAmb'], caps['result']['NoNewPrivs']),
                         ('0000000000000000', '0000000000000000', '1'))
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(first['result']['payload'], {'op': 'echo', 'value': 42})
        self.assertEqual(first['result']['count'], 1, 'on-demand state is not reused')
        context = first['result']['context']
        self.assertEqual({k: context[k] for k in ('version', 'function', 'mode', 'source', 'subject', 'attempt', 'timeout_ms')},
                         dict(version=1, function='fn', mode='on-demand', source='sensor', subject='sensors/temp',
                              attempt=1, timeout_ms=5000))
        self.assertEqual({k: first[k] for k in ('version', 'function', 'source', 'subject', 'attempt', 'invocation_id')},
                         dict(version=1, function='fn', source='sensor', subject='sensors/temp', attempt=1,
                              invocation_id=f"sensors/temp#{first['message_id']}/fn"))
        env = second['result']
        self.assertEqual(set(env['env']) - {'PWD'}, {'MARK_DIR', 'PATH', 'LANG', 'HOME', 'SUIFLEET_FUNCTION', 'SUIFLEET_FUNCTION_MODE'})
        self.assertEqual(env['cwd'], str(f.root / 'work/fn'))
        self.assertEqual(len({m[0] for m in f.marks_of('start')}), 3, 'one process per invocation')
        self.assertEqual(f.daemon.queues[('functions', 'sensors/temp')], [], 'messages acknowledged')

    def test_long_lived_lifecycle_state_and_restart(self):
        f = self.fixture({'fn': dict(mode='long-lived', timeout_ms=1000, result_topic='functions/results', max_queue=5)})
        wait_for(lambda: f.marks_of('start'), 'long-lived function did not start with the manager')
        for _ in range(2):
            f.daemon.inject('sensor', 'sensors/temp', {'op': 'echo'})
        a, b = f.result(2)
        self.assertEqual((a['result']['count'], b['result']['count']), (1, 2), 'state persists between invocations')
        self.assertEqual(a['result']['pid'], b['result']['pid'])
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'crash'})
        crashed = f.result(3)[2]
        self.assertEqual((crashed['status'], crashed.get('exit_code')), ('crashed', 3))
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'sleep', 'seconds': 5})
        self.assertEqual(f.result(4)[3]['status'], 'timeout')
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'echo'})
        after = f.result(5)[4]
        self.assertEqual((after['status'], after['result']['count']), ('ok', 1), 'restarted with fresh state')
        pids = {m[0] for m in f.marks_of('start')}
        self.assertEqual(len(pids), 3, 'started, restarted after crash and after timeout')
        self.assertFalse(any(alive(int(p)) for p in pids - {str(after['result']['pid'])}))

    def test_error_responses(self):
        f = self.fixture({'fn': on_demand(timeout_ms=2000)})
        ops = ['raise', 'value-error', 'crash', 'garbage', 'version2', 'flood', 'big']
        for op in ops:
            f.daemon.inject('sensor', 'sensors/temp', {'op': op})
        results = {r['message_id']: r for r in f.result(len(ops))}
        by_op = dict(zip(ops, (results[k] for k in sorted(results))))
        self.assertEqual(by_op['raise']['error'], {'type': 'Validation', 'message': 'bad input'})
        self.assertEqual(by_op['value-error']['error'], {'type': 'ValueError', 'message': 'boom'})
        self.assertEqual({op: r['status'] for op, r in by_op.items()},
                         {'raise': 'error', 'value-error': 'error', 'crash': 'crashed', 'garbage': 'invalid-response',
                          'version2': 'unsupported-version', 'flood': 'invalid-response', 'big': 'result-too-large'})
        self.assertEqual(by_op['crash']['exit_code'], 3)
        self.assertNotIn('result', by_op['big'])
        for result in results.values():
            self.assertLessEqual(len(lm.compact(result).encode()), lm.MAX_ENVELOPE)
            self.assertIn(result['status'], lm.STATUSES)

    def test_timeout_kills_process_group_and_escaped_descendants(self):
        f = self.fixture({'fn': on_demand(timeout_ms=400)})
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'sleep', 'seconds': 30})
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'escape'})
        timeout, escaped = f.result(2)
        self.assertEqual((timeout['status'], escaped['status']), ('timeout', 'ok'))
        self.assertGreaterEqual(timeout['duration_ms'], 400)
        sleeper = int(f.marks_of('sleeping')[0][0])
        escapee = int((f.marks / 'escaped').read_text())
        wait_for(lambda: not alive(sleeper) and not alive(escapee), 'function descendants survived')

    def test_concurrency_bound_queue_and_busy(self):
        f = self.fixture({'fn': on_demand(max_concurrency=2, max_queue=0), 'serial': on_demand(result_topic='functions/second', max_queue=1)},
                         SUBSCRIBE + [dict(source='sensor', subject='sensors/other', target='serial')])
        for tag in 'abc':
            f.daemon.inject('sensor', 'sensors/temp', {'op': 'sleep', 'seconds': 1, 'tag': tag})
            f.daemon.inject('sensor', 'sensors/other', {'op': 'sleep', 'seconds': 0.5, 'tag': 's' + tag})
        statuses = sorted(r['status'] for r in f.result(3))
        self.assertEqual(statuses, ['busy', 'ok', 'ok'])
        serial = f.result(3, 'functions/second')
        self.assertEqual([r['status'] for r in sorted(serial, key=lambda r: r['message_id'])], ['ok', 'ok', 'busy'])
        spans = {}
        for kind in ('sleeping', 'woke'):
            for pid, at, tag in f.marks_of(kind):
                spans.setdefault(tag, []).append(float(at))
        self.assertGreaterEqual(spans['sb'][0], spans['sa'][1], 'queued invocation waited for the bound')
        busy = [r for r in f.result(3) if r['status'] == 'busy'][0]
        self.assertNotIn('result', busy)

    def test_subscription_routing_fanout_and_unrouted(self):
        f = self.fixture({'a': on_demand(), 'b': on_demand(result_topic='functions/second')},
                         [dict(source='sensor', subject='sensors/temp', target='b'),
                          dict(source='sensor', subject='sensors/temp', target='a'),
                          dict(source='sensor', subject='sensors/temp', target='a')])
        f.daemon.inject('other', 'sensors/temp', {'op': 'echo', 'tag': 'unrouted'})
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'echo', 'tag': 'routed'})
        a, b = f.result(1)[0], f.result(1, 'functions/second')[0]
        self.assertEqual((a['function'], b['function'], a['message_id']), ('a', 'b', b['message_id']))
        time.sleep(0.5)
        self.assertEqual([json.loads(m[2])['tag'] for m in f.marks_of('invoke')], ['routed', 'routed'])
        self.assertEqual(f.daemon.queues[('functions', 'sensors/temp')], [], 'unrouted message acknowledged')

    def test_shutdown_signal_does_not_acquire_an_event_lock(self):
        # A signal can interrupt Event.wait with its non-reentrant lock held.
        code = r"""
import os, signal, threading
from unittest import mock
import lambda_manager as lm
stop = threading.Event()
install_handler = signal.signal
def register(number, handler):
    install_handler(number, handler)
    if number == signal.SIGTERM:
        with stop._cond:
            os.kill(os.getpid(), number)
with (
    mock.patch.object(lm, 'Manager'),
    mock.patch.object(lm, 'load_config'),
    mock.patch.object(lm.Path, 'read_text', return_value='{}'),
    mock.patch.object(lm.threading, 'Event', return_value=stop),
    mock.patch.object(lm.signal, 'signal', side_effect=register),
):
    lm.main()
"""
        result = subprocess.run([sys.executable, '-c', code, '--config', 'unused'],
                                env={**os.environ, 'PYTHONPATH': str(ROOT / 'scripts')},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_manager_kill_reinvokes_interrupted_work_once(self):
        f = self.fixture()
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'sleep', 'seconds': 2, 'tag': 'k'})
        wait_for(lambda: f.marks_of('sleeping'), 'function did not start')
        sleeper = int(f.marks_of('sleeping')[0][0])
        f.stop(kill=True)
        wait_for(lambda: not alive(sleeper), 'function survived the manager (parent-death signal)')
        self.assertEqual(f.daemon.results(), [])
        f.start()
        result = f.result(1)[0]
        self.assertEqual((result['status'], result['attempt']), ('ok', 2))
        time.sleep(0.5)
        self.assertEqual(len(f.daemon.results()), 1)
        self.assertEqual([json.loads(m[2])['attempt'] for m in f.marks_of('invoke')], [1, 2])

    def test_duplicate_delivery_is_not_reinvoked(self):
        f = self.fixture()
        f.daemon.fail_ack = 1
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'echo'})
        f.result(1)
        wait_for(lambda: f.daemon.queues[('functions', 'sensors/temp')] == [], 'redelivered message not acknowledged')
        time.sleep(0.3)
        self.assertEqual(len(f.marks_of('invoke')), 1)
        self.assertIn('duplicate delivery acknowledged without invocation', f.log.read_text())

    def test_result_retained_across_backpressure_and_restart(self):
        f = self.fixture()
        f.daemon.fail_publish = 10 ** 6
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'echo'})
        wait_for(lambda: 'result retained (backpressure)' in f.log.read_text(), 'publish not retried')
        f.stop(kill=True)
        f.daemon.fail_publish = 2
        f.start()
        result = f.result(1)[0]
        self.assertEqual((result['status'], result['attempt']), ('ok', 1))
        self.assertEqual(len(f.marks_of('invoke')), 1, 'completed result republished without reinvocation')

    def test_graceful_stop_finishes_in_flight_invocation(self):
        f = self.fixture()
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'sleep', 'seconds': 1})
        wait_for(lambda: f.marks_of('sleeping'), 'function did not start')
        f.stop()
        self.assertEqual(f.process.returncode, 0)
        self.assertEqual(len(f.marks_of('woke')), 1)
        f.start()
        self.assertEqual(f.result(1)[0]['attempt'], 1)
        self.assertEqual(len(f.marks_of('invoke')), 1)

    def test_exclusive_state_and_rejected_startup(self):
        f = self.fixture()
        second = subprocess.run([sys.executable, str(ROOT / 'scripts/lambda_manager.py'), '--config', str(f.config_path)],
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('startup rejected', second.stderr)
        f.stop()
        f.config['component'] = 'renamed'
        f.write()
        rebound = subprocess.run([sys.executable, str(ROOT / 'scripts/lambda_manager.py'), '--config', str(f.config_path)],
                                 capture_output=True, text=True, timeout=30)
        self.assertIn('state belongs to another socket/component', rebound.stderr)
        f.config.update(component='functions', version=2)
        f.write()
        version = subprocess.run([sys.executable, str(ROOT / 'scripts/lambda_manager.py'), '--config', str(f.config_path)],
                                 capture_output=True, text=True, timeout=30)
        self.assertIn('unsupported configuration version', version.stderr)

    @unittest.skipUnless(os.geteuid() == 0, 'needs a root manager')
    def test_root_manager_requires_run_as(self):
        f = self.fixture(start=False)
        del f.config['functions']['fn']['run_as']
        f.write()
        refused = subprocess.run([sys.executable, str(ROOT / 'scripts/lambda_manager.py'), '--config', str(f.config_path)],
                                 capture_output=True, text=True, timeout=30)
        self.assertIn('a root manager requires run_as for every function', refused.stderr)

    def test_memory_limit(self):
        f = self.fixture({'fn': on_demand(memory_bytes=128 << 20)})
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'alloc'})
        self.assertEqual(f.result(1)[0]['error']['type'], 'MemoryError')

    @unittest.skipUnless(os.geteuid() == 0, 'run_as needs a root manager (make test-lambda)')
    def test_run_as_identity_and_private_state(self):
        f = self.fixture({'fn': on_demand(run_as={'uid': FUNCTION_UID, 'gid': FUNCTION_UID})})
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'env'})
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'read', 'path': str(f.root / 'state/journal.sqlite3')})
        f.daemon.inject('sensor', 'sensors/temp', {'op': 'caps'})
        env, read, caps = f.result(3)
        self.assertEqual((caps['result']['CapPrm'], caps['result']['CapEff'], caps['result']['NoNewPrivs']),
                         ('0000000000000000', '0000000000000000', '1'), 'no capabilities from a root manager')
        self.assertEqual((env['result']['uid'], env['result']['gid'], env['result']['groups']),
                         (FUNCTION_UID, FUNCTION_UID, []))
        self.assertEqual(os.stat(f.root / 'work/fn').st_uid, FUNCTION_UID)
        self.assertEqual(read['result'], 'PermissionError', 'functions cannot read the manager journal')


if __name__ == '__main__':
    unittest.main()
