#!/usr/bin/env python3
"""Cooperative single-WSL-host ticket claims. Never creates GitHub issues."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
CLAIMABLE = {'Backlog', 'Ready', 'Todo'}
MAX_SNAPSHOT_AGE = 300


class CoordinationError(Exception):
    pass


def run_git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True)
    if result.returncode:
        raise CoordinationError(result.stderr.strip())
    return result.stdout.strip()


def database_path(root):
    # --path-format=absolute also resolves the shared directory of linked worktrees.
    common = run_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    return Path(common) / 'suifleet-coordinator.sqlite3'


def scope_path(value):
    if not value or '\\' in value or value.startswith('/') or '..' in value.split('/'):
        raise CoordinationError(f'Invalid repository-relative scope: {value!r}')
    # Wildcards reserve the entire directory before the first wildcard, conservatively.
    parts = value.split('/')
    for index, part in enumerate(parts):
        if any(c in part for c in '*?['):
            parts = parts[:index]
            break
    normalized = str(PurePosixPath('/'.join(parts)))
    return normalized if normalized != '.' else '.'


def overlaps(left, right):
    left, right = scope_path(left), scope_path(right)
    return left == '.' or right == '.' or left == right or left.startswith(right + '/') or right.startswith(left + '/')


def tracking_module():
    spec = importlib.util.spec_from_file_location('suifleet_tracking', ROOT / 'scripts/bootstrap_tracking.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def live_snapshot(config, state):
    gh = tracking_module().Gh()
    items = gh.run('project', 'item-list', str(state['project']['number']), '--owner',
                   state['project']['owner']['login'], '--limit', '1000', '--format', 'json', as_json=True)
    if items['totalCount'] > len(items['items']):
        raise CoordinationError('Incomplete Project snapshot; refusing claims')
    issues = gh.api(f"repos/{config['repository']}/issues?state=all&per_page=100", paginate=True)
    by_number = {item['number']: item for item in issues if 'pull_request' not in item}
    by_url = {item['content'].get('url'): item for item in items['items'] if item.get('content')}
    snapshot = {}
    by_issue_id = {item['id']: key for key, item in state['issues'].items()}
    for issue in config['issues']:
        published = state['issues'].get(issue['key'])
        if not published or published['number'] not in by_number or published['url'] not in by_url:
            raise CoordinationError(f"Missing published issue/Project item: {issue['key']}")
        live = by_number[published['number']]
        item = by_url[published['url']]
        dependencies = gh.api(f"repos/{config['repository']}/issues/{published['number']}/dependencies/blocked_by?per_page=100", paginate=True)
        snapshot[issue['key']] = {
            'state': live['state'], 'status': item.get('status', ''),
            'agent': item.get('agent', ''), 'assignees': [a['login'] for a in live.get('assignees', [])],
            'url': published['url'], 'item_id': item['id'],
            'depends_on': [by_issue_id[d['id']] for d in dependencies if d['id'] in by_issue_id],
            'external_blockers': [d['html_url'] for d in dependencies if d['id'] not in by_issue_id and d['state'] != 'closed'],
        }
    return snapshot


class Coordinator:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The DB and WAL live on the WSL Linux filesystem, not /mnt/c or a network share.
        self.db = sqlite3.connect(self.path, timeout=120, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (
                key TEXT PRIMARY KEY, spec TEXT NOT NULL, remote TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'backlog', token TEXT, agent TEXT, model TEXT,
                session TEXT, heartbeat REAL, expires REAL, worktree TEXT, branch TEXT,
                evidence TEXT, revision INTEGER NOT NULL DEFAULT 0, published_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS unique_claim_token ON tickets(token) WHERE token IS NOT NULL;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, ticket TEXT NOT NULL,
                actor TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
            );
        ''')
        version = self.db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if version and int(version[0]) != SCHEMA:
            raise CoordinationError('Unsupported coordinator database schema')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema', ?)", (str(SCHEMA),))
        os.chmod(self.path, 0o600)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def event(self, key, actor, kind, detail):
        self.db.execute('INSERT INTO events(at,ticket,actor,kind,detail) VALUES (?,?,?,?,?)',
                        (self.clock(), key, actor, kind, detail))

    def import_catalog(self, config, snapshot):
        tracking_module().ordered_issues(config)
        if set(snapshot) != {i['key'] for i in config['issues']}:
            raise CoordinationError('Snapshot must contain every catalog ticket')
        with self.transaction():
            repo = self.db.execute("SELECT value FROM metadata WHERE key='repository'").fetchone()
            if repo and repo[0] != config['repository']:
                raise CoordinationError('Database belongs to another repository')
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('repository', ?)", (config['repository'],))
            # Never silently discard claimed tickets when a worktree has an older catalog.
            existing = {r[0] for r in self.db.execute('SELECT key FROM tickets')}
            if existing - set(snapshot):
                raise CoordinationError('Catalog is older/incomplete; refresh from the canonical checkout')
            for issue in config['issues']:
                for path in issue['paths']:
                    scope_path(path)
                if not issue['paths']:
                    raise CoordinationError('Empty ownership scope')
                key = issue['key']
                old = self.db.execute('SELECT * FROM tickets WHERE key=?', (key,)).fetchone()
                spec = dict(issue)
                if old and (old['token'] or old['status'] == 'done'):
                    # Catalog edits can only expand an outstanding reservation.
                    previous = json.loads(old['spec'])
                    spec['paths'] = sorted(set(spec['paths']) | set(previous['paths']))
                    status = old['status']
                else:
                    remote = snapshot[key]
                    status = 'done' if remote['state'] == 'closed' and remote['status'] == 'Done' else 'backlog'
                self.db.execute('''INSERT INTO tickets(key,spec,remote,status) VALUES (?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET spec=excluded.spec, remote=excluded.remote, status=excluded.status''',
                    (key, json.dumps(spec), json.dumps(snapshot[key]), status))
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('snapshot_at', ?)", (str(self.clock()),))

    def row(self, key):
        row = self.db.execute('SELECT * FROM tickets WHERE key=?', (key,)).fetchone()
        if not row:
            raise CoordinationError(f'Unknown ticket: {key}')
        return row

    def view(self, row):
        item = dict(row)
        item['spec'] = json.loads(item['spec'])
        item['remote'] = json.loads(item['remote'])
        item['expired'] = bool(item['expires'] is not None and item['expires'] <= self.clock())
        item['publish_pending'] = item['published_revision'] < item['revision']
        return item

    def holds_scope(self, row):
        if row['status'] == 'done':
            return False
        remote = json.loads(row['remote'])
        return bool(row['token'] or (remote['state'] == 'open' and (
            remote['agent'] != 'Unassigned' or remote.get('assignees') or
            remote['status'] in {'In progress', 'Review', 'Blocked'})))

    def eligible(self, row, reserved, statuses):
        spec, remote = json.loads(row['spec']), json.loads(row['remote'])
        if row['revision'] > row['published_revision']:
            return 'GitHub ownership mirror pending; publish then refresh'
        if row['token'] or row['status'] != 'backlog':
            return 'already reserved or completed'
        if remote['state'] != 'open' or remote['status'] not in CLAIMABLE or remote['agent'] != 'Unassigned' or remote.get('assignees'):
            return 'reserved/blocked/closed in GitHub'
        if remote.get('external_blockers'):
            return 'external GitHub dependencies incomplete: ' + ', '.join(remote['external_blockers'])
        missing = [key for key in set(spec['depends_on']) | set(remote.get('depends_on', [])) if statuses.get(key) != 'done']
        if missing:
            return 'dependencies incomplete: ' + ', '.join(missing)
        for other in reserved:
            if any(overlaps(a, b) for a in spec['paths'] for b in json.loads(other['spec'])['paths']):
                return f"scope conflicts with {other['key']} ({other['status']})"
        return None

    def claim(self, agent, model, session, ticket=None, extra_paths=(), ttl=900):
        if agent not in {'Codex', 'Claude', 'Other'} or not model.strip() or not session.strip() or not 60 <= ttl <= 3600:
            raise CoordinationError('Supply agent, actual model, session and a 60–3600 second lease')
        with self.transaction():
            updated = self.db.execute("SELECT value FROM metadata WHERE key='snapshot_at'").fetchone()
            if not updated or self.clock() - float(updated[0]) > MAX_SNAPSHOT_AGE:
                raise CoordinationError('GitHub snapshot stale/missing; run refresh before claiming')
            rows = list(self.db.execute('SELECT * FROM tickets'))
            if any(r['session'] == session and r['status'] in {'in_progress', 'blocked'} for r in rows):
                raise CoordinationError('This session already owns an implementation; finish or release it first')
            statuses = {r['key']: ('done' if r['status'] == 'done' and r['revision'] == r['published_revision'] else r['status'] if r['status'] != 'done' else 'review') for r in rows}
            reserved = [r for r in rows if self.holds_scope(r)]
            candidates = [self.row(ticket)] if ticket else sorted(rows, key=lambda r: (json.loads(r['spec'])['priority'], r['key']))
            reasons = {}
            for original in candidates:
                candidate = dict(original)
                spec = json.loads(candidate['spec'])
                spec['paths'] = sorted(set(spec['paths']) | {scope_path(p) for p in extra_paths})
                candidate['spec'] = json.dumps(spec)
                reason = self.eligible(candidate, reserved, statuses)
                if reason:
                    reasons[candidate['key']] = reason
                    continue
                token = uuid.uuid4().hex
                now = self.clock()
                self.db.execute('''UPDATE tickets SET spec=?,status='in_progress',token=?,agent=?,model=?,session=?,
                    heartbeat=?,expires=?,worktree=NULL,branch=NULL,evidence=NULL,revision=revision+1 WHERE key=?''',
                    (candidate['spec'], token, agent, model, session, now, now + ttl, candidate['key']))
                self.event(candidate['key'], session, 'claim', f'{agent}/{model}; token={token}; scope={spec["paths"]}')
                return self.view(self.row(candidate['key']))
            raise CoordinationError('No eligible ticket: ' + json.dumps(reasons, sort_keys=True))

    def owned(self, key, token, live=True):
        row = self.row(key)
        if not token or row['token'] != token:
            raise CoordinationError('Claim token no longer owns this ticket; stop editing')
        if live and row['expires'] is not None and row['expires'] <= self.clock():
            raise CoordinationError('Lease expired; stop editing and heartbeat before resuming')
        return row

    def heartbeat(self, key, token, ttl=900):
        if not 60 <= ttl <= 3600:
            raise CoordinationError('Lease must be 60–3600 seconds')
        with self.transaction():
            row = self.owned(key, token, live=False)
            if row['status'] not in {'in_progress', 'blocked'}:
                raise CoordinationError('Only implementation/blocked leases need heartbeats')
            self.db.execute('UPDATE tickets SET heartbeat=?, expires=? WHERE key=?', (self.clock(), self.clock() + ttl, key))
            return self.view(self.row(key))

    def assert_owner(self, key, token, paths=()):
        with self.transaction():
            row = self.owned(key, token)
            if row['status'] != 'in_progress':
                raise CoordinationError('Ticket is not in implementation; do not edit it')
            if row['revision'] != row['published_revision']:
                raise CoordinationError('Ownership mirror pending; publish before editing')
            remote = json.loads(row['remote'])
            if remote['state'] != 'open':
                raise CoordinationError('GitHub issue is closed; stop editing and coordinate review')
            if remote.get('external_blockers'):
                raise CoordinationError('External dependency incomplete; stop editing')
            for dependency in set(json.loads(row['spec'])['depends_on']) | set(remote.get('depends_on', [])):
                dep = self.row(dependency)
                if dep['status'] != 'done' or dep['revision'] != dep['published_revision']:
                    raise CoordinationError(f'Dependency no longer complete: {dependency}; stop editing')
            scope = json.loads(row['spec'])['paths']
            for path in paths:
                path = scope_path(path)
                if not any(s == '.' or path == s or path.startswith(s.rstrip('/') + '/') for s in map(scope_path, scope)):
                    raise CoordinationError(f'Path outside reserved scope: {path}')
            return self.view(row)

    def expand(self, key, token, paths):
        if not paths:
            raise CoordinationError('Supply additional paths')
        with self.transaction():
            row = self.owned(key, token)
            if row['status'] != 'in_progress':
                raise CoordinationError('Only implementation can expand scope')
            spec = json.loads(row['spec'])
            expanded = set(spec['paths']) | {scope_path(p) for p in paths}
            for other in self.db.execute('SELECT * FROM tickets WHERE key != ?', (key,)):
                if not self.holds_scope(other):
                    continue
                if any(overlaps(a, b) for a in expanded for b in json.loads(other['spec'])['paths']):
                    raise CoordinationError(f"Scope conflicts with {other['key']}")
            spec['paths'] = sorted(expanded)
            self.db.execute('UPDATE tickets SET spec=?, revision=revision+1 WHERE key=?', (json.dumps(spec), key))
            self.event(key, row['session'], 'scope', json.dumps(spec['paths']))
            return self.view(self.row(key))

    def attach_worktree(self, key, token, path, branch):
        with self.transaction():
            row = self.owned(key, token)
            if row['worktree'] or row['status'] != 'in_progress':
                raise CoordinationError('Claim already has a worktree or is not in progress')
            self.db.execute('UPDATE tickets SET worktree=?,branch=?,revision=revision+1 WHERE key=?', (str(path), branch, key))
            self.event(key, row['session'], 'worktree', str(path))

    def transition(self, key, token, status, evidence):
        if status not in {'review', 'blocked'} or not evidence.strip():
            raise CoordinationError('Review/blocked requires evidence or a blocker explanation')
        with self.transaction():
            row = self.owned(key, token)
            if row['status'] != 'in_progress':
                raise CoordinationError('Only an implementation can enter review/blocked')
            expires = None if status == 'review' else row['expires']
            self.db.execute('UPDATE tickets SET status=?,evidence=?,expires=?,revision=revision+1 WHERE key=?', (status, evidence, expires, key))
            self.event(key, row['session'], status, evidence)
            return self.view(self.row(key))

    def resume(self, key, token):
        with self.transaction():
            row = self.owned(key, token, live=False)
            if row['status'] not in {'review', 'blocked'}:
                raise CoordinationError('Only review/blocked can resume')
            other = self.db.execute("SELECT key FROM tickets WHERE key != ? AND session=? AND status IN ('in_progress','blocked')", (key, row['session'])).fetchone()
            if other:
                raise CoordinationError('This session already owns another implementation')
            self.db.execute("UPDATE tickets SET status='in_progress',heartbeat=?,expires=?,revision=revision+1 WHERE key=?", (self.clock(), self.clock() + 900, key))
            self.event(key, row['session'], 'resume', 'Reserved scope retained')
            return self.view(self.row(key))

    def release(self, key, token, reason, stopped):
        if not stopped or not reason.strip():
            raise CoordinationError('Release requires a reason and confirmation that editing has stopped')
        with self.transaction():
            row = self.owned(key, token, live=False)
            self.db.execute("UPDATE tickets SET status='backlog',token=NULL,expires=NULL,session=NULL,agent=NULL,model=NULL,revision=revision+1 WHERE key=?", (key,))
            self.event(key, row['session'], 'release', reason)
            return self.view(self.row(key))

    def recover(self, key, expected_token, reason, stopped):
        # No automatic expiration/reclaim: an old process could still write its checkout.
        if not stopped or not reason.strip():
            raise CoordinationError('Recovery requires explicit confirmation the old worker has stopped')
        return self.release(key, expected_token, 'Operator recovery: ' + reason, True)

    def complete(self, key, expected_token, reviewer, evidence):
        if not reviewer.strip() or not evidence.strip():
            raise CoordinationError('Completion requires reviewer and acceptance evidence')
        with self.transaction():
            row = self.owned(key, expected_token, live=False)
            if row['status'] != 'review':
                raise CoordinationError('Only reviewed work can complete')
            self.db.execute("UPDATE tickets SET status='done',token=NULL,expires=NULL,revision=revision+1 WHERE key=?", (key,))
            self.event(key, reviewer, 'complete', evidence)
            return self.view(self.row(key))

    def note(self, key, token, text):
        if not text.strip():
            raise CoordinationError('Empty note')
        with self.transaction():
            row = self.owned(key, token, live=False)
            self.event(key, row['session'], 'note', text)

    def publish(self, key, state, gh=None):
        module = tracking_module()
        gh = gh or module.Gh()
        # Serialize field writes with all claim mutations to prevent stale mirrors.
        with self.transaction():
            row = self.row(key)
            if row['revision'] == row['published_revision']:
                return
            project = state['project']
            fields = {f['name']: f for f in gh.run('project', 'field-list', str(project['number']), '--owner',
                project['owner']['login'], '--limit', '100', '--format', 'json', as_json=True)['fields']}
            item = json.loads(row['remote'])['item_id']
            status = {'in_progress': 'In progress', 'review': 'Review', 'blocked': 'Blocked', 'done': 'Done', 'backlog': 'Backlog'}[row['status']]
            for name, value in {'Status': status, 'Agent': row['agent'] or 'Unassigned'}.items():
                module.set_option(gh, project, item, fields[name], value)
            for name, value in {'Model': row['model'] or '', 'Work branch': row['branch'] or ''}.items():
                gh.graphql('''mutation($input: UpdateProjectV2ItemFieldValueInput!) {
                    updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
                }''', {'input': {'projectId': project['id'], 'itemId': item, 'fieldId': fields[name]['id'], 'value': {'text': value}}})
            self.db.execute('UPDATE tickets SET published_revision=revision WHERE key=?', (key,))


def prepare_worktree(coordinator, root, claim, requested=None, base='HEAD'):
    branch = f"work/{claim['key'].lower()}-{claim['token'][:8]}"
    path = Path(requested).resolve() if requested else Path(run_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).parent.parent / 'suifleet-worktrees' / branch.replace('/', '-')
    if path.exists():
        raise CoordinationError(f'Worktree path already exists; reservation retained: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    run_git(root, 'worktree', 'add', '-b', branch, str(path), base)
    coordinator.attach_worktree(claim['key'], claim['token'], path, branch)
    return coordinator.view(coordinator.row(claim['key']))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=ROOT, help='Canonical checkout or any linked worktree')
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('init', 'refresh', 'list'):
        sub.add_parser(name)
    claim = sub.add_parser('claim')
    claim.add_argument('--agent', choices=['Codex', 'Claude', 'Other'], required=True)
    claim.add_argument('--model', required=True)
    claim.add_argument('--session', required=True)
    claim.add_argument('--ticket')
    claim.add_argument('--path', action='append', default=[])
    claim.add_argument('--worktree', type=Path)
    claim.add_argument('--base', default='HEAD')
    claim.add_argument('--local-only', action='store_true', help='Keep the durable mirror pending; publish before editing')
    for name in ('show', 'publish'):
        command = sub.add_parser(name)
        command.add_argument('ticket')
    for name in ('heartbeat', 'assert', 'expand', 'review', 'block', 'resume', 'release', 'note'):
        command = sub.add_parser(name)
        command.add_argument('ticket')
        command.add_argument('--token', required=True)
        if name in {'assert', 'expand'}:
            command.add_argument('--path', action='append', default=[])
        if name in {'review', 'block', 'release', 'note'}:
            command.add_argument('--text', required=True, help='Evidence, handoff, blocker or note')
        if name == 'release':
            command.add_argument('--confirm-stopped', action='store_true')
        if name in {'review', 'block', 'resume', 'release'}:
            command.add_argument('--local-only', action='store_true')
    recover = sub.add_parser('recover')
    recover.add_argument('ticket')
    recover.add_argument('--expect-token', required=True)
    recover.add_argument('--text', required=True)
    recover.add_argument('--confirm-worker-stopped', action='store_true')
    recover.add_argument('--local-only', action='store_true')
    complete = sub.add_parser('complete')
    complete.add_argument('ticket')
    complete.add_argument('--expect-token', required=True)
    complete.add_argument('--reviewer', required=True)
    complete.add_argument('--text', required=True)
    complete.add_argument('--merged-commit', required=True)
    complete.add_argument('--integration-ref', default='main')
    complete.add_argument('--local-only', action='store_true')
    return p


def main():
    args = parser().parse_args()
    root = args.root.resolve()
    config = json.loads((root / 'docs/tracking/backlog.json').read_text())
    state = json.loads((root / 'docs/tracking/github-state.json').read_text())
    db_path = database_path(root)
    if args.command != 'init' and not db_path.exists():
        raise CoordinationError('Coordinator is not initialized; run init from the canonical checkout')
    coordinator = Coordinator(db_path)
    try:
        result = None
        if args.command in {'init', 'refresh'}:
            coordinator.import_catalog(config, live_snapshot(config, state))
            result = {'database': str(db_path), 'tickets': len(config['issues']), 'snapshot_max_age_seconds': MAX_SNAPSHOT_AGE}
        elif args.command == 'list':
            result = [coordinator.view(row) for row in coordinator.db.execute('SELECT * FROM tickets ORDER BY key')]
        elif args.command == 'show':
            result = coordinator.view(coordinator.row(args.ticket))
            result['events'] = [dict(r) for r in coordinator.db.execute('SELECT * FROM events WHERE ticket=? ORDER BY id', (args.ticket,))]
        elif args.command == 'claim':
            result = coordinator.claim(args.agent, args.model, args.session, args.ticket, args.path)
            # Output ownership immediately: subsequent worktree/network failure does not lose it.
            print(json.dumps({'reserved': result['key'], 'token': result['token'], 'database': str(db_path)}), file=sys.stderr, flush=True)
            result = prepare_worktree(coordinator, root, result, args.worktree, args.base)
        elif args.command == 'heartbeat':
            result = coordinator.heartbeat(args.ticket, args.token)
        elif args.command == 'assert':
            result = coordinator.assert_owner(args.ticket, args.token, args.path)
        elif args.command == 'expand':
            result = coordinator.expand(args.ticket, args.token, args.path)
        elif args.command in {'review', 'block'}:
            result = coordinator.transition(args.ticket, args.token, 'review' if args.command == 'review' else 'blocked', args.text)
        elif args.command == 'resume':
            result = coordinator.resume(args.ticket, args.token)
        elif args.command == 'release':
            result = coordinator.release(args.ticket, args.token, args.text, args.confirm_stopped)
        elif args.command == 'recover':
            result = coordinator.recover(args.ticket, args.expect_token, args.text, args.confirm_worker_stopped)
        elif args.command == 'complete':
            commit = run_git(root, 'rev-parse', '--verify', args.merged_commit + '^{commit}')
            run_git(root, 'merge-base', '--is-ancestor', commit, args.integration_ref)
            result = coordinator.complete(args.ticket, args.expect_token, args.reviewer, args.text + '\nMerged commit: ' + commit)
        elif args.command == 'note':
            coordinator.note(args.ticket, args.token, args.text)
            result = {'noted': args.ticket}
        if args.command in {'claim', 'expand', 'review', 'block', 'resume', 'release', 'recover', 'complete', 'publish'} and not getattr(args, 'local_only', False):
            key = result['key'] if args.command == 'claim' else args.ticket
            coordinator.publish(key, state)
            result = coordinator.view(coordinator.row(key))
        print(json.dumps(result, indent=2))
    finally:
        coordinator.close()


if __name__ == '__main__':
    try:
        main()
    except (CoordinationError, ValueError, RuntimeError, sqlite3.Error, OSError, KeyError) as error:
        raise SystemExit('Coordination stopped: ' + str(error) + '\nAny acquired claim remains reserved; inspect list/show before retrying.')
