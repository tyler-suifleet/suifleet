#!/usr/bin/env python3
"""Restore authenticated ciphertext from this main-only workflow's artifacts.

workflow_run runtime cache tokens can be read-only. Artifacts belong to the
producing run, so persisting encrypted fixture state needs no cache write grant.
The runtime decryptor still rejects tampering and wrong keys before mutation.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess

from testnet_ci import api

NAME = 'encrypted-testnet-fixture'
WORKFLOW = '.github/workflows/testnet.yml'


def restore(repo, destination, get=api, download=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    if any(destination.iterdir()):
        raise RuntimeError('ciphertext restore destination must be empty')
    # Query by artifact name, not recent run count: skipped/untrusted runs must
    # not evict a valid fixture from discovery and force fresh publication.
    candidates = []
    for page in range(1, 6):
        listing = get(f'repos/{repo}/actions/artifacts?name={NAME}&per_page=100&page={page}')
        for artifact in listing['artifacts']:
            if artifact.get('name') != NAME or artifact.get('expired') is not False:
                continue
            try:
                created = datetime.strptime(artifact['created_at'], '%Y-%m-%dT%H:%M:%SZ')
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError('invalid fixture artifact creation time') from error
            artifact_id = artifact['id']
            if type(artifact_id) is not int or artifact_id <= 0:
                raise RuntimeError('invalid fixture artifact identity')
            candidates.append((created, artifact_id, artifact))
        if listing['total_count'] <= page * 100:
            break
    else:
        raise RuntimeError('fixture artifact history exceeds bounded discovery; refusing to republish blindly')
    # GitHub's response/ID ordering differs from creation time across reruns.
    # Search the complete bounded history before selecting the newest state.
    for _, artifact_id, artifact in sorted(candidates, key=lambda item: item[:2], reverse=True):
        run_id = artifact['workflow_run']['id']
        if type(run_id) is not int or run_id <= 0:
            raise RuntimeError('invalid fixture-producing run identity')
        run = get(f'repos/{repo}/actions/runs/{run_id}')
        if (run.get('head_branch') != 'main' or run.get('path') != WORKFLOW
                or (run.get('repository') or {}).get('full_name') != repo
                or run.get('event') not in ('workflow_run', 'workflow_dispatch', 'schedule')):
            continue
        return download_fixture(repo, destination, run_id, artifact_id, download)
    return {'restored': False, 'reason': 'no prior main-workflow ciphertext artifact'}


def download_fixture(repo, destination, run_id, artifact_id, download):
    if download is None:
        result = subprocess.run(['gh','run','download',str(run_id),'--repo',repo,
                                 '--name',NAME,'--dir',str(destination)],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('encrypted fixture artifact download failed')
    else:
        download(run_id, NAME, destination)
    files = list(destination.iterdir())
    if len(files) != 1 or files[0].name != 'fixture.bin' or files[0].is_symlink() or not files[0].is_file():
        raise RuntimeError('fixture artifact must contain only ciphertext fixture.bin')
    files[0].chmod(0o600)
    return {'restored': True, 'run': run_id, 'artifact': artifact_id}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    print(json.dumps(restore(os.environ['GITHUB_REPOSITORY'], args.destination)))
