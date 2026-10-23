#!/usr/bin/env python3
"""Resolve the licenses of everything compiled into @mysten/walrus-wasm (SF-120).

The published npm package has no ``license`` field, but its own files carry the
publisher's ``SPDX-License-Identifier`` headers, and it ships the ``Cargo.lock``
its WASM module was built from. This resolves, from those sources only:

* the package license, from the publisher's SPDX headers (all must agree);
* the wasm32 normal-dependency closure of its ``Cargo.lock``, with each
  crate's declared SPDX expression and the license/notice texts it ships;
* for a crate that ships no license text, the license files of its upstream
  repository at the exact commit recorded in ``.cargo_vcs_info.json`` (or the
  git checkout itself for git dependencies), else the license notice in its
  source header, else the canonical SPDX text with its declared authors.

The output is a reviewed inventory that ``package_walrus_codec.py`` consumes
offline; it is bound to the npm tarball integrity and ``Cargo.lock`` digest.
This needs network access (``cargo metadata --locked`` and GitHub reads) and is
run once per walrus-wasm version, not at image build time::

    cargo metadata --locked --format-version 1 --filter-platform wasm32-unknown-unknown \\
        --manifest-path PKG/Cargo.toml > metadata.json
    python3 scripts/resolve_walrus_wasm_licenses.py --package PKG --tarball walrus-wasm-0.3.1.tgz \\
        --metadata metadata.json --path-map /w=/tmp/work \\
        --spdx-dir poky/meta/files/common-licenses --output scripts/walrus_wasm_licenses/0.3.1.json
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.request

LICENSE_FILE = re.compile(r'^(licen[cs]e|copying|notice|copyright|unlicense)([-._].*)?$', re.I)
SPDX = re.compile(r'SPDX-License-Identifier:\s*([^\s*]+)')
# crates.io metadata naming a repository that no longer holds the recorded commit.
REPOSITORY_OVERRIDES = {'bitcoin_hashes': 'https://github.com/rust-bitcoin/rust-bitcoin'}
TARGET = 'wasm32-unknown-unknown'


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def package_license(package):
    """The publisher's SPDX declaration, from every header in the published package."""
    found = {}
    for path in sorted(package.rglob('*')):
        if path.is_file() and path.suffix in {'.js', '.mjs', '.cjs', '.ts', '.mts', '.rs'}:
            for spdx in SPDX.findall(path.read_text(errors='replace')):
                found.setdefault(spdx, []).append(str(path.relative_to(package)))
    if len(found) != 1:
        raise ValueError(f'expected one publisher SPDX license in the package, found {sorted(found)}')
    (spdx, files), = found.items()
    return spdx, files


def closure(metadata):
    """Packages reachable from the root through normal (non-dev, non-build) edges."""
    nodes = {n['id']: n for n in metadata['resolve']['nodes']}
    seen, stack = set(), [metadata['resolve']['root']]
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            stack += [d['pkg'] for d in nodes[current]['deps'] if any(k['kind'] is None for k in d['dep_kinds'])]
    return seen


def github(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'suifleet-license-resolver'}), timeout=60) as response:
        return response.read()


def upstream_texts(name, directory):
    """License files from the crate's repository at its recorded commit."""
    vcs = json.loads((directory/'.cargo_vcs_info.json').read_text())
    commit, inner = vcs['git']['sha1'], vcs.get('path_in_vcs', '')
    manifest = (directory/'Cargo.toml').read_text()
    repository = REPOSITORY_OVERRIDES.get(name) or re.search(r'^repository = "([^"]+)"', manifest, re.M).group(1)
    owner_repo = re.match(r'https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$', repository).group(1)
    parts = [p for p in inner.split('/') if p]
    for depth in range(len(parts), -1, -1):
        location = '/'.join(parts[:depth])
        listing = json.loads(github(f'https://api.github.com/repos/{owner_repo}/contents/{location}?ref={commit}'))
        names = sorted(e['name'] for e in listing if e['type'] == 'file' and LICENSE_FILE.match(e['name']))
        if names:
            origin = f'https://github.com/{owner_repo}/blob/{commit}/' + (location + '/' if location else '')
            return [(origin + n, github(f'https://raw.githubusercontent.com/{owner_repo}/{commit}/' + (location + '/' if location else '') + n))
                    for n in names]
    return []


def header_notice(directory):
    """A license notice in the leading comment of the crate root, if any."""
    lines = []
    for line in (directory/'src/lib.rs').read_text().splitlines():
        if not line.startswith('//') or line.startswith('//!'):
            break
        lines.append(line)
    text = '\n'.join(lines)
    return text.encode() + b'\n' if re.search(r'Permission is hereby granted|Licensed under|SPDX-License-Identifier', text) else None


def spdx_ids(expression):
    return sorted(set(re.findall(r'[A-Za-z0-9.+-]+', expression)) - {'OR', 'AND', 'WITH'})


def checkout_texts(directory):
    """License files at the root of a cargo git checkout (git dependencies)."""
    parts = directory.parts
    root = Path(*parts[:parts.index('checkouts') + 3])
    names = sorted(p.name for p in root.iterdir() if p.is_file() and LICENSE_FILE.match(p.name))
    if not names:
        raise ValueError(f'no license file in git checkout {root}')
    return root, [(n, (root/n).read_bytes()) for n in names]


def mapped(directory, path_map):
    for prefix, replacement in path_map:
        if str(directory).startswith(prefix):
            return Path(replacement + str(directory)[len(prefix):])
    return directory


def resolve(package, tarball, metadata, path_map, spdx_dir):
    spdx, spdx_files = package_license(package)
    meta = json.loads((package/'package.json').read_text())
    if meta.get('license'):
        raise ValueError('the package now declares a license; review whether this inventory is still needed')
    packages = {p['id']: p for p in metadata['packages']}
    texts, crates = {}, []

    def keep(data):
        digest = sha256(data)
        texts[digest] = data.decode('utf-8')
        return digest

    root = metadata['resolve']['root']
    for current in sorted(closure(metadata), key=lambda i: (packages[i]['name'], packages[i]['version'], i)):
        p = packages[current]
        directory = mapped(Path(p['manifest_path']).parent, path_map)
        entry = dict(name=p['name'], version=p['version'], source=p['source'], license=p['license'],
                     proc_macro=any('proc-macro' in t['kind'] for t in p['targets']), texts=[])
        if current == root:
            # The npm package's own crate: no Cargo license field, publisher SPDX headers instead.
            entry['license'] = spdx
            entry['license_origin'] = 'publisher SPDX-License-Identifier headers: ' + ', '.join(spdx_files)
        if not entry['license']:
            raise ValueError(f'{p["name"]} {p["version"]} declares no license')
        files = sorted(f for f in directory.iterdir() if f.is_file() and LICENSE_FILE.match(f.name))
        if files:
            entry['texts'] = [dict(file=f.name, sha256=keep(f.read_bytes())) for f in files]
        elif current == root or (p['source'] or '').startswith('git+'):
            origin = p
            if current == root:
                # Same publisher and license: ship the Apache-2.0 text of its walrus-core checkout.
                origin = next(packages[i] for i in closure(metadata) if packages[i]['name'] == 'walrus-core')
            _, found = checkout_texts(mapped(Path(origin['manifest_path']).parent, path_map))
            entry['license_text_origin'] = 'repository root of ' + origin['source']
            entry['texts'] = [dict(file=n, sha256=keep(data)) for n, data in found]
        elif found := upstream_texts(p['name'], directory):
            entry['license_text_origin'] = 'upstream repository at the published commit (.cargo_vcs_info.json)'
            entry['texts'] = [dict(file=url, sha256=keep(data)) for url, data in found]
        elif notice := header_notice(directory):
            entry['license_text_origin'] = 'license notice in the crate source header (crate and repository ship no license file)'
            entry['texts'] = [dict(file='src/lib.rs header', sha256=keep(notice))]
        else:
            # Neither the crate nor its repository ships any text: record the
            # declared authors and the canonical SPDX text of each license.
            authors = re.search(r'^authors = \[(.*?)\]', (directory/'Cargo.toml').read_text(), re.M | re.S)
            entry['authors'] = re.findall(r'"([^"]+)"', authors.group(1)) if authors else []
            entry['license_text_origin'] = 'canonical SPDX text (crate and repository ship no license text)'
            entry['texts'] = [dict(file='SPDX ' + i, sha256=keep((spdx_dir/i).read_bytes())) for i in spdx_ids(entry['license'])]
        crates.append(entry)
    lock = (package/'Cargo.lock').read_bytes()
    return dict(schema=1, package='@mysten/walrus-wasm', version=meta['version'],
                npm_integrity='sha512-' + base64.b64encode(hashlib.sha512(Path(tarball).read_bytes()).digest()).decode(),
                cargo_lock_sha256=sha256(lock), wasm_sha256=sha256((package/'nodejs/walrus_wasm_bg.wasm').read_bytes()),
                package_license=spdx, package_license_evidence=spdx_files, target=TARGET,
                crates=crates, texts=dict(sorted(texts.items())))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--package', type=Path, required=True, help='unpacked npm package directory')
    parser.add_argument('--tarball', type=Path, required=True, help='the npm tarball it was unpacked from')
    parser.add_argument('--metadata', type=Path, required=True, help='cargo metadata JSON (see above)')
    parser.add_argument('--path-map', action='append', default=[], help='PREFIX=REPLACEMENT for metadata paths')
    parser.add_argument('--spdx-dir', type=Path, required=True, help='canonical license texts (poky meta/files/common-licenses)')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    path_map = [tuple(m.split('=', 1)) for m in args.path_map]
    inventory = resolve(args.package, args.tarball, json.loads(args.metadata.read_text()), path_map, args.spdx_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(inventory, indent=1, sort_keys=False) + '\n')
    print(f'{len(inventory["crates"])} crates, {len(inventory["texts"])} distinct license texts -> {args.output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
