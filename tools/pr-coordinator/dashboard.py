#!/usr/bin/env python3
"""Read-only local dashboard of agent workers, tickets and PR jobs.

Reads the atomic-ticketing and PR-coordinator SQLite stores, the private
coordinator configuration and existing job snapshot receipts. It opens every
database read-only, contacts GitHub only with explicit --refresh-github, and never outputs claim
tokens, claim blobs, prompts, PR bodies/comments or worker logs.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import textwrap
from urllib.parse import quote

ACTIVE_RUN = ('claiming', 'launching', 'running')
RETAINED = ('orphaned', 'quarantined', 'parked')
QUOTA_RETRIES = 4  # Same bound as coordinator.QUOTA_RETRIES (asserted by tests).
JOB_OPEN = ('reserved', 'running', 'retry', 'orphaned', 'quarantined', 'quota_wait', 'quota_launch')
ROLE = {'review': 'review', 'fix': 'repair'}
CI_MAX_AGE = 900
BATCH_TERMINAL = ('done', 'invalidated', 'failed')
BATCH_ACTIVE = ('collecting', 'building', 'branch_publishing', 'pr_creating', 'awaiting_ci',
                'awaiting_review', 'ready', 'approving_members', 'build_uncertain', 'publish_pending', 'published', 'reconciling')
PIPELINE_LABELS = {
    'actively-working': 'actively working', 'queued-in-batch': 'queued in batch',
    'changes-requested': 'changes requested', 'conflict': 'merge conflict',
    'ci-failed': 'CI failed', 'needs-source-review': 'needs source review',
    'awaiting-ci': 'awaiting CI', 'awaiting-acceptance': 'awaiting acceptance',
    'ready-for-batch': 'source ready for batch',
    'awaiting-evidence-review': 'awaiting evidence review', 'ready-to-merge': 'ready to merge',
    'unavailable': 'evidence unavailable',
}
_SOURCE_MODULE = None
# Dispatcher CI repair-first redirects (SF-116) that pause a live implementation.
REDIRECT_PAUSING = ('guarding', 'interrupting', 'releasing')
REDIRECT_PAUSED = ('stopped', 'integrating', 'resuming')
REDIRECT_REPAIRING = ('repairing', 'repair_stopping')
# Free-text fields (handoff evidence, job errors) are redacted before display.
# Claim and lock tokens are 32-hex; commit IDs are shown separately, shortened.
SECRET = re.compile(r'(?i)(gh[opsru]_[A-Za-z0-9]{16,}|github_pat_\w+|suiprivkey\w+|bearer\s+\S+'
                    r'|(?:token|secret|password|key)\s*[=:]\s*\S+|\b[0-9a-f]{32,}\b|[A-Za-z0-9+/_-]{40,}={0,2})')


def default_paths():
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state')
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')
    return dict(tickets=state/'atomic-ticketing/coordination.sqlite3',
                dispatch=state/'pr-coordinator/dispatch.sqlite3',
                reviews=state/'pr-coordinator/reviews.sqlite3',
                config=config/'pr-coordinator/config.json',
                inventory=state/'pr-coordinator/github-inventory')


def safe_text(value, limit=160):
    text = ' '.join(str(value or '').split())
    text = SECRET.sub('[redacted]', text)
    return text if len(text) <= limit else text[:limit-1] + '…'


class Unavailable(Exception):
    pass


def connect(path):
    """Open an existing store read-only; never create or migrate it."""
    path = Path(path)
    if not path.is_file():
        raise Unavailable('missing')
    try:
        db = sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('SELECT count(*) FROM sqlite_master').fetchone()
        return db
    except sqlite3.DatabaseError as error:
        raise Unavailable(f'unreadable ({type(error).__name__})') from error


def modified(path):
    times = []
    for p in (Path(path), Path(str(path)+'-wal')):
        try:
            times.append(p.stat().st_mtime)
        except OSError:
            pass
    return max(times) if times else None


class Processes:
    """One /proc scan per refresh: identity is the recorded PID *and* start time."""

    def __init__(self, root='/proc'):
        self.root = Path(root)
        self.sessions = {}
        self.readable = True
        self.boot = None
        try:
            for line in (self.root/'stat').read_text().splitlines():
                if line.startswith('btime '):
                    self.boot = float(line.split()[1])
            for entry in self.root.iterdir():
                if entry.name.isdigit():
                    fields = self.stat(entry.name)
                    if fields and fields[0] not in 'ZX':
                        self.sessions[fields[3]] = self.sessions.get(fields[3], 0) + 1
        except (OSError, ValueError, IndexError):
            self.readable = False
        try:
            self.ticks = os.sysconf('SC_CLK_TCK')
        except (ValueError, OSError):
            self.ticks = 100

    @staticmethod
    def identity(value):
        """A recorded PID or start time as a positive decimal string, else None."""
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        value = str(value).strip()
        return value if value.isascii() and value.isdigit() and int(value) > 0 else None

    def stat(self, pid):
        try:
            fields = (self.root/str(int(pid))/'stat').read_text().rsplit(')', 1)[1].split()
            return fields if len(fields) > 19 else None
        except (OSError, ValueError, TypeError, IndexError):
            return None

    def state(self, pid, started):
        """alive | zombie | lingering | exited | unknown (no valid identity) | unreadable."""
        pid, started = self.identity(pid), self.identity(started)
        if not pid or not started:
            return 'unknown'
        if not self.readable:
            return 'unreadable'
        fields = self.stat(pid)
        if fields and fields[19] == str(started):
            if fields[0] in 'ZX':
                return 'zombie'
            return 'alive'
        # Wrapper gone (or PID reused): a surviving member of its session still
        # holds capacity, but it is not a verified worker.
        return 'lingering' if self.sessions.get(str(pid)) else 'exited'

    def started_at(self, started):
        try:
            return self.boot + int(started)/self.ticks if self.boot else None
        except (TypeError, ValueError):
            return None


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def receipt(directory, name):
    # A missing directory must never fall back to a relative path in the cwd.
    return Path(directory)/name if isinstance(directory, str) and os.path.isabs(directory) else None


def short_key(key):
    return str(key).split('::', 1)[-1] if key else None


def source_fingerprint(data):
    """Use the coordinator's canonical receipt binding, never review prose.

    The separately installed dashboard also supports older coordinator generations:
    an unavailable helper means unknown source evidence, not inferred approval.
    """
    global _SOURCE_MODULE
    if (not all(k in data for k in ('headRefOid', 'comments', 'reviews', 'threads'))
            or not all(isinstance(data[k], list) for k in ('comments', 'reviews', 'threads'))):
        return None
    try:
        directory = Path(__file__).resolve().parent
        path = directory/'current/coordinator.py'
        if not path.is_file():
            path = directory/'coordinator.py'
        info = path.stat()
        stamp = (str(path.resolve()), info.st_mtime_ns, info.st_size)
        if _SOURCE_MODULE is None or _SOURCE_MODULE[0] != stamp:
            spec = importlib.util.spec_from_file_location('dashboard_source_receipts', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _SOURCE_MODULE = (stamp, module)
        helper = getattr(_SOURCE_MODULE[1], 'source_feedback', None)
        return helper(data) if helper else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError, SyntaxError):
        return None


class SnapshotCache:
    """Public projection of PR job snapshots, reparsed only when a file changes."""

    def __init__(self):
        self.entries = {}

    def get(self, directory):
        path = receipt(directory, 'snapshot.json')
        try:
            info = path.stat()
        except (OSError, AttributeError):
            return None
        stamp = (info.st_mtime_ns, info.st_size)
        cached = self.entries.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        data = read_json(path)
        try:
            projection = project_snapshot(data, info.st_mtime)
        except ValueError:
            projection = {'corrupt': True, 'mtime': info.st_mtime}
        self.entries[path] = (stamp, projection)
        return projection


def project_snapshot(data, mtime):
    """Validate every nested field used; a malformed receipt is reported corrupt."""
    if not isinstance(data, dict):
        raise ValueError('snapshot is not an object')
    checks = data.get('statusCheckRollup') or []
    if not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks):
        raise ValueError('malformed status checks')
    outcome = {'success': 0, 'pending': 0, 'failed': 0}
    for check in checks:
        value = str(check.get('conclusion') or check.get('state') or check.get('status') or '').upper()
        if value in ('SUCCESS', 'NEUTRAL', 'SKIPPED'):
            outcome['success'] += 1
        elif value in ('', 'PENDING', 'QUEUED', 'IN_PROGRESS', 'EXPECTED', 'WAITING', 'REQUESTED'):
            outcome['pending'] += 1
        else:
            outcome['failed'] += 1
    testnet = data.get('_testnet_status') if isinstance(data.get('_testnet_status'), dict) else {}
    ticket = data.get('ticket') if isinstance(data.get('ticket'), dict) else {}
    spec = ticket.get('spec') if isinstance(ticket.get('spec'), dict) else {}
    number, branch = data.get('number'), data.get('headRefName')
    if (number is not None and (not isinstance(number, int) or isinstance(number, bool))) \
            or (branch is not None and not isinstance(branch, str)) \
            or (ticket.get('key') is not None and not isinstance(ticket.get('key'), str)):
        raise ValueError('malformed snapshot identity')
    state = testnet.get('state')
    full_head = data.get('headRefOid')
    full_base = data.get('baseRefOid')
    full_head = full_head if isinstance(full_head, str) and re.fullmatch(r'[0-9a-f]{40}', full_head) else None
    full_base = full_base if isinstance(full_base, str) and re.fullmatch(r'[0-9a-f]{40}', full_base) else None
    threads = data.get('threads')
    unresolved = (any(not t.get('isResolved') for t in threads)
                  if isinstance(threads, list) and all(isinstance(t, dict) and type(t.get('isResolved')) is bool for t in threads)
                  else None)
    return dict(pr=number, title=safe_text(data.get('title'), 120), branch=branch,
                head=str(data.get('headRefOid') or '')[:12], draft=bool(data.get('isDraft')),
                head_oid=full_head, base_oid=full_base, source_head=full_head,
                source_fingerprint=source_fingerprint(data), unresolved_threads=unresolved,
                mergeable=data.get('mergeable') if data.get('mergeable') in ('MERGEABLE', 'CONFLICTING', 'UNKNOWN') else None,
                ticket=short_key(ticket.get('key')), ticket_title=safe_text(spec.get('title'), 120) or None,
                checks=outcome, testnet=(safe_text(state, 30) or 'missing') if data.get('_testnet_required') else None,
                mtime=mtime)


def inventory_path(directory, project):
    return Path(directory)/(quote(project, safe='') + '.json')


def inventory_items(items):
    if not isinstance(items, list):
        raise ValueError('invalid inventory')
    result = []
    seen = set()
    for row in items:
        number = row.get('number') if isinstance(row, dict) else None
        if type(number) is not int or number <= 0 or number in seen:
            raise ValueError('invalid or duplicate issue number')
        seen.add(number)
        title = safe_text(row.get('title'), 120)
        match = re.search(r'\b[A-Z][A-Z0-9]*-\d+\b', title)
        result.append(dict(number=number, title=title, ticket=match.group() if match else None,
                           draft=bool(row.get('draft', row.get('isDraft', False)))))
    return sorted(result, key=lambda r: r['number'])


def fetch_ci(repository, prs, run, env, branch='main'):
    """One bounded GraphQL request per 25 PRs; never infer complete partial data."""
    owner, repo = repository.split('/')
    result = {}
    if len(prs) > 1000:
        raise ValueError('CI inventory exceeds bound')
    for offset in range(0, len(prs), 25):
        batch = prs[offset:offset+25]
        fields = ' '.join('p%d:pullRequest(number:%d){number state headRefOid baseRefOid baseRef{target{oid}} headRefName isDraft title commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:100){pageInfo{hasNextPage} nodes{__typename ...on CheckRun{name status conclusion} ...on StatusContext{context state description}}}}}}}}' % (p['number'], p['number']) for p in batch)
        query = 'query{repository(owner:%s,name:%s){testnet:object(expression:%s){oid} %s}}' % (
            json.dumps(owner), json.dumps(repo), json.dumps(branch+':.github/workflows/testnet.yml'), fields)
        response = json.loads(run(['api', 'graphql', '--input', '-'], env, input=json.dumps({'query': query})))
        if response.get('errors'):
            raise ValueError('partial CI response')
        data = response['data']['repository']
        for pr in batch:
            row = data['p'+str(pr['number'])]
            if not isinstance(row, dict) or row.get('number') != pr['number'] or row.get('state') != 'OPEN':
                raise ValueError('PR changed during refresh')
            if not all(re.fullmatch(r'[0-9a-f]{40}', row.get(k, '')) for k in ('headRefOid', 'baseRefOid')):
                raise ValueError('missing CI revision')
            # baseRefOid may retain the PR comparison base after the branch advances.
            base = ((row.get('baseRef') or {}).get('target') or {}).get('oid')
            if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}', base):
                raise ValueError('missing current CI base revision')
            commits = row['commits']['nodes']
            if len(commits) != 1 or commits[0]['commit']['oid'] != row['headRefOid']:
                raise ValueError('CI head changed during refresh')
            rollup = commits[0]['commit']['statusCheckRollup']
            contexts = rollup['contexts'] if rollup else {'nodes': [], 'pageInfo': {'hasNextPage': False}}
            if contexts['pageInfo']['hasNextPage']:
                raise ValueError('CI context pagination exceeds bound')
            checks = contexts['nodes']
            if not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks):
                raise ValueError('invalid CI contexts')
            testnet = [c for c in checks if c.get('context') == 'testnet/package-upgrade']
            if len(testnet) > 1:
                raise ValueError('ambiguous testnet status')
            status = testnet[0] if testnet else {}
            description = status.get('description') or ''
            prefix = 'base:'+base
            state = str(status.get('state', 'missing')).lower()
            if status and (not re.fullmatch(re.escape(prefix)+r'(?: class:(?:source|infrastructure|funding|uncertain))?', description)
                           or state == 'success' and description != prefix):
                state = 'stale-base'
            row = {k: row[k] for k in ('number', 'title', 'headRefOid', 'baseRefOid', 'headRefName', 'isDraft')}
            row.update(statusCheckRollup=[c for c in checks if c.get('context') != 'testnet/package-upgrade'],
                       _testnet_required=data.get('testnet') is not None,
                       _testnet_status={'state': state})
            projection = project_snapshot(row, time.time())
            projection.update(head_oid=row['headRefOid'], base_oid=base, source='GitHub')
            result[str(pr['number'])] = projection
    return result


def refresh_github(paths, project=None):
    """Explicit opt-in REST inventory read. Never used by the live refresh loop.

    Fetch both complete lists before atomically replacing a project's cache.
    Use its configured account; do not fall back to another logged-in identity.
    """
    import subprocess
    import tempfile
    config = read_json(paths['config'])
    if not isinstance(config, dict) or not isinstance(config.get('projects'), list):
        raise Unavailable('configuration unavailable')
    projects = [p for p in config['projects'] if isinstance(p, dict) and (project is None or p.get('id') == project)]
    if not projects:
        raise Unavailable('no matching configured project')
    completed = []
    for p in projects:
        name, repository, account = p.get('id'), p.get('repository'), p.get('github_account')
        if not all(isinstance(v, str) and v for v in (name, repository, account)) or not re.fullmatch(r'[\w.-]+/[\w.-]+', repository):
            raise Unavailable('project repository/account missing or invalid')
        def run(args, env=None, **kwargs):
            return subprocess.run(['gh', *args], env=env, capture_output=True, text=True, check=True, timeout=60, **kwargs).stdout
        try:
            token = run(['auth', 'token', '--hostname', 'github.com', '--user', account]).strip()
            if not token:
                raise ValueError('missing authentication')
            env = {**os.environ, 'GH_TOKEN': token, 'GH_HOST': 'github.com'}
            def listing(endpoint):
                pages = json.loads(run(['api', f'repos/{repository}/{endpoint}?state=open&per_page=100', '--paginate', '--slurp'], env))
                if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
                    raise ValueError('incomplete inventory')
                rows = [r for page in pages for r in page]
                if not all(isinstance(r, dict) and r.get('state') == 'open' for r in rows):
                    raise ValueError('invalid inventory')
                return rows
            issues = inventory_items([r for r in listing('issues') if 'pull_request' not in r])
            prs = inventory_items(listing('pulls'))
            ci = fetch_ci(repository, prs, run, env, p.get('integration_branch', 'main'))
            data = dict(schema=1, project=name, repository=repository, fetched_at=time.time(), issues=issues, prs=prs, ci=ci)
            path = inventory_path(paths['inventory'], name)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(data, handle)
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            completed.append(name)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            # gh stderr can contain credentials: report only the exception type.
            raise Unavailable(f'{safe_text(name)} refresh failed ({type(error).__name__}); previous snapshot retained') from error
    return completed


def reconcile_inventory(paths, config, by_ticket, now, project, ticket_pr=None):
    result = {}
    configured = {p['id']: p for p in config.get('projects', []) if isinstance(p, dict) and isinstance(p.get('id'), str)}
    projects = set(configured) | {p for p, _ in by_ticket}
    if project:
        projects = {project}
    for name in sorted(projects):
        local = {key: row for (p, key), row in by_ticket.items() if p == name and row['status'] != 'done'}
        counts = {}
        for row in local.values():
            counts[row['status']] = counts.get(row['status'], 0) + 1
        entry = dict(state='unavailable', tracked_open=len(local), breakdown=counts,
                     github_open_issues=None, github_open_prs=None)
        result[name] = entry
        raw = read_json(inventory_path(paths['inventory'], name))
        try:
            if not isinstance(raw, dict) or raw.get('schema') != 1 or raw.get('project') != name:
                continue
            repository = configured.get(name, {}).get('repository')
            if not repository or raw.get('repository') != repository:
                continue
            stamp = raw['fetched_at']
            if type(stamp) not in (float, int) or not 0 < stamp <= now + 300:
                continue
            issues, prs = inventory_items(raw['issues']), inventory_items(raw['prs'])
            # Compare GitHub issue numbers, not title guesses or PR job counts.
            tracked_numbers = {}
            for key, row in local.items():
                url = row['remote'].get('url', '')
                match = re.fullmatch(r'https://github\.com/' + re.escape(repository) + r'/issues/(\d+)', str(url))
                if match:
                    tracked_numbers[int(match[1])] = key
            remote_numbers = {r['number'] for r in issues}
            active_keys = set(local)
            receipt_prs = {number for (p, key), number in (ticket_pr or {}).items() if p == name and key in active_keys}
            linked = [r for r in prs if any(re.search(r'\b' + re.escape(key) + r'\b', r['title']) for key in active_keys)
                      or r['number'] in receipt_prs]
            entry.update(state='stale' if now - stamp > 1800 else 'ok', fetched_at=stamp, repository=repository,
                         github_open_issues=len(issues), github_open_prs=len(prs),
                         linked_prs=len(linked), other_prs=[r['number'] for r in prs if r not in linked],
                         untracked_issues=sorted(remote_numbers - set(tracked_numbers)),
                         absent_from_github=[tracked_numbers[n] for n in sorted(set(tracked_numbers) - remote_numbers)],
                         unpublished_tickets=sorted(set(local) - set(tracked_numbers.values())))
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            entry['state'] = 'unavailable'
    return result


def render_inventory(model, width):
    lines = ['', 'GITHUB ISSUES / PULL REQUESTS']
    ticket_ok = model['sources'].get('tickets', {}).get('state') == 'ok'
    for project, entry in model.get('inventory', {}).items():
        counts = entry['breakdown']
        if entry['state'] == 'unavailable':
            lines.append(f'  {project}: GitHub open issues ? · open PRs ? (no usable snapshot)')
        else:
            lines.append(f"  {project}: {entry['github_open_issues']} open issues · {entry['github_open_prs']} open PRs")
            lines.append(f"  GitHub REST snapshot: {fmt_age(model['generated_at'] - entry['fetched_at'])} ago"
                         + (' · STALE' if entry['state'] == 'stale' else '') + ' · not a live query')
            lines.append(f"  PRs: {entry['linked_prs']} linked to open tickets · {len(entry['other_prs'])} other")
            if entry['other_prs']:
                lines.append('  Other PRs: ' + ', '.join(f'#{n}' for n in entry['other_prs']))
        if ticket_ok:
            lines.append(f"  Tracked open tickets: {entry['tracked_open']} = " + ' + '.join(
                f"{counts.get(s, 0)} {s.replace('_', ' ')}" for s in ('backlog', 'in_progress', 'review', 'blocked')))
            extra = {k: v for k, v in counts.items() if k not in ('backlog', 'in_progress', 'review', 'blocked')}
            if extra:
                lines[-1] += ' + ' + ' + '.join(f'{v} {k}' for k, v in sorted(extra.items()))
            for field, label in [('untracked_issues', 'GitHub issues outside open local tracking'),
                                 ('absent_from_github', 'Local tickets absent from GitHub open snapshot'),
                                 ('unpublished_tickets', 'Local tickets without matching GitHub issue URL')]:
                if entry.get(field):
                    values = [f'#{v}' if isinstance(v, int) else v for v in entry[field]]
                    lines.append('  ! ' + label + ': ' + ', '.join(values))
        else:
            lines.append('  Tracked open tickets: unknown (ticket store unavailable)')
    if not model.get('inventory'):
        lines.append('  No configured projects or tickets.')
    lines.append(f"  Running workers: {len(model['workers'])} verified · activity on these tickets/PRs; do not add to issue totals")
    lines.append('  Update GitHub counts: fleet-status --refresh-github --once (normal refresh stays offline)')
    return [part for line in lines for part in (textwrap.wrap(line, width, subsequent_indent='    ') or [''])]


def ci_state(snapshot, now=None):
    if not snapshot or snapshot.get('corrupt'):
        return None
    if now is not None and now - snapshot['mtime'] > CI_MAX_AGE:
        return 'ci-stale'
    if snapshot.get('ci_mismatch'):
        return 'ci-unknown'
    checks = snapshot['checks']
    if checks['failed'] or snapshot.get('testnet') in ('failure', 'error'):
        return 'ci-failed'
    if snapshot.get('testnet') in ('missing', 'stale-base') or not sum(checks.values()):
        return 'ci-unknown'
    if checks['pending'] or (snapshot.get('testnet') not in (None, 'success')):
        return 'ci-pending'
    return 'ci-passed'


def cached_ci(paths, config, now, project):
    result = {}
    for p in config.get('projects', []):
        if project is not None and p['id'] != project:
            continue
        raw = read_json(inventory_path(paths['inventory'], p['id']))
        if not isinstance(raw, dict) or raw.get('schema') != 1 or raw.get('repository') != p.get('repository') or raw.get('project') != p['id']:
            continue
        try:
            open_prs = {row['number'] for row in inventory_items(raw['prs'])}
            for number, value in raw.get('ci', {}).items():
                number = int(number)
                if (number not in open_prs or value['pr'] != number or value.get('source') != 'GitHub'
                        or not all(re.fullmatch(r'[0-9a-f]{40}', value[k]) for k in ('head_oid', 'base_oid'))
                        or type(value['mtime']) not in (int, float) or not 0 < value['mtime'] <= now+300
                        or set(value['checks']) != {'success', 'pending', 'failed'}
                        or not all(type(v) is int and 0 <= v <= 100 for v in value['checks'].values())):
                    continue
                result[(p['id'], number)] = {k: value[k] for k in
                    ('pr', 'title', 'branch', 'head', 'head_oid', 'base_oid', 'draft', 'checks', 'testnet', 'mtime', 'source')}
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return result


def observed_prs(paths, config, now, project):
    """Fresh coordinator decision projections; never request GitHub here."""
    result = {}
    for settings in config.get('projects', []):
        name = settings.get('id')
        if (project is not None and name != project) or not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            continue
        directory = Path(paths['reviews']).parent/name/'pr-observations'
        for path in directory.glob('pr-*.json'):
            try:
                if path.is_symlink() or path.stat().st_size > 262144:
                    continue
                raw = read_json(path)
                if (raw.get('schema') != 1 or raw.get('project') != name or raw.get('repository') != settings.get('repository')
                        or type(raw.get('observed')) not in (int, float) or not 0 < raw['observed'] <= now+300
                        or not isinstance(raw.get('source_fingerprint'), str)
                        or not re.fullmatch(r'[0-9a-f]{64}', raw['source_fingerprint'])):
                    continue
                view = project_snapshot(raw['pr'], raw['observed'])
                if type(view['pr']) is not int or view['pr'] <= 0 or not view['head_oid'] or not view['base_oid']:
                    continue
                view.update(source_fingerprint=raw['source_fingerprint'], source='coordinator observation',
                            closed=raw['pr'].get('state') != 'OPEN')
                result[(name, view['pr'])] = view
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
    return result


def batch_receipts(db):
    """Public, read-only projection; old installations need no migration."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='integration_batches'").fetchone():
        return {'state': 'unavailable', 'detail': 'batch receipt schema not installed', 'rows': []}
    try:
        rows = []
        for raw in db.execute('SELECT project,id,status,base,head,pr,members,updated FROM integration_batches'):
            row = dict(raw)
            if row['status'] in BATCH_TERMINAL:
                continue
            if (row['status'] not in BATCH_ACTIVE or not isinstance(row['project'], str)
                    or not isinstance(row['id'], str) or not row['id']
                    or not isinstance(row['base'], str) or not re.fullmatch(r'[0-9a-f]{40}', row['base'])
                    or row['pr'] is not None and (type(row['pr']) is not int or row['pr'] <= 0)
                    or row['head'] is not None and (not isinstance(row['head'], str)
                                                   or not re.fullmatch(r'[0-9a-f]{40}', row['head']))):
                raise ValueError('invalid batch identity')
            members = json.loads(row['members'])
            if (not isinstance(members, list) or not all(isinstance(m, dict) and type(m.get('pr')) is int
                    and m['pr'] > 0 and isinstance(m.get('head'), str)
                    and re.fullmatch(r'[0-9a-f]{40}', m['head']) for m in members)):
                raise ValueError('invalid batch member projection')
            if len({m['pr'] for m in members}) != len(members):
                raise ValueError('duplicate batch member')
            # Never expose full manifests, owner receipts, claim hashes or error strings.
            row['members'] = [{'pr': m['pr'], 'head': m['head']} for m in members]
            row['id'] = safe_text(row['id'], 48)
            rows.append(row)
        return {'state': 'ok', 'rows': rows}
    except (sqlite3.DatabaseError, ValueError, TypeError, KeyError):
        return {'state': 'unavailable', 'detail': 'batch receipts unreadable', 'rows': []}


def pipeline_stage(snapshot, review, *, active=False, batch=None, candidate=False, now=None, batch_mode=False):
    """One explanatory state per PR. This projection never grants merge authority."""
    if active:
        return 'actively-working', 'PID-verified worker assigned to this PR'
    if not snapshot or snapshot.get('corrupt') or snapshot.get('ci_mismatch'):
        return 'unavailable', 'current PR snapshot unavailable or revisions disagree'
    if now is not None and now - snapshot.get('mtime', 0) > CI_MAX_AGE:
        return 'unavailable', 'PR evidence is stale; refresh GitHub for current checks'
    if batch is not None:
        if batch.get('status') == 'build_uncertain':
            return 'unavailable', 'interrupted candidate build is retained for verified recovery'
        if (snapshot.get('base_oid') != batch.get('base')
                or (candidate and snapshot.get('head_oid') != batch.get('head'))):
            return 'unavailable', 'batch revision no longer matches current PR evidence'
        if not candidate:
            return 'queued-in-batch', f"held in integration batch {batch['id']} ({safe_text(batch['status'], 30)})"
    if snapshot.get('mergeable') == 'CONFLICTING':
        return 'conflict', 'candidate has a recorded merge conflict'
    if snapshot.get('unresolved_threads') is True or (review or {}).get('verdict') == 'changes_requested':
        return 'changes-requested', 'unresolved review feedback needs an implementation change'
    checks = snapshot.get('checks') or {}
    if checks.get('failed', 0) or snapshot.get('testnet') in ('failure', 'error'):
        return 'ci-failed', 'ordinary or testnet validation failed'
    if candidate:
        if checks.get('pending', 0) or not sum(checks.values()):
            return 'awaiting-ci', 'combined candidate CI is pending'
        if snapshot.get('testnet') not in (None, 'success'):
            return 'awaiting-acceptance', 'combined candidate testnet evidence is pending or stale'
        if batch['status'] == 'ready':
            return 'ready-to-merge', 'coordinator recorded combined integration readiness; publication rechecks still apply'
        return 'awaiting-evidence-review', 'combined candidate awaits independent integration evidence review'
    if not snapshot.get('head_oid') or not snapshot.get('source_fingerprint'):
        return 'unavailable', 'source-review identity or feedback evidence is unavailable'
    if not review or review.get('source_complete') is not True:
        return 'needs-source-review', 'no current published typed source-complete receipt'
    if batch_mode and review.get('acceptance_ready') is True:
        return 'ready-for-batch', 'source receipt ready; combined candidate will supply CI and testnet evidence'
    if checks.get('pending', 0) or not sum(checks.values()):
        return 'awaiting-ci', 'source review complete; ordinary CI is pending'
    if review.get('acceptance_ready') is not True:
        return 'awaiting-acceptance', 'source review complete; ticket acceptance evidence remains outstanding'
    if snapshot.get('testnet') not in (None, 'success'):
        return 'awaiting-acceptance', 'ordinary CI passed; current-base testnet evidence is pending or stale'
    if (review.get('verdict') == 'approve' and review.get('acceptance_complete') is True
            and review.get('base_oid') and review['base_oid'] == snapshot.get('base_oid')
            and snapshot.get('mergeable') == 'MERGEABLE' and snapshot.get('unresolved_threads') is False):
        return 'ready-to-merge', 'published complete review and recorded checks passed; coordinator rechecks before merging'
    return 'awaiting-evidence-review', 'source review complete; final acceptance/evidence review is outstanding'


def derive_pipeline(snapshots, jobs, tickets, workers, batches, cache, now, batch_projects=()):
    """Derived PR states are independent of authoritative ticket/issue totals."""
    active = {(w['project'], w.get('pr')) for w in workers if w.get('pr')}
    memberships, candidates = {}, {}
    ambiguous = set()
    for row in batches.get('rows', []):
        if row.get('pr'):
            candidates[(row['project'], row['pr'])] = row
        for member in row['members']:
            key = (row['project'], member['pr'])
            if key in memberships:
                ambiguous.add(key)
            memberships[key] = (row, member['head'])
    rows = []
    for key, snapshot in sorted(snapshots.items()):
        owner = next((t for (p, _), t in tickets.items() if p == key[0]
                      and (short_key(t['key']) == snapshot.get('ticket') or t.get('branch') == snapshot.get('branch'))), None)
        review = None
        for job in reversed(jobs):
            if ((job['project'], job['pr']) != key or job['kind'] != 'review' or job['status'] != 'done'
                    or not job.get('publication_done') or job.get('head') != snapshot.get('head_oid')):
                continue
            evidence = cache.get(job['directory'])
            if (not evidence or not snapshot.get('source_fingerprint')
                    or evidence.get('source_fingerprint') != snapshot['source_fingerprint']):
                continue
            if owner is None or job['family'] == owner.get('agent') or job['family'] not in ('Codex', 'Claude'):
                continue
            try:
                result = json.loads(job.get('result') or '{}')
                if not isinstance(result, dict):
                    continue
                review = {k: result.get(k) for k in ('verdict', 'source_complete', 'acceptance_ready', 'acceptance_complete')}
                review['base_oid'] = evidence.get('base_oid')
                break
            except (TypeError, ValueError):
                continue
        member = memberships.get(key)
        batch = candidates.get(key) or (member[0] if member else None)
        if key in active:
            stage, reason = pipeline_stage(snapshot, review, active=True, now=now)
        elif key in ambiguous or member and member[1] != snapshot.get('head_oid') or member and key in candidates:
            stage, reason = 'unavailable', 'batch membership is ambiguous or source head has changed'
        else:
            stage, reason = pipeline_stage(snapshot, review, active=key in active, batch=batch,
                                           candidate=key in candidates, now=now, batch_mode=key[0] in batch_projects)
        rows.append(dict(project=key[0], pr=key[1], ticket=short_key(owner['key']) if owner else None,
                         integration=key in candidates, state=stage, reason=reason,
                         title=snapshot.get('title') or '', observed=snapshot.get('mtime')))
    counts = {}
    for row in rows:
        counts[row['state']] = counts.get(row['state'], 0) + 1
    return dict(rows=rows, counts=counts, total=len(rows), batch_receipts=batches['state'],
                detail=batches.get('detail'))


def collect(paths=None, project=None, now=None, proc='/proc', cache=None):
    paths = {**default_paths(), **(paths or {})}
    now = time.time() if now is None else now
    cache = cache or SnapshotCache()
    processes = Processes(proc)
    model = dict(generated_at=now, project_filter=project, sources={}, ticket_refresh={}, capacity=[],
                 workers=[], attention=[], queue={}, locks=[], warnings=[])
    if not processes.readable:
        model['warnings'].append('Process table unreadable: no worker can be verified active')

    def source(name, fn):
        try:
            result = fn()
            model['sources'][name] = dict(state='ok', modified_at=modified(paths[name]))
            return result
        except Unavailable as error:
            model['sources'][name] = dict(state='unavailable', detail=str(error))
        except (sqlite3.DatabaseError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            model['sources'][name] = dict(state='unavailable', detail=f'unreadable ({type(error).__name__})')
        return None

    def wanted(row_project):
        return project is None or row_project == project

    def load_config():
        path = Path(paths['config'])
        if not path.is_file():
            raise Unavailable('missing')
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError('config is not an object')
        projects = data.get('projects', [])
        if not isinstance(projects, list) or not all(isinstance(p, dict) for p in projects):
            raise ValueError('malformed projects')
        slots = data.get('implementation_slots', [])
        limits = data.get('review_worker_limits', {})
        if not isinstance(slots, list) or not all(isinstance(s, dict) and isinstance(s.get('family'), str) for s in slots):
            raise ValueError('malformed implementation_slots')
        if not isinstance(limits, dict) or not all(isinstance(v, dict) for v in limits.values()):
            raise ValueError('malformed review_worker_limits')
        return data

    def load_tickets():
        db = connect(paths['tickets'])
        try:
            # Explicit columns: claim tokens are never selected.
            rows = db.execute('SELECT key,project,spec,remote,status,agent,model,session,heartbeat,expires,branch,evidence FROM tickets').fetchall()
            refresh = {r['key'][len('snapshot_at:'):]: float(r['value'])
                       for r in db.execute("SELECT key,value FROM metadata WHERE key LIKE 'snapshot_at:%'")}
            locks = db.execute('SELECT resource,agent,model,session,project,ticket,purpose,acquired,expires FROM locks WHERE expires>?', (now,)).fetchall()
            return [dict(r) for r in rows], refresh, [dict(r) for r in locks]
        finally:
            db.close()

    def load_dispatch():
        db = connect(paths['dispatch'])
        try:
            runs = [dict(r) for r in db.execute("SELECT session,project,family,status,directory,claim,pid,started,message FROM runs WHERE status!='finished'")]
            backoff = [dict(r) for r in db.execute('SELECT project,family,attempts,retry_after,reason FROM backoff')]
            redirects = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='ci_redirects'").fetchone():
                # Explicit columns: the claim digest and guard lease tokens are never selected.
                redirects = [dict(r) for r in db.execute('SELECT id,source,project,family,ticket,pr,head,fingerprint,failures,state,'
                                                         'reason,repair_dir,resumed_as,updated FROM ci_redirects ORDER BY id')]
            capacities = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='capacity_retries'").fetchone():
                kinds = db.execute("SELECT 1 FROM sqlite_master WHERE name='capacity_kinds'").fetchone()
                capacities = [dict(r) for r in db.execute(
                    "SELECT c.source,c.state,c.attempt,c.retry_after,c.reason,c.replacement,c.updated,"
                    + ("COALESCE(k.kind,'capacity') AS kind FROM capacity_retries c LEFT JOIN capacity_kinds k ON k.source=c.source"
                       if kinds else "'capacity' AS kind FROM capacity_retries c"))]
            return runs, backoff, redirects, capacities
        finally:
            db.close()

    def load_reviews():
        db = connect(paths['reviews'])
        try:
            columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
            optional = ', '.join(c if c in columns else f'NULL AS {c}' for c in
                                 ('error', 'created', 'retry_after', 'attempts', 'result', 'publication_done'))
            jobs = [dict(r) for r in db.execute(f'SELECT id,project,pr,family,kind,head,status,directory,pid,started,{optional} FROM jobs ORDER BY id')]
            merged = set()
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='integrations'").fetchone():
                merged = {(r[0], r[1]) for r in db.execute('SELECT project,pr FROM integrations WHERE reconciled=1')}
            pauses = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='quarantine_backoff'").fetchone():
                pauses = [dict(r) for r in db.execute('SELECT family,kind,attempts,retry_after FROM quarantine_backoff')]
            observations = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='ci_observations'").fetchone():
                observations = [dict(r) for r in db.execute('SELECT project,pr,ticket,owner_status,head,draft,failures,fingerprint,'
                                                            'responsibility,observed FROM ci_observations ORDER BY project,pr')]
            return jobs, merged, pauses, observations, batch_receipts(db)
        finally:
            db.close()

    config = source('config', load_config) or {}
    tickets_data = source('tickets', load_tickets)
    dispatch_data = source('dispatch', load_dispatch)
    reviews_data = source('reviews', load_reviews)

    tickets, refresh, locks = tickets_data or ([], {}, [])
    runs, backoff, redirects, capacities = dispatch_data or ([], [], [], [])
    capacity_for = {r["source"]: r for r in capacities}
    jobs, merged, pauses, observations, batches = reviews_data or (
        [], set(), [], [], {'state': 'unavailable', 'detail': 'review store unavailable', 'rows': []})
    batches['rows'] = [b for b in batches['rows'] if wanted(b['project'])]
    by_ticket = {}
    for row in tickets:
        if not wanted(row['project']):
            continue
        try:
            row['spec'] = json.loads(row['spec'] or '{}')
            row['remote'] = json.loads(row['remote'] or '{}')
            if not isinstance(row['spec'], dict) or not isinstance(row['remote'], dict):
                raise ValueError('not an object')
        except (ValueError, TypeError):
            row['spec'], row['remote'] = {}, {}
            model['warnings'].append(f"Ticket {short_key(row['key'])} has an unreadable specification")
        by_ticket[(row['project'], short_key(row['key']))] = row
    for name, stamp in refresh.items():
        if wanted(name):
            model['ticket_refresh'][name] = stamp
    slots = [s for s in config.get('implementation_slots', []) if isinstance(s, dict) and wanted(s.get('project'))]
    slot_model = {(s.get('project'), s.get('family')): s.get('model') for s in slots}

    def ticket_title(project_id, key, fallback=None):
        row = by_ticket.get((project_id, key))
        title = (row or {}).get('spec', {}).get('title') or fallback
        return safe_text(title, 120) if title else None

    # Latest public snapshot per PR links tickets to PRs and CI evidence.
    latest = {}
    for job in jobs:
        if wanted(job['project']):
            latest[(job['project'], job['pr'])] = job
    pr_snapshot, ticket_pr = {}, {}
    for key, job in latest.items():
        if key in merged:
            continue
        snapshot = cache.get(job['directory'])
        if snapshot is None:
            continue
        pr_snapshot[key] = snapshot
        if snapshot.get('ticket'):
            ticket_pr[(key[0], snapshot['ticket'])] = key[1]
    for key, snapshot in cached_ci(paths, config, now, project).items():
        previous = pr_snapshot.get(key)
        if previous and (snapshot['mtime'] < previous.get('mtime', 0) or snapshot['branch'] != previous.get('branch')):
            if snapshot.get('head') != previous.get('head') or snapshot['branch'] != previous.get('branch'):
                pr_snapshot[key] = {**previous, 'ci_mismatch': True}
            continue
        pr_snapshot[key] = {**(previous or {}), **snapshot}
        if pr_snapshot[key].get('source_head') != pr_snapshot[key].get('head_oid'):
            pr_snapshot[key]['source_fingerprint'] = None
    for key, observation in observed_prs(paths, config, now, project).items():
        if observation.pop('closed', False) or key in merged:
            pr_snapshot.pop(key, None)
            continue
        previous = pr_snapshot.get(key)
        if previous and previous.get('mtime', 0) > observation['mtime']:
            continue
        title = (previous or {}).get('title')
        pr_snapshot[key] = {**(previous or {}), **observation}
        if not observation.get('title') and title:
            pr_snapshot[key]['title'] = title
    branch_pr = {(k[0], s.get('branch')): k[1] for k, s in pr_snapshot.items() if s.get('branch')}

    def pr_for(project_id, key, branch=None):
        return ticket_pr.get((project_id, key)) or branch_pr.get((project_id, branch))

    def item(kind, **fields):
        fields.setdefault('since', None)
        target = model['workers'] if kind == 'worker' else model['attention']
        target.append(fields)

    # Latest redirect per paused/redirected source run; current coordinator CI receipts per PR.
    redirect_for = {r['source']: r for r in redirects if wanted(r['project'])}
    resumed_from = {r['resumed_as']: r for r in redirects if wanted(r['project']) and r['state'] == 'resumed' and r.get('resumed_as')}
    observed = {(o['project'], o['pr']): o for o in observations if wanted(o['project'])}
    redirect_shown = set()

    def failed_names(row):
        try:
            names = [str(f.get('name') or '?') for f in json.loads(row.get('failures') or '[]') if isinstance(f, dict)]
        except (ValueError, TypeError, AttributeError):
            names = []
        return safe_text(', '.join(names) or 'checks', 80)

    def redirect_items(run, base, count, redirect):
        """One capacity count for the claim: either the live CI repair or the paused owner."""
        state, pr = redirect['state'], redirect.get('pr') or base['pr']
        head = str(redirect.get('head') or '')[:12] or 'unknown head'
        reason = safe_text(redirect.get('reason'), 110)
        redirect_shown.add((run['project'], pr))
        paused = dict(base, pr=pr)
        if state in REDIRECT_REPAIRING:
            process_file = receipt(redirect.get('repair_dir'), 'process.json')
            identity = read_json(process_file) if process_file else None
            identity = identity if isinstance(identity, dict) else {}
            process = processes.state(identity.get('pid'), identity.get('started')) if identity else 'unknown'
            repair = dict(base, id=f"cirepair:{run['session']}:{redirect['id']}", role='ci-repair', pr=pr, process=process)
            task = f"CI repair-first: fixing failed {failed_names(redirect)} at {head} (same claim; implementation paused)"
            if state == 'repair_stopping':
                task += ' — overdue, stopping under guard leases'
            if process == 'alive':
                count['active'] += 1
                item('worker', **repair, state='active', since=processes.started_at(identity.get('started')), summary=task)
            else:
                count['held'] += 1
                waiting = not identity
                item('attention', **repair, state='waiting' if waiting else {'lingering': 'lingering'}.get(process, 'stale'),
                     since=redirect.get('updated'),
                     summary=f"{task}: " + ('repair launch awaiting its process receipt' if waiting else f'repair worker {process}; {reason}'))
            item('attention', **paused, state='paused', since=redirect.get('updated'),
                 summary=f'implementation paused for CI repair-first on PR #{pr}; resumes automatically after the repair')
            return
        count['held'] += 1
        if state in REDIRECT_PAUSING:
            item('attention', **paused, state='pausing', since=redirect.get('updated'),
                 summary=f'guarded CI repair-first interruption ({state}) for failed {failed_names(redirect)} at {head}: {reason}')
        elif state in REDIRECT_PAUSED:
            item('attention', **paused, state='paused', since=redirect.get('updated'),
                 summary=f'paused for CI repair-first ({state}) for failed {failed_names(redirect)} at {head}: {reason}')
        else:
            # A terminal state while the run is still reserved (e.g. an uncertain stop).
            item('attention', **paused, state=state if state in ('uncertain', 'orphaned') else 'unverified', since=redirect.get('updated'),
                 summary=f'CI repair-first {safe_text(state, 20)}: {reason}')

    impl_counts, managed = {}, set()
    seen_keys = {}
    for run in runs:
        if not wanted(run['project']):
            continue
        managed.add(run['session'])
        count = impl_counts.setdefault((run['project'], run['family']), dict(active=0, held=0, retained=0))
        try:
            claim = json.loads(run['claim']) if run['claim'] else {}
            if not isinstance(claim, dict) or not all(isinstance(claim.get(f), (str, type(None)))
                                                      for f in ('key', 'session', 'branch', 'model')):
                raise ValueError
        except (ValueError, TypeError):
            claim = {}
            model['warnings'].append(f"Implementation receipt for {run['family']} has an unreadable claim")
        # Whitelisted projection only: the claim blob also contains the token.
        key = short_key(claim.get('key'))
        if claim.get('session'):
            managed.add(claim['session'])
        process_file = receipt(run['directory'], 'process.json')
        # Like the dispatcher, a present process.json is the only identity; the
        # persisted DB identity applies only when that receipt does not exist.
        try:
            receipt_present = bool(process_file) and process_file.exists()
        except OSError:
            receipt_present = True
        if receipt_present:
            identity = read_json(process_file)
            identity = identity if isinstance(identity, dict) else {}
            pid, started = identity.get('pid'), identity.get('started')
            # Never fall back to another identity: a malformed receipt is unverified.
            if not Processes.identity(pid) or not Processes.identity(started):
                model['warnings'].append(f"Implementation receipt for {run['family']} has an unreadable process identity")
        else:
            pid, started = run['pid'], run['started']
            if (pid is not None and not Processes.identity(pid)) or (started is not None and not Processes.identity(started)):
                model['warnings'].append(f"Implementation receipt for {run['family']} has an unreadable process identity")
        process = processes.state(pid, started)
        owner = by_ticket.get((run['project'], key)) or {}
        base = dict(id='impl:'+run['session'], project=run['project'], family=run['family'],
                    model=owner.get('model') or claim.get('model') or slot_model.get((run['project'], run['family'])),
                    role='implementation', ticket=key, pr=pr_for(run['project'], key, claim.get('branch')),
                    title=ticket_title(run['project'], key, (claim.get('spec') or {}).get('title') if isinstance(claim.get('spec'), dict) else None),
                    process=process, ticket_status=owner.get('status'))
        if key:
            seen_keys.setdefault((run['project'], key), []).append(base['id'])
        started_at = processes.started_at(started) if process == 'alive' else None
        try:
            since = started_at or (process_file.stat().st_mtime if process_file else None)
        except OSError:
            since = None
        redirect = redirect_for.get(run['session'])
        capacity = capacity_for.get(run['session'])
        if run['status'] == 'capacity_wait':
            count['retained' if process == 'exited' else 'held'] += 1
            quota = bool(capacity) and capacity.get('kind') == 'quota'
            item('attention', **base, state='quota-wait' if quota else 'capacity-backoff',
                 since=capacity.get('updated') if capacity else since,
                 retry_after=capacity.get('retry_after') if capacity else None,
                 summary=(f"provider {'quota' if quota else 'capacity'} retry {capacity['attempt']}/{QUOTA_RETRIES if quota else 3}; next retry "
                          f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(capacity['retry_after']))}: "
                          f"{safe_text(capacity['reason'])}" if capacity else 'capacity wait without retry receipt; inspect dispatcher'))
            continue
        if run['status'] == 'redirected':
            if redirect:
                redirect_items(run, base, count, redirect)
            else:
                count['held'] += 1
                item('attention', **base, state='unverified', since=since, summary='redirected run without a CI redirect record')
            continue
        if redirect and redirect['state'] == 'pending' and run['status'] in ACTIVE_RUN:
            current = observed.get((run['project'], redirect['pr']))
            # Only a still-current failure: closed, green or newer snapshots never alert.
            if current and current.get('fingerprint') == redirect.get('fingerprint'):
                redirect_shown.add((run['project'], redirect['pr']))
                item('attention', **dict(base, id=f"ciredirect:{run['project']}:{redirect['id']}", pr=redirect['pr']),
                     state='ci-repair-requested', since=redirect.get('updated'),
                     summary=f"failed {failed_names(redirect)} at {str(redirect.get('head') or '')[:12]}: "
                             f"{safe_text(redirect.get('reason'), 110)}")
        elif redirect and redirect['state'] == 'guarding' and run['status'] in ACTIVE_RUN:
            redirect_shown.add((run['project'], redirect['pr']))
            item('attention', **dict(base, id=f"ciredirect:{run['project']}:{redirect['id']}", pr=redirect['pr']),
                 state='pausing', since=redirect.get('updated'),
                 summary=f"acquiring guard leases before a CI repair-first interruption for failed {failed_names(redirect)}")
        if run['status'] in ACTIVE_RUN and process == 'alive':
            count['active'] += 1
            resumed = resumed_from.get(run['session'])
            item('worker', **base, state='active', since=since,
                 summary=f"Implementing {key or 'unknown ticket'}" + (f" (ticket now {owner['status']})" if owner.get('status') not in (None, 'in_progress') else '')
                 + (f" (resumed after CI repair-first on PR #{resumed['pr']})" if resumed else ''))
        elif run['status'] in ACTIVE_RUN:
            # Dispatcher counts these until it reconciles them; they are never active.
            count['held'] += 1
            state = {'lingering': 'lingering', 'unknown': 'unverified', 'unreadable': 'unverified'}.get(process, 'stale')
            detail = {'lingering': 'wrapper exited; a session process remains (holds capacity)',
                      'unverified': 'no verifiable process identity (holds capacity)',
                      'stale': f'recorded worker {process}; awaiting dispatcher reconciliation'}[state]
            item('attention', **base, state=state, since=since, summary=detail)
        elif run['status'] in RETAINED:
            # Like the dispatcher, an orphaned run still holds its slot; only
            # quarantined/parked reservations release launch capacity.
            count['held' if run['status'] == 'orphaned' else 'retained'] += 1
            item('attention', **base, state='capacity-exhausted' if capacity and capacity['state'] == 'exhausted' else run['status'], since=since,
                 summary=safe_text(run['message']) or f"{run['status']}; claim retained for explicit recovery")
        else:
            count['held'] += 1
            item('attention', **base, state='unverified', since=since, summary=f"unrecognised dispatcher state {safe_text(run['status'], 30)}")
    for (project_id, key), ids in seen_keys.items():
        if len(ids) > 1:
            model['warnings'].append(f'{key}: {len(ids)} retained implementation receipts (recovered or duplicate); only PID-verified ones count as active')

    # Review/repair jobs.
    job_counts = {}
    open_by_pr = {}
    repairing = set()
    for job in jobs:
        if not wanted(job['project']):
            continue
        if (job['project'], job['pr']) in merged and job['status'] == 'retry':
            continue
        is_latest = latest.get((job['project'], job['pr'])) is job
        if job['status'] not in JOB_OPEN and not (job['status'] == 'failed' and is_latest and (job['project'], job['pr']) not in merged):
            continue
        role = ROLE.get(job['kind'], safe_text(job['kind'], 20))
        count = job_counts.setdefault((job['family'], role), dict(active=0, held=0, retained=0))
        snapshot = cache.get(job['directory']) or {}
        key = snapshot.get('ticket')
        if job['status'] in ('reserved', 'running', 'orphaned', 'quarantined', 'quota_wait', 'quota_launch'):
            open_by_pr.setdefault((job['project'], job['pr']), []).append(job['id'])
        process = processes.state(job['pid'], job['started']) if job['status'] == 'running' else None
        if role == 'repair' and key and job['status'] in ('reserved', 'running'):
            # Same rule as the dispatcher: a resumed Review claim is repair work.
            repairing.add((job['project'], key))
        base = dict(id=f"job:{job['project']}:{job['id']}", project=job['project'], family=job['family'],
                    model=slot_model.get((job['project'], job['family'])), role=role, ticket=key, pr=job['pr'],
                    head=str(job['head'] or '')[:12],
                    title=ticket_title(job['project'], key, snapshot.get('ticket_title')) or snapshot.get('title'),
                    process=process, attempts=job['attempts'] or 0)
        verb = 'Reviewing' if role == 'review' else 'Repairing feedback on'
        task = f"{verb} PR #{job['pr']} at {base['head'] or 'unknown head'}"
        if base['attempts']:
            task += f" (attempt {base['attempts']+1})"
        since = job['created']
        if job['status'] == 'running' and process == 'alive':
            count['active'] += 1
            item('worker', **base, state='active', since=processes.started_at(job['started']) or since, summary=task)
        elif job['status'] == 'running':
            count['held'] += 1
            state = {'lingering': 'lingering', 'unknown': 'unverified', 'unreadable': 'unverified'}.get(process, 'stale')
            item('attention', **base, state=state, since=since, summary=f"{task}: recorded worker {process}")
        elif job['status'] == 'reserved':
            count['held'] += 1
            item('attention', **base, state='waiting', since=since, summary=f'{task}: reserved, awaiting launch')
        elif job['status'] == 'retry':
            item('attention', **base, state='retry', since=since, retry_after=job['retry_after'],
                 summary=f"{task}: {safe_text(job['error'], 90) or 'retry scheduled'}")
        elif job['status'] == 'quota_wait':
            # Verified stopped; resumes under the same claim after the provider's stated reset.
            count['retained'] += 1
            item('attention', **base, state='quota-wait', since=since, retry_after=job['retry_after'],
                 summary=f"{task}: {safe_text(job['error'], 90) or 'waiting for provider quota reset'}")
        elif job['status'] == 'quota_launch':
            count['held'] += 1
            item('attention', **base, state='waiting', since=since, summary=f'{task}: quota recovery launch in progress')
        elif job['status'] == 'orphaned':
            count['held'] += 1
            item('attention', **base, state='orphaned', since=since, summary=f"{task}: {safe_text(job['error'], 90) or 'requires explicit recovery'}")
        elif job['status'] == 'quarantined':
            count['retained'] += 1
            item('attention', **base, state='quarantined', since=since, summary=f'{task}: verified stopped; PR reserved for explicit recovery')
        else:
            item('attention', **base, state='failed', since=since, summary=f"{task}: {safe_text(job['error'], 90) or 'failed'}")
    for (project_id, pr), ids in open_by_pr.items():
        if len(ids) > 1:
            model['warnings'].append(f'PR #{pr}: {len(ids)} open jobs (duplicate reservation); only PID-verified ones count as active')

    # Tickets: manual claims, blocked work and PRs awaiting CI/review.
    queue = {}
    for (project_id, key), row in sorted(by_ticket.items()):
        counts = queue.setdefault(project_id, {})
        counts[row['status']] = counts.get(row['status'], 0) + 1
        base = dict(id=f'ticket:{project_id}:{key}', project=project_id, family=row['agent'], model=row['model'],
                    role='implementation', ticket=key, pr=pr_for(project_id, key, row['branch']),
                    title=ticket_title(project_id, key), ticket_status=row['status'])
        expired = bool(row['expires'] and row['expires'] < now)
        if row['status'] == 'blocked':
            item('attention', **base, state='blocked', since=row['heartbeat'],
                 summary='blocked: ' + (safe_text(row['evidence'], 110) or 'no handoff recorded'))
            if row['agent'] and row['session'] not in managed:
                # The dispatcher counts unmanaged blocked claims against the slot too.
                impl_counts.setdefault((project_id, row['agent']), dict(active=0, held=0, retained=0))['held'] += 1
        elif row['status'] == 'in_progress' and row['session'] not in managed and (project_id, key) not in repairing:
            item('attention', **base, state='manual', since=row['heartbeat'],
                 summary='manual claim without worker identity; not counted active' + ('; lease EXPIRED' if expired else ''))
            if row['agent']:
                counts_for = impl_counts.setdefault((project_id, row['agent']), dict(active=0, held=0, retained=0))
                counts_for['held'] += 1
        elif row['status'] == 'review' and (project_id, key) not in repairing:
            pr = base['pr']
            snapshot = pr_snapshot.get((project_id, pr)) if pr else None
            state = ci_state(snapshot, now) or 'review'
            if snapshot and not snapshot.get('corrupt'):
                c = snapshot['checks']
                detail = (f"checks {c['success']} ok/{c['pending']} pending/{c['failed']} failed"
                          + (f"; testnet {snapshot['testnet']}" if snapshot.get('testnet') else '')
                          + f" ({snapshot.get('source', 'worker-start snapshot')}; observed {fmt_age(now - snapshot['mtime'])} ago"
                          + ('; STALE' if state == 'ci-stale' else '') + ')')
                state = 'awaiting-review' if state == 'ci-passed' else state
            else:
                detail = 'no local PR snapshot yet' if not pr else 'PR snapshot unreadable'
            item('attention', **base, state=state, since=snapshot.get('mtime') if snapshot else None, summary=detail)

    for (project_id, pr), row in sorted(observed.items()):
        if (project_id, pr) in redirect_shown or row.get('owner_status') == 'review':
            continue  # Shown above, or covered by the Review ticket's CI state.
        key = short_key(row.get('ticket'))
        owner = by_ticket.get((project_id, key)) or {}
        item('attention', id=f'ciobs:{project_id}:{pr}', project=project_id, family=owner.get('agent'), model=owner.get('model'),
             role='implementation', ticket=key, pr=pr, title=ticket_title(project_id, key), ticket_status=row.get('owner_status'),
             state='ci-failed', since=row.get('observed'),
             summary=f"failed {failed_names(row)} at {str(row.get('head') or '')[:12]}" + (' (draft)' if row.get('draft') else '')
                     + f"; repair: {safe_text(row.get('responsibility'), 110) or 'unassigned'}")

    for lock in locks:
        if wanted(lock['project']):
            model['locks'].append(dict(resource=lock['resource'], family=lock['agent'], model=lock['model'],
                                       project=lock['project'], ticket=short_key(lock['ticket']),
                                       purpose=safe_text(lock['purpose'], 80), acquired=lock['acquired'], expires=lock['expires']))

    # Capacity: implementation slots are per project/family; review/repair are global per family.
    for slot in slots:
        key = (slot.get('project'), slot.get('family'))
        count = impl_counts.pop(key, dict(active=0, held=0, retained=0))
        pause = next((b for b in backoff if (b['project'], b['family']) == key and (b['retry_after'] or 0) > now), None)
        model['capacity'].append(dict(project=key[0], family=key[1], model=slot.get('model'), category='implementation',
                                      enabled=bool(slot.get('enabled', True)), limit=slot.get('max_workers', 1), **count,
                                      backoff_until=pause['retry_after'] if pause else None,
                                      backoff_reason=safe_text(pause['reason'], 60) if pause else None))
    for (project_id, family), count in sorted(impl_counts.items()):
        if any(count.values()):
            model['capacity'].append(dict(project=project_id, family=family, model=None, category='implementation',
                                          enabled=False, limit=0, **count, backoff_until=None, backoff_reason='no configured slot'))
    limits = config.get('review_worker_limits', {}) if isinstance(config.get('review_worker_limits'), dict) else {}
    families = sorted({s.get('family') for s in config.get('implementation_slots', []) if isinstance(s, dict)}
                      | set(limits) | set(config.get('workers', {}) if isinstance(config.get('workers'), dict) else {})
                      | {f for f, _ in job_counts})
    for family in families:
        for role, kind in (('review', 'review'), ('repair', 'fix')):
            count = job_counts.get((family, role), dict(active=0, held=0, retained=0))
            pause = next((p for p in pauses if (p['family'], p['kind']) == (family, kind) and (p['retry_after'] or 0) > now), None)
            model['capacity'].append(dict(project='*', family=family, model=None, category=role, enabled=True,
                                          limit=(limits.get(family) or {}).get(kind, 1), **count,
                                          backoff_until=pause['retry_after'] if pause else None,
                                          backoff_reason='quarantine backoff' if pause else None))
    model['inventory'] = reconcile_inventory(paths, config, by_ticket, now, project, ticket_pr)
    model['queue'] = queue
    model['pipeline'] = derive_pipeline(pr_snapshot, jobs, by_ticket, model['workers'], batches, cache, now,
        batch_projects={p['id'] for p in config.get('projects', []) if p.get('integration_batch', {}).get('enabled') is True})
    if model['sources'].get('reviews', {}).get('state') != 'ok':
        model['pipeline']['unavailable'] = 'review store unavailable'
    order = {s: i for i, s in enumerate(['ci-failed', 'ci-stale', 'ci-unknown', 'ci-repair-requested', 'uncertain', 'retry', 'quota-wait', 'stale', 'lingering',
                                          'unverified', 'orphaned', 'manual', 'pausing', 'paused', 'waiting', 'ci-pending',
                                          'awaiting-review', 'review', 'blocked', 'failed', 'quarantined', 'parked'])}
    model['attention'].sort(key=lambda x: (order.get(x['state'], 99), x['project'], str(x.get('ticket') or ''), x.get('pr') or 0))
    model['workers'].sort(key=lambda x: (x['project'], x['family'], x['role'], x.get('since') or 0))
    return model


def changes(previous, current):
    """Human-readable differences between two refreshes."""
    if previous is None:
        return []
    out = []
    old_workers = {w['id']: w for w in previous['workers']}
    new_workers = {w['id']: w for w in current['workers']}
    for wid in new_workers.keys() - old_workers.keys():
        out.append('+ started: ' + label(new_workers[wid]))
    for wid in old_workers.keys() - new_workers.keys():
        out.append('- no longer active: ' + label(old_workers[wid]))
    old_items = {a['id']: a for a in previous['attention']}
    for item in current['attention']:
        before = old_items.get(item['id'])
        if before is None:
            out.append(f"+ {item['state']}: " + label(item))
        elif before['state'] != item['state']:
            out.append(f"~ {before['state']} -> {item['state']}: " + label(item))
    for aid in old_items.keys() - {a['id'] for a in current['attention']} - new_workers.keys():
        out.append(f"- cleared {old_items[aid]['state']}: " + label(old_items[aid]))
    for name, src in current['sources'].items():
        before = previous['sources'].get(name, {}).get('state')
        if before and before != src['state']:
            out.append(f"~ source {name}: {before} -> {src['state']}")
    for project_id, counts in current['queue'].items():
        old = previous['queue'].get(project_id, {})
        for status in sorted(set(counts) | set(old)):
            if counts.get(status, 0) != old.get(status, 0):
                out.append(f"~ {project_id} {status}: {old.get(status, 0)} -> {counts.get(status, 0)}")
    return sorted(out, key=lambda x: x[0] != '+')


def label(entry):
    parts = [entry.get('family') or '?', entry.get('role') or '']
    if entry.get('ticket'):
        parts.append(entry['ticket'])
    if entry.get('pr'):
        parts.append(f"PR #{entry['pr']}")
    return ' '.join(p for p in parts if p)


def fmt_age(seconds):
    if seconds is None:
        return '?'
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds//60}m'
    if seconds < 86400:
        return f'{seconds//3600}h{seconds%3600//60:02d}m'
    return f'{seconds//86400}d{seconds%86400//3600}h'


def fit(text, width):
    return text if len(text) <= width else text[:max(0, width-1)] + '…'


def table(rows, width):
    """Left-aligned columns; the last column takes the remaining width."""
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]) - 1)]
    return [fit('  ' + '  '.join([str(c).ljust(w) for c, w in zip(r, widths)] + [str(r[-1])]).rstrip(), width) for r in rows]


def render(model, width=100, delta=None, interval=None):
    now = model['generated_at']
    width = max(20, width)
    compact = width < 90
    stamp = time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(now))
    lines = [fit(f"Agent dashboard · {stamp} · project {model['project_filter'] or 'all'}"
                 + (f' · refresh {interval:g}s · q quits' if interval else ' · read-only snapshot'), width)]
    sources = []
    for name, src in model['sources'].items():
        if src['state'] == 'ok':
            sources.append(f"{name} ok ({fmt_age(now - src['modified_at']) if src.get('modified_at') else 'no writes'})")
        else:
            sources.append(f"{name} UNAVAILABLE: {src.get('detail', '')}")
    lines.append(fit('Sources: ' + ' · '.join(sources), width))
    lines.append(fit('Source ages measure data-file changes, not service heartbeats.', width))
    if model['ticket_refresh']:
        lines.append(fit('Ticket snapshot: ' + ' · '.join(f'{p} refreshed {fmt_age(now - t)} ago' for p, t in sorted(model['ticket_refresh'].items())), width))
    for warning in model['warnings']:
        lines.append(fit('! ' + warning, width))

    lines += render_inventory(model, width)
    pipeline = model.get('pipeline', {})
    lines += ['', 'PR PIPELINE (derived waits; separate from ticket totals)']
    if pipeline.get('unavailable'):
        lines.append(fit('  Unknown: ' + pipeline['unavailable'], width))
    elif not pipeline.get('rows'):
        lines.append('  No usable local PR evidence; not proof of an empty GitHub queue.')
    else:
        candidates = sum(row['integration'] for row in pipeline['rows'])
        lines.append(fit(f"  {pipeline['total'] - candidates} source PRs · {candidates} integration candidates"
                         ' · one state per PR; observed evidence, not merge authorization', width))
        for state, label_text in PIPELINE_LABELS.items():
            rows = [row for row in pipeline['rows'] if row['state'] == state]
            if rows:
                references = ', '.join(f"{row['project']}#{row['pr']}" for row in rows)
                lines.append(fit(f'  {label_text}: {len(rows)} — {references}', width))
    if pipeline.get('batch_receipts') == 'unavailable':
        lines.append(fit('  Batch status unavailable: ' + (pipeline.get('detail') or 'no usable receipts'), width))
    lines += ['', 'CAPACITY (active = PID+start-time verified; held = unverified/waiting; retained = parked/quarantined)']
    if not model['capacity']:
        lines.append('  ' + ('No configured slots.' if model['sources'].get('config', {}).get('state') == 'ok' else 'Configuration unavailable.'))
    rows = []
    for c in model['capacity']:
        scope = c['project'] if c['project'] != '*' else 'all'
        note = '' if c['enabled'] else 'disabled'
        if c.get('backoff_until'):
            note = (note + ' ' if note else '') + f"paused {fmt_age(c['backoff_until'] - now)} ({c.get('backoff_reason') or 'backoff'})"
        rows.append((scope, c['family'], c['category'], f"{c['active']}/{c['limit']}", f"held {c['held']}",
                     f"retained {c['retained']}", *(() if compact else (c.get('model') or '',)), note))
    lines += table(rows, width) if rows else []

    workers = model['workers']
    lines += ['', f'ACTIVE WORKERS ({len(workers)})']
    if not workers:
        lines.append('  None verified.' if model['sources'].get('dispatch', {}).get('state') == 'ok'
                     or model['sources'].get('reviews', {}).get('state') == 'ok' else '  Unknown: worker stores unavailable.')
    # Narrow terminals keep the task readable by dropping model/project columns.
    pick = (lambda row: row[:1] + row[2:3] + row[4:]) if compact else (lambda row: row)
    rows = [pick(('FAMILY', 'MODEL', 'ROLE', 'PROJECT', 'TICKET', 'PR', 'ELAPSED', 'TASK'))]
    for w in workers:
        rows.append(pick((w['family'], w.get('model') or '?', w['role'], w['project'], w.get('ticket') or '-',
                          f"#{w['pr']}" if w.get('pr') else '-', fmt_age(now - w['since']) if w.get('since') else '?',
                          ' — '.join(x for x in (w.get('title'), w['summary']) if x))))
    if workers:
        lines += table(rows, width)

    attention = model['attention']
    lines += ['', f'WORKER / JOB NOTICES ({len(attention)} items; not additional issues)']
    if not attention:
        lines.append('  Nothing waiting.' if model['sources'].get('tickets', {}).get('state') == 'ok' else '  Unknown: ticket store unavailable.')
    for a in attention:
        when = f" for {fmt_age(now - a['since'])}" if a.get('since') else ''
        if a['state'] in ('retry', 'quota-wait'):
            due = a.get('retry_after')
            when += f' · retry in {fmt_age(due - now)}' if due and due > now else ' · retry due'
        head = ' '.join(x for x in (f"[{a['state']}]", a.get('family') or '', a['role'], a.get('ticket') or '',
                                    f"PR #{a['pr']}" if a.get('pr') else '') if x)
        lines.append(fit(f"  {head}{when}: " + ' — '.join(x for x in (a.get('title'), a['summary']) if x), width))

    lines += ['', 'LOCAL TICKET HISTORY (completed tickets excluded from open totals above)']
    if model['sources'].get('tickets', {}).get('state') != 'ok':
        lines.append('  Unknown: ticket store unavailable (not an empty queue).')
    elif not model['queue']:
        lines.append('  Empty: no tickets for this project filter.')
    for project_id, counts in sorted(model['queue'].items()):
        order = ['backlog', 'in_progress', 'review', 'blocked', 'done']
        parts = [f"{counts[s]} {s.replace('_', ' ')}" for s in order if s in counts]
        parts += [f'{v} {k}' for k, v in sorted(counts.items()) if k not in order]
        lines.append(fit(f'  {project_id}: ' + ' · '.join(parts), width))

    if model['locks']:
        lines += ['', 'RESOURCE LOCKS']
        for lock in model['locks']:
            lines.append(fit(f"  {lock['resource']} — {lock['family']} {lock.get('ticket') or ''} expires in {fmt_age(lock['expires'] - now)}: {lock['purpose']}", width))

    if delta is not None:
        lines += ['', 'CHANGES SINCE LAST REFRESH'] + (['  ' + d for d in delta] or ['  None.'])
    return [fit(line, width) for line in lines]


def public(model, delta=None):
    data = dict(model)
    data['generated_at_iso'] = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(model['generated_at']))
    if delta is not None:
        data['changes'] = delta
    return data


def live(args, paths):
    import curses
    cache, previous, delta = SnapshotCache(), None, []
    state = dict(model=None, offset=0, due=0.0, error=None)

    def refresh():
        nonlocal previous, delta
        state['due'] = time.monotonic() + args.interval
        try:
            current = collect(paths, args.project, cache=cache)
        except Exception as error:  # keep the last good view rather than crash the terminal
            state['error'] = f'refresh failed ({type(error).__name__}); showing previous data'
            return
        state['error'] = None
        delta = changes(previous, current) if previous else delta
        previous, state['model'] = current, current

    def loop(screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(200)
        while True:
            if time.monotonic() >= state['due']:
                refresh()
            height, width = screen.getmaxyx()
            lines = render(state['model'], width - 1, delta, args.interval) if state['model'] else []
            if state['error']:
                lines.insert(0, '! ' + state['error'])
            body = max(1, height - 1)
            state['offset'] = max(0, min(state['offset'], len(lines) - body))
            screen.erase()
            for row, text in enumerate(lines[state['offset']:state['offset'] + body]):
                try:
                    screen.addnstr(row, 0, text, width - 1)
                except curses.error:
                    pass
            hidden = len(lines) - body
            footer = (f"lines {state['offset']+1}-{min(len(lines), state['offset']+body)} of {len(lines)} · ↑/↓ scroll · r refresh · q quit"
                      if hidden > 0 else 'r refresh · q quit')
            try:
                screen.addnstr(height - 1, 0, fit(footer, width - 1), width - 1, curses.A_REVERSE)
            except curses.error:
                pass
            screen.refresh()
            key = screen.getch()
            if key in (ord('q'), ord('Q'), 27):
                return
            if key in (ord('r'), ord('R')):
                state['due'] = 0.0
            elif key in (curses.KEY_DOWN, ord('j')):
                state['offset'] += 1
            elif key in (curses.KEY_UP, ord('k')):
                state['offset'] -= 1
            elif key == curses.KEY_NPAGE:
                state['offset'] += body
            elif key == curses.KEY_PPAGE:
                state['offset'] -= body
            elif key == curses.KEY_HOME:
                state['offset'] = 0

    try:
        curses.wrapper(loop)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true', help='Print one readable snapshot and exit')
    mode.add_argument('--json', action='store_true', help='Print one machine-readable snapshot and exit')
    mode.add_argument('--live', action='store_true', help='Live terminal view (default on a terminal)')
    parser.add_argument('--refresh-github', action='store_true', help='Update cached GitHub issue/PR counts and exact-head CI once before viewing; never polls in live mode')
    parser.add_argument('--project', help='Only show this project (for example suifleet)')
    parser.add_argument('--interval', type=float, default=5.0, help='Live refresh seconds (minimum 1)')
    parser.add_argument('--width', type=int, help='Text width for --once (default: terminal width)')
    for name in ('tickets', 'dispatch', 'reviews', 'config', 'inventory'):
        parser.add_argument(f'--{name}-path', dest=name, type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.interval = max(1.0, args.interval)
    paths = {k: getattr(args, k) for k in ('tickets', 'dispatch', 'reviews', 'config', 'inventory') if getattr(args, k)}
    if args.refresh_github:
        try:
            refresh_github({**default_paths(), **paths}, args.project)
        except Unavailable as error:
            print(str(error), file=sys.stderr)
            return 3
    interactive = sys.stdout.isatty() and sys.stdin.isatty()
    if args.live and not interactive:
        print('Live mode needs an interactive terminal; use --once or --json.', file=sys.stderr)
        return 2
    if args.live or not (args.once or args.json) and interactive and os.environ.get('TERM', 'dumb') != 'dumb':
        return live(args, paths)
    model = collect(paths, args.project)
    if args.json:
        json.dump(public(model), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write('\n')
    else:
        width = args.width or (os.get_terminal_size().columns if sys.stdout.isatty() else 120)
        sys.stdout.write('\n'.join(render(model, width)) + '\n')
    return 3 if any(s['state'] != 'ok' for s in model['sources'].values()) else 0


if __name__ == '__main__':
    try:
        current = Path(__file__).resolve().parent/'current/dashboard.py'
        if current.is_file() and current.resolve() != Path(__file__).resolve():
            import runpy
            runpy.run_path(str(current.resolve()), run_name='__main__')
        else:
            sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
