#!/usr/bin/env python3
"""Restore a verified dApp configuration on a clean operator machine (SF-091).

Offline and stdlib only; it never reads keys, signs or submits transactions.

restore   Verify the public deployment manifest, fleet-data, Seal and storage
          settings against the retained site build receipt and Site publication
          receipt, optionally verify a retained static export byte-for-byte,
          and write the public NEXT_PUBLIC_* environment for a rebuild/run.
rollback  Check that a retained earlier build receipt was actually published
          to the same Site and binds the same deployment, before an authorized
          `live_walrus_site.py --operation rollback`.

Inputs must be public. Private keys, wallet configuration, runtime journals
and provider credentials are refused, never copied into the restored state.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_walrus_site import canonical, digest, fleet_binding, inventory, seal_binding, storage_binding
from deployment_manifest import validate

SECRET_MARKERS = ('suiprivkey', 'PRIVATE KEY', 'privateKey', 'mnemonic', 'keystore', 'api_key', 'secret')
CONFIG_FILES = {'fleet-data-config.json': 'fleet_data', 'seal-settings.json': 'seal', 'fleet-storage-config.json': 'storage'}


class RecoveryError(ValueError):
    pass


def public(path):
    raw = Path(path).read_bytes()
    text = raw.decode('utf-8')
    for marker in SECRET_MARKERS:
        if marker.lower() in text.lower():
            raise RecoveryError(f'{path}: refusing input that looks private ({marker}); restore only public records')
    return text


def files_digest(files):
    return digest(canonical(files))


def releases(publication, site_id=None):
    """Published phases in order, each with its verified resource-map digest."""
    phases = publication.get('phases')
    if not isinstance(phases, list) or not phases:
        raise RecoveryError('publication receipt has no published phases')
    result = []
    for phase in phases:
        if site_id is not None and phase.get('siteId') != site_id:
            raise RecoveryError('publication phases name different Sites')
        if not phase.get('fullWalrusReadback') or not phase.get('digest') or not isinstance(phase.get('files'), dict):
            raise RecoveryError(f"phase {phase.get('phase')} lacks verified readback evidence")
        result.append({'phase': phase['phase'], 'transaction': phase['digest'], 'blob_id': phase.get('blobId'),
                       'resources': len(phase['files']), 'files_sha256': files_digest(phase['files'])})
    return result


def current_release(publication, receipt_text, receipt):
    """The receipt the publication names, served by its latest verified phase."""
    if publication.get('build_receipt_sha256') != digest(receipt_text.encode()):
        raise RecoveryError('publication receipt does not name this build receipt')
    published = releases(publication, publication.get('site_id'))
    if published[-1]['files_sha256'] != files_digest(receipt['files']):
        raise RecoveryError('the latest verified Site phase does not serve this build; select the published receipt')
    return published


# Every binding a frontend build pins; a change in any of them (including one
# appearing or disappearing) is a deployment change, not a frontend rollback.
BINDINGS = ('chain_identifier', 'call_package_id', 'deployment_manifest_sha256', 'fleet_data_sha256',
            'seal_settings_sha256', 'fleet_storage_sha256')


def restore(args):
    manifest = json.loads(public(args.manifest))
    validate(manifest)
    receipt_text = public(args.build_receipt)
    receipt = json.loads(receipt_text)
    publication = json.loads(public(args.publication_receipt))
    bindings = {'deployment_manifest': digest(canonical(manifest))}
    environment = {'NEXT_PUBLIC_DEPLOYMENT_MANIFEST': canonical(manifest).decode().strip()}
    configs = {}
    if args.fleet_data:
        configs['fleet_data'] = fleet_binding(public(args.fleet_data), manifest)
        environment['NEXT_PUBLIC_FLEET_DATA'] = canonical(configs['fleet_data']).decode().strip()
    if args.seal_settings:
        configs['seal'] = seal_binding(public(args.seal_settings))
        environment['NEXT_PUBLIC_SEAL_KEY_SERVERS'] = json.dumps(configs['seal']['keyServers'], separators=(',', ':'))
        environment['NEXT_PUBLIC_SEAL_THRESHOLD'] = str(configs['seal']['threshold'])
    if args.fleet_storage:
        configs['storage'] = storage_binding(public(args.fleet_storage), manifest)
        environment['NEXT_PUBLIC_FLEET_STORAGE_PACKAGE'] = configs['storage']['packageId']
        environment['NEXT_PUBLIC_FLEET_STORAGE_ORIGIN'] = configs['storage']['storageOrigin']
        environment['NEXT_PUBLIC_FLEET_RETENTION_ORIGIN'] = configs['storage']['retentionOrigin']
    for name, value in configs.items():
        bindings[name] = digest(canonical(value))
    expected = {'deployment_manifest': receipt.get('deployment_manifest_sha256'), 'fleet_data': receipt.get('fleet_data_sha256'),
                'seal': receipt.get('seal_settings_sha256'), 'storage': receipt.get('fleet_storage_sha256')}
    for name, value in expected.items():
        if value is not None and name not in bindings:
            raise RecoveryError(f'the build receipt binds {name}; supply its retained public file')
        if name in bindings and bindings[name] != value:
            raise RecoveryError(f'{name} differs from the retained build receipt')
    if receipt.get('chain_identifier') != manifest['chain_identifier'] or receipt.get('call_package_id') != manifest['call_package_id']:
        raise RecoveryError('build receipt is for another chain or call package')
    if publication.get('chain') not in (None, manifest['chain_identifier']):
        raise RecoveryError('publication receipt is for another chain')
    published = current_release(publication, receipt_text, receipt)
    exported = None
    if args.export:
        export = Path(args.export)
        if inventory(export) != receipt['files']:
            raise RecoveryError('retained static export differs from the build receipt; never publish or serve it')
        if (export / 'deployment-manifest.json').read_bytes() != canonical(manifest):
            raise RecoveryError('exported deployment manifest differs')
        for filename, name in CONFIG_FILES.items():
            if name in configs and (export / filename).read_bytes() != canonical(configs[name]):
                raise RecoveryError(f'exported {filename} differs')
        exported = len(receipt['files'])
    summary = {'schema': 1, 'kind': 'suifleet-restored-configuration', 'network': manifest['network'],
               'chain_identifier': manifest['chain_identifier'], 'call_package_id': manifest['call_package_id'],
               'site_id': publication.get('site_id'), 'bindings': bindings, 'current_release': published[-1],
               'earlier_releases': published[:-1], 'verified_export_resources': exported,
               'fleet_id': configs.get('fleet_data', {}).get('fleetId')}
    if args.env_out:
        out = Path(args.env_out)
        if out.exists():
            raise RecoveryError(f'{out} exists; restore into a new file')
        out.write_text(''.join(f'{key}={json.dumps(value)}\n' for key, value in environment.items()))
    return summary


def rollback(args):
    retained = json.loads(public(args.retained_build_receipt))
    current_text = public(args.build_receipt)
    current = json.loads(current_text)
    publication = json.loads(public(args.publication_receipt))
    if publication.get('chain') not in (None, current.get('chain_identifier')):
        raise RecoveryError('publication receipt is for another chain')
    published = current_release(publication, current_text, current)
    target = files_digest(retained['files'])
    if target == files_digest(current['files']):
        raise RecoveryError('the retained receipt is the current release; nothing to roll back')
    matches = [release for release in published[:-1] if release['files_sha256'] == target]
    if not matches:
        raise RecoveryError('the retained receipt was never verified as published to this Site; do not roll back to it')
    for field in BINDINGS:
        if retained.get(field) != current.get(field):
            raise RecoveryError(f'rollback would change {field}; a deployment change is not a frontend rollback')
    return {'schema': 1, 'kind': 'suifleet-frontend-rollback-plan', 'site_id': publication.get('site_id'),
            'rollback_to': matches[-1], 'current': published[-1],
            'next': 'Run scripts/live_walrus_site.py --operation rollback with the retained export and receipt under the same '
                    'authorization and preflight; verify every resource and route before changing any domain.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    r = commands.add_parser('restore')
    r.add_argument('--manifest', required=True)
    r.add_argument('--build-receipt', required=True)
    r.add_argument('--publication-receipt', required=True)
    r.add_argument('--fleet-data')
    r.add_argument('--seal-settings')
    r.add_argument('--fleet-storage')
    r.add_argument('--export', help='retained static export directory to verify byte-for-byte')
    r.add_argument('--env-out', help='new file for the public NEXT_PUBLIC_* environment')
    b = commands.add_parser('rollback')
    b.add_argument('--build-receipt', required=True, help='current (bad) release build receipt')
    b.add_argument('--retained-build-receipt', required=True, help='retained earlier release build receipt')
    b.add_argument('--publication-receipt', required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps((restore if args.command == 'restore' else rollback)(args), indent=2))
    except (RecoveryError, ValueError, KeyError, OSError) as error:
        print(f'refused: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
