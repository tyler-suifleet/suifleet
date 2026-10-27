#!/usr/bin/env python3
"""Seal/verify prebuilt static releases; no rebuilds and no network writes.

The SHA256 of release.json is the approval input. Every file, including host
configuration, is bound. An expected digest must come from the release issue,
not from the bundle being verified. Do not put private data in static exports.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory(root):
    files = {}
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if path.is_symlink():
            raise ValueError('symlinks are forbidden')
        if any(p.startswith('.') for p in rel.parts):
            raise ValueError('hidden paths are forbidden')
        if path.is_file():
            if path.suffix in {'.key', '.pem', '.toml', '.map'} or path.name in {'_worker.js', '_routes.json', 'ws-resources.json'} or 'functions' in rel.parts:
                raise ValueError(f'not a static public resource: {rel}')
            if path.stat().st_size > 25 * 1024 * 1024:
                raise ValueError(f'Pages 25 MiB file limit exceeded: {rel}')
            files[rel.as_posix()] = digest(path.read_bytes())
    if not {'index.html', '_headers', 'robots.txt', 'sitemap.xml'} <= files.keys():
        raise ValueError('missing static entry, security headers or discovery files')
    if len(files) > 20000:
        raise ValueError('Pages Free file limit exceeded')
    headers = (root / '_headers').read_text()
    for required in ["/*", "Content-Security-Policy:", "frame-ancestors 'none'", 'Strict-Transport-Security: max-age=31536000', 'Referrer-Policy: strict-origin-when-cross-origin', 'X-Content-Type-Options: nosniff']:
        if required not in headers:
            raise ValueError(f'missing header policy: {required}')
    return files


def seal(source, out, revision, hostname):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('source revision must be a full Git SHA')
    if hostname not in {'suifleet.io', 'app.suifleet.io', 'docs.suifleet.io'}:
        raise ValueError('unrecognized release hostname')
    files = inventory(source)
    # Never silently replace an older retained release or copy into the input.
    if out.resolve().is_relative_to(source.resolve()):
        raise ValueError('output must be outside the input')
    if out.exists():
        raise ValueError('release output already exists')
    if hostname == 'app.suifleet.io' and 'deployment-manifest.json' not in files:
        raise ValueError('console release requires its deployment manifest')
    shutil.copytree(source, out / 'public')
    record = {'schema': 1, 'revision': revision, 'hostname': hostname, 'files': files}
    data = (json.dumps(record, sort_keys=True, indent=2) + '\n').encode()
    (out / 'release.json').write_bytes(data)
    return digest(data)


def verify(bundle, expected):
    data = (bundle / 'release.json').read_bytes()
    if not re.fullmatch('[0-9a-f]{64}', expected) or digest(data) != expected:
        raise ValueError('release approval digest mismatch')
    record = json.loads(data)
    if set(record) != {'schema', 'revision', 'hostname', 'files'} or record['schema'] != 1:
        raise ValueError('unsupported release record')
    if set(p.name for p in bundle.iterdir()) != {'public', 'release.json'}:
        raise ValueError('unrecorded bundle entries')
    if (bundle / 'public').is_symlink() or (bundle / 'release.json').is_symlink():
        raise ValueError('symlinks are forbidden')
    if inventory(bundle / 'public') != record['files']:
        raise ValueError('release files differ (missing, extra or modified bytes)')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    pack = commands.add_parser('seal')
    pack.add_argument('source', type=Path)
    pack.add_argument('out', type=Path)
    pack.add_argument('--revision', required=True)
    pack.add_argument('--hostname', required=True)
    check = commands.add_parser('verify')
    check.add_argument('bundle', type=Path)
    check.add_argument('--sha256', required=True)
    args = parser.parse_args()
    if args.command == 'seal':
        print(seal(args.source, args.out, args.revision, args.hostname))
    else:
        print(json.dumps(verify(args.bundle, args.sha256), sort_keys=True))


if __name__ == '__main__':
    main()
