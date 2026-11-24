#!/usr/bin/env python3
"""Build the immutable daemon source distribution fetched by the meta-suifleet recipe.

The archive is produced from committed Git objects only (never a working tree):

    git archive --format=tar REVISION -- <paths> | gzip -n -6

Paths are the daemon build closure: the root LICENSE, the Cargo manifest and
lockfile, the crate sources and examples, every Cargo workspace member and
each file named by a literal ``include_str!``/``include_bytes!`` in those
sources, or by a `#[path]` module attribute (resolved transitively). The closure is computed from the revision's
tree, so a missing workspace member or include target is an error instead of a
broken release. Ownership, timestamps (the commit time) and modes come from
``git archive``; ``gzip -n`` omits the name and time, so rebuilding the same
revision with the same git/gzip produces byte-identical output.

Runtime identities, keys, enrollment configuration and journals must never be
in the archive; files whose names look like runtime state are refused. Content
is not pattern-scanned here because the redaction test vectors deliberately
contain fake private keys: build only from reviewed main, which the repository
secret-scanning gates already cover (docs/SECURITY_CI.md).
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

CRATE = 'suifleet-edge'
ROOTS = (
    'LICENSE',
    f'{CRATE}/Cargo.toml',
    f'{CRATE}/Cargo.lock',
    f'{CRATE}/config.example.toml',
    f'{CRATE}/build.rs',
    f'{CRATE}/src/',
    f'{CRATE}/examples/',
)
REQUIRED = ('LICENSE', f'{CRATE}/Cargo.toml', f'{CRATE}/Cargo.lock')
INCLUDE = re.compile(rb'include_(?:str|bytes)!\(\s*"([^"\\]+)"\s*\)|#\[path\s*=\s*"([^"\\]+)"\s*\]')
# Runtime identity/state names that must never be distributed.
FORBIDDEN_NAME = re.compile(
    r'(^|/)(device\.key|signer\.key|config\.toml|firmware\.toml|signer\.toml|'
    r'enrollment(\.[a-z]+)?|\.env|\.netrc|id_(rsa|ecdsa|ed25519)|[^/]*\.(pem|p12|pfx|key)|'
    r'[^/]*journal[^/]*|[^/]*\.sqlite3?|[^/]*\.db)$')
GZIP_LEVEL = '-6'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def tree(repo, revision):
    """Map every path in the commit to (mode, type, object)."""
    entries = {}
    for record in git(repo, 'ls-tree', '-r', '-z', '--full-tree', revision).split(b'\0'):
        if not record:
            continue
        meta, path = record.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        entries[path.decode()] = (mode, kind, oid)
    return entries


def workspace_members(manifest):
    """Return `[workspace] members` from a Cargo manifest (literal paths only)."""
    section = re.search(r'^\[workspace\]\s*$(.*?)(?=^\[|\Z)', manifest, re.M | re.S)
    if not section:
        return []
    match = re.search(r'^members\s*=\s*\[(.*?)\]', section.group(1), re.M | re.S)
    if not match:
        return []
    members = re.findall(r'"([^"]+)"', match.group(1))
    for member in members:
        if any(c in member for c in '*?[') or '..' in PurePosixPath(member).parts or member.startswith('/'):
            raise ValueError(f'unsupported workspace member pattern: {member}')
    return members


def include_targets(source_path, data):
    base = PurePosixPath(source_path).parent
    targets = []
    for include, module in INCLUDE.findall(data):
        raw = include or module
        parts = []
        for part in (base / raw.decode()).parts:
            if part == '..':
                if not parts:
                    raise ValueError(f'{source_path} includes a file outside the repository: {raw.decode()}')
                parts.pop()
            elif part != '.':
                parts.append(part)
        targets.append('/'.join(parts))
    return targets


def closure(files, read):
    """Select the build closure from `files` ({path: (mode, kind, oid)})."""
    selected = set()

    def add_root(root):
        if root.endswith('/'):
            selected.update(p for p in files if p.startswith(root))
        elif root in files:
            selected.add(root)

    for root in ROOTS:
        add_root(root)
    for name in REQUIRED:
        if name not in files:
            raise ValueError(f'source revision lacks required {name}')
    manifest = read(f'{CRATE}/Cargo.toml').decode()
    for member in workspace_members(manifest):
        member_manifest = f'{CRATE}/{member}/Cargo.toml'
        if member_manifest not in files:
            raise ValueError(f'workspace member {member} has no Cargo.toml at this revision')
        add_root(f'{CRATE}/{member}/')
    pending = sorted(p for p in selected if p.endswith('.rs'))
    while pending:
        path = pending.pop()
        for target in include_targets(path, read(path)):
            if target not in files:
                raise ValueError(f'{path} includes missing file {target}')
            if target not in selected:
                selected.add(target)
                if target.endswith('.rs'):
                    pending.append(target)
    return audit(selected, files, read)


def audit(paths, files, read):
    """Refuse absent or non-regular files and runtime-state names."""
    for path in sorted(paths):
        if path not in files:
            raise ValueError(f'path absent from revision: {path}')
        mode, kind, _ = files[path]
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError(f'source distribution requires regular tracked files: {path} ({mode} {kind})')
        if FORBIDDEN_NAME.search(path):
            raise ValueError(f'refusing runtime identity/state-like file: {path}')
    return sorted(paths)


def crate_version(manifest):
    match = re.search(r'^\[package\][^\[]*?^version\s*=\s*"([^"]+)"', manifest, re.M | re.S)
    if not match:
        raise ValueError('Cargo.toml has no [package] version')
    return match.group(1)


def plan(repo, revision, paths=None):
    revision = git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    files = tree(repo, revision)
    cache = {}

    def read(path):
        if path not in cache:
            cache[path] = git(repo, 'cat-file', 'blob', files[path][2])
        return cache[path]

    if paths is None:
        paths = closure(files, read)
    else:
        # Reproduce a previously published file list; it gets the same audit.
        paths = audit(set(paths), files, read)
    license_md5 = hashlib.md5(read('LICENSE')).hexdigest()
    return {
        'source_commit': revision,
        'paths': paths,
        'crate_version': crate_version(read(f'{CRATE}/Cargo.toml').decode()),
        'lock_sha256': hashlib.sha256(read(f'{CRATE}/Cargo.lock')).hexdigest(),
        'license_md5': license_md5,
    }


def build(repo, revision, output, paths=None):
    """Write the archive with exclusive creation; return its provenance."""
    info = plan(repo, revision, paths)
    archive = subprocess.Popen(['git', '-C', str(repo), 'archive', '--format=tar', info['source_commit'], '--', *info['paths']],
                               stdout=subprocess.PIPE)
    compressed = subprocess.run(['gzip', '-n', GZIP_LEVEL], stdin=archive.stdout, stdout=subprocess.PIPE, check=True).stdout
    archive.stdout.close()
    if archive.wait() != 0:
        raise RuntimeError('git archive failed')
    with Path(output).open('xb') as target:
        target.write(compressed)
    info.update(sha256=hashlib.sha256(compressed).hexdigest(), files=len(info['paths']),
                filename=Path(output).name,
                tools={'git': git(repo, '--version').decode().strip(),
                       'gzip': subprocess.check_output(['gzip', '--version']).decode().splitlines()[0]})
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--revision', required=True, help='reviewed, committed source revision')
    parser.add_argument('--output', type=Path, help='new archive path, conventionally suifleet-edge-<commit>.tar.gz')
    parser.add_argument('--list', action='store_true', help='print the selected paths without writing an archive')
    parser.add_argument('--paths-from', type=Path, help='reproduce an existing archive from its newline-separated file list')
    args = parser.parse_args()
    if args.list:
        print('\n'.join(plan(args.repo, args.revision)['paths']))
        return
    if not args.output:
        parser.error('--output is required unless --list is given')
    paths = args.paths_from.read_text().split() if args.paths_from else None
    result = build(args.repo, args.revision, args.output, paths)
    result.pop('paths')
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
