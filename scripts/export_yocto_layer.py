#!/usr/bin/env python3
"""Export only reviewed, explicitly listed Git blobs; never walk a working tree."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

PREFIX = 'suifleet-edge/meta-suifleet/'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def export(repo, revision, output):
    revision = git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    paths = json.loads(git(repo, 'show', f'{revision}:{PREFIX}export-files.json'))
    if not isinstance(paths, list) or len(paths) != len(set(paths)):
        raise ValueError('export file list must contain unique paths')
    members = {}
    for name in sorted(paths):
        path = PurePosixPath(name)
        if name == 'export-provenance.json' or path.is_absolute() or '..' in path.parts or str(path) != name:
            raise ValueError('unsafe export path')
        entry = git(repo, 'ls-tree', revision, '--', PREFIX + name).decode().split()
        if len(entry) < 3 or entry[0] not in ('100644', '100755') or entry[1] != 'blob':
            raise ValueError(f'export requires a regular tracked file: {name}')
        members[name] = (git(repo, 'cat-file', 'blob', entry[2]), int(entry[0], 8) & 0o777)
    manifest = {'schema_version': 1, 'source_commit': revision,
                'files': {n: hashlib.sha256(v[0]).hexdigest() for n, v in members.items()}}
    members['export-provenance.json'] = ((json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode(), 0o644)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, (data, mode) in sorted(members.items()):
            info = tarfile.TarInfo('meta-suifleet/' + name)
            info.size, info.mode, info.mtime = len(data), mode, 0
            archive.addfile(info, io.BytesIO(data))
    data = gzip.compress(raw.getvalue(), mtime=0)
    # Exclusive creation prevents accidental replacement of a release artifact.
    with Path(output).open('xb') as target:
        target.write(data)
    return {'source_commit': revision, 'sha256': hashlib.sha256(data).hexdigest(), 'files': len(members)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--revision', required=True, help='reviewed committed layer revision')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.repo, args.revision, args.output), sort_keys=True))
