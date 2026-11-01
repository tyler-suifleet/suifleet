#!/usr/bin/env python3
"""Restore authenticated ciphertext from this main-only workflow's artifacts.

workflow_run runtime cache tokens can be read-only. Artifacts belong to the
producing run, so persisting encrypted fixture state needs no cache write grant.
The runtime decryptor still rejects tampering and wrong keys before mutation.

Every serialized run uploads one artifact, so history grows with routine CI.
Restore reads the complete name-filtered history (bounded far above retention)
and `--prune` deletes trusted snapshots older than the newest KEEP, from a
separate main-sourced job that alone holds `actions: write`.
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
PAGE = 100
# Discovery bound: far above pruned history, and above 90-day artifact expiry
# at ~70 executed runs a day. Exceeding it is ambiguous; never republish blindly.
MAX_PAGES = 100
# Trusted snapshots preserved for recovery from a bad newest state.
KEEP = 10
# Per-run mutation/request bounds; larger backlogs converge over later runs.
MAX_DELETES = 50
MAX_LOOKUPS = 200


class HistoryChanged(RuntimeError):
    """Pagination shifted under discovery; the listing is ambiguous."""


def discover(repo, get, name=NAME):
    """Return every listed artifact, failing closed unless the set is complete."""
    seen, total = {}, None
    for page in range(1, MAX_PAGES + 1):
        listing = get(f'repos/{repo}/actions/artifacts?name={name}&per_page={PAGE}&page={page}')
        if (not isinstance(listing, dict) or type(listing.get('total_count')) is not int
                or listing['total_count'] < 0 or not isinstance(listing.get('artifacts'), list)):
            raise RuntimeError('malformed fixture artifact listing')
        if total is None:
            total = listing['total_count']
            if total > MAX_PAGES * PAGE:
                raise RuntimeError('fixture artifact history exceeds bounded discovery; refusing to republish blindly')
        elif listing['total_count'] != total:
            raise HistoryChanged('fixture artifact history changed during discovery')
        for artifact in listing['artifacts']:
            artifact_id = artifact.get('id') if isinstance(artifact, dict) else None
            if type(artifact_id) is not int or artifact_id <= 0:
                raise RuntimeError('invalid fixture artifact identity')
            if artifact_id in seen:
                raise HistoryChanged('fixture artifact history changed during discovery')
            seen[artifact_id] = artifact
        if len(seen) >= total or not listing['artifacts']:
            break
    if len(seen) != total:
        raise HistoryChanged('fixture artifact discovery is incomplete')
    return list(seen.values())


def candidates(repo, get, name=NAME):
    """Unexpired fixture artifacts, newest creation first."""
    for attempt in range(2):
        try:
            artifacts = discover(repo, get, name)
            break
        except HistoryChanged:
            if attempt:
                raise
    usable = []
    for artifact in artifacts:
        if artifact.get('name') != name or artifact.get('expired') is not False:
            continue
        try:
            created = datetime.strptime(artifact['created_at'], '%Y-%m-%dT%H:%M:%SZ')
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError('invalid fixture artifact creation time') from error
        run_id = (artifact.get('workflow_run') or {}).get('id')
        if type(run_id) is not int or run_id <= 0:
            raise RuntimeError('invalid fixture-producing run identity')
        usable.append((created, artifact['id'], run_id))
    # GitHub's response/ID ordering differs from creation time across reruns.
    # Search the complete history before selecting the newest state.
    return sorted(usable, reverse=True)


def producer_check(repo, get):
    runs = {}
    def trusted(run_id):
        if run_id not in runs:
            run = get(f'repos/{repo}/actions/runs/{run_id}')
            runs[run_id] = (isinstance(run, dict) and run.get('id') == run_id
                            and run.get('head_branch') == 'main' and run.get('path') == WORKFLOW
                            and (run.get('repository') or {}).get('full_name') == repo
                            and run.get('event') in ('workflow_run', 'workflow_dispatch', 'schedule'))
        return runs[run_id]
    trusted.lookups = runs
    return trusted


def restore(repo, destination, get=api, download=None, name=NAME):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    if any(destination.iterdir()):
        raise RuntimeError('ciphertext restore destination must be empty')
    # Query by artifact name, not recent run count: skipped/untrusted runs must
    # not evict a valid fixture from discovery and force fresh publication.
    trusted = producer_check(repo, get)
    for _, artifact_id, run_id in candidates(repo, get, name):
        if trusted(run_id):
            return download_fixture(repo, destination, run_id, artifact_id, download, name)
    return {'restored': False, 'reason': 'no prior main-workflow ciphertext artifact'}


def prune(repo, current_run, get=api, delete=None, keep=KEEP,
          max_deletes=MAX_DELETES, max_lookups=MAX_LOOKUPS, name=NAME):
    """Delete trusted fixture snapshots older than the newest `keep` trusted ones.

    Only the fixture artifact name from this repository's main testnet workflow
    is ever deleted; untrusted, expired and current-run artifacts are left alone.
    """
    if keep < 1:
        raise ValueError('at least one trusted fixture snapshot must be kept')
    delete = delete or (lambda artifact_id: delete_artifact(repo, artifact_id))
    trusted = producer_check(repo, get)
    ordered = candidates(repo, get, name)
    kept = []
    for index, (_, artifact_id, run_id) in enumerate(ordered):
        if len(trusted.lookups) >= max_lookups:
            return {'deleted': [], 'kept': kept, 'reason': 'lookup bound reached before retained set was verified'}
        if trusted(run_id):
            kept.append(artifact_id)
            if len(kept) == keep:
                older = ordered[index + 1:]
                break
    else:
        return {'deleted': [], 'kept': kept, 'reason': 'no snapshots beyond retained set'}
    deleted = []
    # Oldest first: the snapshots closest to expiry go before newer ones.
    for _, artifact_id, run_id in reversed(older):
        if len(deleted) >= max_deletes or len(trusted.lookups) >= max_lookups:
            break
        if run_id == current_run or not trusted(run_id):
            continue
        delete(artifact_id)
        deleted.append(artifact_id)
    return {'deleted': deleted, 'kept': kept}


def delete_artifact(repo, artifact_id):
    result = subprocess.run(['gh', 'api', '--method', 'DELETE',
                             f'repos/{repo}/actions/artifacts/{artifact_id}'],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'encrypted fixture artifact {artifact_id} deletion failed')


def download_fixture(repo, destination, run_id, artifact_id, download, name=NAME):
    if download is None:
        result = subprocess.run(['gh','run','download',str(run_id),'--repo',repo,
                                 '--name',name,'--dir',str(destination)],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('encrypted fixture artifact download failed')
    else:
        download(run_id, name, destination)
    files = list(destination.iterdir())
    if len(files) != 1 or files[0].name != 'fixture.bin' or files[0].is_symlink() or not files[0].is_file():
        raise RuntimeError('fixture artifact must contain only ciphertext fixture.bin')
    files[0].chmod(0o600)
    return {'restored': True, 'run': run_id, 'artifact': artifact_id}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--destination')
    action.add_argument('--prune', action='store_true')
    parser.add_argument('--lane', type=int, default=0)
    args = parser.parse_args()
    from testnet_pool import lane_info
    name = lane_info(args.lane)['fixture']
    repo = os.environ['GITHUB_REPOSITORY']
    if args.prune:
        print(json.dumps(prune(repo, int(os.environ['GITHUB_RUN_ID']), name=name)))
    else:
        print(json.dumps(restore(repo, args.destination, name=name)))
