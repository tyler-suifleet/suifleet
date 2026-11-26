#!/usr/bin/env python3
"""Build, inspect and compare two Raspberry Pi starter images without signing."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from deployment_manifest import version
from inspect_pi_image import inspect, recipe_pins, sha
from release import write_json

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'kas/pi-starter.yml'
MACHINE = 'raspberrypi3-64'
TARGET = 'raspberrypi3-64-starter'
LAYER = 'meta-suifleet'
REPOS = ('poky', 'meta-openembedded', 'meta-raspberrypi', 'meta-lts-mixins', LAYER)
RECIPE = Path(LAYER) / 'recipes-edge/suifleet/suifleet_1.0.bb'
IMAGE = f'suifleet-pi-image-{MACHINE}.rootfs'


def check_compatibility(manifest, crate_version):
    if (manifest['network'], manifest['chain_identifier']) != ('testnet', '4c78adac'):
        raise ValueError('starter image requires the Sui testnet deployment manifest')
    bounds = manifest['supported_clients']['edge']
    if not version(bounds['min']) <= version(crate_version) < version(bounds['max_exclusive']):
        raise ValueError(f'Edge {crate_version} is outside [{bounds["min"]}, {bounds["max_exclusive"]})')
    return bounds


def read_pins(work, manifest):
    recipe = (work / RECIPE).read_text()
    pins = recipe_pins(recipe)
    match = re.search(r'^SUIFLEET_CRATE_VERSION = "([^"]+)"$', recipe, re.M)
    if not match:
        raise ValueError('public daemon recipe does not pin SUIFLEET_CRATE_VERSION')
    pins['crate_version'] = match.group(1)
    check_compatibility(manifest, pins['crate_version'])
    enrollment_files = ('first-boot-enrollment.patch', 'suifleet-first-boot-enrollment.service')
    for name in enrollment_files:
        if f'file://{name}' not in recipe or not (work / RECIPE.parent / 'files' / name).is_file():
            raise ValueError(f'public meta-suifleet pin lacks SF-322 file {name}')
    return pins


def git(directory, *args):
    return subprocess.check_output(['git', '-C', directory, *args], text=True).strip()


def build(work, manifest, preflight_only):
    work.mkdir()
    # Separate checkouts, build directories and default sstate caches.
    env = os.environ | {
        'KAS_WORK_DIR': str(work),
        'KAS_BUILD_DIR': str(work / 'build'),
        'KAS_TARGET': 'suifleet-pi-image',
        'KAS_MACHINE': MACHINE,
        'KAS_DISTRO': 'poky',
    }
    for name in ('KAS_REPO_REF_DIR', 'SSTATE_DIR', 'SSTATE_MIRRORS', 'DL_DIR',
                 'BB_ENV_PASSTHROUGH_ADDITIONS', 'BBPATH', 'TMPDIR'):
        env.pop(name, None)
    args = ['kas', 'build', str(PROFILE)]
    subprocess.run(['kas', 'checkout', str(PROFILE)], env=env, cwd=ROOT, check=True)
    revisions = {repo: git(work / repo, 'rev-parse', 'HEAD') for repo in REPOS}
    pins = read_pins(work, manifest)
    if preflight_only:
        return
    subprocess.run(args, env=env, cwd=ROOT, check=True)
    deploy = work / 'build/tmp/deploy/images' / MACHINE
    with tempfile.TemporaryDirectory(dir=work, prefix='inspect-') as scratch:
        evidence = inspect(deploy / (IMAGE + '.wic.bz2'), Path(scratch), Path('/usr/bin'),
                           e2fsck='e2fsck', pins=pins, starter=True)
    evidence.pop('image')
    return deploy, revisions, pins, evidence


def prepare_bundle(out, deploy, second, commit, pins, manifest_path, provenance):
    outputs = {
        '.wic.bz2': 'suifleet-pi-starter.wic.bz2',
        '.wic.bmap': 'suifleet-pi-starter.wic.bmap',
        '.spdx.tar.zst': 'sbom.spdx.tar.zst',
        '.manifest': 'packages.manifest',
    }
    for suffix in ('.wic.bz2', '.manifest'):
        name = IMAGE + suffix
        if sha(deploy / name) != sha(second / name):
            raise ValueError(f'independent builds differ for {suffix}')
    out.mkdir()
    for suffix, name in outputs.items():
        shutil.copyfile(deploy / (IMAGE + suffix), out / name)
    shutil.copyfile(manifest_path, out / 'deployment-manifest.json')
    write_json(out / 'provenance.json', provenance)
    files = sorted(out.iterdir())
    (out / 'SHA256SUMS').write_text(''.join(f'{sha(path)}  {path.name}\n' for path in files))
    files.append(out / 'SHA256SUMS')
    write_json(out / 'release.json', {
        'schema': 1,
        'name': 'suifleet-pi-starter',
        'version': pins['crate_version'],
        'commit': commit,
        'target': TARGET,
        'files': [{'path': path.name, 'size': path.stat().st_size, 'sha256': sha(path)} for path in files],
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path, help='public testnet deployment-manifest.json')
    parser.add_argument('--work', required=True, type=Path, help='new directory for both independent builds')
    parser.add_argument('--out', type=Path, help='new directory for the unsigned candidate')
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    if not args.preflight_only and (args.out is None or args.out.exists()):
        parser.error('--out must name a new directory')
    if git(ROOT, 'status', '--porcelain'):
        parser.error('build from a clean committed checkout')
    commit = git(ROOT, 'rev-parse', 'HEAD')
    args.work.mkdir(parents=True)
    manifest_path = args.work / 'deployment-manifest.json'
    shutil.copyfile(args.manifest, manifest_path)
    manifest = json.loads(manifest_path.read_text())
    work = args.work.resolve()
    first = build(work / 'first', manifest, args.preflight_only)
    if args.preflight_only:
        return
    deploy, revisions, pins, evidence = first
    second, revisions_b, pins_b, evidence_b = build(work / 'second', manifest, False)
    if (revisions, pins, evidence) != (revisions_b, pins_b, evidence_b):
        raise ValueError('independent build provenance or filesystem inspection differs')
    prepare_bundle(args.out, deploy, second, commit, pins, manifest_path, {
        'schema': 1,
        'commit': commit,
        'target': TARGET,
        'repositories': revisions,
        'kas': subprocess.check_output(['kas', '--version'], text=True).strip(),
        'edge_compatibility': check_compatibility(manifest, pins['crate_version']),
        'inspection': evidence,
        'independent_image_digests_match': True,
        'qemu_boot_tested': False,
        'physical_boot_tested': False,
    })


if __name__ == '__main__':
    main()
