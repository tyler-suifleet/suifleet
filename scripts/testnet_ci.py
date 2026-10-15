#!/usr/bin/env python3
"""Trusted-main workflow planner. All PR trust decisions use server-side facts."""
import json
import os
import re
import subprocess

CONTEXT = 'testnet/package-upgrade'
SHA = re.compile(r'^[0-9a-f]{40}$')


def api(path, payload=None):
    command = ['gh', 'api', path]
    if payload is not None:
        command += ['--method', 'POST', '--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload else None,
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def trusted_pr(pr, repo, sha, permissions):
    return (pr['state'] == 'open' and pr['base']['ref'] == 'main'
            and pr['base']['repo']['full_name'] == repo
            and (pr['head'].get('repo') or {}).get('full_name') == repo
            and pr['head']['sha'] == sha and bool(SHA.fullmatch(sha))
            and permissions.get(pr['user']['login']) in ('admin', 'maintain', 'write'))


def plan(event_name, event, repo, get=api):
    base = get(f'repos/{repo}/commits/main')['sha']
    if event_name in ('schedule', 'workflow_dispatch'):
        return {'head': base, 'base': get(f'repos/{repo}/commits/{base}')["parents"][0]["sha"], 'pr': ''}
    run = event['workflow_run']
    # Re-fetch rather than trust event fields or artifacts from the originating job.
    run = get(f'repos/{repo}/actions/runs/{run["id"]}')
    workflow = get(f'repos/{repo}/actions/workflows/{run["workflow_id"]}')
    if (run['conclusion'] != 'success' or run['status'] != 'completed'
            or workflow['path'] != '.github/workflows/ci.yml'
            or run['repository']['full_name'] != repo):
        return None
    sha = run['head_sha']
    if not SHA.fullmatch(sha):
        return None
    if run['event'] == 'push' and run['head_branch'] == 'main':
        return {'head': sha, 'base': get(f'repos/{repo}/commits/{sha}')["parents"][0]["sha"], 'pr': ''} if sha == base else None
    if run['event'] != 'pull_request':
        return None
    actor = run['actor']['login']
    actor_permission = get(f'repos/{repo}/collaborators/{actor}/permission')['permission']
    if actor_permission not in ('admin', 'maintain', 'write'):
        return None
    for candidate in get(f'repos/{repo}/commits/{sha}/pulls'):
        pr = get(f'repos/{repo}/pulls/{candidate["number"]}')
        author = pr['user']['login']
        permission = get(f'repos/{repo}/collaborators/{author}/permission')['permission']
        if trusted_pr(pr, repo, sha, {author: permission}):
            return {'head': sha, 'base': base, 'pr': str(pr['number'])}
    return None


def environment_ready(environment, branches):
    policy = environment.get('deployment_branch_policy') or {}
    reviewers = [r for r in environment.get('protection_rules', []) if r['type'] == 'required_reviewers']
    return (environment.get('can_admins_bypass') is False and not reviewers
            and policy.get('custom_branch_policies') is True
            and policy.get('protected_branches') is False
            and [(b['name'], b.get('type', 'branch')) for b in branches['branch_policies']] == [('main', 'branch')])


def status(repo, head, base, state, url):
    assert SHA.fullmatch(head) and SHA.fullmatch(base)
    api(f'repos/{repo}/statuses/{head}', dict(state=state, context=CONTEXT,
        description=f'base:{base}', target_url=url))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--recheck', action='store_true')
    parser.add_argument('--status', choices=['pending', 'success', 'failure'])
    args = parser.parse_args()
    repo = os.environ['GITHUB_REPOSITORY']
    if args.status:
        status(repo, os.environ['TEST_HEAD'], os.environ['BASE_REF'], args.status,
               f'https://github.com/{repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}')
    else:
        with open(os.environ['GITHUB_EVENT_PATH']) as stream:
            selection = plan(os.environ['GITHUB_EVENT_NAME'], json.load(stream), repo)
        if args.recheck and (not selection or selection['head'] != os.environ['TEST_HEAD']
                             or selection['base'] != os.environ['BASE_REF']):
            raise RuntimeError('superseded head/base: no testnet mutation submitted')
        if selection:
            environment = api(f'repos/{repo}/environments/live-package-upgrade')
            branches = api(f'repos/{repo}/environments/live-package-upgrade/deployment-branch-policies')
            if not environment_ready(environment, branches):
                raise RuntimeError('dedicated testnet environment must be main-only, without reviewers or admin bypass')
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in (selection or {}).items():
                print(f'{key}={value}', file=stream)
            print(f'run={str(bool(selection)).lower()}', file=stream)
