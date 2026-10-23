#!/usr/bin/env python3
"""Package the SF-082 keyless Walrus codec for the Yocto image (SF-120).

Runs the reviewed ``scripts/build_walrus_codec.cjs`` of an exact daemon source
checkout whose ``dapp/node_modules`` came from ``pnpm install --frozen-lockfile``
and adds what a device package needs besides the bundle: the license texts of
every npm package compiled into it, the reviewed inventory of the Rust crates
compiled into its WASM module (``walrus_wasm_licenses/``), and a provenance
record binding the bundle to that source revision and lockfile. Prints the SHA-256 pins for
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
SPDX = re.compile(r'SPDX-License-Identifier:\s*([^\s*]+)')
# Reviewed license inventories (scripts/resolve_walrus_wasm_licenses.py) for npm
# packages whose metadata omits the license field: the publisher's SPDX headers
# plus every crate compiled into the WASM module, bound to the locked tarball
# and its Cargo.lock.
INVENTORIES = {'@mysten/walrus-wasm': Path(__file__).resolve().parent/'walrus_wasm_licenses'}
# Elected license of each OR choice. The union is the recipe LICENSE.
PREFERENCE = ['Apache-2.0', 'MIT', 'BSD-3-Clause']


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def elected(expression):
    """The licenses SuiFleet distributes under for one SPDX expression."""
    tokens = re.findall(r'\(|\)|[^\s()]+', expression.replace('/', ' OR '))
    position = 0

    def rank(choice):
        return max(PREFERENCE.index(c) if c in PREFERENCE else len(PREFERENCE) for c in choice), len(choice)

    def parse(operator, operand):
        nonlocal position
        parts = [operand()]
        while position < len(tokens) and tokens[position] == operator:
            position += 1
            parts.append(operand())
        return parts

    def any_of():
        return min(parse('OR', all_of), key=rank)  # first best choice

    def all_of():
        return set().union(*parse('AND', atom))

    def atom():
        nonlocal position
        if position >= len(tokens) or tokens[position] in {'AND', 'OR', ')'}:
            raise ValueError(f'invalid SPDX expression {expression!r}')
        token, position = tokens[position], position + 1
        if token != '(':
            return {token}
        inner = any_of()
        if position >= len(tokens) or tokens[position] != ')':
            raise ValueError(f'invalid SPDX expression {expression!r}')
        position += 1
        return inner

    result = any_of()
    if position != len(tokens):
        raise ValueError(f'invalid SPDX expression {expression!r}')
    return result


def inventory(name, version, root, lockfile):
    """The reviewed license inventory for a package without a license field, verified against what is bundled."""
    directory = INVENTORIES.get(name)
    path = directory/f'{version}.json' if directory else None
    if not path or not path.exists():
        return None
    data = json.loads(path.read_text())
    if (data['package'], data['version']) != (name, version):
        raise ValueError(f'{path} does not describe {name}@{version}')
    if sha256(root/'Cargo.lock') != data['cargo_lock_sha256']:
        raise ValueError(f'{name}@{version} Cargo.lock differs from its license inventory')
    if lockfile is not None and f"'{name}@{version}':\n    resolution: {{integrity: {data['npm_integrity']}}}" not in lockfile:
        raise ValueError(f'{name}@{version} is not locked to the tarball its license inventory describes')
    headers = {spdx for f in sorted(root.rglob('*.*js')) if f.is_file() for spdx in SPDX.findall(f.read_text(errors='replace'))}
    if headers != {data['package_license']}:
        raise ValueError(f'{name}@{version} SPDX headers {sorted(headers)} differ from its inventory')
    for digest, text in data['texts'].items():
        if hashlib.sha256(text.encode()).hexdigest() != digest:
            raise ValueError(f'{path}: license text {digest} is corrupt')
    for crate in data['crates']:
        if not crate['license'] or not crate['texts'] or any(t['sha256'] not in data['texts'] for t in crate['texts']):
            raise ValueError(f'{path}: crate {crate["name"]} {crate["version"]} lacks license data')
        elected(crate['license'])
    data['sha256'] = sha256(path)
    return data


def bundled_packages(bundle_text, node_modules, lockfile=None):
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
        reviewed = inventory(name, meta['version'], root, lockfile) if declared is None else None
        if declared is None and reviewed is None:
            raise ValueError(f'{name}@{meta["version"]} declares no license; review it before packaging')
        if declared is not None and (INVENTORIES.get(name, Path('/nonexistent'))/f'{meta["version"]}.json').exists():
            raise ValueError(f'{name}@{meta["version"]} now declares a license; remove its license inventory')
        key = (name, meta['version'])
        packages[key] = dict(name=name, version=meta['version'], license=declared or reviewed['package_license'],
                             inventory=reviewed, root=root, license_files=files)
    if not packages:
        raise ValueError('no bundled packages found; is this the esbuild codec bundle?')
    return [packages[k] for k in sorted(packages)]


def licenses(packages):
    """The recipe LICENSE terms: elected licenses of every npm package and compiled crate."""
    out = set()
    for p in packages:
        out |= elected(p['license'])
        for crate in (p['inventory'] or {}).get('crates', []):
            out |= elected(crate['license'])
    return sorted(out)


def license_text(packages):
    out = ['Third-party software compiled into walrus-device-codec.cjs and walrus_wasm_bg.wasm.',
           'Node.js itself is packaged separately (suifleet-node) with its own LICENSE.',
           'Distributed under: ' + ' & '.join(licenses(packages)) + ' (Apache-2.0, then MIT, elected where a choice is offered).', '']
    for p in packages:
        out += ['=' * 78, f'{p["name"]} {p["version"]}', f'Declared license: {p["license"]}']
        if p['inventory']:
            out.append('The npm package.json has no license field. The publisher declares ' + p['license'] +
                       ' in the SPDX-License-Identifier headers of ' + ', '.join(p['inventory']['package_license_evidence']) + '.')
        if not p['license_files'] and not p['inventory']:
            out.append('No license file is shipped in the published package.')
        for name in p['license_files']:
            out += [f'--- {name} ---', (p['root']/name).read_text(errors='replace').rstrip('\n')]
        if p['inventory']:
            out += crate_text(p['inventory'])
        out.append('')
    return '\n'.join(out) + '\n'


def crate_text(data):
    """The Rust crates compiled into the WASM module and their license texts."""
    ids = {digest: f'T{i}' for i, digest in enumerate(sorted({t['sha256'] for c in data['crates'] for t in c['texts']}), 1)}
    out = ['', f'Rust crates in the {data["target"]} build of this package (its Cargo.lock SHA-256 {data["cargo_lock_sha256"]}).',
           'Proc-macro crates run only at build time and are listed for completeness.', '']
    for crate in data['crates']:
        source = (crate['source'] or 'npm package').split('#')[0]
        line = f'{crate["name"]} {crate["version"]} ({source}): {crate["license"]}; texts ' + ', '.join(ids[t['sha256']] for t in crate['texts'])
        if crate.get('authors'):
            line += '; authors ' + ', '.join(crate['authors'])
        if crate.get('license_text_origin'):
            line += '; text from ' + crate['license_text_origin']
        out.append(line)
    for digest, label in ids.items():
        files = sorted({t['file'] for c in data['crates'] for t in c['texts'] if t['sha256'] == digest})
        out += ['', f'--- {label}: {", ".join(files)} ---', data['texts'][digest].rstrip('\n')]
    return out


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
        packages = bundled_packages((stage/'walrus-device-codec.cjs').read_text(), source/'dapp/node_modules',
                                    (source/'dapp/pnpm-lock.yaml').read_text())
        (stage/'THIRD_PARTY_LICENSES.txt').write_text(license_text(packages))
        wasm = next(p['inventory'] for p in packages if p['name'] == '@mysten/walrus-wasm')
        if sha256(stage/'walrus_wasm_bg.wasm') != wasm['wasm_sha256']:
            raise ValueError('the bundled WASM module is not the one its license inventory describes')
        provenance = dict(schema=1, source_revision=revision, builder_node=builder,
                          pnpm_lock_sha256=sha256(source/'dapp/pnpm-lock.yaml'),
                          manifest_sha256=sha256(stage/'manifest.json'),
                          walrus_wasm_cargo_lock_sha256=wasm['cargo_lock_sha256'],
                          walrus_wasm_license_inventory_sha256=wasm['sha256'],
                          walrus_wasm_crates=len(wasm['crates']), licenses=licenses(packages),
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


def recipe_license(text):
    match = re.search(r'^LICENSE = "([^"]+)"$', text, re.M)
    return match.group(1).split(' & ') if match else None


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
        if recipe_license(args.recipe.read_text()) != provenance.get('licenses'):
            errors.append('recipe LICENSE differs from provenance licenses')
        if errors:
            print('codec artifact differs from recipe: ' + ', '.join(errors), file=sys.stderr)
            return 1
        print(json.dumps(dict(verified=str(args.output), source_revision=revision, sha256=actual), indent=2))
        return 0
    if not (args.source and args.revision):
        parser.error('--source and --revision are required to build')
    result = build(args.source, args.output, args.revision, args.node)
    print(f'LICENSE = "{" & ".join(json.loads((args.output/"provenance.json").read_text())["licenses"])}"')
    print(f'SUIFLEET_CODEC_SOURCE_REV = "{args.revision}"')
    for name in ARTIFACTS:
        print(f'SUIFLEET_CODEC_SHA256[{name}] = "{result[name]}"')
    return 0


if __name__ == '__main__':
    sys.exit(main())
