#!/usr/bin/env python3
"""Packaged target daemon + codec encrypted-archive round trip in user-mode QEMU.

Runs the image's AArch64 ``/usr/bin/suifleet`` against the SF-082 fleet-data
simulator (``test_fleet_data_ota.Simulator`` behind the gRPC shim). Its archive
helper is the image's own Node runtime and Walrus codec, at their rootfs paths
and with the codec's pinned SHA-256. The daemon collects and redacts a
component log, signs the archive and Seal-encrypts it natively with the
fixture key servers in ``interfaces/seal-vectors.json``. The packaged codec
encodes that exact ciphertext. After the simulated registration, certification
and anchor, the owner library (``verify_native_fleet.cjs``) threshold-decrypts
the uploaded ciphertext with the fixture keys and verifies its signature,
binding and redaction.

Without binfmt_misc the host kernel cannot execute an AArch64 ``node``, so the
configured ``node`` is a host launcher that runs the rootfs ``node`` under
``qemu-aarch64 -L``; on a device that path is the binary itself. The codec
supports only the Testnet Walrus deployment, so for ``prepare`` the launcher
replaces the simulated chain, RPC URL and Walrus IDs with read-only Testnet
values (no transaction). ``registered`` and ``write`` are answered by the
simulator stand-in, as in ``test_fleet_data_ota.py``; storage-node writes,
certificates and physical boot are not tested here.

Negative controls run the same daemon first. Each must leave the archive
blocked, with no prepaid registration:

* tampered codec bundle (refused by the daemon's checksum, never executed);
* missing codec and missing Node runtime;
* a target sysroot without ``libstdc++`` (the runtime library Node needs);
* a codec directory without its WASM module, and one with a corrupt module.
"""
import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_budget_ota as budget  # noqa: E402
import test_fleet_data_ota as fleet  # noqa: E402
import test_ota as ota  # noqa: E402
from deployment_manifest import object_id  # noqa: E402
from test_pi_codec import CODEC, NODE, check_artifact, sha256  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TESTNET = dict(chain='4c78adac', system_id='0x6c2547cbbc38025cf3adac45f63cb0a8d12ecf777cdc75a4971612bf97fdf6af',
               walrus_package='0x7d3b2086808a62bbc44d18c6b1d407973d77dd26946735873092c2f5650b4611')
# The host launcher: the stand-in for executing an AArch64 ELF directly.
LAUNCHER = r'''#!/usr/bin/python3 -I
import hashlib, json, pathlib, shutil, subprocess, sys, urllib.request
C = json.loads(%r)
request, codec = json.loads(sys.stdin.readline()), sys.argv[1]
record = dict(case=C['case'], operation=request['operation'], codec=codec)
def finish(code, out=''):
    with open(C['log'], 'a') as log: log.write(json.dumps(record) + '\n')
    sys.stdout.write(out); sys.exit(code)
if request['operation'] == 'prepare':
    request.update(C['testnet'])
    line = json.dumps(request) + '\n'
    record['ciphertext_sha256'] = hashlib.sha256(pathlib.Path(request['ciphertext_path']).read_bytes()).hexdigest()
    target = subprocess.run([C['qemu'], '-L', C['sysroot'], C['node'], codec], input=line, capture_output=True, text=True, timeout=280)
    record.update(exit=target.returncode, stderr=target.stderr.strip()[-200:], target=target.stdout.strip())
    if target.returncode == 0 and C['host_node']:
        host = subprocess.run([C['host_node'], codec], input=line, capture_output=True, text=True, timeout=280)
        record.update(host_exit=host.returncode, host=host.stdout.strip())
    finish(target.returncode, target.stdout)
if request['operation'] == 'registered':
    q = urllib.request.Request(C['simulator'], json.dumps(dict(jsonrpc='2.0', id=1, method='test_blob', params=[request['digest']])).encode(),
                               {'Content-Type': 'application/json'})
    with urllib.request.urlopen(q) as response:
        finish(0, json.dumps(json.load(response)['result']) + '\n')
if request['operation'] == 'write':
    shutil.copyfile(request['ciphertext_path'], pathlib.Path(C['uploads'])/(request['blob_object_id'] + '.bin'))
    finish(0, json.dumps(dict(signature=__import__('base64').b64encode(bytes([2]) + bytes(95)).decode(), bitmap='AQ==', message='Zml4dHVyZQ==')) + '\n')
finish(1)
'''


def wait(predicate, seconds, what, process):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        if process.poll() is not None:
            raise AssertionError(f'daemon exited ({process.returncode}) before: {what}')
        time.sleep(0.25)
    raise AssertionError(f'timed out: {what}')


@contextlib.contextmanager
def failure_context(path):
    """Show the daemon's own log when a check fails."""
    try:
        yield
    except BaseException:
        print('--- daemon log tail ---\n' + path.read_text(errors='replace')[-6000:], file=sys.stderr)
        raise


def minimal_sysroot(rootfs, directory):
    """The target's library directories, minus libstdc++."""
    removed = set()
    for sub in ['lib', 'usr/lib']:
        (directory/sub).mkdir(parents=True)
        for entry in (rootfs/sub).iterdir():
            if entry.name.startswith('libstdc++'):
                removed.add(entry.name)
            elif not entry.is_dir() or entry.is_symlink():
                (directory/sub/entry.name).symlink_to(entry)
    assert removed, 'the target rootfs must ship libstdc++ for Node'
    return sorted(removed)


def run(args):
    rootfs = args.rootfs.resolve()
    daemon, node, codec_dir = rootfs/'usr/bin/suifleet', rootfs/NODE, rootfs/CODEC
    manifest_node = check_artifact(codec_dir, json.loads((codec_dir/'manifest.json').read_text())['node'])[0]['node']
    pinned = sha256(codec_dir/'walrus-device-codec.cjs')
    ota.SHIM = args.shim.resolve()
    sim = fleet.Simulator()
    sim.fail_once = sim.certify_outage = sim.uploads_paused = sim.register_reply_outage = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            r = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:
                answer = {'result': sim.rpc(r['method'], r['params'])}
            except Exception as e:  # the simulator reports refusals as RPC errors
                if not isinstance(e, ConnectionError):
                    sim.errors.append(repr(e))
                answer = {'error': {'code': -32000, 'message': str(e)}}
            b = json.dumps({'jsonrpc': '2.0', 'id': r['id'], **answer}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers()
            try:
                self.wfile.write(b)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    simulator = f'http://127.0.0.1:{server.server_port}'
    shim, rpc = ota.start_grpc(simulator)
    process = None
    result = dict(daemon=dict(path='/usr/bin/suifleet', sha256=sha256(daemon)), node=dict(path='/'+NODE, sha256=sha256(node), version=manifest_node),
                  codec=dict(path='/'+CODEC, bundle_sha256=pinned), negative={}, user_mode_qemu=True, physical_boot_tested=False,
                  storage_node_write_tested=False, testnet_transactions=0)
    try:
        with tempfile.TemporaryDirectory(prefix='sf120-archive-') as temp:
            root = Path(temp)
            key = root/'device.key'; key.write_text('07'*32); key.chmod(0o600)
            log, uploads = root/'codec-calls.jsonl', root/'uploaded'
            uploads.mkdir(); sim.upload_dir = uploads
            manifest = dict(schema_version=1, protocol_version=1, network='localnet', chain_identifier=sim.chain, rpc_url=rpc,
                            call_package_id=ota.PACKAGE, upgrade_cap_id=ota.UPGRADE_CAP, package_version='3', type_origins=ota.TYPE_ORIGINS,
                            shared_objects={'registry': {'id': ota.REGISTRY, 'initial_shared_version': '1'}},
                            supported_clients={'dapp': {'min': '0.0.1', 'max_exclusive': '0.3.0'}, 'edge': {'min': '0.1.0', 'max_exclusive': '0.3.0'}},
                            walrus={'publisher_url': rpc, 'aggregator_url': rpc})
            (root/'deployment.json').write_text(json.dumps(manifest))
            vectors = json.loads((ROOT/'interfaces/seal-vectors.json').read_text())
            for server_key in vectors['servers']:
                id = server_key['object_id']
                sim.key_objects[id] = fleet.raw(id, '0xf5::key_server::KeyServer', {}, fleet.addr(id)+fleet.u64(1)+fleet.u64(1))
                field = hashlib.blake2b(b'\xf0'+fleet.addr(id)+fleet.u64(8)+fleet.u64(1)+b'\x02', digest_size=32).digest()
                contents = field+fleet.u64(1)+fleet.vec(b'fixture')+fleet.vec(b'https://fixture.invalid')+b'\0'+fleet.vec(bytes.fromhex(server_key['public_key']))
                sim.key_objects['0x'+field.hex()] = fleet.raw('0x'+field.hex(), '0x2::dynamic_field::Field<u64,0xf5::key_server::KeyServerV1>', {}, contents)
            state = root/'state'
            (state/'supervised'/'mqtt').mkdir(parents=True)
            (state/'overlays').mkdir()
            (state/'overlays'/'mqtt.json').write_text(json.dumps({'/password': 'fixture-confidential-value'}))

            def launcher(case, sysroot=rootfs):
                path = root/f'node-{case}'
                path.write_text(LAUNCHER % json.dumps(dict(case=case, log=str(log), qemu=str(args.qemu.resolve()), sysroot=str(sysroot),
                                                           node=str(node), testnet=dict(TESTNET, rpc_url=args.rpc_url),
                                                           host_node=str(args.host_node) if args.host_node else None,
                                                           simulator=simulator, uploads=str(uploads))))
                path.chmod(0o755)
                return path

            def configure(node_path, codec_path, codec_sha=pinned):
                (root/'config.toml').write_text(
                    f'[sui]\nmanifest_path="deployment.json"\n[device]\nkeypair_path={json.dumps(str(key))}\ndevice_object_id="{ota.DEVICE}"\n'
                    f'device_cap_id="{ota.CAP}"\n[walrus]\naggregator_url="{rpc}"\n[apply]\ncommand="true"\n[runtime]\nstate_dir={json.dumps(str(state))}\n'
                    f'poll_seconds=1\nevent_discovery_seconds=1\n[components]\nconfiguration_overlay_dir={json.dumps(str(state/"overlays"))}\n'
                    '[logs]\ninterval_seconds=10\nmax_lines_per_interval=128\nbuffer_max=16\n'
                    f'[fleet_data]\npackage_id="{fleet.PACKAGE}"\ntype_origin="{fleet.PACKAGE}"\nfleet_id="{fleet.FLEET}"\nbudget_id="{budget.BUDGET}"\n'
                    f'storage_id="{fleet.STORAGE}"\nwriter_id="{fleet.WRITER}"\naccount_id="{budget.ACCOUNT}"\n'
                    '[fleet_data.archives]\nstorage_package="0xf6"\nstorage_type_origin="0xf6"\nsystem_id="0xf4"\nwalrus_package="0xf7"\nkey_server_package="0xf5"\n'
                    'key_servers = ' + json.dumps([{'object_id': s['object_id'], 'weight': 1} for s in vectors['servers']]).replace(': ', ' = ') + '\n'
                    f'threshold=2\nnode={json.dumps(str(node_path))}\ncodec={json.dumps(str(codec_path))}\ncodec_sha256="{codec_sha}"\n')

            def calls(case):
                return [json.loads(line) for line in log.read_text().splitlines() if json.loads(line)['case'] == case] if log.exists() else []

            def start():
                env = {'PATH': '/usr/bin:/bin', 'EDGE_CONFIG': str(root/'config.toml'), 'RUST_LOG': 'info'}
                return subprocess.Popen([str(args.qemu.resolve()), '-L', str(rootfs), str(daemon)], env=env, cwd=root,
                                        stdout=daemon_log, stderr=daemon_log)

            def stop():
                nonlocal process
                process.send_signal(signal.SIGINT); process.wait(timeout=60)
                assert process.returncode == 0, f'daemon exit {process.returncode}'
                process = None

            def status():
                path = state/'fleet-data'/'status.json'
                return json.loads(path.read_text()) if path.exists() else None

            def log_line(text):
                with (state/'supervised'/'mqtt'/'component.log').open('a') as out:
                    out.write(text + '\n')

            damaged = root/'damaged'
            cases = []
            tampered = damaged/'tampered'; shutil.copytree(codec_dir, tampered)
            bundle = bytearray((tampered/'walrus-device-codec.cjs').read_bytes()); bundle[-2] ^= 1
            (tampered/'walrus-device-codec.cjs').write_bytes(bytes(bundle))
            cases.append(('tampered_codec', launcher('tampered_codec'), tampered/'walrus-device-codec.cjs', False, 'archive codec checksum mismatch'))
            cases.append(('missing_codec', launcher('missing_codec'), damaged/'absent'/'walrus-device-codec.cjs', False, 'No such file'))
            cases.append(('missing_node', root/'absent-node', codec_dir/'walrus-device-codec.cjs', False, 'No such file'))
            removed = minimal_sysroot(rootfs, damaged/'sysroot')
            cases.append(('missing_libstdcxx', launcher('missing_libstdcxx', damaged/'sysroot'), codec_dir/'walrus-device-codec.cjs', True, 'archive codec unavailable'))
            for name in ['missing_wasm', 'corrupt_wasm']:
                copy = damaged/name; shutil.copytree(codec_dir, copy)
                wasm = copy/'walrus_wasm_bg.wasm'
                if name == 'missing_wasm':
                    wasm.unlink()
                else:
                    wasm.write_bytes(wasm.read_bytes()[:4096])
                cases.append((name, launcher(name), copy/'walrus-device-codec.cjs', True, 'archive codec unavailable'))

            with (root/'daemon.log').open('w') as daemon_log, failure_context(root/'daemon.log'):
                for name, node_path, codec_path, executed, reason in cases:
                    configure(node_path, codec_path)
                    log_line(f'{name}: login fixture-confidential-value succeeded')
                    since = int(time.time()*1000)
                    process = start()
                    wait(lambda: (s := status()) is not None and s['at_ms'] >= since and s['blocked'], args.timeout, f'{name} blocked', process)
                    stop()
                    made = calls(name)
                    assert not (state/'fleet-data'/'pending.json').exists() and not sim.registered and not sim.anchors, f'{name} advanced the archive'
                    assert bool(made) == executed, f'{name}: codec executions {made}'
                    assert all(c['exit'] != 0 for c in made), f'{name}: codec succeeded {made}'
                    tail = (root/'daemon.log').read_text()[-20000:]
                    assert reason in tail, f'{name}: expected {reason!r} in the daemon log'
                    result['negative'][name] = dict(blocked=True, codec_executed=executed, reason=reason,
                                                    codec_exit=[c['exit'] for c in made], codec_stderr=[c['stderr'] for c in made])
                result['negative']['missing_libstdcxx']['removed'] = removed

                configure(launcher('packaged'), codec_dir/'walrus-device-codec.cjs')
                log_line('packaged: login fixture-confidential-value succeeded')
                process = start()
                wait(lambda: len(sim.anchors) == 1 and not (state/'fleet-data'/'pending.json').exists(), args.timeout, 'packaged archive anchored', process)
                stop()
            [prepare] = [c for c in calls('packaged') if c['operation'] == 'prepare']
            assert prepare['exit'] == 0, prepare
            encoded = json.loads(prepare['target'])
            uploaded = uploads/(sim.anchors[0]['blob_object_id']+'.bin')
            assert prepare['ciphertext_sha256'] == sha256(uploaded) == bytes(sim.anchors[0]['ciphertext_sha256']).hex(), \
                'the codec must encode exactly the anchored ciphertext'
            if args.host_node:
                assert prepare['host_exit'] == 0 and json.loads(prepare['host']) == encoded, 'target and host encodings differ'
            fixture = root/'fixture.json'
            fixture.write_text(json.dumps({'manifest': manifest, 'config': dict(schema=1, chainIdentifier=sim.chain, registryId=object_id(ota.REGISTRY),
                                           packageId=object_id(fleet.PACKAGE), typeOrigin=object_id(fleet.PACKAGE), fleetId=object_id(fleet.FLEET),
                                           budgetId=object_id(budget.BUDGET), storageId=object_id(fleet.STORAGE)),
                                           'anchors': sim.anchors, 'ciphertexts': {a['blob_object_id']: base64.b64encode((uploads/(a['blob_object_id']+'.bin')).read_bytes()).decode() for a in sim.anchors},
                                           'device': object_id(ota.DEVICE), 'signer': ota.ADDRESS, 'fleet': sim.fleet(), 'writer': sim.writer(),
                                           'now': max(sim.clock, int(time.time()*1000))}))
            verified = subprocess.run([args.verifier_node, str(ROOT/'scripts/verify_native_fleet.cjs'), str(fixture)], capture_output=True, text=True, timeout=300)
            assert verified.returncode == 0, verified.stderr[-2000:]
            assert not sim.errors, sim.errors
            ciphertext = uploaded.read_bytes()
            result['round_trip'] = dict(ciphertext_bytes=len(ciphertext), ciphertext_sha256=sha256(uploaded), seal_threshold='2 of 3 fixture key servers',
                                        prepare=dict(rpc_url=args.rpc_url, target=encoded, host=json.loads(prepare['host']) if args.host_node else None,
                                                     host_node=subprocess.run([str(args.host_node), '--version'], capture_output=True, text=True).stdout.strip() if args.host_node else None),
                                        anchors=len(sim.anchors), owner_verification=verified.stdout.strip().splitlines())
    finally:
        if process:
            process.terminate(); process.wait(timeout=60)
        shim.terminate(); shim.wait(timeout=10); server.shutdown(); server.server_close()
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--qemu', type=Path, required=True, help='qemu-aarch64 user-mode emulator')
    parser.add_argument('--rootfs', type=Path, required=True)
    parser.add_argument('--shim', type=Path, required=True, help='host grpc_sim_shim example binary')
    parser.add_argument('--rpc-url', required=True, help='Testnet gRPC fullnode for the read-only prepare')
    parser.add_argument('--host-node', type=Path, help='host Node of the same release, for target/host encoding equality')
    parser.add_argument('--verifier-node', default='node', help='Node for verify_native_fleet.cjs (needs dapp/node_modules)')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args(argv))
    return 0


if __name__ == '__main__':
    sys.exit(main())
