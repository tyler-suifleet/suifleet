#!/usr/bin/env python3
"""Package the SF-082 keyless Walrus codec for the Yocto image (SF-120).

Runs the reviewed ``scripts/build_walrus_codec.cjs`` of an exact daemon source
checkout whose ``dapp/node_modules`` came from ``pnpm install --frozen-lockfile``
and adds what a device package needs besides the bundle: the license texts of
every npm package compiled into it and a provenance record binding the bundle
to that source revision and lockfile. Prints the SHA-256 pins for
``suifleet-walrus-codec_1.0.bb`` or, with ``--recipe``, verifies them.

Nothing is downloaded and nothing from the build host's ``node_modules`` is
shipped other than the bundled code, the WASM module and license texts.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ARTIFACTS = ['walrus-device-codec.cjs', 'walrus_wasm_bg.wasm', 'manifest.json',
             'THIRD_PARTY_LICENSES.txt', 'provenance.json']
LICENSE_FILE = re.compile(r'^(licen[cs]e|copying|notice)([.-].*)?$', re.I)
BUNDLED = re.compile(r'^\s*// dapp/node_modules/\.pnpm/([^/]+)/node_modules/((?:@[^/]+/)?[^/]+)/', re.M)
# Packages whose published metadata declares no license. They are shipped only
# with this explicit record; the recipe names the gap rather than guessing.
UNDECLARED = {('@mysten/walrus-wasm', '0.3.1'): (
    'UNDECLARED: the published package has no license field or license file, and its '
    'repository (github.com/MystenLabs/ts-sdks, packages/walrus-wasm) has no LICENSE. '
    'Sibling @mysten packages in this bundle declare Apache-2.0. The WASM also compiles '
    'Rust crates listed in that package\'s Cargo.lock; their licenses were not resolved '
    'here. Resolve with the publisher before a production release.')}
UNDECLARED_LICENSE = 'SuiFleet-walrus-wasm-undeclared'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bundled_packages(bundle_text, node_modules):
    """Every npm package esbuild compiled into the bundle, with its license data."""
    packages = {}
    for store, name in set(BUNDLED.findall(bundle_text)):
        root = node_modules/'.pnpm'/store/'node_modules'/name
        meta = json.loads((root/'package.json').read_text())
        if meta.get('name') != name:
            raise ValueError(f'unexpected package at {root}')
        files = sorted(p.name for p in root.iterdir() if p.is_file() and LICENSE_FILE.match(p.name))
        declared = meta.get('license')
        if not isinstance(declared, str) or not declared:
            declared = None
        undeclared = UNDECLARED.get((name, meta['version']))
        if declared is None and undeclared is None:
            raise ValueError(f'{name}@{meta["version"]} declares no license; review it before packaging')
        if declared is not None and undeclared is not None:
            raise ValueError(f'{name}@{meta["version"]} now declares a license; remove its undeclared override')
        key = (name, meta['version'])
        packages[key] = dict(name=name, version=meta['version'], license=declared or UNDECLARED_LICENSE,
                             note=undeclared, root=root, license_files=files)
    if not packages:
        raise ValueError('no bundled packages found; is this the esbuild codec bundle?')
    return [packages[k] for k in sorted(packages)]


def license_text(packages):
    out = ['Third-party software compiled into walrus-device-codec.cjs and walrus_wasm_bg.wasm.',
           'Node.js itself is packaged separately (suifleet-node) with its own LICENSE.', '']
    for p in packages:
        out += ['=' * 78, f'{p["name"]} {p["version"]}', f'Declared license: {p["license"]}']
        if p['note']:
            out.append(p['note'])
        if not p['license_files']:
            out.append('No license file is shipped in the published package.')
        for name in p['license_files']:
            out += [f'--- {name} ---', (p['root']/name).read_text(errors='replace').rstrip('\n')]
        out.append('')
    return '\n'.join(out) + '\n'


def git(source, *args):
    return subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True, text=True).stdout.strip()


def build(source, output, revision, node='node'):
    source = Path(source).resolve()
    if git(source, 'rev-parse', 'HEAD') != revision:
        raise ValueError(f'source checkout is not at {revision}')
    if git(source, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('source checkout has tracked modifications')
    with tempfile.TemporaryDirectory(prefix='sf120-codec-') as directory:
        stage = Path(directory)/'codec'
        subprocess.run([node, str(source/'scripts/build_walrus_codec.cjs'), str(stage)], check=True,
                       cwd=source, stdout=subprocess.DEVNULL)
        manifest = json.loads((stage/'manifest.json').read_text())
        builder = subprocess.run([node, '--version'], check=True, capture_output=True, text=True).stdout.strip()
        if builder != 'v' + manifest['node']:
            raise ValueError(f'codec targets Node {manifest["node"]}; build it with that version, not {builder}')
        for name, pin in manifest['files'].items():
            if sha256(stage/name) != pin['sha256']:
                raise ValueError('codec manifest does not describe its files: ' + name)
        packages = bundled_packages((stage/'walrus-device-codec.cjs').read_text(), source/'dapp/node_modules')
        (stage/'THIRD_PARTY_LICENSES.txt').write_text(license_text(packages))
        wasm_lock = next((p['root']/'Cargo.lock' for p in packages if p['name'] == '@mysten/walrus-wasm'), None)
        provenance = dict(schema=1, source_revision=revision, builder_node=builder,
                          pnpm_lock_sha256=sha256(source/'dapp/pnpm-lock.yaml'),
                          manifest_sha256=sha256(stage/'manifest.json'),
                          walrus_wasm_cargo_lock_sha256=sha256(wasm_lock) if wasm_lock and wasm_lock.exists() else None,
                          packages=[dict(name=p['name'], version=p['version'], license=p['license'],
                                         license_files=p['license_files']) for p in packages])
        (stage/'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
        if output.exists():
            shutil.rmtree(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output)
    for name in ARTIFACTS:
        (output/name).chmod(0o644)
    return pins(output)


def pins(directory):
    present = sorted(p.name for p in Path(directory).iterdir())
    if present != sorted(ARTIFACTS):
        raise ValueError(f'codec artifact must contain exactly {sorted(ARTIFACTS)}, found {present}')
    return {name: sha256(Path(directory)/name) for name in ARTIFACTS}


def recipe_pins(text):
    found = dict(re.findall(r'^SUIFLEET_CODEC_SHA256\[([^\]]+)\] = "([0-9a-f]{64})"$', text, re.M))
    revision = re.search(r'^SUIFLEET_CODEC_SOURCE_REV = "([0-9a-f]{40})"$', text, re.M)
    if sorted(found) != sorted(ARTIFACTS) or not revision:
        raise ValueError('recipe must pin SUIFLEET_CODEC_SOURCE_REV and every codec artifact SHA-256')
    return revision.group(1), found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', type=Path, help='daemon source checkout with frozen dapp/node_modules')
    parser.add_argument('--revision', help='exact source revision (the recipe SRCREV)')
    parser.add_argument('--output', type=Path, required=True, help='artifact directory (replaced)')
    parser.add_argument('--node', default='node', help='Node used to run the reviewed build script')
    parser.add_argument('--recipe', type=Path, help='verify an existing artifact against this recipe')
    args = parser.parse_args(argv)
    if args.recipe:
        revision, expected = recipe_pins(args.recipe.read_text())
        actual = pins(args.output)
        provenance = json.loads((args.output/'provenance.json').read_text())
        errors = [n for n in ARTIFACTS if actual[n] != expected[n]]
        if provenance.get('source_revision') != revision:
            errors.append('provenance source_revision')
        if errors:
            print('codec artifact differs from recipe: ' + ', '.join(errors), file=sys.stderr)
            return 1
        print(json.dumps(dict(verified=str(args.output), source_revision=revision, sha256=actual), indent=2))
        return 0
    if not (args.source and args.revision):
        parser.error('--source and --revision are required to build')
    result = build(args.source, args.output, args.revision, args.node)
    print(f'SUIFLEET_CODEC_SOURCE_REV = "{args.revision}"')
    for name in ARTIFACTS:
        print(f'SUIFLEET_CODEC_SHA256[{name}] = "{result[name]}"')
    return 0


if __name__ == '__main__':
    sys.exit(main())
