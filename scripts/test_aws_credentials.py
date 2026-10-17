#!/usr/bin/env python3
"""Actual-process SF-057 acceptance. Root, disposable Linux network-none container.

Test clocks are supplied by this harness through Python constructors, never a
production command-line option, HTTP header or component control operation.
"""
import datetime
import http.client
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import aws_credentials as adapter

ROOT = Path(__file__).resolve().parents[1]
BASE = int(time.time())


def control(path, operation, component):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(path)
        sock.sendall(json.dumps(dict(version=1, operation=operation, component=component)).encode() + b'\n')
        with sock.makefile('rb') as stream:
            return json.loads(stream.readline(16385))


def actor(pipe, uid):
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
    try:
        while True:
            command = pipe.recv()
            if command['op'] == 'control':
                pipe.send(control(command['socket'], command['operation'], command['component']))
            elif command['op'] == 'http':
                parsed = __import__('urllib.parse', fromlist=['urlsplit']).urlsplit(command['uri'])
                client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
                headers = {} if command.get('token') is None else {'Authorization': command['token']}
                client.request(command.get('method', 'GET'), command.get('path', parsed.path), headers=headers)
                response = client.getresponse()
                body = json.loads(response.read())
                pipe.send((response.status, dict(response.getheaders()), body))
                client.close()
            elif command['op'] == 'read-denied':
                try:
                    with open(command['path'], 'rb') as source:
                        source.read(1)
                    pipe.send(False)
                except PermissionError:
                    pipe.send(True)
            else:
                raise ValueError('unknown actor command')
    except EOFError:
        pass


def broker(pipe, settings, timestamp, epoch):
    service = adapter.Adapter(settings, lambda: epoch + timestamp.value, lambda: timestamp.value)
    service.start()
    pipe.send((service.control.uri, os.getpid()))
    try:
        pipe.recv()
    finally:
        service.close()


class Upstream(__import__('http.server', fromlist=['HTTPServer']).HTTPServer):
    def __init__(self, directory):
        from http.server import BaseHTTPRequestHandler
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                owner.calls += 1
                owner.peer_verified = bool(self.connection.getpeercert())
                owner.paths.append(self.path)
                try:
                    result = owner.results.get_nowait()
                except queue.Empty:
                    result = {'result': 'server-error'}
                status = {'ok': 200, 'client-error': 403, 'server-error': 500, 'unknown-error': 200}[result['result']]
                if result['result'] == 'ok':
                    name = result['credential_set']
                    expiration = datetime.datetime.fromtimestamp(BASE + result['expiration'], datetime.timezone.utc).isoformat()
                    body = json.dumps({'credentials': {'accessKeyId': 'AKIAFIXTURE' if name == 'sdk' else name,
                        'secretAccessKey': 'fixture-secret', 'sessionToken': 'fixture-session', 'expiration': expiration}}).encode()
                else:
                    body = b'not a credential response'
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        super().__init__(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(directory / 'server.crt', directory / 'server.key')
        context.load_verify_locations(directory / 'ca.crt')
        context.verify_mode = ssl.CERT_REQUIRED
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.results = queue.Queue()
        self.calls = 0
        self.peer_verified = False
        self.paths = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()


class Runtime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise RuntimeError('run only as root in a disposable network-none container')
        cls.directory = Path(tempfile.mkdtemp(prefix='sf057-', dir='/run'))
        os.chmod(cls.directory, 0o755)
        def openssl(*args):
            subprocess.run(['openssl', *map(str, args)], cwd=cls.directory,
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key', '-out', 'ca.crt', '-days', '1', '-subj', '/CN=fixture-ca')
        for name in ('client', 'server'):
            openssl('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', f'{name}.key', '-out', f'{name}.csr', '-subj', f'/CN={name}')
            ext = cls.directory / f'{name}.ext'
            ext.write_text('subjectAltName=IP:127.0.0.1\nextendedKeyUsage=' + ('serverAuth' if name == 'server' else 'clientAuth') + '\n')
            openssl('x509', '-req', '-in', f'{name}.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial', '-out', f'{name}.crt', '-days', '1', '-extfile', ext)
        for file in cls.directory.iterdir():
            file.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory)

    def start(self, grants=(), ttl=86400):
        self.upstream = Upstream(self.directory)
        self.timestamp = mp.Value('d', 0)
        self.socket = str(self.directory / 'credentials.sock')
        self.settings = dict(socket=self.socket, endpoint=f'https://127.0.0.1:{self.upstream.server_port}/role-aliases/fixture/credentials',
            ca=str(self.directory / 'ca.crt'), certificate=str(self.directory / 'client.crt'), key=str(self.directory / 'client.key'),
            daemon_uid=1001, components={'c1': 61001, 'c2': 61002, 'c3': 61003}, grants=list(grants), token_ttl_seconds=ttl)
        self.pipe, child = mp.Pipe()
        self.process = mp.Process(target=broker, args=(child, self.settings, self.timestamp, BASE))
        self.process.start()
        child.close()
        self.assertTrue(self.pipe.poll(10))
        self.uri, self.pid = self.pipe.recv()
        self.actors = {}
        for name, uid in self.settings['components'].items():
            parent, child = mp.Pipe()
            process = mp.Process(target=actor, args=(child, uid))
            process.start()
            child.close()
            self.actors[name] = (parent, process)
        self.addCleanup(self.stop)

    def stop(self):
        for pipe, process in self.actors.values():
            pipe.close()
            process.terminate()
            process.join(5)
        if self.process.is_alive():
            self.pipe.send('stop')
        self.process.join(10)
        if self.process.is_alive():
            self.process.kill()
            self.process.join()
            self.fail('adapter failed to stop')
        self.assertEqual(self.process.exitcode, 0)
        self.pipe.close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def ask(self, principal, **command):
        pipe, _ = self.actors[principal]
        pipe.send(command)
        self.assertTrue(pipe.poll(15), 'actor response timeout')
        return pipe.recv()

    def bootstrap(self, name):
        value = self.ask(name, op='control', socket=self.socket, operation='start', component=name)
        self.assertTrue(value['ok'])
        return value['data']['env']

    def request(self, name, token=None, **kwargs):
        return self.ask(name, op='http', uri=self.uri, token=token, **kwargs)

    def test_declared_fixtures_actual_process(self):
        cases = json.loads((ROOT / 'interfaces/compat/credential-provider-cases.json').read_text())
        for case in cases:
            self.doCleanups()
            with self.subTest(case=case['id']):
                self.start()
                tokens = {}
                owners = {}
                for step in case['steps']:
                    op, expected = step['op'], step['expect']
                    self.timestamp.value = op.get('now', self.timestamp.value)
                    kind = op['op']
                    actual = {}
                    if kind in ('grant', 'revoke'):
                        self.assertTrue(control(self.socket, kind, op['component'])['ok'])
                    elif kind in ('start', 'stop'):
                        name = op['component']
                        if kind == 'stop':
                            self.assertTrue(self.ask(name, op='control', socket=self.socket, operation='stop', component=name)['ok'])
                        else:
                            env = self.bootstrap(name)
                            if env:
                                tokens[op['token']] = env['AWS_CONTAINER_AUTHORIZATION_TOKEN']
                                owners[op['token']] = name
                                self.assertEqual(env['AWS_CONTAINER_CREDENTIALS_FULL_URI'], self.uri)
                            normalized = dict(env)
                            if env:
                                self.assertGreaterEqual(len(env['AWS_CONTAINER_AUTHORIZATION_TOKEN']), 43)
                                self.assertEqual(len(set(tokens.values())), len(tokens))
                                normalized['AWS_CONTAINER_AUTHORIZATION_TOKEN'] = op['token']
                                normalized['AWS_CONTAINER_CREDENTIALS_FULL_URI'] = re.sub(
                                    r':\d+/', ':{port}/', env['AWS_CONTAINER_CREDENTIALS_FULL_URI'])
                            actual = {'env': normalized}
                    elif kind == 'upstream':
                        self.upstream.results.put(op)
                    else:
                        token = op.get('authorization')
                        code, headers, body = self.request(owners.get(token, 'c1'), tokens.get(token, token), method=op['method'], path=op['path'])
                        status = {200: 'ok', 401: 'unauthorized', 404: 'not-found', 405: 'method-not-allowed', 503: 'unavailable'}[code]
                        actual = {'status': status, 'upstream_calls': self.upstream.calls}
                        if code == 200:
                            expiration = datetime.datetime.fromisoformat(body['Expiration'].replace('Z', '+00:00')).timestamp() - BASE
                            actual.update(credential_set=body['AccessKeyId'], expiration=int(expiration))
                        if code == 503:
                            actual['retry_after'] = int(headers['Retry-After'])
                    self.assertEqual(actual, expected, f"fixture {case['id']} operation {kind}")
                if self.upstream.calls:
                    self.assertTrue(self.upstream.peer_verified)
                    self.assertTrue(all(p == '/role-aliases/fixture/credentials' for p in self.upstream.paths))
                self.doCleanups()

    def test_wrong_caller_expiry_rotation_and_exit(self):
        self.start(['c1', 'c2'], ttl=10)
        self.upstream.results.put(dict(result='ok', credential_set='set-1', expiration=3600))
        token = self.bootstrap('c1')['AWS_CONTAINER_AUTHORIZATION_TOKEN']
        self.assertEqual(self.request('c2', token)[0], 401)
        self.assertEqual(self.request('c1', 'foreign')[0], 401)
        self.assertFalse(self.ask('c2', op='control', socket=self.socket, operation='start', component='c1')['ok'])
        self.assertFalse(self.ask('c2', op='control', socket=self.socket, operation='grant', component='c2')['ok'])
        self.assertEqual(self.request('c1', token)[0], 200)
        newer = self.bootstrap('c1')['AWS_CONTAINER_AUTHORIZATION_TOKEN']
        self.assertNotEqual(newer, token)
        self.assertEqual(self.request('c1', token)[0], 401)
        self.assertEqual(self.request('c1', newer)[0], 200)
        self.timestamp.value = 10
        self.assertEqual(self.request('c1', newer)[0], 401)
        for path in [str(self.directory / 'client.key'), f'/proc/{self.pid}/environ']:
            self.assertTrue(self.ask('c2', op='read-denied', path=path))
        pipe, process = self.actors['c1']
        token = self.bootstrap('c1')['AWS_CONTAINER_AUTHORIZATION_TOKEN']
        process.terminate()
        process.join(5)
        # Same UID successor cannot reuse the dead process's token.
        parent, child = mp.Pipe()
        successor = mp.Process(target=actor, args=(child, 61001))
        successor.start()
        child.close()
        pipe.close()
        self.actors['c1'] = parent, successor
        self.assertEqual(self.request('c1', token)[0], 401)
        fresh = self.bootstrap('c1')['AWS_CONTAINER_AUTHORIZATION_TOKEN']
        self.assertNotEqual(token, fresh)
        self.assertEqual(self.request('c1', fresh)[0], 200)
        # Restart the actual adapter; no token/cache may survive in a journal.
        self.pipe.send('stop')
        self.process.join(10)
        self.assertEqual(self.process.exitcode, 0)
        self.pipe.close()
        self.pipe, child = mp.Pipe()
        self.process = mp.Process(target=broker, args=(child, self.settings, self.timestamp, BASE))
        self.process.start()
        child.close()
        self.assertTrue(self.pipe.poll(10))
        self.uri, self.pid = self.pipe.recv()
        self.assertEqual(self.request('c1', fresh)[0], 401)
        newest = self.bootstrap('c1')['AWS_CONTAINER_AUTHORIZATION_TOKEN']
        self.assertNotEqual(newest, fresh)
        self.upstream.results.put(dict(result='ok', credential_set='set-2', expiration=3600))
        self.assertEqual(self.request('c1', newest)[0], 200)


    def test_unmodified_sdks_via_exec_wrapper(self):
        self.start(['c1'])
        self.upstream.results.put(dict(result='ok', credential_set='sdk', expiration=3600))
        python = '''import boto3
c = boto3.Session().get_credentials().get_frozen_credentials()
assert c.access_key == 'AKIAFIXTURE'
assert c.secret_key == 'fixture-secret'
assert c.token == 'fixture-session'
print('PASS: boto3 default credential chain')
'''
        # The host checkout may intentionally be 0700. Provision only public
        # test executables/dependencies into a traversable disposable directory.
        public = self.directory / 'public'
        public.mkdir(mode=0o755)
        shutil.copytree(ROOT / '.ci-cache/aws-python', public / 'python')
        shutil.copyfile(ROOT / 'sdk/python/suifleet_aws.py', public / 'wrapper.py')
        shutil.copyfile(ROOT / '.ci-cache/aws-rust-target/debug/suifleet-aws-sdk-acceptance', public / 'rust-sdk')
        for item in public.rglob('*'):
            item.chmod(0o755 if item.is_dir() else 0o644)
        (public / 'rust-sdk').chmod(0o755)
        self.addCleanup(shutil.rmtree, public)
        clients = [[sys.executable, '-c', python], [str(public / 'rust-sdk')]]
        def drop():
            os.setgroups([])
            os.setgid(61001)
            os.setuid(61001)
        for client in clients:
            env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'AWS_REGION': 'us-east-1',
                   'PYTHONPATH': str(public / 'python')}
            result = subprocess.run([sys.executable, str(public / 'wrapper.py'), '--socket', self.socket,
                '--component', 'c1', '--', *client], preexec_fn=drop, env=env, capture_output=True, timeout=30)
            # Never include client stderr/credential objects in test diagnostics.
            self.assertEqual(result.returncode, 0, f'SDK client {Path(client[0]).name} failed (private diagnostics withheld)')
            self.assertIn(b'PASS:', result.stdout)
        self.assertEqual(self.upstream.calls, 1)
        env.update(AWS_CONTAINER_AUTHORIZATION_TOKEN='ambient', AWS_ACCESS_KEY_ID='ambient',
                   AWS_SECRET_ACCESS_KEY='ambient', AWS_DEFAULT_PROFILE='ambient')
        denied = subprocess.run([sys.executable, str(public / 'wrapper.py'), '--socket', self.socket,
            '--component', 'c3', '--', sys.executable, '-c',
            "import os; assert not any(k in os.environ for k in ('AWS_CONTAINER_AUTHORIZATION_TOKEN', 'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_DEFAULT_PROFILE'))"],
            preexec_fn=lambda: (os.setgroups([]), os.setgid(61003), os.setuid(61003)),
            env=env, capture_output=True, timeout=10)
        self.assertEqual(denied.returncode, 0, 'ungranted wrapper inherited credentials')

    def test_tls_rejects_missing_client_identity_and_wrong_ca(self):
        self.start(['c1'])
        context = ssl.create_default_context(cafile=str(self.directory / 'ca.crt'))
        connection = http.client.HTTPSConnection('127.0.0.1', self.upstream.server_port, context=context, timeout=3)
        with self.assertRaises((ssl.SSLError, OSError)):
            connection.request('GET', '/role-aliases/fixture/credentials')
            connection.getresponse()
        connection.close()
        settings = dict(self.settings, ca=str(self.directory / 'server.crt'))
        provider = adapter.Provider(settings)
        self.assertIsNone(provider.credentials()[0])
        self.assertEqual(self.upstream.calls, 0)

    def test_production_cli_and_secret_free_shutdown(self):
        self.start(['c1'])
        self.pipe.send('stop')
        self.process.join(10)
        self.assertEqual(self.process.exitcode, 0)
        config = self.directory / 'provider.json'
        config.write_text(json.dumps(self.settings))
        config.chmod(0o600)
        before = {p.name for p in self.directory.iterdir()}
        self.upstream.results.put(dict(result='ok', credential_set='set-1', expiration=3600))
        process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/aws_credentials.py'),
            '--config', str(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not Path(self.socket).exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.02)
            self.assertIsNone(process.poll(), 'production service failed startup')
            env = self.bootstrap('c1')
            self.uri = env['AWS_CONTAINER_CREDENTIALS_FULL_URI']
            self.assertEqual(self.request('c1', env['AWS_CONTAINER_AUTHORIZATION_TOKEN'])[0], 200)
            # Unknown bootstrap fields cannot smuggle a caller or test clock.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(self.socket)
                sock.sendall(b'{"version":1,"operation":"grant","component":"c1","now":999}\n')
                self.assertFalse(json.loads(sock.recv(16384))['ok'])
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(len(stdout) + len(stderr), 0, 'service emitted private diagnostics')
            config.unlink()
        self.assertEqual({p.name for p in self.directory.iterdir()}, before - {'provider.json'})

    def test_policy_and_files_fail_closed(self):
        with self.assertRaises(ValueError):
            adapter.Policy({'components': {'c1': 0}})
        with self.assertRaises(ValueError):
            adapter.Policy({'components': {'c1': 61001, 'c2': 61001}})
        with self.assertRaises(ValueError):
            adapter.Policy({'components': {}, 'grants': ['c1']})
        with self.assertRaises(ValueError):
            adapter.Policy({'token_ttl_seconds': 86401})
        with self.assertRaises(ValueError):
            adapter.private_file('/etc/passwd')


if __name__ == '__main__':
    mp.set_start_method('spawn')
    unittest.main(verbosity=2)
