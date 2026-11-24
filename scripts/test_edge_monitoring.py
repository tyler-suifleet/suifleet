#!/usr/bin/env python3
"""Customer-owned Prometheus/OpenTelemetry monitoring (SF-303), end to end.

The real daemon runs against the local chain/Walrus simulator with its local
support console. The real adapter (scripts/edge_monitoring.py) reads it with a
read-only console token and exports to a real Prometheus server and a real
OpenTelemetry Collector (pinned upstream binaries prepared by
scripts/test_edge_monitoring.sh), all on loopback without public network,
keys or gas:

  disabled by default        -> adapter exits without listening or polling
  unsafe configuration       -> public /metrics without a token refused
  Prometheus pull            -> bearer-authenticated scrape ingested; a wrong
                                scrape token is refused (target down, 401)
  current vs historical      -> device healthy while a past deployment failed;
                                labels carry evidence="simulated"
  OTLP push                  -> metrics and redacted log tails reach the
                                collector through an authenticating gateway;
                                a wrong gateway credential is refused
  collector outage           -> exponential backoff reported, buffered log
                                lines delivered after recovery without a gap
  daemon stopped             -> suifleet_up 0, stale device metrics withdrawn
  redaction                  -> no console token, scrape token, gateway
                                credential or log canary in any export
  bounded resources          -> adapter peak RSS, collector memory limiter and
                                Prometheus retention bounds recorded
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_ota
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'suifleet-edge/target/debug/suifleet'
ADAPTER = ROOT / 'scripts/edge_monitoring.py'
CANARY = 'hunter2-monitoring-secret'
GATEWAY_TOKEN = 'gateway-credential-canary'
ADAPTER_RSS_LIMIT_KIB = 64 * 1024


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def listening(port):
    with socket.socket() as probe:
        return probe.connect_ex(('127.0.0.1', port)) == 0


def peak_rss_kib(pid):
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        if line.startswith('VmHWM:'):
            return int(line.split()[1])
    return None


def http(url, *, data=None, headers=None, timeout=5):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method='POST' if data is not None else 'GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def console_token():
    result = subprocess.run([str(BINARY), '--console-token'], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class Gateway:
    """Stand-in for a customer's authenticating OTLP gateway in front of the
    collector: requires a bearer credential, forwards the body unchanged."""

    def __init__(self, upstream_port):
        self.upstream = f'http://127.0.0.1:{upstream_port}'
        self.unauthorized = 0
        self.forwarded = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                if self.headers.get('Authorization') != f'Bearer {GATEWAY_TOKEN}':
                    gateway.unauthorized += 1
                    return self.reply(401, b'')
                gateway.forwarded.append((self.path, body))
                try:
                    status, data = http(gateway.upstream + self.path, data=body,
                                        headers={'Content-Type': self.headers.get('Content-Type', '')})
                except OSError:
                    status, data = 503, b''
                self.reply(status, data)

            def reply(self, status, data):
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def prometheus_query(port, expr):
    status, body = http(f'http://127.0.0.1:{port}/api/v1/query?' + urllib.parse.urlencode(dict(query=expr)))
    assert status == 200, body
    return json.loads(body)['data']['result']


def scrape(port, token):
    return http(f'http://127.0.0.1:{port}/metrics', headers={'Authorization': f'Bearer {token}'})


def sample(text, name, **labels):
    for line in text.splitlines():
        if line.startswith(name + '{') and all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.rsplit(' ', 1)[1])
    return None


def collector_records(path):
    """(metric names -> resource attributes, log bodies) from the file exporter."""
    metrics, logs = {}, []
    if not path.exists():
        return metrics, logs
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        batch = json.loads(line)
        for rm in batch.get('resourceMetrics', []):
            attrs = {a['key']: a['value'].get('stringValue') for a in rm['resource'].get('attributes', [])}
            for sm in rm.get('scopeMetrics', []):
                for metric in sm.get('metrics', []):
                    metrics.setdefault(metric['name'], []).append((attrs, metric))
        for rl in batch.get('resourceLogs', []):
            attrs = {a['key']: a['value'].get('stringValue') for a in rl['resource'].get('attributes', [])}
            for sl in rl.get('scopeLogs', []):
                for record in sl.get('logRecords', []):
                    component = {a['key']: a['value'].get('stringValue') for a in record.get('attributes', [])}
                    logs.append((attrs, component.get('suifleet.component'), record['body'].get('stringValue')))
    return metrics, logs


def run(bin_dir, evidence_path):
    prometheus_bin, otelcol_bin = bin_dir / 'prometheus', bin_dir / 'otelcol'
    for path in (prometheus_bin, otelcol_bin, BINARY):
        assert path.is_file() and os.access(path, os.X_OK), f'missing executable {path}'
    evidence = dict(ticket='SF-303', evidence_class='simulated', binaries={
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (prometheus_bin, otelcol_bin)})
    for name, binary in (('prometheus', prometheus_bin), ('otelcol', otelcol_bin)):
        version = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=30)
        evidence.setdefault('versions', {})[name] = (version.stdout or version.stderr).splitlines()[0]

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
    processes = {}

    def stop(name):
        process = processes.pop(name, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    try:
        with tempfile.TemporaryDirectory(prefix='sf303-') as temporary:
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
            console_port = free_port()
            monitor = console_token()
            config = root / 'config.toml'
            config.write_text(
                '[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state))}\npoll_seconds = 1\nevent_discovery_seconds = 1\n'
                'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 3\ncomponent_stop_timeout_seconds = 1\n'
                f'[console]\nlisten = "127.0.0.1:{console_port}"\n'
                # Least privilege for monitoring: read views only, no configuration/audit/support operations.
                f'[console.users.monitoring]\ntoken_sha256 = "{monitor["token_sha256"]}"\n'
                'grants = ["inventory", "health", "deployments", "logs"]\n')
            daemon_log = root / 'daemon.log'

            def start_daemon():
                log = daemon_log.open('a')
                processes['daemon'] = subprocess.Popen([str(BINARY)], env={**os.environ, 'EDGE_CONFIG': str(config)},
                                                       stdout=log, stderr=log)
                wait_for(lambda: 'Local support console listening' in daemon_log.read_text(), 'console did not start',
                         processes['daemon'], timeout=60)
            start_daemon()

            def initialized():
                try:
                    return json.loads((state / 'state.json').read_text()).get('initialized')
                except (OSError, ValueError):
                    return False
            # Deployments published before the first journal initialization are history.
            wait_for(initialized, 'daemon did not initialize', processes['daemon'], timeout=90)

            # A historical failure (artifact digest mismatch), then a healthy
            # supervised component that logs credentials and a steady tick.
            sim.add(test_ota.object_id('0x7a1'), bad_hash=True, event=dict(component_name='broken'))
            sim.add(test_ota.object_id('0x7a2'), event=dict(component_name='svc'), recipe=dict(lifecycle=dict(
                run=f'echo "start password={CANARY} Authorization: Bearer {monitor["token"]} suiprivkey1abc {"07" * 32}"; '
                    'i=0; while true; do echo "tick $i"; i=$((i+1)); sleep 1; done')))

            def journal():
                try:
                    return json.loads((state / 'state.json').read_text()).get('deployments', {})
                except (OSError, ValueError):
                    return {}
            try:
                wait_for(lambda: len(journal()) == 2 and all(o.get('status') for o in journal().values()),
                         'deployments did not settle', processes['daemon'], timeout=90)
            except AssertionError:
                print(json.dumps(journal(), indent=1), daemon_log.read_text()[-4000:], sep='\n', file=sys.stderr)
                raise
            statuses = sorted(o['status'] for o in journal().values())
            assert statuses == [1, 2], statuses
            print('PASS: real daemon recorded one applied and one failed deployment')

            secrets_dir = root / 'monitoring'
            secrets_dir.mkdir(mode=0o700)
            token_file = secrets_dir / 'console.token'
            token_file.write_text(monitor['token'] + '\n')
            token_file.chmod(0o600)
            scrape_token = json.loads(subprocess.run([sys.executable, str(ADAPTER), '--token'], capture_output=True,
                                                     text=True, check=True).stdout)
            scrape_file = secrets_dir / 'scrape.token'
            scrape_file.write_text(scrape_token['token'])
            scrape_file.chmod(0o600)
            headers_file = secrets_dir / 'otlp.headers'
            headers_file.write_text(f'Authorization: Bearer {GATEWAY_TOKEN}\n')
            headers_file.chmod(0o600)

            def adapter_config(name, **fields):
                path = secrets_dir / name
                path.write_text(json.dumps(fields))
                path.chmod(0o644)
                return path

            # Disabled by default: no listener, no console request.
            metrics_port = free_port()
            disabled = adapter_config('disabled.json', prometheus=dict(listen=f'127.0.0.1:{metrics_port}'))
            result = subprocess.run([sys.executable, str(ADAPTER), '--config', str(disabled)], capture_output=True,
                                    text=True, timeout=30)
            assert result.returncode == 0 and 'disabled' in result.stderr, result
            assert not listening(metrics_port), 'disabled adapter listened'
            unsafe = adapter_config('unsafe.json', enabled=True, evidence='simulated',
                                    console=dict(url=f'http://127.0.0.1:{console_port}', token_file=str(token_file)),
                                    prometheus=dict(listen=f'0.0.0.0:{metrics_port}', allow_remote=True))
            result = subprocess.run([sys.executable, str(ADAPTER), '--config', str(unsafe)], capture_output=True,
                                    text=True, timeout=30)
            assert result.returncode == 2 and 'token_sha256' in result.stderr, result
            assert monitor['token'] not in result.stderr + result.stdout
            print('PASS: adapter is disabled by default and refuses a public unauthenticated /metrics')

            # Real OpenTelemetry Collector behind an authenticating gateway.
            otlp_port = free_port()
            otel_out = root / 'otel-out.jsonl'
            otel_config = root / 'otelcol.json'
            otel_config.write_text(json.dumps({
                'receivers': {'otlp': {'protocols': {'http': {'endpoint': f'127.0.0.1:{otlp_port}'}}}},
                'processors': {'memory_limiter': {'check_interval': '1s', 'limit_mib': 128, 'spike_limit_mib': 32}},
                'exporters': {'file': {'path': str(otel_out), 'flush_interval': '200ms'}},
                'service': {'telemetry': {'metrics': {'level': 'none'}, 'logs': {'level': 'warn'}},
                            'pipelines': {sig: {'receivers': ['otlp'], 'processors': ['memory_limiter'], 'exporters': ['file']}
                                          for sig in ('metrics', 'logs')}}}))
            otel_log = root / 'otelcol.log'

            def start_collector():
                log = otel_log.open('a')
                processes['otelcol'] = subprocess.Popen([str(otelcol_bin), '--config', str(otel_config)],
                    env={'GOMEMLIMIT': '160MiB', 'GOMAXPROCS': '2', 'PATH': '/usr/bin:/bin'}, stdout=log, stderr=log)
                wait_for(lambda: listening(otlp_port), 'collector did not listen',
                         processes['otelcol'], timeout=60)
            start_collector()
            gateway = Gateway(otlp_port)

            adapter_port = free_port()
            adapter_cfg = adapter_config('monitoring.json', enabled=True, evidence='simulated', device='sim-device-1',
                interval_seconds=10,
                console=dict(url=f'http://127.0.0.1:{console_port}', token_file=str(token_file)),
                prometheus=dict(listen=f'127.0.0.1:{adapter_port}', token_sha256=scrape_token['token_sha256']),
                otlp=dict(endpoint=f'http://127.0.0.1:{gateway.port}', headers_file=str(headers_file),
                          timeout_seconds=2, initial_backoff_seconds=1, max_backoff_seconds=4),
                logs=dict(components=['svc'], max_lines=50, max_buffer=500))
            adapter_log = root / 'adapter.log'
            processes['adapter'] = subprocess.Popen([sys.executable, str(ADAPTER), '--config', str(adapter_cfg)],
                                                    stdout=adapter_log.open('a'), stderr=subprocess.STDOUT)
            wait_for(lambda: listening(adapter_port) and scrape(adapter_port, scrape_token['token'])[0] == 200,
                     'adapter did not serve /metrics', processes['adapter'], timeout=30)

            # Real Prometheus scraping the adapter; one job uses a wrong token.
            prom_port = free_port()
            prom_config = root / 'prometheus.yml'
            prom_config.write_text(json.dumps({
                'global': {'scrape_interval': '1s', 'scrape_timeout': '1s'},
                'scrape_configs': [
                    {'job_name': 'suifleet-edge', 'authorization': {'type': 'Bearer', 'credentials_file': str(scrape_file)},
                     'static_configs': [{'targets': [f'127.0.0.1:{adapter_port}']}]},
                    {'job_name': 'wrong-token', 'authorization': {'type': 'Bearer', 'credentials': 'sfm_wrong'},
                     'static_configs': [{'targets': [f'127.0.0.1:{adapter_port}']}]}]}))
            prom_log = root / 'prometheus.log'
            processes['prometheus'] = subprocess.Popen([str(prometheus_bin), f'--config.file={prom_config}',
                f'--storage.tsdb.path={root / "tsdb"}', '--storage.tsdb.retention.size=64MB',
                f'--web.listen-address=127.0.0.1:{prom_port}', '--log.level=warn'],
                env={'GOMEMLIMIT': '256MiB', 'GOMAXPROCS': '2', 'PATH': '/usr/bin:/bin'},
                stdout=prom_log.open('a'), stderr=subprocess.STDOUT)

            def ingested(expr):
                try:
                    return prometheus_query(prom_port, expr)
                except (OSError, AssertionError, KeyError, ValueError):
                    return []
            wait_for(lambda: ingested('suifleet_device_healthy{job="suifleet-edge"} == 1'),
                     'Prometheus did not ingest a healthy device', processes['prometheus'], timeout=90)
            series = ingested('suifleet_device_healthy{job="suifleet-edge"}')[0]['metric']
            assert series['evidence'] == 'simulated' and series['device'] == 'sim-device-1', series
            failed = ingested('suifleet_deployment_outcomes{job="suifleet-edge",outcome="failed"}')
            applied = ingested('suifleet_deployment_outcomes{job="suifleet-edge",outcome="applied"}')
            assert failed and failed[0]['value'][1] == '1' and applied[0]['value'][1] == '1', (failed, applied)
            running = ingested('suifleet_component_running{job="suifleet-edge",component="svc"}')
            assert running and running[0]['value'][1] == '1', running
            assert not ingested('suifleet_component_running{component="broken"}')
            wait_for(lambda: any(t['labels']['job'] == 'wrong-token' and t['health'] == 'down' and '401' in t['lastError']
                                 for t in json.loads(http(f'http://127.0.0.1:{prom_port}/api/v1/targets')[1])['data']['activeTargets']),
                     'wrong scrape token was not refused', timeout=30)
            assert not ingested('suifleet_up{job="wrong-token"} == 1')
            print('PASS: real Prometheus ingested current health (healthy) and historical failure (1) with evidence="simulated"; wrong scrape token refused (401)')

            # OTLP: metrics and redacted logs reach the real collector.
            def delivered():
                metrics, logs = collector_records(otel_out)
                return 'suifleet_device_healthy' in metrics and any(body.startswith('start ') for _, _, body in logs)
            wait_for(delivered, 'collector did not receive metrics and logs', processes['otelcol'], timeout=60)
            metrics, logs = collector_records(otel_out)
            attrs, healthy = metrics['suifleet_device_healthy'][-1]
            assert attrs['suifleet.evidence'] == 'simulated' and attrs['suifleet.device'] == 'sim-device-1', attrs
            assert healthy['gauge']['dataPoints'][0]['asInt'] == '1', healthy
            outcome_points = metrics['suifleet_deployment_outcomes'][-1][1]['gauge']['dataPoints']
            failed_point = next(p for p in outcome_points if {'key': 'outcome', 'value': {'stringValue': 'failed'}} in p['attributes'])
            assert failed_point['asInt'] == '1', failed_point
            start_line = next(body for _, component, body in logs if body.startswith('start '))
            assert '[redacted]' in start_line and CANARY not in start_line, start_line
            assert all(component == 'svc' for _, component, _ in logs)
            print(f'PASS: real OpenTelemetry Collector received {len(metrics)} metric names and {len(logs)} redacted log records through the authenticating gateway')

            # A wrong gateway credential is refused and reported, never retried as success.
            wrong_headers = secrets_dir / 'wrong.headers'
            wrong_headers.write_text('Authorization: Bearer not-the-gateway-token\n')
            wrong_headers.chmod(0o600)
            wrong_port = free_port()
            wrong_cfg = adapter_config('wrong.json', enabled=True, evidence='simulated', interval_seconds=10,
                console=dict(url=f'http://127.0.0.1:{console_port}', token_file=str(token_file)),
                prometheus=dict(listen=f'127.0.0.1:{wrong_port}'),
                otlp=dict(endpoint=f'http://127.0.0.1:{gateway.port}', headers_file=str(wrong_headers)))
            processes['wrong'] = subprocess.Popen([sys.executable, str(ADAPTER), '--config', str(wrong_cfg)],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def wrong_metrics():
                try:
                    return http(f'http://127.0.0.1:{wrong_port}/metrics')[1].decode()
                except OSError:
                    return ''
            wait_for(lambda: (sample(wrong_metrics(), 'suifleet_exporter_otlp_exports_total', signal='metrics', result='unauthorized') or 0) >= 1,
                     'unauthorized OTLP export not reported', processes['wrong'], timeout=30)
            assert gateway.unauthorized >= 1
            stop('wrong')
            print('PASS: OTLP gateway refused a wrong credential (401) and the adapter reported it as unauthorized')

            # Collector outage: backoff, then buffered lines arrive without a gap.
            stop('otelcol')
            outage_started = time.monotonic()

            def adapter_metrics():
                status, body = scrape(adapter_port, scrape_token['token'])
                assert status == 200
                return body.decode()
            wait_for(lambda: (sample(adapter_metrics(), 'suifleet_exporter_otlp_backoff_seconds', signal='metrics') or 0) > 0,
                     'adapter did not back off during the outage', processes['adapter'], timeout=40)
            wait_for(lambda: (sample(adapter_metrics(), 'suifleet_exporter_log_buffered') or 0) > 0,
                     'no log lines buffered during the outage', processes['adapter'], timeout=40)
            text = adapter_metrics()
            retries = sample(text, 'suifleet_exporter_otlp_exports_total', signal='metrics', result='retry') or 0
            backoff = sample(text, 'suifleet_exporter_otlp_backoff_seconds', signal='metrics')
            assert retries >= 1 and 0 < backoff <= 4, (retries, backoff)
            buffered = sample(text, 'suifleet_exporter_log_buffered')
            evidence['outage'] = dict(retries=retries, backoff_seconds=backoff, buffered_lines=buffered,
                                      seconds=round(time.monotonic() - outage_started, 1))
            start_collector()
            wait_for(lambda: sample(adapter_metrics(), 'suifleet_exporter_log_buffered') == 0
                     and sample(adapter_metrics(), 'suifleet_exporter_otlp_backoff_seconds', signal='metrics') == 0,
                     'adapter did not recover after the collector returned', processes['adapter'], timeout=60)
            time.sleep(1)
            _, logs = collector_records(otel_out)
            ticks = sorted({int(body.split()[1]) for _, _, body in logs if body.startswith('tick ')})
            assert ticks and ticks == list(range(ticks[0], ticks[-1] + 1)), ('gap in delivered tick lines', ticks)
            gaps = sample(adapter_metrics(), 'suifleet_exporter_log_gaps_total')
            assert gaps == 0, gaps
            print(f'PASS: collector outage backed off ({retries:.0f} retries, delay {backoff}s), then delivered {buffered:.0f} buffered lines; ticks {ticks[0]}..{ticks[-1]} contiguous')

            # Daemon stopped: current state withdrawn, never presented as stale health.
            stop('daemon')
            wait_for(lambda: sample(adapter_metrics(), 'suifleet_up') == 0, 'adapter did not report the console down',
                     processes['adapter'], timeout=30)
            text = adapter_metrics()
            assert 'suifleet_device_healthy{' not in text and 'suifleet_deployment_outcomes{' not in text, text
            assert (sample(text, 'suifleet_exporter_polls_total', result='unreachable') or 0) >= 1
            wait_for(lambda: ingested('suifleet_up{job="suifleet-edge"} == 0'), 'Prometheus did not record suifleet_up 0',
                     timeout=30)
            print('PASS: stopped daemon reported as suifleet_up 0 with device metrics withdrawn')

            # Redaction across every export surface.
            exported = otel_out.read_text() + adapter_metrics() + ''.join(body.decode() for _, body in gateway.forwarded)
            for name, secret in (('console token', monitor['token']), ('scrape token', scrape_token['token']),
                                 ('gateway credential', GATEWAY_TOKEN), ('log canary', CANARY),
                                 ('suiprivkey', 'suiprivkey1abc'), ('raw hex', '07' * 32)):
                assert secret not in exported, f'{name} leaked into an export'
            logs_text = adapter_log.read_text()
            for secret in (monitor['token'], scrape_token['token'], GATEWAY_TOKEN):
                assert secret not in logs_text, 'secret in adapter log'
            print('PASS: no console token, scrape token, gateway credential or log canary in any export or adapter log')

            # Bounded resources.
            rss = {name: peak_rss_kib(processes[name].pid) for name in ('adapter', 'prometheus', 'otelcol')}
            evidence['peak_rss_kib'] = rss
            assert rss['adapter'] < ADAPTER_RSS_LIMIT_KIB, rss
            assert rss['otelcol'] < 256 * 1024 and rss['prometheus'] < 512 * 1024, rss
            print(f'PASS: bounded resources, peak RSS KiB {rss} (adapter limit {ADAPTER_RSS_LIMIT_KIB})')

            stop('adapter')
            evidence['result'] = 'pass'
            if evidence_path:
                Path(evidence_path).write_text(json.dumps(evidence, indent=2) + '\n')
            print('PASS: SF-303 customer-owned Prometheus/OpenTelemetry monitoring (simulated evidence)')
    finally:
        for name in list(processes):
            stop(name)
        shim.terminate()
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--bin-dir', default=str(ROOT / '.ci-cache/monitoring-bin'),
                        help='directory holding the pinned prometheus and otelcol binaries')
    parser.add_argument('--evidence', help='write a JSON evidence summary here')
    args = parser.parse_args()
    run(Path(args.bin_dir), args.evidence)


if __name__ == '__main__':
    main()
