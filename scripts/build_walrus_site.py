#!/usr/bin/env python3
"""Build a deployment-bound static Walrus Site; never submits transactions.

Run in the pinned dev image, after pnpm install --frozen-lockfile. A receipt
outside the export records every resource digest. Compare two receipts' files
for reproducibility. Publication is a separate authorized operation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from deployment_manifest import validate, object_id

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / 'tools/walrus-sites/toolchain.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def verified_builder(path):
    pin = json.loads(PIN.read_text())
    path = Path(path).resolve(strict=True)
    if digest(path.read_bytes()) != pin['binary_sha256']:
        raise ValueError('site-builder checksum differs from the reviewed pin')
    actual = subprocess.check_output([str(path), '--version'], text=True, timeout=30).strip()
    if actual != pin['version_output']:
        raise ValueError('site-builder version differs from the reviewed pin')
    return pin


def inventory(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('a static export must contain no symbolic links')
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            if relative == 'ws-resources.json':
                continue  # site-builder configuration is not a served resource
            if any(part.startswith('.') for part in path.relative_to(directory).parts):
                raise ValueError('a static export must contain no hidden files')
            if path.suffix in ('.map', '.toml', '.key', '.pem'):
                raise ValueError('source maps and private runtime/configuration files are not site resources')
            content = path.read_bytes()
            result['/' + relative] = {'sha256': digest(content), 'bytes': len(content)}
    if '/index.html' not in result:
        raise ValueError('the export has no index.html')
    return result


def fleet_binding(raw, manifest):
    value = json.loads(raw)
    fields = {"schema", "chainIdentifier", "registryId", "packageId", "typeOrigin", "fleetId", "budgetId", "storageId"}
    if not isinstance(value, dict) or set(value) != fields or type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("invalid fleet-data configuration")
    if value["chainIdentifier"] != manifest["chain_identifier"]:
        raise ValueError("fleet data is on another chain")
    for key in fields - {"schema", "chainIdentifier"}:
        value[key] = object_id(value[key])
    if value["registryId"] != object_id(manifest["shared_objects"]["registry"]["id"]):
        raise ValueError("fleet data is bound to another registry")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--builder', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--fleet-data', type=Path, help='public fleet-data object/type binding for this deployment')
    args = parser.parse_args()
    pin = verified_builder(args.builder)
    manifest = json.loads(args.manifest.read_text())
    validate(manifest)
    encoded = canonical(manifest)
    raw_data = args.fleet_data.read_text() if args.fleet_data else os.environ.get('NEXT_PUBLIC_FLEET_DATA')
    data = fleet_binding(raw_data, manifest) if raw_data else None
    output = ROOT / 'dapp/out'
    if args.receipt.resolve().is_relative_to(output.resolve()):
        raise ValueError('the evidence receipt must be outside the published export')
    environment = dict(os.environ, NEXT_PUBLIC_DEPLOYMENT_MANIFEST=encoded.decode().strip(), NEXT_TELEMETRY_DISABLED='1')
    if data:
        environment['NEXT_PUBLIC_FLEET_DATA'] = canonical(data).decode().strip()
    subprocess.run(['pnpm', 'build'], cwd=ROOT / 'dapp', env=environment, check=True)
    (output / 'deployment-manifest.json').write_bytes(encoded)
    if data:
        (output / 'fleet-data-config.json').write_bytes(canonical(data))
    routes = {'/': '/index.html'}
    for path in sorted(output.rglob('index.html')):
        relative = '/' + path.relative_to(output).as_posix()
        prefix = relative.removesuffix('/index.html')
        if prefix:
            routes[prefix] = relative
            routes[prefix + '/'] = relative
    # Do not use a catch-all rewrite: Next's route payloads and JS/WASM are
    # real resources and must never be replaced with HTML.
    resources = {'site_name': 'SuiFleet', 'routes': routes,
                 'headers': {'/deployment-manifest.json': {'Content-Type': 'application/json', 'Cache-Control': 'no-cache'}}}
    (output / 'ws-resources.json').write_bytes(canonical(resources))
    receipt = {'schema': 1, 'builder': pin, 'deployment_manifest_sha256': digest(encoded),
               'ws_resources_sha256': digest(canonical(resources)),
               'chain_identifier': manifest['chain_identifier'], 'call_package_id': manifest['call_package_id'],
               'files': inventory(output), 'published': False}
    if data:
        receipt['fleet_data_sha256'] = digest(canonical(data))
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_bytes(canonical(receipt))
    print(f'Static site prepared: {len(receipt["files"])} resources; no transaction submitted')


if __name__ == '__main__':
    main()
