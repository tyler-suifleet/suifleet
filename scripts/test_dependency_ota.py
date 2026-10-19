#!/usr/bin/env python3
"""Actual daemon: multi-component dependency resolution through the SF-030 test updater.

Schema-2 catalog deployments whose schema-1.1 recipes declare dependencies,
against the local chain/Walrus simulator. The catalog index is served as real
Move-object BCS by the gRPC shim. Covers dependency-first install order,
unavailable platform and soft dependencies, rejection of conflicts with
installed components and of cycles before any artifact download or install,
reuse of installed versions, a failed dependency install that stops its
dependent, and the `cached` offline policy against `never`. SF-037: a
dependency translated from AWS SOFT is required, and a supervised dependency's
restart does not restart its dependent (no AWS HARD restart coupling).

Local simulator evidence only: no public network, wallet or hardware.
"""
import hashlib, json, os, platform, signal, subprocess, tempfile, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import test_ota as ota
from deployment_manifest import CATALOG_TYPES
from test_component_ota import ComponentSimulator
import test_component as component

ota.TYPE_ORIGINS.update({n: ota.object_id('0x4') for n in CATALOG_TYPES})
CATALOG = ota.object_id('0xcafe')
# A manifest architecture that never matches this host.
OTHER_ARCH = 'arm' if platform.machine() in ('x86_64', 'aarch64') else 'x86_64'


def tag(name, version):
    """Blob IDs allow only letters, digits, '-' and '_'."""
    return f'{name}-{version}'.replace('.', '_')


def translated_dependencies(dependencies):
    """componentDependencies the real SF-037 translator emits for an AWS recipe."""
    recipe = dict(RecipeFormatVersion='2020-01-25', ComponentName='aws-app', ComponentVersion='1.0.0',
                  ComponentDependencies=dependencies,
                  Manifests=[dict(Platform=dict(os='linux'), Lifecycle=dict(install='true'), Artifacts=[dict(Uri='s3://b/app.tar')])])
    with tempfile.NamedTemporaryFile('w', suffix='.json') as file:
        json.dump(recipe, file); file.flush()
        done = subprocess.run(['node', 'scripts/greengrass_translate.cjs', file.name, '--bind', 's3://b/app.tar=blob:' + 'ab' * 32],
                              capture_output=True, text=True)
    result = json.loads(done.stdout)
    assert done.returncode == 0 and 'ComponentDependencies' in result['informational'], (done.stdout, done.stderr)
    return result['manifests'][0]['recipe']['componentDependencies']


def uleb(n):
    out = bytearray()
    while True:
        byte, n = n & 0x7f, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def address(id):
    return bytes.fromhex(id[2:].rjust(64, '0'))


class DependencySimulator(ComponentSimulator):
    def __init__(self):
        super().__init__()
        self.index = {}       # "name@version" -> version object ID
        self.versions = {}    # version object ID -> fields
        self.bindings = {}    # deployment ID -> version object ID
        self.downloads = []
        self.catalog_offline = False
        self.next_id = 0x5000

    def publish(self, name, version, dependencies=None, fail='none', available=True, lifecycle=None):
        artifact = component.build(version, 'deps-sim', fail, component=name)
        blob = tag(name, version)
        document = json.loads(component.recipe(artifact, f'artifact-{blob}'))
        if dependencies is not None:
            document['schemaVersion'] = '1.1'
            document['componentDependencies'] = dependencies
        if lifecycle:
            document['lifecycle'] = lifecycle
        recipe = json.dumps(document).encode()
        self.blobs[f'artifact-{blob}'], self.blobs[f'recipe-{blob}'] = artifact, recipe
        self.next_id += 1
        version_id = ota.object_id(hex(self.next_id))
        self.index[f'{name}@{version}'] = version_id
        self.versions[version_id] = dict(
            catalog_id=CATALOG, publisher=ota.OPERATOR, component_name=name, version=version, schema_version='1',
            platform_os=['linux'], platform_arch=['*' if available else OTHER_ARCH],
            recipe_blob_ids=[f'recipe-{blob}'], recipe_sha256s=[hashlib.sha256(recipe).hexdigest()],
            artifact_sha256s=[hashlib.sha256(artifact).hexdigest()])

    def deploy(self, id, name, version):
        self.bindings[id] = self.index[f'{name}@{version}']
        event = dict(deployment_id=id, component_name=name, version=version, recipe_blob_id='', artifact_sha256='',
                     target_groups=[], target_devices=[ota.DEVICE], creator=ota.OPERATOR)
        self.events.append(dict(id={'txDigest': f'event-{len(self.events)}', 'eventSeq': '0'}, parsedJson=event))
        self.schema_fields[id] = {'schema_version': '2'}

    def catalog_bcs(self):
        entries = sorted(self.index.items())
        body = address(CATALOG) + address(ota.OPERATOR) + uleb(len(entries))
        for key, id in entries:
            body += uleb(len(key)) + key.encode() + address(id)
        return body.hex()

    def obj(self, id):
        if id in self.versions:
            return {'data': {'objectId': id, 'owner': 'Immutable', 'content': {
                'type': f'{ota.TYPE_ORIGINS["component_catalog::ComponentVersion"]}::component_catalog::ComponentVersion',
                'fields': self.versions[id]}}}
        return super().obj(id)

    def rpc(self, method, params):
        if method == 'sui_getObject' and params[0] == CATALOG:
            if self.catalog_offline:
                raise ConnectionError('simulated unreachable catalog')
            return {'data': {'version': '7', 'owner': {'Shared': {'initial_shared_version': '1'}}, 'bcs': {
                'dataType': 'moveObject', 'bcsBytes': self.catalog_bcs(),
                'type': f'{ota.TYPE_ORIGINS["component_catalog::ComponentCatalog"]}::component_catalog::ComponentCatalog'}}}
        if method == 'shim_fields':
            key = f'{ota.TYPE_ORIGINS["component_catalog::CatalogBindingKey"]}::component_catalog::CatalogBindingKey'
            return super().rpc(method, params) + [[parent, key, '00'] for parent in self.bindings]
        if method == 'suix_getDynamicFieldObject' and params[1]['type'].endswith('CatalogBindingKey'):
            return {'data': {'content': {'fields': {'value': {'version_id': self.bindings[params[0]]}}}}}
        return super().rpc(method, params)


def run():
    sim = DependencySimulator()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def respond(self, body, status=200):
            self.send_response(status); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:
                response = {'id': request['id'], 'result': sim.rpc(request['method'], request['params'])}
            except Exception as error:
                if not isinstance(error, ConnectionError):
                    sim.errors.append(repr(error))
                response = {'id': request['id'], 'error': {'code': -32000, 'message': str(error)}}
            self.respond(json.dumps(response).encode())

        def do_GET(self):
            sim.downloads.append(self.path)
            body = sim.blobs.get(self.path.removeprefix('/v1/blobs/'))
            self.respond(body or b'not found', 200 if body else 404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); threading.Thread(target=server.serve_forever, daemon=True).start()
    shim, url = ota.start_grpc(f'http://127.0.0.1:{server.server_port}'); process = None
    try:
        with tempfile.TemporaryDirectory(prefix='dependency-ota-') as temp:
            root = Path(temp); key = root / 'device.key'; key.write_text('07' * 32); key.chmod(0o600)
            aggregator = f'http://127.0.0.1:{server.server_port}'
            manifest = dict(schema_version=1, protocol_version=1, network='localnet', chain_identifier=sim.chain, rpc_url=url,
                call_package_id=ota.PACKAGE, upgrade_cap_id=ota.UPGRADE_CAP, package_version='3', type_origins=ota.TYPE_ORIGINS,
                shared_objects={'registry': {'id': ota.REGISTRY, 'initial_shared_version': '1'}},
                supported_clients={'edge': {'min': '0.2.0', 'max_exclusive': '0.3.0'}},
                walrus={'publisher_url': aggregator, 'aggregator_url': aggregator})
            (root / 'deployment.json').write_text(json.dumps(manifest))
            order = root / 'order.log'
            # Record the global install order, then run the real SF-030 updater.
            wrapper = root / 'updater.sh'
            wrapper.write_text(f'#!/bin/sh\nsed -n "s/^# component=//p" "$1" | tr -d "\\n" >> {order}\n'
                               f'printf "@" >> {order}\nsed -n "s/^# version=//p" "$1" >> {order}\n'
                               f'exec /bin/sh {component.UPDATER} --root {root / "components"} "$1"\n')
            config = root / 'config.toml'

            def configure(policy, walrus='', apply=''):
                config.write_text(f'[sui]\nmanifest_path="deployment.json"\n[device]\nkeypair_path={json.dumps(str(key))}\n'
                    f'device_object_id="{ota.DEVICE}"\ndevice_cap_id="{ota.CAP}"\n[apply]\ncommand={json.dumps("/bin/sh " + str(wrapper))}\n{apply}'
                    f'[runtime]\nstate_dir={json.dumps(str(root / "state"))}\npoll_seconds=1\n'
                    f'[components]\noffline_candidates="{policy}"\n' + (f'[walrus]\n{walrus}\n' if walrus else ''))

            def state():
                p = root / 'state/state.json'; return json.loads(p.read_text()) if p.exists() else {}

            def installs():
                return order.read_text().split() if order.exists() else []

            def registry():
                return {n: (c['version'], c['confirmed']) for n, c in state().get('components', {}).items()}

            log = (root / 'daemon.log').open('w')

            def start():
                return subprocess.Popen([str(Path('suifleet-edge/target/debug/suifleet').resolve())],
                                        env={**os.environ, 'EDGE_CONFIG': str(config)}, stdout=log, stderr=log)

            def stop(process):
                process.send_signal(signal.SIGINT); process.wait(timeout=10)

            def settle(id):
                ota.wait_for(lambda: state().get('deployments', {}).get(id, {}).get('reported'), f'deployment {id}', process)
                return state()['deployments'][id]

            def artifact_downloads(*components):
                return [p for p in sim.downloads if any(p.endswith(f'artifact-{tag(*c)}') for c in components)]

            # Catalog. lib-log 1.3.0 has no manifest for this platform; metrics is never published.
            for v in ['1.0.0', '1.2.0', '2.0.0']:
                sim.publish('lib-log', v)
            sim.publish('lib-log', '1.3.0', available=False)
            sim.publish('lib-db', '3.1.0', {'lib-log': {'versionRequirement': '>=1.1.0 <2.0.0'}})
            sim.publish('app', '1.0.0', {'lib-db': {'versionRequirement': '^3.0.0', 'dependencyType': 'HARD'},
                                         'lib-log': {'versionRequirement': '^1.0.0'},
                                         'metrics': {'versionRequirement': '^1.0.0', 'dependencyType': 'SOFT'}})
            sim.publish('needs-log2', '1.0.0', {'lib-log': {'versionRequirement': '^2.0.0'}})
            sim.publish('cyc-a', '1.0.0', {'cyc-b': {'versionRequirement': '*'}})
            sim.publish('cyc-b', '1.0.0', {'cyc-a': {'versionRequirement': '*', 'dependencyType': 'SOFT'}})
            sim.publish('tool', '1.0.0', {'lib-log': {'versionRequirement': '^1.0.0'}})
            sim.publish('broken-dep', '1.0.0', fail='install')
            sim.publish('uses-broken', '1.0.0', {'broken-dep': {'versionRequirement': '1.0.0'}})
            # SF-037: AWS SOFT still requires the dependency at start, so the translation is native HARD.
            aws_dependencies = translated_dependencies({'gg-required': {'VersionRequirement': '^1.0.0', 'DependencyType': 'SOFT'}})
            assert aws_dependencies == {'gg-required': {'versionRequirement': '^1.0.0'}}, aws_dependencies
            sim.publish('aws-app', '1.0.0', aws_dependencies)

            configure('never')
            process = start(); ota.wait_for(lambda: state().get('initialized'), 'enrollment', process)

            sim.deploy('0x601', 'app', '1.0.0')
            assert settle('0x601')['status'] == 1, state()['deployments']['0x601']
            assert installs() == ['lib-log@1.2.0', 'lib-db@3.1.0', 'app@1.0.0'], installs()
            assert registry() == {'lib-log': ('1.2.0', True), 'lib-db': ('3.1.0', True), 'app': ('1.0.0', True)}, registry()
            assert not any(tag('lib-log', '1.3.0') in p for p in sim.downloads), 'unavailable platform candidate was downloaded'
            assert not any(tag('lib-log', v) in p for v in ['1.0.0', '2.0.0'] for p in sim.downloads), 'unneeded candidates were loaded'
            for name, version in [('lib-log', '1.2.0'), ('lib-db', '3.1.0'), ('app', '1.0.0')]:
                history = (root / 'components' / name / 'history.jsonl').read_text().splitlines()
                assert [json.loads(l)['version'] for l in history] == [version], (name, history)

            # Conflict with an installed dependent, then a cycle: rejected before any artifact download or install.
            before = (installs(), registry())
            sim.deploy('0x602', 'needs-log2', '1.0.0')
            outcome = settle('0x602')
            assert outcome['status'] == 2 and 'installed app 1.0.0 requires lib-log "^1.0.0"' in outcome['error'], outcome
            sim.deploy('0x603', 'cyc-a', '1.0.0')
            outcome = settle('0x603')
            assert outcome['status'] == 2 and 'dependency cycle: cyc-a -> cyc-b -> cyc-a' in outcome['error'], outcome
            assert (installs(), registry()) == before
            assert not artifact_downloads(('needs-log2', '1.0.0'), ('lib-log', '2.0.0'), ('cyc-a', '1.0.0'), ('cyc-b', '1.0.0')), sim.downloads

            # An installed compatible version is reused, not reinstalled.
            sim.deploy('0x604', 'tool', '1.0.0')
            assert settle('0x604')['status'] == 1
            assert installs()[len(before[0]):] == ['tool@1.0.0'], installs()

            # A failed dependency install stops the dependent and stays unconfirmed.
            sim.deploy('0x605', 'uses-broken', '1.0.0')
            outcome = settle('0x605')
            assert outcome['status'] == 2 and 'dependency broken-dep 1.0.0 install failed' in outcome['error'], outcome
            assert installs()[-1] == 'broken-dep@1.0.0' and 'uses-broken' not in registry(), installs()
            assert registry()['broken-dep'] == ('1.0.0', False)

            # A translated AWS SOFT dependency that nothing satisfies fails the deployment
            # before any download; unlike native SOFT (metrics above), it is never omitted.
            before = (installs(), registry())
            sim.deploy('0x60d', 'aws-app', '1.0.0')
            outcome = settle('0x60d')
            assert outcome['status'] == 2 and 'no available version of gg-required' in outcome['error'], outcome
            assert (installs(), registry()) == before and not artifact_downloads(('aws-app', '1.0.0')), sim.downloads
            stop(process)

            # The cached offline policy keeps authenticated candidates locally.
            for v in ['1.0.0', '1.1.0']:
                sim.publish('lib-gfx', v)
            sim.publish('ui', '1.0.0', {'lib-gfx': {'versionRequirement': '~1.0.0'}})
            sim.publish('ui', '1.1.0', {'lib-gfx': {'versionRequirement': '~1.1.0'}})
            configure('cached')
            process = start()
            sim.deploy('0x606', 'lib-gfx', '1.0.0'); assert settle('0x606')['status'] == 1
            sim.deploy('0x607', 'lib-gfx', '1.1.0'); assert settle('0x607')['status'] == 1
            assert registry()['lib-gfx'] == ('1.1.0', True)
            # Catalog unreachable and lib-gfx blobs gone: the downgrade comes from the cache.
            sim.catalog_offline = True
            for v in ['1.0.0', '1.1.0']:
                del sim.blobs[f'artifact-{tag("lib-gfx", v)}'], sim.blobs[f'recipe-{tag("lib-gfx", v)}']
            downloads = len(sim.downloads)
            sim.deploy('0x608', 'ui', '1.0.0')
            outcome = settle('0x608')
            assert outcome['status'] == 1, outcome
            assert installs()[-2:] == ['lib-gfx@1.0.0', 'ui@1.0.0'], installs()
            assert not any('lib-gfx' in p for p in sim.downloads[downloads:]), sim.downloads[downloads:]
            stop(process)

            # Cached copies obey a tightened artifact size limit and free-space reserve:
            # with ui 1.0.0 gone from Walrus, only the cache could satisfy it.
            del sim.blobs[f'artifact-{tag("ui", "1.0.0")}'], sim.blobs[f'recipe-{tag("ui", "1.0.0")}']
            for id, limit, refusal in [('0x60a', 'max_artifact_bytes=1', 'download limit'),
                                       ('0x60b', 'min_free_bytes=1000000000000000', 'insufficient disk space')]:
                configure('cached', limit)
                process = start()
                before, downloads = (installs(), registry()), len(sim.downloads)
                sim.deploy(id, 'ui', '1.0.0')
                outcome = settle(id)
                assert outcome['status'] == 2 and refusal in outcome['error'], outcome
                assert (installs(), registry()) == before
                assert not any('artifact-' in p for p in sim.downloads[downloads:]), sim.downloads[downloads:]
                stop(process)

            # The never policy does not use those cached candidates.
            configure('never')
            process = start()
            before = (installs(), registry())
            sim.deploy('0x609', 'ui', '1.1.0')
            outcome = settle('0x609')
            assert outcome['status'] == 2 and 'component catalog unavailable' in outcome['error'], outcome
            assert (installs(), registry()) == before
            stop(process); process = None

            # Supervised dependencies (SF-014): a dependency with a run command is
            # ready before its supervised dependent starts, at deploy and again at
            # daemon start; the dependent fails if started without it.
            sup = root / 'state' / 'supervised'
            sim.catalog_offline = False
            sim.publish('svc-db', '1.0.0', lifecycle={
                'run': 'echo svc-db >> ../../starts; sleep 0.5; touch up; exec sleep 300',
                'readiness': 'test -e up', 'shutdown': 'rm -f up; kill -TERM {pid}'})
            sim.publish('svc-app', '1.0.0', {'svc-db': {'versionRequirement': '^1.0.0'}}, lifecycle={
                'run': 'test -e ../../svc-db/work/up || exit 1; echo svc-app >> ../../starts; exec sleep 300'})

            def supervised():
                p = sup / 'status.json'
                return json.loads(p.read_text()) if p.exists() else {}

            def starts():
                p = sup / 'starts'
                return p.read_text().split() if p.exists() else []

            def both_running():
                current = supervised()
                return all(current.get(n, {}).get('phase') == 'running' for n in ('svc-db', 'svc-app'))

            configure('never', apply='allow_recipe_commands=true\n')
            process = start()
            sim.deploy('0x60c', 'svc-app', '1.0.0')
            outcome = settle('0x60c')
            assert outcome['status'] == 1, outcome
            assert both_running() and starts() == ['svc-db', 'svc-app'], (supervised(), starts())
            assert registry()['svc-db'] == ('1.0.0', True) and registry()['svc-app'] == ('1.0.0', True), registry()
            stop(process)
            assert not (sup / 'svc-db/work/up').exists(), 'svc-db was not stopped through its shutdown command'
            process = start()
            ota.wait_for(both_running, 'supervised components did not restart on daemon start', process)
            assert starts()[2:] == ['svc-db', 'svc-app'], starts()
            assert supervised()['svc-app']['restarts'] == 0, supervised()
            # No AWS HARD restart coupling (reported by the SF-037 translator): a
            # dependency crash restarts only the dependency, never its dependent.
            app_pid = supervised()['svc-app']['pid']
            os.kill(supervised()['svc-db']['pid'], signal.SIGKILL)
            ota.wait_for(lambda: supervised()['svc-db']['restarts'] == 1 and both_running(), 'svc-db did not restart', process)
            assert supervised()['svc-app']['pid'] == app_pid and supervised()['svc-app']['restarts'] == 0, supervised()
            assert starts()[4:] == ['svc-db'], starts()
            stop(process); process = None
            assert not sim.errors, sim.errors
            log.close()
        print('PASS: actual-daemon dependency-first multi-component install; unavailable platform/soft omission; '
              'installed-dependent conflict and cycle rejected before downloads/installs; installed reuse; '
              'failed dependency stops dependent; cached offline candidates only under the cached policy and download limits; '
              'supervised dependency ready before its dependent at deploy and at daemon start; '
              'translated AWS SOFT dependency required, not omitted; dependency restart leaves its dependent running')
    finally:
        if process and process.poll() is None:
            process.kill(); process.wait()
        shim.kill(); shim.wait(); server.shutdown(); server.server_close()


if __name__ == '__main__':
    run()
