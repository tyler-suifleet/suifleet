#!/usr/bin/env python3
"""Trusted-main workflow planner. All PR trust decisions use server-side facts."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONTEXT = 'testnet/package-upgrade'
# Failure classes a status may carry. Only `source` (and unclassified) failures
# are source-repair candidates; the others are infrastructure, funding or a
# retained on-chain uncertainty that a rerun reconciles. Merge gates ignore the
# class: anything but exact-head/base success blocks.
FAILURE_CLASSES = ('source', 'infrastructure', 'funding', 'uncertain')
SHA = re.compile(r'^[0-9a-f]{40}$')
# Privileged reads retain the reviewed 900-second budget; keyless probes allow one hourly reset.
PUBLIC_RETRY_BUDGET = 900
PROBE_RETRY_BUDGET = 3700
PUBLIC_MAX_ATTEMPTS = 6
TRANSIENT_STATUS = (500, 502, 503, 504)
RATE_HEADERS = ('retry-after', 'x-ratelimit-limit', 'x-ratelimit-remaining',
                'x-ratelimit-reset', 'x-ratelimit-used', 'x-ratelimit-resource')
# Branch policy verified by this job's planning step, kept beside the preserved
# trusted controller in RUNNER_TEMP (never in the test revision's workspace).
ENVIRONMENT_RECORD = 'testnet-environment-policy.json'


class PublicRateLimited(RuntimeError):
    """The anonymous quota stayed exhausted beyond the retry budget."""


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


def rate_facts(error):
    """Whitelisted rate-limit headers only; values must be short tokens."""
    headers = error.headers or {}
    facts = {}
    for name in RATE_HEADERS:
        value = headers.get(name)
        if value is not None and re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', str(value).strip()):
            facts[name] = str(value).strip()
    return facts


def retry_delay(error, now):
    """Seconds to wait before retrying `error`, or None when it is not transient.

    Only confirmed rate limits (429, or 403 with exhausted quota, Retry-After or
    a rate-limit message) and transient 5xx responses are retryable. Any other
    403 is a real denial and fails immediately.
    """
    facts = rate_facts(error)
    code = error.code
    if code in TRANSIENT_STATUS:
        return 0.0
    if code not in (403, 429):
        return None
    if code == 403 and not ('retry-after' in facts or facts.get('x-ratelimit-remaining') == '0'):
        try:
            body = error.read(4096).decode('utf-8', 'replace').lower()
        except (OSError, ValueError, AttributeError):
            body = ''
        if 'rate limit' not in body:
            return None
    if facts.get('retry-after', '').isdigit():
        return float(facts['retry-after'])
    if facts.get('x-ratelimit-remaining') == '0' and facts.get('x-ratelimit-reset', '').isdigit():
        return max(0.0, int(facts['x-ratelimit-reset']) - now + 1)
    return 0.0


def public_api(path, opener=urlopen, sleep=time.sleep, clock=time.monotonic, wall=time.time,
               budget=PUBLIC_RETRY_BUDGET, attempts=PUBLIC_MAX_ATTEMPTS, log=None):
    """Read public metadata without forwarding the workflow token.

    GitHub documents deployment branch policies as public-readable. The
    Actions installation token is rejected by this endpoint in observed CI;
    no administrator credential is needed for a public repository.

    Anonymous reads share a per-runner-IP quota, so confirmed rate limits and
    transient transport/5xx failures are retried with header-aware backoff,
    bounded by `attempts` and `budget` seconds of total elapsed time. Every
    attempt fetches fresh metadata; exhaustion fails closed.
    """
    if not re.fullmatch(r'repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/environments/live-package-upgrade/deployment-branch-policies', path):
        raise ValueError('public metadata path is outside the dedicated environment')
    log = log or (lambda line: print(line, file=sys.stderr))
    start = clock()
    for attempt in range(1, attempts + 1):
        request = Request('https://api.github.com/' + path + '?per_page=100', headers={
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2026-03-10',
            'User-Agent': 'suifleet-testnet-preflight',
        })
        try:
            with opener(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            delay = retry_delay(error, wall())
            facts = rate_facts(error)
            reason = f'HTTP {error.code}' + (f' {json.dumps(facts, sort_keys=True)}' if facts else '')
            if delay is None:
                raise RuntimeError(f'Public environment policy metadata: {reason}') from error
            failure = error
        except (URLError, HTTPException, OSError) as error:
            reason, delay, failure = f'transport {type(error).__name__}', 0.0, error
        delay = max(delay, min(2.0 ** attempt, 30.0))
        elapsed = clock() - start
        if attempt == attempts or elapsed + delay > budget:
            limited = isinstance(failure, HTTPError) and failure.code in (403, 429)
            raise (PublicRateLimited if limited else RuntimeError)(
                f'Public environment policy metadata unavailable after {attempt} attempt(s) '
                f'in {elapsed:.0f}s (retry budget {budget}s, next wait {delay:.0f}s): {reason}') from failure
        log(f'testnet preflight: public policy read attempt {attempt} failed ({reason}); retrying in {delay:.0f}s')
        sleep(delay)


def verify_environment(repo, get=api, read_public=public_api, record=None, fallback=None):
    """Verify the dedicated environment is main-only without reviewers or bypass.

    `record` saves the verified branch policy for the same job. `fallback` lets
    the pre-spend recheck reuse that record only when the anonymous quota stays
    exhausted (shared runner IPs routinely drain it); the authenticated
    environment facts are always fetched afresh, and a missing or unready
    record fails closed. GitHub admits the job to the environment before any
    step runs, so the recheck guards against configuration drift only.
    """
    environment = get(f'repos/{repo}/environments/live-package-upgrade')
    transport = 'public HTTPS, no authorization header'
    try:
        branches = read_public(f'repos/{repo}/environments/live-package-upgrade/deployment-branch-policies')
    except PublicRateLimited:
        branches = load_record(fallback)
        if branches is None:
            raise
        print('testnet recheck: public quota exhausted; reusing branch policy verified by this job', file=sys.stderr)
        transport = 'planning-step record (public quota exhausted)'
    if not environment_ready(environment, branches):
        raise RuntimeError('dedicated testnet environment must be main-only, without reviewers or admin bypass')
    if record:
        with open(record, 'w') as stream:
            json.dump(branches, stream, sort_keys=True)
    return {'environment': 'live-package-upgrade', 'branch': 'main',
            'policy_id': branches['branch_policies'][0]['id'],
            'branch_policy_transport': transport,
            'required_reviewers': False, 'admin_bypass': False}


def load_record(path):
    if not path:
        return None
    try:
        with open(path) as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


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
    if run['event'] in ('push', 'workflow_dispatch', 'schedule') and run['head_branch'] == 'main':
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


def failure_class(outcome, evidence_dir='evidence', dependencies='success'):
    """Classify a failed run for repair scheduling.

    A revision whose dependencies fail to build is a source failure. A run that
    otherwise failed before the acceptance step executed is infrastructure.
    Otherwise the revision's own evidence may name a class; anything missing,
    malformed or unknown is treated as a source failure. A wrong class can only
    change who is asked to look at the failure, never the merge gate."""
    if dependencies == 'failure':
        return 'source'
    if outcome != 'failure':
        return 'infrastructure'
    found = []
    for path in sorted(Path(evidence_dir).glob('*.json'))[:16]:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 << 20:
                continue
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get('success') is False:
            found.append(value.get('failure_class'))
    if not found or any(value not in FAILURE_CLASSES for value in found):
        return 'source'
    for name in ('source', 'funding', 'uncertain', 'infrastructure'):
        if name in found:
            return name


def description(base, state, failure=None):
    assert SHA.fullmatch(base)
    if state == 'failure':
        assert failure in FAILURE_CLASSES
        return f'base:{base} class:{failure}'
    return f'base:{base}'


def status(repo, head, base, state, url, failure=None):
    assert SHA.fullmatch(head) and SHA.fullmatch(base)
    api(f'repos/{repo}/statuses/{head}', dict(state=state, context=CONTEXT,
        description=description(base, state, failure), target_url=url))


def verify_checkout(expected, run=subprocess.run):
    """Reject a changed or mismatched checkout before any revision code executes."""
    if not isinstance(expected, str) or not SHA.fullmatch(expected):
        raise RuntimeError('checkout expectation must be an immutable commit SHA')
    actual = run(['git', 'rev-parse', '--verify', 'HEAD'], text=True,
                 capture_output=True, check=True).stdout.strip()
    if actual != expected:
        raise RuntimeError('checkout does not match authenticated revision')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe-environment', action='store_true', help='keyless public-metadata preflight; never publishes a test status or spends gas')
    parser.add_argument('--recheck', action='store_true')
    parser.add_argument('--verify-checkout', action='store_true')
    # `error` marks a cancelled run: never a source failure, never success.
    parser.add_argument('--status', choices=['pending', 'success', 'failure', 'error'])
    args = parser.parse_args()
    repo = os.environ['GITHUB_REPOSITORY']
    if args.verify_checkout:
        if args.status or args.recheck or args.probe_environment:
            parser.error('checkout verification cannot perform other operations')
        verify_checkout(os.environ['TEST_HEAD'])
    elif args.probe_environment:
        if args.status or args.recheck:
            parser.error('probe cannot publish status or authorize a run')
        print(json.dumps(verify_environment(
            repo, read_public=lambda path: public_api(path, budget=PROBE_RETRY_BUDGET)),
            sort_keys=True))
    elif args.status:
        failure = failure_class(os.environ.get('ACCEPTANCE_OUTCOME', ''),
                                dependencies=os.environ.get('DEPENDENCIES_OUTCOME', '')) if args.status == 'failure' else None
        status(repo, os.environ['TEST_HEAD'], os.environ['BASE_REF'], args.status,
               f'https://github.com/{repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}', failure)
    else:
        with open(os.environ['GITHUB_EVENT_PATH']) as stream:
            selection = plan(os.environ['GITHUB_EVENT_NAME'], json.load(stream), repo)
        if args.recheck and (not selection or selection['head'] != os.environ['TEST_HEAD']
                             or selection['base'] != os.environ['BASE_REF']):
            raise RuntimeError('superseded head/base: no testnet mutation submitted')
        if selection:
            saved = os.path.join(os.environ['RUNNER_TEMP'], ENVIRONMENT_RECORD) if os.environ.get('RUNNER_TEMP') else None
            if args.recheck:
                verify_environment(repo, fallback=saved)
            else:
                verify_environment(repo, record=saved)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in (selection or {}).items():
                print(f'{key}={value}', file=stream)
            print(f'run={str(bool(selection)).lower()}', file=stream)
