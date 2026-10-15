#!/usr/bin/env python3
"""Trusted-main workflow planner. All PR trust decisions use server-side facts."""
import json
import os
import re
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONTEXT = 'testnet/package-upgrade'
SHA = re.compile(r'^[0-9a-f]{40}$')


def api(path, payload=None):
    command = ['gh', 'api', path]
    if payload is not None:
        command += ['--method', 'POST', '--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload else None,
                            text=True, capture_output=True, check=False)
    if result.returncode:
        try:
            message = str(json.loads(result.stdout).get('message', 'request failed'))
        except (ValueError, AttributeError):
            message = 'request failed without a JSON error message'
        for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
            secret = os.environ.get(name)
            if secret:
                message = message.replace(secret, '[redacted]')
        raise RuntimeError(f'GitHub API {path}: {message[:500]} (exit {result.returncode})')
    return json.loads(result.stdout)


def public_api(path, opener=urlopen):
    """Read public metadata without forwarding the workflow token.

    GitHub documents deployment branch policies as public-readable. The
    Actions installation token is rejected by this endpoint in observed CI;
    no administrator credential is needed for a public repository.
    """
    if not re.fullmatch(r'repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/environments/live-package-upgrade/deployment-branch-policies', path):
        raise ValueError('public metadata path is outside the dedicated environment')
    request = Request('https://api.github.com/' + path + '?per_page=100', headers={
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2026-03-10',
        'User-Agent': 'suifleet-testnet-preflight',
    })
    try:
        with opener(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f'Public environment policy metadata: HTTP {error.code}') from error
    except URLError as error:
        raise RuntimeError('Public environment policy metadata is unavailable') from error


def verify_environment(repo, get=api, read_public=public_api):
    environment = get(f'repos/{repo}/environments/live-package-upgrade')
    branches = read_public(f'repos/{repo}/environments/live-package-upgrade/deployment-branch-policies')
    if not environment_ready(environment, branches):
        raise RuntimeError('dedicated testnet environment must be main-only, without reviewers or admin bypass')
    return {'environment': 'live-package-upgrade', 'branch': 'main',
            'policy_id': branches['branch_policies'][0]['id'],
            'branch_policy_transport': 'public HTTPS, no authorization header',
            'required_reviewers': False, 'admin_bypass': False}


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
    return (type(branches.get('total_count')) is int and branches['total_count'] == 1
            and environment.get('can_admins_bypass') is False and not reviewers
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
    parser.add_argument('--probe-environment', action='store_true', help='keyless public-metadata preflight; never publishes a test status or spends gas')
    parser.add_argument('--recheck', action='store_true')
    parser.add_argument('--status', choices=['pending', 'success', 'failure'])
    args = parser.parse_args()
    repo = os.environ['GITHUB_REPOSITORY']
    if args.probe_environment:
        if args.status or args.recheck:
            parser.error('probe cannot publish status or authorize a run')
        print(json.dumps(verify_environment(repo), sort_keys=True))
    elif args.status:
        status(repo, os.environ['TEST_HEAD'], os.environ['BASE_REF'], args.status,
               f'https://github.com/{repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}')
    else:
        with open(os.environ['GITHUB_EVENT_PATH']) as stream:
            selection = plan(os.environ['GITHUB_EVENT_NAME'], json.load(stream), repo)
        if args.recheck and (not selection or selection['head'] != os.environ['TEST_HEAD']
                             or selection['base'] != os.environ['BASE_REF']):
            raise RuntimeError('superseded head/base: no testnet mutation submitted')
        if selection:
            verify_environment(repo)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in (selection or {}).items():
                print(f'{key}={value}', file=stream)
            print(f'run={str(bool(selection)).lower()}', file=stream)
