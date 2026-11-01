#!/usr/bin/env python3
"""Bind secretless prepared tools to this trusted workflow run and exact revision."""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

SHA = re.compile(r'[0-9a-f]{40}')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


def expected(repo, head, base, run_id, attempt, controller):
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
            or not all(SHA.fullmatch(s) for s in (head, base, controller))
            or not str(run_id).isdigit() or int(run_id) <= 0
            or not str(attempt).isdigit() or int(attempt) <= 0):
        raise ValueError('invalid prepared-image provenance')
    return {'io.suifleet.head': head, 'io.suifleet.base': base,
            'io.suifleet.run': str(run_id), 'io.suifleet.attempt': str(attempt),
            'io.suifleet.controller': controller,
            'org.opencontainers.image.source': 'https://github.com/'+repo}


def image_name(repo):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('invalid image repository')
    return 'ghcr.io/'+repo.lower()+'-testnet-prepared'


def image_ref(repo, digest):
    if not DIGEST.fullmatch(digest):
        raise ValueError('prepared image must use an immutable sha256 digest')
    return image_name(repo)+'@'+digest


def verify(labels, wanted):
    if not isinstance(labels, dict) or any(labels.get(k) != v for k,v in wanted.items()):
        raise RuntimeError('prepared image does not match trusted run/head/base/controller')


def package(root, destination, labels):
    """Only prebuilt public dependencies/tools enter the image, never keys/state."""
    root, destination = Path(root), Path(destination)
    destination.mkdir(mode=0o700)
    shutil.copytree(root/'dapp/node_modules',destination/'node_modules',symlinks=True)
    for name, source in [('suifleet','suifleet-edge/target/debug/suifleet'),
                         ('chain_facts','suifleet-edge/target/debug/examples/chain_facts')]:
        path=root/source
        if path.is_symlink() or not path.is_file():
            raise ValueError('prepared binary is missing or symlinked')
        shutil.copy2(path,destination/name)
    text = 'FROM suifleet-dev:testnet\n'
    text += '\n'.join('LABEL '+k+'='+json.dumps(v) for k,v in labels.items())+'\n'
    text += 'COPY node_modules /opt/suifleet-prepared/node_modules\nCOPY suifleet chain_facts /opt/suifleet-prepared/\n'
    (destination/'Dockerfile').write_text(text)


def prune_versions(versions, now=None):
    """Retain ten recent versions and everything younger than 48 hours.

    Only this workflow's run tags qualify; other packages/tags are untouched.
    The API caller also verifies each producer workflow has completed before
    deletion; queued/running validation retains its input regardless of age.
    """
    now = now or datetime.now(timezone.utc)
    ordered = sorted(versions, key=lambda v: v['created_at'], reverse=True)
    result = []
    for version in ordered[10:]:
        tags = version.get('metadata', {}).get('container', {}).get('tags', [])
        if not tags or not all(re.fullmatch(r'run-[0-9]+-[0-9]+', t) for t in tags):
            continue
        created = datetime.fromisoformat(version['created_at'].replace('Z', '+00:00'))
        if now-created > timedelta(hours=48):
            if type(version.get('id')) is not int or version['id'] <= 0:
                raise ValueError('invalid prepared image version identity')
            result.append(version['id'])
    return result[:50]


def prune(repo):
    from urllib.parse import quote
    image_name(repo)  # Validate fixed package/owner path before API requests.
    owner, repository = repo.split('/')
    endpoint = f'users/{owner}/packages/container/{quote(repository.lower()+"-testnet-prepared", safe="")}/versions'
    versions = []
    for page in range(1, 6):
        found = json.loads(subprocess.check_output(['gh','api',endpoint+f'?per_page=100&page={page}'],text=True))
        versions.extend(found)
        if len(found) < 100:
            break
    else:
        raise RuntimeError('prepared image history exceeds bounded discovery')
    delete = []
    by_id = {v['id']: v for v in versions}
    from testnet_ci import api
    for version in prune_versions(versions):
        tags = by_id[version]['metadata']['container']['tags']
        runs = {int(tag.split('-')[1]) for tag in tags}
        producers = [api(f'repos/{repo}/actions/runs/{run}') for run in sorted(runs)]
        if not all(run.get('status') == 'completed' and run.get('head_branch') == 'main'
                   and run.get('path') == '.github/workflows/testnet.yml'
                   and (run.get('repository') or {}).get('full_name') == repo for run in producers):
            continue
        subprocess.run(['gh','api','--method','DELETE',endpoint+'/'+str(version)],check=True)
        delete.append(version)
    return {'deleted_versions': delete, 'retained_minimum':10, 'minimum_age_hours':48}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path)
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--prune',action='store_true')
    args=parser.parse_args()
    if args.prune:
        print(json.dumps(prune(os.environ['GITHUB_REPOSITORY'])))
        raise SystemExit(0)
    labels=expected(os.environ['GITHUB_REPOSITORY'],os.environ['TEST_HEAD'],os.environ['BASE_REF'],
                    os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_RUN_ATTEMPT'],os.environ['CONTROLLER_SHA'])
    if args.package:
        package('.',args.package,labels)
    elif args.verify:
        ref=image_ref(os.environ['GITHUB_REPOSITORY'],os.environ['PREPARED_DIGEST'])
        actual=json.loads(subprocess.check_output(['docker','image','inspect',ref,'--format','{{json .Config.Labels}}'],text=True))
        verify(actual,labels)
        print('Verified prepared image provenance and immutable reference')
    else:
        print(image_name(os.environ['GITHUB_REPOSITORY']))
