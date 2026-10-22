#!/usr/bin/env python3
"""Dispatch authorized implementation slots; the ticket database owns tickets."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid

from coordinator import network_authorization, process_identity, worker_stopped, run, effective_policy

TICKET_KEY = re.compile(r'[A-Z][A-Z0-9]*-[0-9]+')
COMMIT = re.compile(r'[0-9a-f]{40}')
MAX_RESUMES_PER_CLAIM = 3
MAX_RESUME_ATTEMPTS = 5


def dependency_wait(result, key):
    """Return the prerequisite keys of a structured dependency-wait receipt.

    Anything else (free-form blocked text, hardware/production authorization,
    errors or extra conditions) is not resumable automatically."""
    wait = result.get('dependency_wait') if isinstance(result, dict) else None
    if (result.get('verdict') != 'blocked' or not isinstance(result.get('evidence'), str)
            or not result['evidence'].strip() or not isinstance(wait, dict)):
        return None
    if set(wait) != {'tickets', 'integration'} or wait['integration'] != 'merged_main':
        return None
    tickets = wait['tickets']
    if (not isinstance(tickets, list) or not 1 <= len(tickets) <= 16 or len(set(map(str, tickets))) != len(tickets)
            or not all(isinstance(t, str) and TICKET_KEY.fullmatch(t) and t != key for t in tickets)):
        return None
    return sorted(tickets)


class Dispatcher:
    def __init__(self, config, state):
        self.config, self.state = config, Path(state)
        for project in config['projects']:
            network_authorization(project)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.state/'dispatch.sqlite3', isolation_level=None)
        self.children = {}
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS runs (
            session TEXT PRIMARY KEY, project TEXT, family TEXT, status TEXT,
            directory TEXT, claim TEXT, pid INTEGER, started TEXT, message TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS backoff (
            project TEXT, family TEXT, attempts INTEGER, retry_after REAL, reason TEXT,
            PRIMARY KEY(project,family))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS retired_runs (
            session TEXT PRIMARY KEY, original_status TEXT, original_message TEXT,
            reason TEXT NOT NULL, retired_at REAL NOT NULL)''')
        self.db.execute('CREATE TABLE IF NOT EXISTS handoff_audits (source TEXT PRIMARY KEY, session TEXT UNIQUE, state TEXT, reason TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS eligibility (project TEXT PRIMARY KEY, fingerprint TEXT)')
        # One durable record per parked source receipt; terminal states never relaunch.
        self.db.execute('''CREATE TABLE IF NOT EXISTS dependency_resumes (
            source TEXT PRIMARY KEY, project TEXT, family TEXT, claim TEXT, state TEXT,
            session TEXT UNIQUE, attempts INTEGER, retry_after REAL, reason TEXT, updated REAL)''')
        for slot in config.get('implementation_slots', []):
            if slot['family'] not in ('Codex', 'Claude') or not slot['model'].strip():
                raise ValueError('Each slot needs a known family and actual model')
            if slot['project'] not in {p['id'] for p in config['projects']}:
                raise ValueError('Unknown slot project')
            limit = slot.get('max_quarantined', 3)
            if type(limit) is not int or not 1 <= limit <= 16:
                raise ValueError('max_quarantined must be an integer from 1 to 16')
            limit = slot.get('max_workers', 1)
            if type(limit) is not int or not 1 <= limit <= 16:
                raise ValueError('max_workers must be an integer from 1 to 16')
            command = config['workers'].get(slot['family'])
            if not isinstance(command, list) or not command or not all(isinstance(s, str) and s for s in command):
                raise ValueError('Worker command must be a nonempty argv list')
        pairs = [(s['project'], s['family']) for s in config.get('implementation_slots', [])]
        if len(set(pairs)) != len(pairs):
            raise ValueError('Use one configuration entry per project/family; set max_workers for concurrency')

    def ticket(self, project, *args):
        try:
            return json.loads(run(['ticket', '--project', project['id'], *args]))
        except subprocess.CalledProcessError as error:
            if not (args[0] == 'claim' and 'No eligible ticket:' in (error.stderr or '')):
                print(f"{project['id']}: ticket {args[0]} failed (exit {error.returncode}); scan retained ownership", flush=True)
            raise

    def defer(self, project, family, reason):
        old = self.db.execute('SELECT attempts FROM backoff WHERE project=? AND family=?', (project['id'],family)).fetchone()
        attempts = old[0]+1 if old else 1
        delay = min(900, 60 * 2**min(attempts-1,4))
        self.db.execute('INSERT OR REPLACE INTO backoff VALUES(?,?,?,?,?)',
                        (project['id'],family,attempts,time.time()+delay,reason))
        print(f"{project['id']}: {family} {reason}; retry in {delay}s",flush=True)

    def update(self, session, status, message):
        self.db.execute('UPDATE runs SET status=?,message=? WHERE session=?', (status, message, session))

    def stopped_identity(self, row):
        path = Path(row['directory'])/'process.json'
        identity = json.loads(path.read_text()) if path.exists() else dict(pid=row['pid'], started=row['started'])
        pid, started = identity['pid'], identity['started']
        # A reused PID is deliberately retained too: inspect it explicitly.
        if not pid or not started or process_identity(pid) is not None or not worker_stopped(pid, started):
            return None
        if row['claim']:
            helpers = self.claim_helpers(json.loads(row['claim']))
            (Path(row['directory'])/'helpers.json').write_text(json.dumps(helpers))
            if helpers:
                return None
        return identity

    def replacement_repair(self, project, claim, identity):
        path = self.state/'reviews.sqlite3'
        with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            if 'repair_claim' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                return False  # Historical jobs have no fenced same-claim proof.
            expected = dict(claim=hashlib.sha256(claim['token'].encode()).hexdigest(),
                            key=claim['key'], branch=claim['branch'], worktree=claim['worktree'])
            for job in db.execute("SELECT * FROM jobs WHERE project=? AND kind='fix' AND repair_claim IS NOT NULL",
                                  (project['id'],)):
                proof = json.loads(job['repair_claim'])
                proof.pop('head', None)
                if proof != expected or not job['pid'] or not job['started']:
                    continue
                if int(job['started']) <= int(identity['started']):
                    continue
                receipt = Path(job['directory'])/'process.json'
                if not receipt.exists() or json.loads(receipt.read_text()) != dict(pid=job['pid'], started=job['started']):
                    continue
                if job['status'] == 'running' and process_identity(job['pid']) == job['started']:
                    return True
                if job['status'] == 'done' and worker_stopped(job['pid'], job['started']):
                    result = json.loads(job['result'])
                    if result.get('verdict') == 'fixed' and result.get('evidence'):
                        return True
        return False

    def retire_obsolete(self, project):
        rows = self.db.execute("SELECT * FROM runs WHERE project=? AND status IN ('parked','quarantined')",
                               (project['id'],)).fetchall()
        for row in rows:
            authority = None
            try:
                claim = json.loads(row['claim'])
                identity = self.stopped_identity(row)
                if identity is None:
                    continue
                owner = self.ticket(project, 'show', claim['key'])
                if owner.get('publish_pending'):
                    continue
                # Lock the authority before the receipt store, never call ticket
                # while locked. No ownership writes are made by this connection.
                path = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))/'atomic-ticketing/coordination.sqlite3'
                authority = sqlite3.connect(f'{path.as_uri()}?mode=rw', uri=True, isolation_level=None, timeout=1)
                authority.row_factory = sqlite3.Row
                authority.execute('BEGIN IMMEDIATE')
                current = authority.execute('SELECT * FROM tickets WHERE key=? AND project=?',
                                            (project['id']+'::'+claim['key'], project['id'])).fetchone()
                fields = ('status','token','session','branch','worktree','revision','published_revision')
                if not current or any(current[k] != owner.get(k) for k in fields):
                    continue
                if current['revision'] != current['published_revision']:
                    continue
                same = all(current[k] == claim[k] for k in ('token','session','branch','worktree'))
                reason = None
                if current['status'] == 'done' and current['token'] is None:
                    reason = 'authoritative published Done'
                elif current['token'] != claim['token']:
                    # Recovery/release must follow this exact historical claim,
                    # not merely some earlier worker for the same ticket.
                    events = authority.execute('SELECT * FROM events WHERE ticket=? ORDER BY id',
                                               (current['key'],)).fetchall()
                    generation = None
                    for event in events:
                        if event['kind'] == 'claim':
                            generation = (event['actor'] == claim['session'] and
                                          ('token='+claim['token']+';') in event['detail'])
                        elif generation and event['kind'] == 'release' and event['actor'] == claim['session']:
                            reason = 'explicit historical claim recovery/release'
                            break
                elif same and current['status'] == 'review' and current['evidence']:
                    reason = 'authoritative published Review handoff'
                elif same and current['status'] == 'in_progress' and self.replacement_repair(project, claim, identity):
                    reason = 'verified same-claim replacement repair'
                if reason is None:
                    continue
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    fresh = self.db.execute('SELECT * FROM runs WHERE session=?', (row['session'],)).fetchone()
                    if not fresh or dict(fresh) != dict(row) or self.stopped_identity(row) != identity:
                        continue
                    if reason == 'verified same-claim replacement repair' and not self.replacement_repair(project, claim, identity):
                        continue
                    # Preserve claim, directory, process and original message.
                    self.db.execute('INSERT INTO retired_runs(session,original_status,original_message,reason,retired_at) VALUES(?,?,?,?,?)',
                                    (row['session'],row['status'],row['message'],reason,time.time()))
                    self.db.execute("UPDATE runs SET status='finished',message=? WHERE session=? AND status=?",
                                    ('Archived obsolete stopped receipt: '+reason,row['session'],row['status']))
                    self.db.execute('COMMIT')
                finally:
                    if self.db.in_transaction:
                        self.db.execute('ROLLBACK')
            except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error, subprocess.CalledProcessError) as error:
                print(f"{project['id']}: obsolete receipt retained ({type(error).__name__})", flush=True)
            finally:
                if authority is not None:
                    authority.close()

    def claim_helpers(self, claim):
        """Record detached claim-bearing processes; never signal an uncertain PID."""
        helpers = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
                if fields[0] == 'Z':
                    continue
                if entry.stat().st_uid != os.getuid():
                    continue
                command = (entry/'cmdline').read_bytes()
                if b'heartbeat' not in command or b'ticket' not in command:
                    continue
                environment = (entry/'environ').read_bytes().split(b'\0')
                marker = ('PR_COORDINATOR_CLAIM_TOKEN='+claim['token']).encode()
                if marker in environment or claim['token'].encode() in command:
                    helpers.append(dict(pid=int(entry.name), started=fields[19]))
            except (FileNotFoundError, ProcessLookupError):
                continue
        return helpers

    def audit_missing_handoff(self, project):
        rows = self.db.execute("SELECT * FROM runs WHERE project=? AND status='quarantined'",
                               (project['id'],)).fetchall()
        for row in rows:
            authority = None
            try:
                if self.db.execute('SELECT 1 FROM handoff_audits WHERE source=? OR session=?',
                                   (row['session'], row['session'])).fetchone():
                    continue
                directory = Path(row['directory'])
                # Require durable wrapper identity and actual clean exit, not a guessed PID.
                if not (directory/'process.json').exists() or (directory/'result.json').exists():
                    continue
                if json.loads((directory/'exit.json').read_text()) != {'returncode': 0}:
                    continue
                identity = self.stopped_identity(row)
                if identity is None:
                    continue
                claim = json.loads(row['claim'])
                if claim.get('agent') != row['family']:
                    continue
                helpers = self.claim_helpers(claim)
                (directory/'helpers.json').write_text(json.dumps(helpers))
                if helpers:
                    continue  # Includes detached heartbeat helpers; retain ownership and capacity.
                owner = self.ticket(project, 'show', claim['key'])
                fields = ('token', 'session', 'branch', 'worktree')
                if (owner.get('publish_pending') or owner.get('status') != 'in_progress'
                        or any(owner.get(k) != claim.get(k) for k in (*fields, 'agent', 'model'))):
                    continue
                if run(['git','branch','--show-current'], cwd=claim['worktree']).strip() != claim['branch']:
                    continue
                if run(['git','status','--porcelain'], cwd=claim['worktree']).strip():
                    continue
                path = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))/'atomic-ticketing/coordination.sqlite3'
                authority = sqlite3.connect(f'{path.as_uri()}?mode=rw', uri=True, isolation_level=None, timeout=1)
                authority.row_factory = sqlite3.Row
                authority.execute('BEGIN IMMEDIATE')
                current = authority.execute('SELECT * FROM tickets WHERE key=? AND project=?',
                                            (project['id']+'::'+claim['key'], project['id'])).fetchone()
                if (not current or any(current[k] != owner.get(k) for k in (*fields, 'status', 'revision', 'published_revision'))
                        or current['revision'] != current['published_revision']):
                    continue
                if self.stopped_identity(row) != identity or self.claim_helpers(claim):
                    continue
                # Expiry preserves ownership. Renew only after proving that the
                # original worker is stopped and its published claim is unchanged.
                # The CLI takes its own authority lock; release ours first, then
                # assert all remote/dependency gates and fence ownership again.
                authority.execute('ROLLBACK')
                self.ticket(project, 'heartbeat', claim['key'], '--token', claim['token'])
                verified = self.ticket(project, 'assert', claim['key'], '--token', claim['token'])
                authority.execute('BEGIN IMMEDIATE')
                current = authority.execute('SELECT * FROM tickets WHERE key=? AND project=?',
                                            (project['id']+'::'+claim['key'], project['id'])).fetchone()
                if (not current or any(current[k] != owner.get(k) for k in (*fields, 'status', 'revision', 'published_revision'))
                        or any(verified.get(k) != owner.get(k) for k in (*fields, 'status', 'revision', 'published_revision'))
                        or self.stopped_identity(row) != identity or self.claim_helpers(claim)):
                    continue
                session = row['session']+'-handoff-audit'
                audit = directory/'handoff-audit'
                # Persist intent BEFORE any launch. Interrupted/failed launches never retry.
                self.db.execute('INSERT INTO handoff_audits VALUES(?,?,?,?)',
                                (row['session'], session, 'attempted', 'one same-claim evidence audit'))
                audit.mkdir(mode=0o700)
                command = self.config['workers'][row['family']]
                (audit/'command.json').write_text(json.dumps(command))
                (audit/'limits.json').write_text(json.dumps({'timeout_seconds': 900}))
                instructions = f"""Audit the existing stopped implementation only. Original evidence/logs: {directory}.
This is the single bounded same-family, same-claim handoff audit, not a new implementation.
Inspect existing commits, clean branch, PR/head, actual test receipts and current-head CI.
Do not rebuild blindly, restart workers, claim/recover/release tickets, change branches or fabricate evidence.
If evidence is incomplete or writes are uncertain, record a concrete blocker and retain the claim.
Hand off completed work only after verifying all implementation gates. Preserve original receipts.
"""
                (audit/'prompt.txt').write_text(instructions+self.prompt(project, claim, audit).replace('Implement existing ticket', 'Audit existing ticket').replace('Implement, run required meaningful tests, record exact results and remaining acceptance gaps.', 'Inspect existing implementation and exact test results; do not implement or rebuild in this audit.'))
                token = run(['gh','auth','token','--hostname','github.com','--user',project['github_account']]).strip()
                env = {**os.environ, 'GH_TOKEN': token, 'PR_COORDINATOR_ACTIVE':'1',
                       'PR_COORDINATOR_CLAIM_TOKEN':claim['token']}
                self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                                (session, project['id'], row['family'], 'launching', str(audit), row['claim'], None, None,
                                 'Bounded evidence audit; original receipts retained'))
                child = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).with_name('worker.py')), str(audit)],
                                         cwd=claim['worktree'], env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                self.children[session] = child
                self.db.execute("UPDATE runs SET status='running',pid=?,started=? WHERE session=?",
                                (child.pid, process_identity(child.pid), session))
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.CalledProcessError):
                # Failed mirrors, ownership races, uncertain launches remain reserved.
                continue
            finally:
                if authority is not None:
                    authority.close()

    def dependency_status(self, project, tickets):
        """Return (None, commits) when every prerequisite is published Done and
        its recorded merge commit is in origin/main; otherwise (reason, None)."""
        policy = project.get('acceptance_policy', {}).get('development_defaults', {})
        excluded = [key for key in tickets if key in policy.get('acceptance_only_tickets', [])]
        if excluded:
            return 'acceptance-only prerequisite needs explicit recovery: ' + ', '.join(excluded), None
        commits, missing = {}, []
        for key in tickets:
            dependency = self.ticket(project, 'show', key)
            if (dependency.get('status') != 'done' or dependency.get('token') is not None
                    or dependency.get('publish_pending')):
                missing.append(key)
                continue
            merged = [event['detail'].rsplit('Merged commit: ', 1)[1].strip() for event in dependency.get('events', [])
                      if event.get('kind') == 'complete' and 'Merged commit: ' in (event.get('detail') or '')]
            if not merged or not COMMIT.fullmatch(merged[-1]):
                return f'{key} is Done without merged-commit evidence; needs explicit recovery', None
            commits[key] = merged[-1]
        if missing:
            return 'waiting for merged prerequisites: ' + ', '.join(missing), None
        run(['git', 'fetch', 'origin', 'main'], cwd=project['root'])
        for key, commit in commits.items():
            try:
                run(['git', 'merge-base', '--is-ancestor', commit, 'origin/main'], cwd=project['root'])
            except subprocess.CalledProcessError:
                return f'{key} merge commit {commit[:12]} is not in origin/main', None
        return None, commits

    def resume_note(self, source, state, reason, attempts=None, retry_after=None):
        """Persist visible resume state; also surface it on the parked receipt."""
        old = self.db.execute('SELECT * FROM dependency_resumes WHERE source=?', (source,)).fetchone()
        if old and (old['state'], old['reason']) == (state, reason) and attempts is None:
            return
        self.db.execute('''UPDATE dependency_resumes SET state=?,reason=?,attempts=COALESCE(?,attempts),
                           retry_after=COALESCE(?,retry_after),updated=? WHERE source=?''',
                        (state, reason, attempts, retry_after, time.time(), source))
        self.db.execute("UPDATE runs SET message=? WHERE session=? AND status='parked'",
                        (f'Dependency wait {state}: {reason}', source))
        print(f'dependency resume {source}: {state}: {reason}', flush=True)

    def resume_dependency_waits(self, project, slots, owners):
        """Resume a verified stopped dependency wait once, on its unchanged claim."""
        families = {slot['family']: slot for slot in slots}
        launched = 0
        rows = self.db.execute("SELECT * FROM runs WHERE project=? AND status='parked' ORDER BY session",
                               (project['id'],)).fetchall()
        for row in rows:
            source = row['session']
            authority = None
            try:
                directory = Path(row['directory'])
                claim = json.loads(row['claim'])
                result_path = directory/'result.json'
                result = json.loads(result_path.read_text()) if result_path.exists() else {}
                tickets = dependency_wait(result, claim['key'])
                if tickets is None:
                    continue  # Ordinary blocked/hardware/authorization: explicit recovery only.
                record = self.db.execute('SELECT * FROM dependency_resumes WHERE source=?', (source,)).fetchone()
                if record is None:
                    self.db.execute('INSERT INTO dependency_resumes VALUES(?,?,?,?,?,?,?,?,?,?)',
                                    (source, project['id'], row['family'], hashlib.sha256(claim['token'].encode()).hexdigest(),
                                     'waiting', None, 0, 0, 'recorded dependency wait: ' + ', '.join(tickets), time.time()))
                    record = self.db.execute('SELECT * FROM dependency_resumes WHERE source=?', (source,)).fetchone()
                if record['state'] in ('launched', 'failed') or (record['retry_after'] or 0) > time.time():
                    continue
                slot = families.get(row['family'])
                if slot is None or claim.get('agent') != row['family']:
                    self.resume_note(source, 'waiting', 'no enabled slot for the original family')
                    continue
                if record['state'] == 'waiting':
                    if json.loads((directory/'exit.json').read_text()) != {'returncode': 0}:
                        continue  # Not a clean handoff; parked rows normally guarantee this.
                    reason, commits = self.dependency_status(project, tickets)
                    if reason:
                        self.resume_note(source, 'waiting', reason)
                        continue
                    used = self.db.execute("SELECT count(*) FROM dependency_resumes WHERE claim=? AND state IN ('resuming','launched','failed')",
                                           (record['claim'],)).fetchone()[0]
                    if used >= MAX_RESUMES_PER_CLAIM:
                        self.resume_note(source, 'failed', f'resume limit {MAX_RESUMES_PER_CLAIM} reached for this claim; explicit recovery required')
                        continue
                    identity = self.stopped_identity(row)
                    if identity is None:
                        self.resume_note(source, 'waiting', 'original worker or claim helper not verified stopped')
                        continue
                    owner = self.ticket(project, 'show', claim['key'])
                    fields = ('token', 'session', 'branch', 'worktree', 'agent', 'model')
                    if owner.get('publish_pending') or owner.get('status') != 'blocked' or any(owner.get(k) != claim.get(k) for k in fields):
                        self.resume_note(source, 'waiting', 'ownership is not the unchanged published blocked claim')
                        continue
                    if (run(['git', 'branch', '--show-current'], cwd=claim['worktree']).strip() != claim['branch']
                            or run(['git', 'status', '--porcelain'], cwd=claim['worktree']).strip()):
                        self.resume_note(source, 'waiting', 'original worktree is not clean on the original branch')
                        continue
                    if not self.available(slot, owners, retention=False):
                        self.resume_note(source, 'waiting', 'prerequisites merged; waiting for implementation capacity')
                        continue
                    path = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))/'atomic-ticketing/coordination.sqlite3'
                    authority = sqlite3.connect(f'{path.as_uri()}?mode=rw', uri=True, isolation_level=None, timeout=1)
                    authority.row_factory = sqlite3.Row
                    authority.execute('BEGIN IMMEDIATE')
                    current = authority.execute('SELECT * FROM tickets WHERE key=? AND project=?',
                                                (project['id']+'::'+claim['key'], project['id'])).fetchone()
                    compared = ('status', 'token', 'session', 'branch', 'worktree', 'revision', 'published_revision')
                    if (not current or any(current[k] != owner.get(k) for k in compared)
                            or current['revision'] != current['published_revision']
                            or self.stopped_identity(row) != identity or self.claim_helpers(claim)):
                        self.resume_note(source, 'waiting', 'ownership or process state changed during verification')
                        continue
                    # Persist intent under the authority lock, BEFORE any ownership mutation.
                    self.db.execute("UPDATE dependency_resumes SET state='resuming',attempts=0,retry_after=0,reason=?,updated=? WHERE source=? AND state='waiting'",
                                    ('prerequisites merged: ' + ', '.join(f'{k}@{c[:12]}' for k, c in commits.items()), time.time(), source))
                    authority.execute('ROLLBACK')
                    authority.close()
                    authority = None
                    record = self.db.execute('SELECT * FROM dependency_resumes WHERE source=?', (source,)).fetchone()
                    if record['state'] != 'resuming':
                        continue
                launched += self.continue_resume(project, row, claim, tickets, record)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.CalledProcessError) as error:
                # Pre-intent failures are retried next scan without any mutation.
                print(f"{project['id']}: dependency resume check deferred for {source} ({type(error).__name__})", flush=True)
            finally:
                if authority is not None:
                    authority.close()
        return launched

    def continue_resume(self, project, row, claim, tickets, record):
        """Drive one recorded resume intent to a single launch, with bounded retry."""
        source = row['session']
        try:
            owner = self.ticket(project, 'show', claim['key'])
            if any(owner.get(k) != claim.get(k) for k in ('token', 'session', 'branch', 'worktree', 'agent', 'model')):
                self.resume_note(source, 'failed', 'ownership changed; original claim never resumed')
                return 0
            if owner.get('status') == 'blocked':
                owner = self.ticket(project, 'resume', claim['key'], '--token', claim['token'])
            elif owner.get('status') != 'in_progress':
                self.resume_note(source, 'failed', f"unexpected ticket status {owner.get('status')}; explicit recovery required")
                return 0
            if owner.get('publish_pending'):
                owner = self.ticket(project, 'publish', claim['key'])
            self.ticket(project, 'heartbeat', claim['key'], '--token', claim['token'])
            # Assert enforces the live claim, published mirror, open issue and dependencies.
            self.ticket(project, 'assert', claim['key'], '--token', claim['token'])
            if (self.stopped_identity(row) is None or self.claim_helpers(claim)
                    or run(['git', 'branch', '--show-current'], cwd=claim['worktree']).strip() != claim['branch']
                    or run(['git', 'status', '--porcelain'], cwd=claim['worktree']).strip()):
                raise RuntimeError('original worker, helper or worktree state changed')
            reason, commits = self.dependency_status(project, tickets)
            if reason:
                raise RuntimeError(reason)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.CalledProcessError) as error:
            attempts = (record['attempts'] or 0) + 1
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            if attempts >= MAX_RESUME_ATTEMPTS:
                self.resume_note(source, 'failed', f'resume failed {attempts} times ({detail}); claim retained for explicit recovery', attempts)
            else:
                delay = min(900, 60 * 2**(attempts-1))
                self.resume_note(source, 'resuming', f'attempt {attempts} failed ({detail}); retry in {delay}s',
                                 attempts, time.time()+delay)
            return 0
        session = source + '-resume'
        directory = Path(row['directory'])/'dependency-resume'
        self.db.execute('BEGIN IMMEDIATE')
        try:
            # Record the single launch BEFORE starting it: an interrupted or failed
            # launch is never repeated. The parked source is archived, not deleted.
            if self.db.execute("UPDATE dependency_resumes SET state='launched',session=?,updated=? WHERE source=? AND state='resuming'",
                               (session, time.time(), source)).rowcount != 1:
                return 0
            self.db.execute('INSERT INTO retired_runs(session,original_status,original_message,reason,retired_at) VALUES(?,?,?,?,?)',
                            (source, row['status'], row['message'], 'dependency wait resumed as '+session, time.time()))
            self.db.execute("UPDATE runs SET status='finished',message=? WHERE session=? AND status='parked'",
                            ('Dependency wait resumed as '+session, source))
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                            (session, project['id'], row['family'], 'launching', str(directory), row['claim'], None, None,
                             'Resumed retained claim after merged prerequisites'))
            self.db.execute('COMMIT')
        finally:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
        try:
            directory.mkdir(mode=0o700)
            merged = ', '.join(f'{key} (merge {commit})' for key, commit in commits.items())
            preface = f"""This resumes your retained claim after its recorded dependency wait. Original receipts: {row['directory']}.
Prerequisites now Done and merged into origin/main: {merged}.
The dispatcher already resumed the claim to In Progress; do not run ticket resume, claim, recover or release.
First fetch origin main and merge origin/main into your existing branch (never force-push or rewrite published history),
resolve conflicts, then continue the unfinished software and rerun all required tests on the merged result.
"""
            token = run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', project['github_account']]).strip()
            env = {**os.environ, 'GH_TOKEN': token, 'PR_COORDINATOR_ACTIVE': '1',
                   'PR_COORDINATOR_CLAIM_TOKEN': claim['token']}
            (directory/'command.json').write_text(json.dumps(self.config['workers'][row['family']]))
            (directory/'prompt.txt').write_text(preface + self.prompt(project, claim, directory))
            child = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).with_name('worker.py')), str(directory)],
                                     cwd=claim['worktree'], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.children[session] = child
            self.db.execute("UPDATE runs SET status='running',pid=?,started=? WHERE session=?",
                            (child.pid, process_identity(child.pid), session))
            print(f"{project['id']}: {row['family']} resumed {claim['key']} after {merged}", flush=True)
        except Exception as error:
            self.update(session, 'orphaned', f'Resume launch failed ({type(error).__name__}); inspect before explicit recovery')
            self.db.execute('UPDATE dependency_resumes SET reason=? WHERE source=?',
                            (f'launch failed ({type(error).__name__}); claim retained, never relaunched', source))
        return 1

    def reconcile(self, project):
        self.retire_obsolete(project)
        rows = self.db.execute("SELECT * FROM runs WHERE project=? AND status IN ('claiming','launching','running','orphaned')", (project['id'],)).fetchall()
        for row in rows:
            try:
                directory = Path(row['directory'])
                child = self.children.get(row['session'])
                if child is not None:
                    child.poll()  # Reap our exited wrappers before checking /proc identity.
                if row['status'] == 'claiming':
                    # Claim may have succeeded before its response was persisted. Never repeat it.
                    self.update(row['session'], 'orphaned', 'Interrupted claim; inspect ticket session and worktree before recovery')
                    continue
                if not row['claim']:
                    continue  # Interrupted claim has no trustworthy process/ownership identity.
                claim = json.loads(row['claim'])
                owner = self.ticket(project, 'show', claim['key'])
                completed = (owner['status'] == 'done' and owner.get('token') is None
                             and owner.get('session') == claim['session'] and owner.get('branch') == claim['branch'])
                ownership_changed = (any(owner.get(k) != claim.get(k) for k in ('token','session','branch','worktree')) and not completed)
                if ownership_changed and row['status'] != 'orphaned':
                    self.update(row['session'], 'orphaned', 'Ownership changed; inspect and stop old worker')
                    continue
                process = directory/'process.json'
                if process.exists():
                    identity = json.loads(process.read_text())
                    pid, started = identity['pid'], identity['started']
                else:
                    pid, started = row['pid'], row['started']
                alive = pid and started and process_identity(pid) == started
                receipt = directory/'exit.json'
                if alive:
                    if not ownership_changed and owner['status'] == 'in_progress':
                        self.ticket(project, 'heartbeat', claim['key'], '--token', claim['token'])
                    continue
                if not worker_stopped(pid, started):
                    continue  # A child may still be running after its wrapper disappeared.
                if ownership_changed:
                    self.update(row['session'], 'quarantined', 'Stopped worker; ownership changed; original claim never resumed')
                    self.defer(project, row['family'], 'stopped worker quarantined')
                    continue
                if self.claim_helpers(claim):
                    continue
                result = {}
                # Exit receipt alone is insufficient: the wrapper can write it just before exiting.
                if receipt.exists() and json.loads(receipt.read_text()).get('returncode') == 0:
                    result_path = directory/'result.json'
                    result = json.loads(result_path.read_text()) if result_path.exists() else {}
                    if (owner['status'] in ('review', 'done') and not owner.get('publish_pending')
                            and result.get('verdict') == 'review_ready' and result.get('evidence')):
                        self.update(row['session'], 'finished', 'Implementation handed off; review coordinator owns integration')
                        self.db.execute('DELETE FROM backoff WHERE project=? AND family=?', (project['id'],row['family']))
                        continue
                if (receipt.exists() and json.loads(receipt.read_text()).get('returncode') == 0
                        and owner['status'] == 'blocked' and not owner.get('publish_pending')
                        and result.get('verdict') == 'blocked' and result.get('evidence')):
                    self.update(row['session'], 'parked', 'Verified stopped blocked worker; ticket retained, execution capacity freed')
                    continue
                self.update(row['session'], 'quarantined', 'Verified stopped without handoff; claim retained for explicit recovery')
                self.defer(project, row['family'], 'stopped worker quarantined')
            except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
                # A corrupt receipt or one unavailable ticket must not starve
                # unrelated slots. Keep its existing capacity/ownership intact.
                print(f"{project['id']}: reconciliation deferred for {row['session']} ({type(error).__name__})", flush=True)

    def wake_on_change(self, project, owners):
        # Completion/new-ticket changes wake idle slots without remote polling or
        # resetting transport/quarantine safeguards. Heartbeats are not new work.
        relevant = sorted((row['key'], row['status'], row.get('agent')) for row in owners)
        fingerprint = hashlib.sha256(json.dumps(relevant).encode()).hexdigest()
        old = self.db.execute('SELECT fingerprint FROM eligibility WHERE project=?', (project['id'],)).fetchone()
        if not old or old[0] != fingerprint:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO eligibility VALUES(?,?)', (project['id'], fingerprint))
                self.db.execute("UPDATE backoff SET attempts=0,retry_after=0 WHERE project=? AND reason='idle: no eligible ticket'", (project['id'],))

    def available(self, slot, owners, retention=True):
        pause = self.db.execute('SELECT retry_after FROM backoff WHERE project=? AND family=?', (slot['project'],slot['family'])).fetchone()
        if pause and pause[0] > time.time():
            return False
        # Review and repair workers have their own capacity. Require the review
        # service database, but a lengthy review/repair must not pause a family.
        if not (self.state/'reviews.sqlite3').exists():
            return False
        rows = self.db.execute("SELECT * FROM runs WHERE project=? AND family=? AND status!='finished'",
                               (slot['project'], slot['family'])).fetchall()
        # Source and bounded audit receipts retain one claim, not two slots.
        # Missing/malformed claims remain separate conservative reservations.
        retained = set()
        for row in rows:
            if row['status'] not in ('quarantined', 'parked'):
                continue
            try:
                claim = json.loads(row['claim'])
                identity = (claim['key'], claim['token'])
                if not all(identity):
                    raise ValueError('Missing claim identity')
            except (ValueError, TypeError, KeyError):
                identity = ('receipt', row['session'])
            retained.add(identity)
        if retention and len(retained) >= slot.get('max_quarantined', 3):
            return False
        managed_sessions = {r['session'] for r in rows}
        # Real ticket responses preserve the dispatch session. Tests/legacy state
        # may preserve it only in the persisted claim; use both identities.
        managed_sessions.update(json.loads(r['claim']).get('session') for r in rows if r['claim'])
        manual = sum(1 for owner in owners
                     if owner.get('agent') == slot['family']
                     and owner['status'] in ('in_progress', 'blocked')
                     and (not owner.get('session') or owner['session'] not in managed_sessions))
        # Retained jobs free execution capacity only while still proven stopped.
        # A dependency resume in flight already holds its slot before launch.
        active = self.db.execute("SELECT count(*) FROM dependency_resumes WHERE project=? AND family=? AND state='resuming'",
                                 (slot['project'], slot['family'])).fetchone()[0]
        for row in rows:
            if row['status'] not in ('quarantined', 'parked'):
                active += 1
                continue
            try:
                active += self.stopped_identity(row) is None
            except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
                active += 1  # Unknown stop state still consumes execution capacity.
        return active + manual < slot.get('max_workers', 1)

    def launch(self, project, slot):
        # A fetch cannot acquire ownership. A transient pre-claim failure is retryable.
        try:
            run(['git','fetch','origin','main'], cwd=project['root'])
        except Exception:
            self.defer(project, slot['family'], 'fetch unavailable before claim')
            return
        session = 'dispatch-' + uuid.uuid4().hex
        directory = self.state/'implementations'/session
        directory.mkdir(parents=True, mode=0o700)
        self.db.execute('INSERT INTO runs(session,project,family,status,directory) VALUES(?,?,?,?,?)',
                        (session, project['id'], slot['family'], 'claiming', str(directory)))
        try:
            try:
                claim = self.ticket(project, 'claim', '--agent', slot['family'], '--model', slot['model'],
                                    '--session', session, '--base', 'origin/main')
            except subprocess.CalledProcessError as error:
                if 'No eligible ticket:' in (error.stderr or ''):
                    self.db.execute('DELETE FROM runs WHERE session=?', (session,))
                    self.defer(project,slot['family'],'idle: no eligible ticket')
                    return
                raise
            self.db.execute("UPDATE runs SET status='launching',claim=? WHERE session=?", (json.dumps(claim), session))
            if claim.get('publish_pending') or not claim.get('worktree') or claim['status'] != 'in_progress':
                raise RuntimeError('Claim not ready for implementation')
            self.ticket(project, 'assert', claim['key'], '--token', claim['token'])
            # Secrets stay in process environment/private SQLite, never argv or prompt files.
            token = run(['gh','auth','token','--hostname','github.com','--user',project['github_account']]).strip()
            env = {**os.environ, 'GH_TOKEN': token, 'PR_COORDINATOR_ACTIVE':'1',
                   'PR_COORDINATOR_CLAIM_TOKEN': claim['token']}
            (directory/'command.json').write_text(json.dumps(self.config['workers'][slot['family']]))
            (directory/'prompt.txt').write_text(self.prompt(project, claim, directory))
            child = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).with_name('worker.py')), str(directory)],
                                     cwd=claim['worktree'], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.children[session] = child
            self.db.execute("UPDATE runs SET status='running',pid=?,started=? WHERE session=?",
                            (child.pid, process_identity(child.pid), session))
            print(f"{project['id']}: {slot['family']} implementing {claim['key']}", flush=True)
        except Exception as error:
            # Keep even a partial claim reserved. A failure is never permission to reclaim.
            self.update(session, 'orphaned', f'Launch failed ({type(error).__name__}); inspect before explicit recovery')

    def prompt(self, project, claim, directory):
        return f'''Implement existing ticket {claim['key']} completely through Review. Read AGENTS.md and applicable docs first.
You are {claim['agent']}, configured model {claim['model']}; record your actual running model/version in ticket evidence.
Your existing session is {claim['session']}; branch {claim['branch']}, worktree {claim['worktree']}.
Claim token is PR_COORDINATOR_CLAIM_TOKEN in the environment; never print/post secrets. Do not claim another ticket.
Do not detach heartbeat helpers or other subprocesses; the dispatcher renews live workers.
Immediately heartbeat/assert; heartbeat at least every five minutes and assert after long commands and before edits/commits/push.
Read ticket show for the full specification/dependencies/notes; coordinate shared APIs in existing notes/issues.
Implement, run required meaningful tests, record exact results and remaining acceptance gaps. Do not fake acceptance or ignore failures.
User-authorized implementation acceptance scope: {json.dumps(effective_policy(project.get('acceptance_policy',{}),claim['key']),sort_keys=True)}
Only when that scope names your ticket, defer its listed external integration checks to the linked acceptance issues. The user permits code-reviewed implementation merge before those checks; local tests, isolated-chain upgrade/compatibility and source correctness remain required. Record limits and links without claiming external tests passed. This newer user scope overrides blanket live/hardware requirements for those listed checks only.
Use small scoped itemized commits with Tyler Bush <tyler.jonathan.bush@gmail.com> as BOTH author and committer; verify metadata.
No model authorship/coauthor/signoff attribution. Push your branch and create a PR with Closes followed by this ticket's existing issue URL.
Authorized: local builds, simulators and disposable network-none local Sui chains; publication on those disposable chains is test setup.
{network_authorization(project)}
Lock shared build/port/hardware resources only during actual use, renew while used and release before Review. Do not touch others' work.
Do not create any new issues, start model workers, merge PRs or relax test/review gates. Report newly discovered work in this issue.
If genuine external acceptance is unavailable, record it honestly and retain/block your claim; do not claim the next ticket yourself.
If blocked ONLY until prerequisite tickets merge, block the ticket with that explanation and add "dependency_wait": {{"tickets": ["SF-NNN"], "integration": "merged_main"}} to a blocked result; the dispatcher then resumes this claim once after they merge. Never use dependency_wait for failures, hardware, production or authorization blockers.
When implementation acceptance is ready, wait for current-head CI, mark PR Ready and ticket Review with complete evidence, release resources.
Write {directory/'result.json'} as JSON with verdict review_ready, actual model, and nonempty evidence including PR/head/tests/limits.
Then EXIT: the existing PR coordinator handles independent review/feedback/merge, and this dispatcher assigns your successor.
On a real blocker write verdict blocked with evidence and leave the claim reserved; do not silently return success.
Specification snapshot: {json.dumps(claim['spec'])}
'''

    def tick(self, execute=False):
        if not execute:
            return
        for project in self.config['projects']:
            self.reconcile(project)
            self.audit_missing_handoff(project)
            slots = [s for s in self.config.get('implementation_slots', [])
                     if s['project'] == project['id'] and s.get('enabled', False)]
            if not slots:
                continue
            owners = self.ticket(project, 'list')
            self.wake_on_change(project, owners)
            # Resuming retained work takes precedence over new claims.
            if self.resume_dependency_waits(project, slots, owners):
                owners = self.ticket(project, 'list')
            if not any(self.available(slot, owners) for slot in slots):
                continue
            try:
                self.ticket(project, 'refresh')
            except subprocess.CalledProcessError:
                for slot in slots:
                    self.defer(project,slot['family'],'refresh unavailable before claim')
                continue
            for slot in slots:
                # Bound each pass even if a claim response is malformed. Each
                # launch persists its reservation before another capacity check.
                for _ in range(slot.get('max_workers', 1)):
                    owners = self.ticket(project, 'list')
                    if not self.available(slot, owners):
                        break
                    self.launch(project, slot)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--state', type=Path, default=Path.home()/'.local/state/pr-coordinator')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--status', action='store_true', help='Read receipts only; no refresh, heartbeat or launch')
    parser.add_argument('--interval', type=int, default=60)
    args = parser.parse_args()
    os.umask(0o077)
    dispatcher = Dispatcher(json.loads(args.config.read_text()), args.state)
    if args.status:
        print(json.dumps([dict(r) for r in dispatcher.db.execute('''SELECT r.session,r.project,r.family,r.status,r.message,
            w.state AS dependency_resume,w.reason AS dependency_reason,w.attempts AS dependency_attempts,w.session AS resumed_as
            FROM runs r LEFT JOIN dependency_resumes w ON w.source=r.session''')], indent=2))
        return
    with open(args.state/'dispatcher.lock','w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.execute and args.watch and Path(__file__).with_name('installed.json').exists():
            from activation import ready
            ready(args.state, 'dispatcher')
        failures = 0
        while True:
            delay = max(10,args.interval)
            try:
                dispatcher.tick(args.execute)
                failures = 0
            except Exception as error:
                failures += 1
                delay = max(delay,min(900,60*2**min(failures-1,4)))
                print(f'Dispatch scan stopped ({type(error).__name__}); no claims recovered', flush=True)
                if not args.watch:
                    raise SystemExit(1)
            if not args.watch:
                break
            time.sleep(delay)


if __name__ == '__main__':
    main()
