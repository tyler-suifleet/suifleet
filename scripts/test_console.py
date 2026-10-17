#!/usr/bin/env python3
"""Local support console (SF-079) with the real daemon against the local
chain/Walrus simulator, without keys, gas or public network.

  loopback-only configuration -> non-loopback listen refused by --check-config
  console port already taken  -> daemon keeps running without a console
  daemon restart, port free   -> console up; sessions do not survive; audit kept
  supervised svc              -> inventory/health/deployments/logs/configuration
                                 views: allowlisted, redacted, bounded, paged
  authentication              -> none, wrong, malformed, expired and ambiguous
                                 credentials refused; generated tokens only
  sessions                    -> Origin/Content-Type/fetch-metadata/CSRF checks,
                                 Host (DNS rebinding) check, logout
  authorization               -> read-only user denied support/other views;
                                 unknown operations/versions/fields rejected
  support operations          -> stop/start/restart run in the daemon and are
                                 audited before they run
  hostile HTTP                -> oversized/duplicate/chunked requests rejected;
                                 idle connection flood released by timeout
  --browser (real Chrome)     -> the UI signs in, renders views and restarts;
                                 a page on another loopback port cannot drive
                                 the API from the signed-in browser
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_ota
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'suifleet-edge/target/debug/suifleet'
CANARY = 'hunter2-log-secret'


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def raw(port, data, timeout=10):
    """Send raw bytes; return (status, headers, body)."""
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as connection:
        connection.sendall(data)
        response = b''
        while chunk := connection.recv(65536):
            response += chunk
    head, _, body = response.partition(b'\r\n\r\n')
    lines = head.decode().split('\r\n')
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(':')
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    return int(lines[0].split()[1]), headers, body


class Console:
    def __init__(self, port):
        self.port = port
        self.origin = f'http://127.0.0.1:{port}'

    def post(self, path, body=None, *, host=None, origin='default', content_type='application/json',
             token=None, cookie=None, csrf=None, extra=()):
        payload = json.dumps(body if body is not None else {}).encode()
        headers = [f'Host: {host or f"127.0.0.1:{self.port}"}']
        if origin == 'default':
            origin = self.origin
        if origin:
            headers.append(f'Origin: {origin}')
        if content_type:
            headers.append(f'Content-Type: {content_type}')
        if token:
            headers.append(f'Authorization: Bearer {token}')
        if cookie:
            headers.append(f'Cookie: {cookie}')
        if csrf:
            headers.append(f'X-SuiFleet-CSRF: {csrf}')
        headers.extend(extra)
        request = (f'POST {path} HTTP/1.1\r\n' + ''.join(h + '\r\n' for h in headers)
                   + f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode() + payload
        status, response_headers, data = raw(self.port, request)
        return status, response_headers, json.loads(data) if data else None

    def call(self, operation, *, version=1, **kwargs):
        fields = {k: kwargs.pop(k) for k in ('component', 'limit', 'after', 'extra_field') if k in kwargs}
        if 'extra_field' in fields:
            fields['unexpected'] = fields.pop('extra_field')
        status, _, body = self.post('/api/v1/request', dict(version=version, operation=operation, **fields), **kwargs)
        assert body['version'] == 1, body
        assert (status == 200) == (body['status'] == 'ok'), (status, body)
        return body

    def login(self, token, **kwargs):
        status, headers, body = self.post('/api/v1/session', token=token, **kwargs)
        if body['status'] != 'ok':
            return body, None
        cookie = headers['set-cookie'][0]
        for attribute in ('HttpOnly', 'SameSite=Strict', 'Path=/api/v1'):
            assert attribute in cookie, cookie
        return body, cookie.split(';')[0]


def token():
    result = subprocess.run([str(BINARY), '--console-token'], capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    assert value['token'].startswith('sfc_') and len(value['token_sha256']) == 64
    return value


def run(browser=False):
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
    blocker = None
    try:
        with tempfile.TemporaryDirectory(prefix='sf079-') as temporary:
            root = Path(temporary)
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
            port = free_port()
            ops, viewer, expired, unconfigured = token(), token(), token(), token()
            base = ('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state))}\npoll_seconds = 1\n'
                'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 3\ncomponent_stop_timeout_seconds = 1\n')
            console = (f'[console]\nlisten = "127.0.0.1:{port}"\nsupport_operations = ["start", "stop", "restart"]\n'
                f'[console.users.ops]\ntoken_sha256 = "{ops["token_sha256"]}"\n'
                'grants = ["inventory", "health", "deployments", "logs", "configuration", "audit", "start", "stop", "restart"]\n'
                f'[console.users.viewer]\ntoken_sha256 = "{viewer["token_sha256"]}"\ngrants = ["inventory", "health"]\n'
                f'[console.users.old]\ntoken_sha256 = "{expired["token_sha256"]}"\ngrants = ["inventory"]\nexpires_unix = 1\n')
            config = root / 'config.toml'

            # Configuration: a public listener is refused before anything starts.
            for bad in (console.replace('127.0.0.1:', '0.0.0.0:'), console.replace(f'listen = "127.0.0.1:{port}"', f'listen = "[::]:{port}"'),
                        console.replace('support_operations', 'origins = ["http://evil.example:1441"]\nsupport_operations'),
                        console.replace('"inventory", "health"]', '"inventory", "health", "restart", "shell"]')):
                config.write_text(base + bad)
                result = subprocess.run([str(BINARY), '--check-config', str(config)], capture_output=True, text=True, timeout=30)
                assert result.returncode != 0 and 'console' in result.stderr, result.stderr
            print('PASS: non-loopback listen, non-loopback origin and unknown grant configurations refused')

            config.write_text(base + console)
            log_path = root / 'daemon.log'
            def initialized():
                try:
                    return json.loads((state / 'state.json').read_text()).get('initialized')
                except (OSError, ValueError):
                    return False

            def start():
                log = log_path.open('a')
                return subprocess.Popen([str(BINARY)], env={**os.environ, 'EDGE_CONFIG': str(config)}, stdout=log, stderr=log)

            # Recovery: a taken port leaves the daemon running without a console.
            blocker = socket.socket()
            blocker.bind(('127.0.0.1', port))
            blocker.listen(1)
            process = start()
            wait_for(initialized, 'daemon did not initialize', process)
            wait_for(lambda: 'Local support console unavailable' in log_path.read_text(), 'bind failure not reported', process)
            assert process.poll() is None
            process.terminate()
            process.wait(timeout=15)
            blocker.close()
            blocker = None
            print('PASS: console bind failure is reported and does not stop the daemon')

            process = start()
            c = Console(port)
            wait_for(lambda: 'Local support console listening' in log_path.read_text(), 'console did not start', process)
            sim.add(test_ota.object_id('0x791'), event=dict(component_name='svc'), recipe=dict(schemaVersion='1.2',
                # Every run ends its output with canary credentials, so they are
                # always in the bounded tail the console serves.
                lifecycle=dict(run=f'echo hello; i=0; while [ $i -lt 300 ]; do echo line $i; i=$((i+1)); done; '
                               f'echo "late password={CANARY} Authorization: Bearer {ops["token"]} suiprivkey1abc {"07" * 32}"; '
                               'exec sleep 300'),
                configuration=dict(defaults=dict(answer=42, nested=dict(password='recipe-secret', port=8080)))))

            sim.add(test_ota.object_id('0x792'), event=dict(component_name='svc2'), recipe=dict(lifecycle=dict(run='exec sleep 300')))

            def running():
                body = c.call('inventory', token=ops['token'])
                return sum(x['component'] in ('svc', 'svc2') and x['health'] == 'running' for x in body['data']['components']) == 2
            wait_for(running, 'svc/svc2 did not start', process)

            # Static UI: no data, strict headers; Host must be configured (DNS rebinding).
            status, headers, body = raw(port, f'GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode())
            assert status == 200 and b'console.js' in body
            assert "default-src 'none'" in headers['content-security-policy'][0] and headers['x-frame-options'] == ['DENY']
            assert headers['cache-control'] == ['no-store'] and 'access-control-allow-origin' not in headers
            status, _, _ = raw(port, f'GET / HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n'.encode())
            assert status == 200
            status, _, body = raw(port, f'GET / HTTP/1.1\r\nHost: rebind.example:{port}\r\n\r\n'.encode())
            assert status == 421, body
            assert c.post('/api/v1/request', dict(version=1, operation='inventory'), host=f'rebind.example:{port}', token=ops['token'])[0] == 421
            status, _, _ = raw(port, f'OPTIONS /api/v1/request HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://evil.example\r\n\r\n'.encode())
            assert status == 405
            print('PASS: static UI security headers; rebinding Host and CORS preflight refused')

            # Authentication.
            assert c.call('inventory')['status'] == 'unauthenticated'
            assert c.call('inventory', token=unconfigured['token'])['status'] == 'unauthenticated'
            assert c.call('inventory', token='hunter2')['status'] == 'unauthenticated'
            assert c.call('inventory', token=expired['token'])['status'] == 'unauthenticated'
            assert c.call('inventory', token=ops['token'].upper())['status'] == 'unauthenticated'
            print('PASS: missing, unconfigured, non-generated, expired and altered tokens refused')

            # Bearer API: grants, versions and request shape.
            body = c.call('inventory', token=viewer['token'])
            svc = next(x for x in body['data']['components'] if x['component'] == 'svc')
            assert set(svc) <= {'component', 'version', 'health', 'deployment', 'installed', 'supervised', 'restarts', 'since_ms', 'last_exit'}, svc
            assert c.call('health', token=viewer['token'])['data']['health'] in ('healthy', 'degraded')
            for operation in ('logs', 'configuration', 'deployments', 'audit', 'restart', 'stop', 'shell'):
                assert c.call(operation, token=viewer['token'], component='svc')['status'] == 'denied', operation
            assert c.call('shell', token=ops['token'])['status'] == 'denied'
            for version in (2, 0, True, '1', 1.5, None):
                assert c.call('inventory', version=version, token=ops['token'])['status'] == 'unsupported-version', version
            assert c.call('inventory', token=ops['token'], extra_field=1)['status'] == 'invalid-request'
            assert c.call('logs', token=ops['token'], component='../etc')['status'] == 'invalid-request'
            assert c.call('logs', token=ops['token'], component='missing')['status'] == 'not-found'
            for limit in (0, 201):
                assert c.call('logs', token=ops['token'], component='svc', limit=limit)['status'] == 'invalid-request'
            for limit in (0, 101):
                assert c.call('deployments', token=ops['token'], limit=limit)['status'] == 'invalid-request'
            # Bearer callers may omit Origin (no ambient credential), never send a foreign one.
            assert c.call('inventory', token=ops['token'], origin=None)['status'] == 'ok'
            assert c.call('inventory', token=ops['token'], origin='http://evil.example')['status'] == 'invalid-origin'
            assert c.call('inventory', token=ops['token'], content_type='text/plain')['status'] == 'invalid-origin'
            print('PASS: grant denial, unknown operation/version/field, path and limit validation')

            # Redaction and bounds.
            wait_for(lambda: 'late password' in '\n'.join(c.call('logs', token=ops['token'], component='svc', limit=200)['data']['lines']),
                     'component log incomplete', process)
            # Raw log holds the canaries; the console view does not.
            raw_log = (state / 'supervised/svc/component.log').read_text()
            assert CANARY in raw_log and ops['token'] in raw_log
            two = c.call('logs', token=ops['token'], component='svc', limit=2)['data']['lines']
            assert two[0] == 'line 299' and two[1].startswith('late password=[redacted]'), two
            full = c.call('logs', token=ops['token'], component='svc', limit=200)['data']['lines']
            assert len(full) == 200
            text = json.dumps(full)
            for i, secret in enumerate((CANARY, ops['token'], 'suiprivkey1abc', '07' * 32)):
                assert secret not in text, f'log canary {i} leaked'
            assert 'late password=[redacted]' in text, text
            configuration = c.call('configuration', token=ops['token'], component='svc')['data']
            assert configuration['effective']['answer'] == 42 and configuration['effective']['nested']['port'] == 8080
            assert configuration['effective']['nested']['password'] == '[redacted]' and 'recipe-secret' not in json.dumps(configuration)
            first = c.call('deployments', token=ops['token'], limit=1)['data']
            assert len(first['deployments']) == 1 and first['deployments'][0]['status'] == 1 and first['next'], first
            second = c.call('deployments', token=ops['token'], limit=1, after=first['next'])['data']
            assert len(second['deployments']) == 1 and second['next'] is None, second
            assert first['deployments'][0]['deployment'] != second['deployments'][0]['deployment']
            page = c.call('inventory', token=ops['token'], limit=1)['data']
            assert [x['component'] for x in page['components']] == ['svc'] and page['next'] == 'svc', page
            page = c.call('inventory', token=ops['token'], limit=1, after='svc')['data']
            assert [x['component'] for x in page['components']] == ['svc2'] and page['next'] is None, page
            print('PASS: allowlisted inventory; redacted logs/configuration; bounded log tail and pages')

            # Sessions: Origin, Content-Type, fetch metadata, CSRF and logout.
            assert c.login(ops['token'], origin='http://evil.example')[0]['status'] == 'invalid-origin'
            assert c.login(ops['token'], content_type='text/plain')[0]['status'] == 'invalid-origin'
            assert c.login(ops['token'], extra=['Sec-Fetch-Site: cross-site'])[0]['status'] == 'invalid-origin'
            assert c.login('sfc_' + '0' * 64)[0]['status'] == 'unauthenticated'
            session, cookie = c.login(ops['token'])
            csrf = session['data']['csrf']
            assert session['data']['user'] == 'ops' and 'restart' in session['data']['grants']
            assert c.call('health', cookie=cookie, csrf=csrf)['status'] == 'ok'
            assert c.call('health', cookie=cookie)['status'] == 'unauthenticated'
            assert c.call('health', cookie=cookie, csrf='0' * 64)['status'] == 'unauthenticated'
            assert c.call('health', cookie=cookie, csrf=csrf, origin=None)['status'] == 'invalid-origin'
            assert c.call('health', cookie=cookie, csrf=csrf, origin='http://evil.example')['status'] == 'invalid-origin'
            assert c.call('health', cookie=cookie, csrf=csrf, extra=['Sec-Fetch-Site: same-site'])['status'] == 'invalid-origin'
            assert c.call('health', cookie=cookie, csrf=csrf, content_type='application/x-www-form-urlencoded')['status'] == 'invalid-origin'
            assert c.call('health', cookie=cookie, csrf=csrf, token=viewer['token'])['status'] == 'unauthenticated'
            assert c.call('health', cookie=f'{cookie}; {cookie}', csrf=csrf)['status'] == 'unauthenticated'
            assert c.call('health', cookie='suifleet_console=' + '0' * 64, csrf=csrf)['status'] == 'unauthenticated'
            assert c.call('health', cookie=cookie, csrf=csrf, origin=f'http://localhost:{port}')['status'] == 'ok'
            viewer_session, viewer_cookie = c.login(viewer['token'])
            assert c.call('restart', cookie=viewer_cookie, csrf=viewer_session['data']['csrf'], component='svc')['status'] == 'denied'
            # A viewer's CSRF token does not unlock the operator's session.
            assert c.call('health', cookie=cookie, csrf=viewer_session['data']['csrf'])['status'] == 'unauthenticated'
            print('PASS: session cookie requires Origin, JSON, same-origin fetch metadata and its own CSRF token')

            # Support operations through the poll-loop owner.
            def runtime():
                return json.loads((state / 'supervised/status.json').read_text())['svc']
            before = runtime()['pid']
            result = c.call('restart', cookie=cookie, csrf=csrf, component='svc')
            assert result['status'] == 'ok' and result['data']['runtime']['phase'] in ('running', 'starting'), result
            wait_for(lambda: runtime()['phase'] == 'running' and runtime()['pid'] != before, 'restart did not replace the run', process)
            assert c.call('stop', cookie=cookie, csrf=csrf, component='svc')['data']['runtime']['phase'] == 'stopped'
            assert runtime()['phase'] == 'stopped' and c.call('health', token=ops['token'])['data']['health'] == 'degraded'
            assert c.call('start', cookie=cookie, csrf=csrf, component='svc')['status'] == 'ok'
            wait_for(lambda: runtime()['phase'] == 'running', 'start did not run svc', process)
            assert c.call('restart', cookie=cookie, csrf=csrf, component='missing')['status'] == 'failed'
            print('PASS: stop/start/restart executed by the daemon; unknown component fails')

            if browser:
                audit_path = state / 'console/audit.jsonl'
                def node(mode):
                    result = subprocess.run(['node', str(ROOT / 'scripts/test_console_browser.cjs'), mode,
                        f'http://127.0.0.1:{port}/', ops['token']], capture_output=True, text=True, timeout=120)
                    assert result.returncode == 0, result.stdout + result.stderr
                    return json.loads(result.stdout.strip().splitlines()[-1])
                before = runtime()['pid']
                assert node('ui')['ok']
                wait_for(lambda: runtime()['phase'] == 'running' and runtime()['pid'] != before, 'UI restart did not run', process)
                pid = runtime()['pid']
                count = len(audit_path.read_text().splitlines())
                attack = node('attack')
                assert attack['form'] in ('unauthenticated', 'invalid-origin'), attack
                time.sleep(2)
                assert runtime()['pid'] == pid and runtime()['phase'] == 'running', 'cross-origin page restarted svc'
                new = [json.loads(line) for line in audit_path.read_text().splitlines()[count:]]
                restarts = [e for e in new if e['operation'] == 'restart']
                assert len(restarts) >= 2 and all(e['status'] in ('unauthenticated', 'invalid-origin') for e in restarts), new
                print('PASS: real browser UI sign-in/views/redacted logs/restart/sign-out without CSP errors; '
                      'cross-origin fetch and form POST from a signed-in browser refused')

            status, _, body = c.post('/api/v1/logout', cookie=cookie, csrf=csrf)
            assert status == 200 and body['status'] == 'ok'
            assert c.call('health', cookie=cookie, csrf=csrf)['status'] == 'unauthenticated'
            print('PASS: logout ends the session')

            # Hostile HTTP.
            host = f'Host: 127.0.0.1:{port}\r\n'
            assert raw(port, b'POST /api/v1/request HTTP/1.1\r\n' + host.encode() + b'Content-Length: 5000\r\n\r\n' + b'{' * 5000)[0] == 413
            assert raw(port, b'GET / HTTP/1.1\r\n' + host.encode() + b'X: ' + b'a' * 9000 + b'\r\n\r\n')[0] == 431
            assert raw(port, b'POST /api/v1/request HTTP/1.1\r\n' + host.encode() + b'Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n')[0] == 501
            assert raw(port, b'GET / HTTP/1.1\r\n' + host.encode() + host.encode() + b'\r\n')[0] == 400
            assert raw(port, b'GET / HTTP/1.1\r\nHost: 127.0.0.1:1\r\nHost: ' + f'127.0.0.1:{port}'.encode() + b'\r\n\r\n')[0] == 400
            assert raw(port, b'GET / HTTP/1.1\r\n ' + host.encode() + b'\r\n')[0] == 400
            assert raw(port, b'GET /../ HTTP/9\r\n' + host.encode() + b'\r\n')[0] == 400
            idle = [socket.create_connection(('127.0.0.1', port)) for _ in range(40)]
            try:
                time.sleep(6)  # Header read timeout frees every slot.
                assert c.call('health', token=ops['token'])['status'] == 'ok'
            finally:
                for s in idle:
                    s.close()
            print('PASS: oversized, chunked, duplicate-Host, folded and malformed requests; idle flood released')

            # Audit: private, complete for decisions, no credentials.
            audit_path = state / 'console/audit.jsonl'
            assert audit_path.stat().st_mode & 0o777 == 0o600
            entries = [json.loads(line) for line in audit_path.read_text().splitlines()]
            text = audit_path.read_text()
            for secret in (ops['token'], viewer['token'], ops['token_sha256'], csrf, cookie.split('=', 1)[1], CANARY):
                assert secret not in text, 'credential in the audit log'
            def seen(**match):
                return any(all(e.get(k) == v for k, v in match.items()) for e in entries)
            assert seen(user='ops', operation='restart', component='svc', status='requested')
            assert seen(user='ops', operation='restart', component='svc', status='ok')
            assert seen(user='viewer', operation='restart', status='denied')
            assert seen(user=None, operation='inventory', status='unauthenticated')
            assert seen(user=None, operation='login', status='invalid-origin')
            assert seen(user='ops', operation='login', status='ok') and seen(user='ops', operation='logout', status='ok')
            assert seen(operation='other', status='denied')  # 'shell' is not echoed into the audit
            assert all(e['peer'].startswith('127.0.0.1:') for e in entries)
            listed = c.call('audit', token=ops['token'], limit=3)['data']['entries']
            assert len(listed) == 3 and listed == entries[-3:], listed
            print(f'PASS: {len(entries)} private audit records cover allowed, denied and unauthenticated decisions without credentials')

            # Restart: sessions are memory-only; the audit persists.
            session, cookie = c.login(ops['token'])
            csrf = session['data']['csrf']
            count = len(audit_path.read_text().splitlines())
            process.terminate()
            process.wait(timeout=15)
            process = start()
            wait_for(lambda: log_path.read_text().count('Local support console listening') == 2, 'console did not restart', process)
            assert c.call('health', cookie=cookie, csrf=csrf)['status'] == 'unauthenticated'
            assert c.call('inventory', token=ops['token'])['status'] == 'ok'
            assert len(audit_path.read_text().splitlines()) == count + 2
            wait_for(lambda: runtime()['phase'] == 'running', 'svc did not restart with the daemon', process)
            print('PASS: daemon restart ends sessions, keeps the audit and serves the console again')
            process.terminate()
            process.wait(timeout=15)
            process = None
            assert not sim.errors, sim.errors
    finally:
        if blocker is not None:
            blocker.close()
        if process is not None:
            process.terminate()
            process.wait(timeout=15)
        shim.terminate()
        shim.wait(timeout=10)
        server.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', action='store_true', help='also drive the UI in Chrome (CHROME_EXECUTABLE)')
    run(parser.parse_args().browser)
