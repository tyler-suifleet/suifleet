#!/usr/bin/env python3
"""Exercise the image's packaged Node runtime and Walrus codec in user-mode QEMU.

Runs the AArch64 ``node`` from the built rootfs against the rootfs codec. The
offline checks need no network: the exact Node release, manifest/provenance
binding, categorical refusal of malformed and foreign-chain requests, and
failure when the WASM module is missing or corrupt. With ``--rpc-url`` it also
encodes a deterministic ciphertext (``prepare``: read-only Testnet chain and
Walrus system reads, no transaction) and, with ``--host-node``, requires the
target's blob ID and root hash to equal the host's. This is not a Pi boot,
systemd service or storage-node write.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

NODE = 'usr/lib/suifleet/node/bin/node'
CODEC = 'usr/lib/suifleet/walrus-codec'
FAILED = 'Walrus codec operation failed'
TESTNET_CHAIN = '4c78adac'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_artifact(codec_dir, expected_node):
    """The installed manifest/provenance describe exactly the installed files."""
    manifest = json.loads((codec_dir/'manifest.json').read_text())
    provenance = json.loads((codec_dir/'provenance.json').read_text())
    if manifest.get('schema') != 1 or manifest.get('node') != expected_node:
        raise ValueError(f'codec manifest targets Node {manifest.get("node")}, packaged {expected_node}')
    for name, entry in manifest['files'].items():
        if sha256(codec_dir/name) != entry['sha256'] or (codec_dir/name).stat().st_size != entry['bytes']:
            raise ValueError('installed codec file differs from its manifest: ' + name)
    if provenance.get('manifest_sha256') != sha256(codec_dir/'manifest.json'):
        raise ValueError('codec provenance does not bind the installed manifest')
    return manifest, provenance


def ciphertext(size):
    """Deterministic, incompressible stand-in for an archive ciphertext."""
    out, block = bytearray(), b'sf120-codec-vector'
    while len(out) < size:
        block = hashlib.sha256(block).digest()
        out += block
    return bytes(out[:size])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--qemu', type=Path, required=True, help='qemu-aarch64 user-mode emulator')
    parser.add_argument('--rootfs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rpc-url', help='Testnet gRPC fullnode for the read-only prepare check')
    parser.add_argument('--system-id', default='0x6c2547cbbc38025cf3adac45f63cb0a8d12ecf777cdc75a4971612bf97fdf6af')
    parser.add_argument('--walrus-package', default='0x7d3b2086808a62bbc44d18c6b1d407973d77dd26946735873092c2f5650b4611')
    parser.add_argument('--host-node', help='host Node of the same release, for target/host encoding equality')
    parser.add_argument('--size', type=int, default=1024*1024)
    args = parser.parse_args(argv)
    rootfs = args.rootfs.resolve()
    node, codec_dir = rootfs/NODE, rootfs/CODEC
    with node.open('rb') as stream:
        elf = stream.read(20)
    assert elf[:4] == b'\x7fELF' and elf[4:6] == b'\x02\x01' and int.from_bytes(elf[18:20], 'little') == 183, 'node must be AArch64'

    def target(*arguments, stdin='', timeout=300):
        return subprocess.run([str(args.qemu), '-L', str(rootfs), str(node), *map(str, arguments)],
                              input=stdin, capture_output=True, text=True, timeout=timeout, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    version = target('--version').stdout.strip()
    manifest, provenance = check_artifact(codec_dir, version.lstrip('v'))
    result = dict(node=dict(path='/'+NODE, version=version, sha256=sha256(node)), codec_manifest=manifest,
                  codec_source_revision=provenance['source_revision'], checks={})

    def refused(name, process):
        ok = process.returncode == 1 and not process.stdout and process.stderr.strip() in (FAILED, 'Walrus codec initialization failed')
        assert ok, f'{name}: expected categorical refusal, got {process.returncode} {process.stdout!r} {process.stderr[-300:]!r}'
        result['checks'][name] = dict(exit=process.returncode, stderr=process.stderr.strip())

    with tempfile.TemporaryDirectory(prefix='sf120-codec-') as directory:
        work = Path(directory)
        data = work/'ciphertext.bin'
        data.write_bytes(ciphertext(args.size))
        codec = codec_dir/'walrus-device-codec.cjs'
        refused('malformed_request', target(codec, stdin='not json\n'))
        foreign = dict(operation='prepare', chain='00000000', rpc_url='http://127.0.0.1:9', ciphertext_path=str(data))
        refused('foreign_chain', target(codec, stdin=json.dumps(foreign)+'\n'))
        for case in ['missing_wasm', 'corrupt_wasm']:
            copy = work/case
            shutil.copytree(codec_dir, copy)
            wasm = copy/'walrus_wasm_bg.wasm'
            if case == 'missing_wasm':
                wasm.unlink()
            else:
                wasm.write_bytes(wasm.read_bytes()[:4096])
            process = target(copy/'walrus-device-codec.cjs', stdin=json.dumps(foreign)+'\n')
            assert process.returncode != 0 and not process.stdout, f'{case}: codec ran without its WASM module'
            result['checks'][case] = dict(exit=process.returncode)
        if args.rpc_url:
            request = json.dumps(dict(operation='prepare', chain=TESTNET_CHAIN, rpc_url=args.rpc_url, ciphertext_path=str(data),
                                      system_id=args.system_id, walrus_package=args.walrus_package)) + '\n'
            encoded = target(codec, stdin=request, timeout=900)
            assert encoded.returncode == 0, f'target prepare failed: {encoded.stderr[-300:]!r}'
            prepared = json.loads(encoded.stdout)
            result['prepare'] = dict(rpc_url=args.rpc_url, size=args.size, ciphertext_sha256=sha256(data), target=prepared)
            if args.host_node:
                host = subprocess.run([args.host_node, str(codec)], input=request, capture_output=True, text=True, timeout=300)
                assert host.returncode == 0, f'host prepare failed: {host.stderr[-300:]!r}'
                host_version = subprocess.run([args.host_node, '--version'], capture_output=True, text=True).stdout.strip()
                assert host_version == version, f'host Node {host_version} differs from target {version}'
                assert json.loads(host.stdout) == prepared, 'target and host encodings differ'
                result['prepare'].update(host=json.loads(host.stdout), host_node=host_version, target_matches_host=True)
    result.update(user_mode_qemu=True, physical_boot_tested=False, storage_node_write_tested=False)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
