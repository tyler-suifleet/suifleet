#!/usr/bin/env python3
"""Dispatch authorized implementation slots; the ticket database owns tickets."""
import argparse
from contextlib import closing
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import quote
import uuid

from coordinator import (network_authorization, process_identity, runnable_identity, worker_stopped, run, effective_policy,
                         failed_checks, repair_feedback, terminal_lines, quota_reset, claim_processes, uninspectable_reason,
                         QUOTA_GRACE, QUOTA_RETRIES, CI_HANDOFF)

TICKET_KEY = re.compile(r'[A-Z][A-Z0-9]*-[0-9]+')
COMMIT = re.compile(r'[0-9a-f]{40}')
MAX_RESUMES_PER_CLAIM = 3
MAX_RESUME_ATTEMPTS = 5
# SF-116 CI repair-first redirects of live managed implementation workers.
CI_REDIRECT_GRACE = 600       # seconds after the failed run completed: the owner may fix it first
CI_REDIRECT_RECHECK = 300     # bounded fresh GitHub revalidation while waiting
CI_REDIRECTS_PER_CLAIM = 3    # interruptions per claim; later failures stay visible only
CI_TERM_SECONDS = 60          # SIGTERM grace before SIGKILL of verified session members
CI_STOP_SECONDS = 300         # then the interruption is uncertain and needs explicit recovery
CI_KILL_SECONDS = 30          # after an attempted SIGKILL before the stop is declared uncertain
CI_GUARD_TTL = 600            # guard lease; renewed every scan while a worker is being stopped
GUARD_PREFIX = 'ci-redirect-guard-'
GUARD_MODEL = 'pr-coordinator dispatcher (no model)'
RESOURCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}')
# Repair deadline. The dispatcher stops an overdue repair only through the same
# guard-fenced escalation; the worker wrapper itself never kills it on a timer.
CI_REPAIR_TIMEOUT = 7200
CI_ATTEMPTS = 8              # bounded backoff (60-900 s) for failed steps, about an hour
# 'guarding', 'interrupting', 'releasing' and 'repair_stopping' hold guard leases; other states hold none.
CI_TERMINAL = ('resumed', 'superseded', 'handed_off', 'exhausted', 'repair_failed', 'conflict',
               'uncertain', 'orphaned', 'failed')
TYLER = {'GIT_AUTHOR_NAME': 'Tyler Bush', 'GIT_AUTHOR_EMAIL': 'tyler.jonathan.bush@gmail.com',
         'GIT_COMMITTER_NAME': 'Tyler Bush', 'GIT_COMMITTER_EMAIL': 'tyler.jonathan.bush@gmail.com'}


CAPACITY_RETRIES = 3
CAPACITY_DELAY = 60
QUOTA_PAUSE = 'provider quota reset wait'


def _capacity_failure(directory):
    """Recognize explicit terminal provider failures, never quoted model content.

    Receipts and output are bounded; success, signals/timeouts, user handoffs,
    usage/auth failures and unknown trailing errors fail closed.
    """
    lines = terminal_lines(directory)
    if not lines:
        return False
    pattern = re.compile(r'(?:ERROR: )?Selected model is at capacity\. Please try a different model\.?')
    # Codex text mode prints an anchored ERROR line; only known CLI accounting
    # metadata may follow. No JSON assistant/tool text is searched for substrings.
    while lines and (re.fullmatch(r'tokens used|[0-9,]+', lines[-1])
                     or re.fullmatch(r'\d{4}-\d\d-\d\dT[0-9:.]+Z ERROR codex_core::session: failed to record rollout items: thread [0-9a-f-]+ not found', lines[-1])):
        lines.pop()
    if lines and pattern.fullmatch(lines[-1]) and lines[-1].startswith('ERROR: '):
        return True
    try:
        terminal = json.loads(lines[-1])
    except (ValueError, IndexError):
        return False
    # Provider CLI terminal error envelopes only. A nested assistant message,
    # source excerpt or successful result containing these words never qualifies.
    if not isinstance(terminal, dict) or terminal.get('type') not in ('error', 'result'):
        return False
    if terminal.get('type') == 'result' and terminal.get('is_error') is not True:
        return False
    message = terminal.get('message') if terminal['type'] == 'error' else terminal.get('result')
    return isinstance(message, str) and bool(pattern.fullmatch(message))


def capacity_failure(directory):
    try:
        return _capacity_failure(directory)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


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
        # One durable record per (claim, failure fingerprint); terminal states never replay.
        self.db.execute('''CREATE TABLE IF NOT EXISTS ci_redirects (
            id INTEGER PRIMARY KEY, source TEXT, project TEXT, family TEXT, claim TEXT, ticket TEXT,
            pr INTEGER, branch TEXT, head TEXT, base TEXT, fingerprint TEXT, failures TEXT,
            state TEXT, reason TEXT, attempts INTEGER DEFAULT 0, retry_after REAL DEFAULT 0,
            members TEXT, signalled REAL, killed REAL DEFAULT 0, guard_session TEXT, guards TEXT, original_head TEXT,
            snapshot TEXT, repair_dir TEXT, repair_deadline REAL, repaired_head TEXT, resumed_as TEXT,
            created REAL, updated REAL, UNIQUE(claim, fingerprint))''')
        # A stable token digest survives replacement run/session names; one row
        # per stopped source preserves evidence and a crash-proof launch history.
        self.db.execute("""CREATE TABLE IF NOT EXISTS capacity_retries (
            source TEXT PRIMARY KEY, claim TEXT NOT NULL, project TEXT, family TEXT,
            state TEXT, attempt INTEGER, retry_after REAL, reason TEXT,
            replacement TEXT UNIQUE, updated REAL)""")
        # SF-145: provider quota resets share the same fenced same-claim recovery,
        # with their own per-claim budget. A side table keeps capacity_retries
        # unchanged for an activation rollback; rows without a kind are capacity.
        self.db.execute('CREATE TABLE IF NOT EXISTS capacity_kinds (source TEXT PRIMARY KEY, kind TEXT NOT NULL)')
        for project in config.get('projects', []):
            # Exhaustive shared-mutation resources fenced before any forced CI redirect.
            # Absent disables forced interruption; an inexact list is a configuration error.
            resources = project.get('ci_redirect_protected_resources')
            if resources is None:
                continue
            if (not isinstance(resources, list) or not resources or len(set(resources)) != len(resources)
                    or not all(isinstance(r, str) and RESOURCE.fullmatch(r) for r in resources)):
                raise ValueError('ci_redirect_protected_resources must be a nonempty list of unique lock resource names')
            grant = project.get('testnet_authorization')
            if grant and (not isinstance(grant, dict) or grant.get('lock_resource') not in resources):
                raise ValueError('ci_redirect_protected_resources must include the testnet authorization lock resource')
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
                if job['status'] in ('quota_wait', 'quota_launch') and proof.get('claim') == expected['claim']:
                    return True  # SF-145: a deferred same-claim repair owns this claim's recovery.
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

    def repair_owned(self, slot, owner):
        """Whether a manual In-progress claim is held by one verified live coordinator fix job.

        Such a resumed Review claim is repair work counted by review/fix
        capacity. Proof is the job's fenced repair_claim (token digest, key,
        branch, worktree), the current published owner generation and the
        job's live process receipt. A ticket key or snapshot alone never
        qualifies; any doubt keeps the implementation reservation.
        """
        try:
            if owner.get('status') != 'in_progress' or owner.get('expired') or owner.get('publish_pending'):
                return False
            project = next(p for p in self.config['projects'] if p['id'] == slot['project'])
            path = self.state/'reviews.sqlite3'
            with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as db:
                db.row_factory = sqlite3.Row
                if 'repair_claim' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                    return False  # Historical jobs have no fenced same-claim proof.
                jobs = db.execute("SELECT * FROM jobs WHERE project=? AND kind='fix' AND repair_claim IS NOT NULL "
                                  "AND status IN ('reserved','running','orphaned')", (project['id'],)).fetchall()
            # Every unfinished fix job naming this ticket must be the same single live repair.
            named = [job for job in jobs if json.loads(job['repair_claim']).get('key') == owner['key']]
            if len(named) != 1:
                return False
            job = named[0]
            proof = json.loads(job['repair_claim'])
            if (job['status'] != 'running' or job['family'] != slot['family'] or not job['pid'] or not job['started']
                    or any(proof.get(k) != owner.get(k) for k in ('branch', 'worktree'))):
                return False
            receipt = Path(job['directory'])/'process.json'
            if json.loads(receipt.read_text()) != dict(pid=job['pid'], started=job['started']):
                return False
            # An unreaped zombie keeps its start identity but is not a live repair.
            if runnable_identity(job['pid']) != job['started']:
                return False
            current = self.ticket(project, 'show', owner['key'])
            if (current.get('status') != 'in_progress' or current.get('expired') or current.get('publish_pending')
                    or current.get('revision') != current.get('published_revision')
                    or not isinstance(current.get('token'), str) or not current['token']
                    or hashlib.sha256(current['token'].encode()).hexdigest() != proof.get('claim')
                    or any(current.get(k) != owner.get(k) for k in ('key', 'agent', 'session', 'branch', 'worktree'))
                    or any(current.get(k) != proof.get(k) for k in ('key', 'branch', 'worktree'))):
                return False
            # SF-116 redirects and SF-117 capacity recoveries of this claim keep their own accounting.
            if self.db.execute("SELECT 1 FROM ci_redirects WHERE claim=? AND state NOT IN ('resumed','superseded','handed_off','exhausted')",
                               (proof['claim'],)).fetchone():
                return False
            if self.db.execute("SELECT 1 FROM capacity_retries WHERE claim=? AND state NOT IN ('cancelled','exhausted','launched')",
                               (proof['claim'],)).fetchone():
                return False
            return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration,
                sqlite3.Error, subprocess.CalledProcessError):
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

    # ---- SF-116: failed CI on active implementation PRs -------------------------

    def ci_observations(self, project):
        """Coordinator receipts of current failures on owned in-progress PRs (drafts too)."""
        path = self.state/'reviews.sqlite3'
        if not path.exists():
            return []
        with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ci_observations'").fetchone():
                return []  # Older coordinator: nothing to act on, nothing guessed.
            return [dict(r) for r in db.execute(
                "SELECT * FROM ci_observations WHERE project=? AND owner_status='in_progress'", (project['id'],))]

    def redirect_note(self, record, state, reason, retain=None, **fields):
        """Persist visible redirect state. retain='quarantined' frees execution
        capacity only once every recorded process is verified stopped."""
        fields = {**fields, 'state': state, 'reason': reason, 'updated': time.time()}
        self.db.execute(f"UPDATE ci_redirects SET {','.join(k+'=?' for k in fields)} WHERE id=?",
                        (*fields.values(), record['id']))
        if retain:
            self.db.execute("UPDATE runs SET status=?,message=? WHERE session=? AND status='redirected'",
                            (retain, f'CI redirect {state}: {reason}', record['source']))
        else:
            self.db.execute("UPDATE runs SET message=? WHERE session=? AND status='redirected'",
                            (f'CI redirect {state}: {reason}', record['source']))
        print(f"{record['project']}: CI redirect {record['id']} {record['ticket']} PR#{record['pr']}: {state}: {reason}", flush=True)

    def redirect_ci_failures(self, project):
        try:
            observations = self.ci_observations(project)
        except (OSError, sqlite3.Error) as error:
            observations = []
            print(f"{project['id']}: CI observations unavailable ({type(error).__name__})", flush=True)
        for row in self.db.execute("SELECT * FROM runs WHERE project=? AND status='running' AND claim IS NOT NULL",
                                   (project['id'],)).fetchall():
            try:
                claim = json.loads(row['claim'])
                for observation in observations:
                    if observation['branch'] == claim['branch'] and observation['ticket'] == claim['key']:
                        self.detect_redirect(project, row, claim, observation)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                print(f"{project['id']}: CI observation deferred for {row['session']} ({type(error).__name__})", flush=True)
        for record in self.db.execute(f"SELECT * FROM ci_redirects WHERE project=? AND state NOT IN ({','.join('?'*len(CI_TERMINAL))}) ORDER BY id",
                                      (project['id'], *CI_TERMINAL)).fetchall():
            if (record['retry_after'] or 0) > time.time():
                continue
            try:
                getattr(self, 'redirect_' + record['state'])(project, record)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, RuntimeError,
                    sqlite3.Error, subprocess.CalledProcessError) as error:
                attempts = (record['attempts'] or 0) + 1
                detail = str(error) if type(error) is RuntimeError else type(error).__name__
                delay = min(900, 60 * 2**(attempts-1))
                if attempts >= CI_ATTEMPTS and record['state'] in ('pending', 'guarding', 'releasing', 'stopped', 'integrating', 'resuming'):
                    # Every recorded process was stopped (or never signalled): free capacity, keep the claim.
                    # An unreleased guard lease expires within CI_GUARD_TTL.
                    self.redirect_note(record, 'failed', f'{record["state"]} failed {attempts} times ({detail}); claim, branch and preserved work retained for explicit recovery'
                                       + ('; unreleased guard leases expire within %ds' % CI_GUARD_TTL if record['state'] in ('guarding', 'releasing') else ''),
                                       retain=None if record['state'] in ('pending', 'guarding') else 'quarantined', attempts=attempts)
                else:
                    self.db.execute('UPDATE ci_redirects SET attempts=?,retry_after=?,reason=?,updated=? WHERE id=?',
                                    (attempts, time.time()+delay, f'{record["state"]}: attempt {attempts} deferred ({detail}); retry in {delay}s',
                                     time.time(), record['id']))
                    print(f"{project['id']}: CI redirect {record['id']} deferred ({detail})", flush=True)

    def detect_redirect(self, project, row, claim, observation):
        digest = hashlib.sha256(claim['token'].encode()).hexdigest()
        if self.db.execute('SELECT 1 FROM ci_redirects WHERE claim=? AND fingerprint=?',
                           (digest, observation['fingerprint'])).fetchone():
            return  # Deduplicated: each failure fingerprint is handled at most once per claim.
        if self.db.execute(f"SELECT 1 FROM ci_redirects WHERE claim=? AND state NOT IN ({','.join('?'*len(CI_TERMINAL))})",
                           (digest, *CI_TERMINAL)).fetchone():
            return  # One redirect at a time per claim.
        failures = json.loads(observation['failures'])
        completed = []
        for failure in failures:
            try:
                completed.append(datetime.fromisoformat(failure['completedAt'].replace('Z', '+00:00')).timestamp())
            except (KeyError, AttributeError, ValueError):
                completed.append(time.time())
        used = self.db.execute('SELECT count(*) FROM ci_redirects WHERE claim=? AND members IS NOT NULL', (digest,)).fetchone()[0]
        state, reason = 'pending', 'failed CI observed; owner grace period before revalidation'
        if used >= CI_REDIRECTS_PER_CLAIM:
            state, reason = 'exhausted', f'redirect limit {CI_REDIRECTS_PER_CLAIM} reached for this claim; failure stays visible for explicit owner repair'
        self.db.execute('''INSERT OR IGNORE INTO ci_redirects(source,project,family,claim,ticket,pr,branch,head,base,fingerprint,
                           failures,state,reason,retry_after,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (row['session'], project['id'], row['family'], digest, claim['key'], observation['pr'], claim['branch'],
                         observation['head'], observation['base'], observation['fingerprint'], observation['failures'], state, reason,
                         max(completed) + CI_REDIRECT_GRACE, time.time(), time.time()))
        print(f"{project['id']}: {claim['key']} PR#{observation['pr']} failed CI on active implementation: {state}", flush=True)

    def fresh_pr(self, project, number):
        """Minimal fresh read: exact head, current integration base and check runs."""
        token = run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', project['github_account']]).strip()
        env = {**os.environ, 'GH_TOKEN': token}
        pr = json.loads(run(['gh', 'pr', 'view', str(number), '--repo', project['repository'], '--json',
                             'number,headRefOid,headRefName,baseRefName,state,isDraft,isCrossRepository,statusCheckRollup'],
                            cwd=project['root'], env=env))
        ref = json.loads(run(['gh', 'api', f"repos/{project['repository']}/git/ref/heads/{quote(pr['baseRefName'], safe='/')}"],
                             cwd=project['root'], env=env))
        pr['baseRefOid'] = ref['object']['sha']
        # Testnet reports a commit status rather than a CheckRun. Revalidate it
        # for this exact head before redirecting an existing implementation.
        statuses = json.loads(run(['gh', 'api', f"repos/{project['repository']}/commits/{pr['headRefOid']}/status"],
                                  cwd=project['root'], env=env))
        pr['_testnet_status'] = next((row for row in statuses.get('statuses', [])
                                      if row.get('context') == 'testnet/package-upgrade'), None)
        return pr

    def failure_superseded(self, project, record):
        pr = self.fresh_pr(project, record['pr'])
        if pr['state'] != 'OPEN' or pr.get('isCrossRepository') or pr['headRefName'] != record['branch']:
            return 'PR is no longer an open same-repository PR for this branch'
        failures = failed_checks(pr)
        if not failures:
            return 'no current actionable failure (green, pending, cancelled or superseded by a rerun)'
        if repair_feedback({**pr, '_ci_repair': failures}) != record['fingerprint']:
            return 'head, base or failed runs changed; a fresh observation is required'
        return None

    def owner_state(self, project, claim):
        """'same' for the unchanged live in-progress claim, else the reason it is not."""
        owner = self.ticket(project, 'show', claim['key'])
        if any(owner.get(k) != claim.get(k) for k in ('token', 'session', 'branch', 'worktree', 'agent', 'model')):
            return 'changed'
        if owner.get('publish_pending'):
            return 'publish_pending'
        return 'same' if owner.get('status') == 'in_progress' else owner.get('status')

    @staticmethod
    def session_members(pid):
        """Live same-user members of a worker's Linux session, with start identities.

        Never call this for a session whose leader may have exited; use
        anchored_members(), which proves the numeric session ID was not reused."""
        members, foreign = [], False
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
                if fields[0] == 'Z' or int(fields[3]) != int(pid):
                    continue
                if entry.stat().st_uid != os.getuid():
                    foreign = True
                    continue
                members.append(dict(pid=int(entry.name), started=fields[19]))
            except (FileNotFoundError, ProcessLookupError):
                continue
        return sorted(members, key=lambda m: (m['pid'] != int(pid), m['pid'])), foreign

    @staticmethod
    def member_session(member):
        """Session ID of a live process whose start identity still matches, else None."""
        try:
            fields = Path(f"/proc/{member['pid']}/stat").read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            return None
        return int(fields[3]) if fields[0] != 'Z' and fields[19] == member['started'] else None

    @staticmethod
    def anchored_members(sid, recorded):
        """Members of session `sid` only while a recorded original member provably survives.

        A dead session's numeric ID can be reused by an unrelated session, whose
        members would pass their own fresh identity checks. A pidfd held on a
        recorded member, whose start identity and session still match before and
        after listing, pins the original session: the kernel never reuses a PID
        number another live process still uses as its session ID. Returns None
        (no expansion) without such an anchor."""
        for anchor in recorded:
            try:
                descriptor = os.pidfd_open(int(anchor['pid']))
            except OSError:
                continue
            try:
                if Dispatcher.member_session(anchor) != int(sid):
                    continue
                listed = Dispatcher.session_members(sid)
                signal.pidfd_send_signal(descriptor, 0)
                if Dispatcher.member_session(anchor) == int(sid):
                    return listed
            except OSError:
                continue
            finally:
                os.close(descriptor)
        return None

    @staticmethod
    def signal_members(members, number):
        """Signal only processes whose start identity still matches (pidfd fences PID reuse)."""
        for member in members:
            try:
                descriptor = os.pidfd_open(member['pid'])
            except OSError:
                continue
            try:
                if process_identity(member['pid']) == member['started']:
                    signal.pidfd_send_signal(descriptor, number)
            except OSError:
                pass
            finally:
                os.close(descriptor)

    def owner_locks(self, project, claim):
        """Live locks attributable to the owner's session or ticket; redirect guards excluded."""
        now, held = time.time(), []
        for lock in self.ticket(project, 'locks'):
            if (lock.get('expired') or (lock.get('expires') or 0) <= now
                    or str(lock.get('session') or '').startswith(GUARD_PREFIX)):
                continue
            ticket = str(lock.get('ticket') or '').split('::')[-1]
            named = set(TICKET_KEY.findall(f"{lock.get('session') or ''} {lock.get('purpose') or ''}".upper()))
            if lock.get('session') == claim['session'] or ticket == claim['key'] or claim['key'] in named:
                held.append(str(lock.get('resource')))
        return held

    @staticmethod
    def sui_activity(project, members):
        """Defence in depth: a session member running the Sui CLI or naming the wallet."""
        wallet = (project.get('testnet_authorization') or {}).get('wallet')
        for member in members:
            try:
                argv = (Path('/proc')/str(member['pid'])/'cmdline').read_bytes().split(b'\0')
            except (FileNotFoundError, ProcessLookupError):
                continue
            if any(Path(arg.decode(errors='replace')).name == 'sui' for arg in argv[:2]) or (wallet and wallet.encode() in b' '.join(argv)):
                return 'a worker process is running the Sui CLI or using the testnet wallet'
        return None

    def interruptible(self, project, claim, identity):
        """(members, blocker) for the live verified wrapper; members None if unverified."""
        listed = self.anchored_members(identity['pid'], [identity])
        if listed is None or not listed[0] or listed[0][0] != dict(pid=int(identity['pid']), started=identity['started']):
            return None, 'worker wrapper identity is no longer verified'
        members, foreign = listed
        if foreign:
            return members, "worker session contains another user's process"
        if self.claim_helpers(claim):
            return members, 'detached claim helper exists'
        held = self.owner_locks(project, claim)
        if held:
            return members, f"owner holds lock {', '.join(held)}"
        return members, self.sui_activity(project, members)

    # Guard leases: real exclusive `ticket lock` leases on every configured protected
    # resource, held only while a worker is being stopped. Workers already obey those
    # locks, so while all guards are held the owner cannot start a shared wallet,
    # network or hardware mutation, and acquiring a guard proves no lock-holding
    # mutation is in progress. A dispatch.sqlite3 transaction cannot fence them.

    def reload(self, record):
        return self.db.execute('SELECT * FROM ci_redirects WHERE id=?', (record['id'],)).fetchone()

    def save_guards(self, record, guards):
        # Lease tokens stay in the private dispatcher state; status output never selects them.
        self.db.execute('UPDATE ci_redirects SET guards=?,updated=? WHERE id=?', (json.dumps(guards), time.time(), record['id']))

    def guard_lock(self, project, record, resource):
        return self.ticket(project, 'lock', resource, '--agent', 'Other', '--model', GUARD_MODEL,
                           '--session', record['guard_session'], '--purpose',
                           f"{record['ticket']} CI redirect {record['id']}: fence shared mutations while its worker stops",
                           '--ttl', str(CI_GUARD_TTL))

    def acquire_guards(self, project, record):
        """Lease every protected resource; each receipt is persisted before the next
        step. Returns the first unavailable resource, else None."""
        guards = json.loads(record['guards'] or '[]')
        for resource in project['ci_redirect_protected_resources']:
            if resource in {g['resource'] for g in guards}:
                continue
            try:
                lease = self.guard_lock(project, record, resource)
            except subprocess.CalledProcessError:
                return resource
            guards.append(dict(resource=resource, token=lease['token'], expires=lease['expires']))
            self.save_guards(record, guards)
        return None

    def renew_guards(self, project, record):
        """Renew every guard. A lapsed lease is re-taken only if nobody holds it now,
        which equally proves no lock-holding mutation is running. Returns the first
        resource that is not fenced right now, else None."""
        guards = json.loads(record['guards'] or '[]')
        lost = None
        for guard in guards:
            try:
                lease = self.ticket(project, 'lock-renew', guard['resource'], '--lock-token', guard['token'],
                                    '--ttl', str(CI_GUARD_TTL))
            except subprocess.CalledProcessError:
                try:
                    lease = self.guard_lock(project, record, guard['resource'])
                    guard['token'] = lease['token']
                except subprocess.CalledProcessError:
                    lost = lost or guard['resource']
                    continue
            guard['expires'] = lease['expires']
        self.save_guards(record, guards)
        missing = sorted(set(project['ci_redirect_protected_resources']) - {g['resource'] for g in guards})
        return lost or (missing[0] if missing else None)

    def guards_asserted(self, project, record):
        guards = json.loads(record['guards'] or '[]')
        if {g['resource'] for g in guards} != set(project['ci_redirect_protected_resources']):
            return False
        try:
            for guard in guards:
                self.ticket(project, 'lock-assert', guard['resource'], '--lock-token', guard['token'])
        except subprocess.CalledProcessError:
            return False
        return True

    def release_guards(self, project, record):
        """Release every guard of this redirect, including one whose receipt a crash
        lost (re-locking under the same guard session returns its token). Raises
        while any remains held, so bounded retries continue."""
        if not record['guard_session']:
            return
        guards = json.loads(record['guards'] or '[]')
        for lock in self.ticket(project, 'locks'):
            if (lock.get('session') == record['guard_session'] and not lock.get('expired')
                    and lock.get('resource') not in {g['resource'] for g in guards}):
                lease = self.guard_lock(project, record, lock['resource'])
                guards.append(dict(resource=lock['resource'], token=lease['token'], expires=lease['expires']))
                self.save_guards(record, guards)
        for guard in guards:
            try:
                self.ticket(project, 'unlock', guard['resource'], '--lock-token', guard['token'])
            except subprocess.CalledProcessError:
                pass  # Already expired or released: verified below.
        remaining = sorted(str(l.get('resource')) for l in self.ticket(project, 'locks')
                           if l.get('session') == record['guard_session'] and not l.get('expired'))
        if remaining:
            raise RuntimeError(f"guard release incomplete ({', '.join(remaining)})")
        self.save_guards(record, [])

    def redirect_pending(self, project, record):
        row = self.db.execute('SELECT * FROM runs WHERE session=?', (record['source'],)).fetchone()
        if not row or row['status'] != 'running':
            self.redirect_note(record, 'superseded', 'worker no longer running before interruption; normal reconciliation applies')
            return
        claim = json.loads(row['claim'])
        process = Path(row['directory'])/'process.json'
        identity = json.loads(process.read_text()) if process.exists() else dict(pid=row['pid'], started=row['started'])
        if not identity['pid'] or runnable_identity(identity['pid']) != identity['started']:
            return  # Exited/zombie/reused: the normal reconciliation decides; never signal an unverified PID.
        owner = self.owner_state(project, claim)
        if owner in ('review', 'done'):
            self.redirect_note(record, 'handed_off', 'owner handed off before interruption; the PR coordinator owns Review CI repair')
            return
        if owner != 'same':
            self.redirect_note(record, 'superseded', f'ownership is not the unchanged published in-progress claim ({owner}); never acted on')
            return
        later = time.time() + CI_REDIRECT_RECHECK
        reason = self.failure_superseded(project, record)
        if reason:
            self.redirect_note(record, 'superseded', reason + '; owner not interrupted')
            return
        if not project.get('ci_redirect_protected_resources'):
            # The held-lock list is never assumed exhaustive: no explicit fence, no forced stop.
            self.redirect_note(record, 'pending', 'forced interruption disabled: project has no explicit ci_redirect_protected_resources; '
                               'failure stays visible for owner repair', retry_after=later)
            return
        members, blocker = self.interruptible(project, claim, identity)
        if members is None:
            return
        if blocker:
            self.redirect_note(record, 'pending', f'waiting to interrupt safely: {blocker}', retry_after=later)
            return
        self.redirect_note(record, 'guarding', 'acquiring shared-resource guard leases before any signal',
                           guard_session=f"{GUARD_PREFIX}{record['id']}-{uuid.uuid4().hex[:12]}", guards='[]')
        def check():
            # Recheck everything under the fence: the owner can no longer take a protected resource.
            members, blocker = self.interruptible(project, claim, identity)
            if members is not None and not blocker and self.owner_state(project, claim) != 'same':
                blocker = 'ownership changed while guarding'
            return members, blocker
        record, members, blocker = self.fenced(project, record, check)
        if members is None or blocker:
            self.redirect_note(record, 'pending', f'waiting to interrupt safely: {blocker or "worker identity changed"}; '
                               'guards released, worker left running', retry_after=later, guard_session=None, guards=None)
            return
        self.db.execute('BEGIN IMMEDIATE')
        try:
            fresh = self.db.execute('SELECT * FROM runs WHERE session=?', (row['session'],)).fetchone()
            if dict(fresh) != dict(row) or runnable_identity(identity['pid']) != identity['started']:
                self.db.execute('ROLLBACK')
                self.redirect_guarding(project, record)
                return
            # Persist the exact identities BEFORE signalling; restart never re-derives them.
            self.redirect_note(record, 'interrupting', 'verified live owner paused for repair-first work under held guards',
                               members=json.dumps(members), signalled=None, killed=0)
            self.db.execute("UPDATE runs SET status='redirected' WHERE session=? AND status='running'", (row['session'],))
            self.db.execute('COMMIT')
        finally:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
        self.signal_members(members, signal.SIGTERM)
        self.db.execute('UPDATE ci_redirects SET signalled=?,updated=? WHERE id=?', (time.time(), time.time(), record['id']))

    def fenced(self, project, record, check):
        """Acquire every guard of record['guard_session'], then rerun check() under the
        fence, where no lock-obeying process can start a protected mutation.
        Returns (record, members, blocker); guards are released again on any blocker."""
        record = self.reload(record)
        unavailable = self.acquire_guards(project, record)
        record = self.reload(record)
        if unavailable:
            members, blocker = None, f'shared resource {unavailable} is held (possible owner mutation)'
        else:
            members, blocker = check()
            if members is not None and not blocker and not self.guards_asserted(project, record):
                blocker = 'guard lease assertion failed'
        if members is None or blocker:
            self.release_guards(project, record)
        return record, members, blocker

    def redirect_guarding(self, project, record):
        """Crash/restart (or a lost race) before any signal: release and re-evaluate."""
        self.release_guards(project, record)
        self.redirect_note(record, 'pending', 'interruption not started; guards released and the worker left running',
                           retry_after=time.time(), guard_session=None, guards=None)

    def escalate(self, project, record, claim, stopped, revert):
        """Guard-fenced SIGTERM, then SIGKILL, of the recorded original members.

        Identities are persisted before each signal; `signalled` and `killed`
        record only attempts that actually returned, so a crash in between
        retries the identity- and guard-verified signal (repeating it is
        harmless). stopped(record) runs once every member is verified stopped;
        revert(record, lost) undoes an intent whose SIGTERM never ran."""
        child = self.children.get(record['source'] if record['state'] == 'interrupting' else f"ci-repair-{record['id']}")
        if child is not None:
            child.poll()
        members = json.loads(record['members'])
        wrapper = members[0]
        # A zombie never runs again (worker_stopped agrees); live original-session
        # descendants keep fencing even after the wrapper exited.
        alive = [m for m in members if self.member_session(m) is not None]
        if not alive and worker_stopped(wrapper['pid'], wrapper['started']) and not self.claim_helpers(claim):
            stopped(record)
            return
        lost = self.renew_guards(project, record)
        record = self.reload(record)
        if record['signalled'] is None:
            if lost:
                revert(record, lost)
                return
            self.signal_members(alive, signal.SIGTERM)
            self.db.execute('UPDATE ci_redirects SET signalled=?,updated=? WHERE id=?', (time.time(), time.time(), record['id']))
            return
        now = time.time()
        elapsed = now - record['signalled']
        if not record['killed'] and elapsed > CI_TERM_SECONDS:
            if lost or not self.guards_asserted(project, record):
                if elapsed > CI_STOP_SECONDS:
                    self.release_guards(project, record)
                    self.redirect_note(record, 'uncertain', f'stopping worker/session did not stop after SIGTERM; SIGKILL withheld: '
                                       f'guard {lost or "assertion"} unavailable while a shared mutation may run; guards released; '
                                       'inspect recorded PIDs; claim and capacity retained', guards=None)
                else:
                    self.db.execute('UPDATE ci_redirects SET reason=?,updated=? WHERE id=?',
                                    (f'guard {lost or "assertion"} unavailable; SIGKILL withheld while a shared mutation may run', now, record['id']))
                return
            # Expand only from a proven surviving original member, never a dead/reused session ID.
            listed = self.anchored_members(wrapper['pid'], alive)
            known = {(m['pid'], m['started']) for m in members}
            added = [m for m in (listed[0] if listed else []) if (m['pid'], m['started']) not in known]
            self.db.execute('UPDATE ci_redirects SET members=?,reason=?,updated=? WHERE id=?',
                            (json.dumps(members + added), 'SIGKILL pending under held guards for identity-verified original members',
                             now, record['id']))
            self.signal_members(alive + added, signal.SIGKILL)
            self.db.execute('UPDATE ci_redirects SET killed=?,reason=?,updated=? WHERE id=?',
                            (time.time(), 'SIGKILL attempted under held guards on identity-verified original members; awaiting stop',
                             time.time(), record['id']))
        elif record['killed'] and elapsed > CI_STOP_SECONDS and now - record['killed'] > CI_KILL_SECONDS:
            # After SIGKILL a process never runs user code again; what remains cannot be stopped by us.
            self.release_guards(project, record)
            self.redirect_note(record, 'uncertain', 'stopping worker/session did not stop after a SIGKILL attempted under held guards '
                               'on identity-verified original members; guards released; inspect recorded PIDs; claim and capacity retained',
                               guards=None)

    def redirect_interrupting(self, project, record):
        row = self.db.execute('SELECT * FROM runs WHERE session=?', (record['source'],)).fetchone()
        claim = json.loads(row['claim'])

        def stopped(record):
            self.redirect_note(record, 'releasing', 'owner worker and its session verified stopped; releasing guards')
            self.redirect_releasing(project, self.reload(record))

        def revert(record, lost):
            self.release_guards(project, record)
            self.db.execute("UPDATE runs SET status='running' WHERE session=? AND status='redirected'", (record['source'],))
            self.redirect_note(record, 'pending', f'guard {lost} lost before any signal; worker never signalled and left running',
                               retry_after=time.time(), guard_session=None, guards=None, members=None)
        self.escalate(project, record, claim, stopped, revert)

    def redirect_releasing(self, project, record):
        self.release_guards(project, record)
        self.redirect_note(record, 'stopped', 'owner worker verified stopped; guard leases released', guards=None, guard_session=None)

    def git_path(self, worktree, name):
        path = Path(run(['git', 'rev-parse', '--git-path', name], cwd=worktree).strip())
        return path if path.is_absolute() else Path(worktree)/path

    def worktree_tree(self, worktree):
        """Tree of the complete working state (tracked + untracked, not ignored) via a
        private copy of the index; the owner's real index and files are never written."""
        with tempfile.TemporaryDirectory(dir=self.state) as scratch:
            index = Path(scratch)/'index'
            source = self.git_path(worktree, 'index')
            if source.exists():
                index.write_bytes(source.read_bytes())  # Reuses stat data; content comes from the files.
            env = {**os.environ, 'GIT_INDEX_FILE': str(index)}
            if not source.exists():
                run(['git', 'read-tree', 'HEAD'], cwd=worktree, env=env)
            run(['git', 'add', '-A', '--', '.'], cwd=worktree, env=env)
            return run(['git', 'write-tree'], cwd=worktree, env=env).strip()

    def preserve(self, record, worktree):
        """Durable backup refs of unfinished work before any other step."""
        head = run(['git', 'rev-parse', 'HEAD'], cwd=worktree).strip()
        tree = self.worktree_tree(worktree)
        with tempfile.TemporaryDirectory(dir=self.state) as scratch:
            index = Path(scratch)/'index'
            index.write_bytes(self.git_path(worktree, 'index').read_bytes())
            staged = run(['git', 'write-tree'], cwd=worktree, env={**os.environ, 'GIT_INDEX_FILE': str(index)}).strip()
        env = {**os.environ, **TYLER}
        prefix = f"refs/pr-coordinator/ci-redirect/{record['source']}-{record['id']}"
        refs = {}
        for name, value in (('worktree', tree), ('index', staged)):
            commit = run(['git', 'commit-tree', value, '-p', head, '-m',
                          f"Preserve unfinished {record['ticket']} {name} state before CI repair redirect {record['id']}"],
                         cwd=worktree, env=env).strip()
            run(['git', 'update-ref', f'{prefix}/{name}', commit], cwd=worktree)
            refs[name] = dict(ref=f'{prefix}/{name}', commit=commit, tree=value)
        return head, refs

    def dirty_preserved(self, record, worktree):
        """Every path that differed from the original head still has its preserved content."""
        snapshot = json.loads(record['snapshot'])['worktree']
        changed = [p for p in run(['git', 'diff', '--name-only', '-z', record['original_head'], snapshot['commit']],
                                  cwd=worktree).split('\0') if p]
        current = self.worktree_tree(worktree)
        def blob(tree, path):
            try:
                return run(['git', 'rev-parse', f'{tree}:{path}'], cwd=worktree).strip()
            except subprocess.CalledProcessError:
                return None
        return all(blob(current, path) == blob(snapshot['tree'], path) for path in changed)

    def redirect_stopped(self, project, record):
        row = self.db.execute('SELECT * FROM runs WHERE session=?', (record['source'],)).fetchone()
        claim = json.loads(row['claim'])
        owner = self.owner_state(project, claim)
        if owner in ('review', 'done'):
            self.redirect_note(record, 'handed_off', 'owner handed off while being paused; Review reconciliation and coordinator repair apply',
                               retain='quarantined')
            return
        if owner != 'same':
            self.redirect_note(record, 'uncertain', f'ownership changed during interruption ({owner}); nothing resumed', retain='quarantined')
            return
        held = self.owner_locks(project, claim)
        if held:
            self.redirect_note(record, 'uncertain', f"paused owner still holds lock {', '.join(held)}; inspect and unlock before recovery",
                               retain='quarantined')
            return
        self.ticket(project, 'heartbeat', claim['key'], '--token', claim['token'])
        worktree = claim['worktree']
        if run(['git', 'branch', '--show-current'], cwd=worktree).strip() != claim['branch']:
            self.redirect_note(record, 'uncertain', 'feature worktree is not on the claimed branch; work left untouched', retain='quarantined')
            return
        busy = [name for name in ('index.lock', 'MERGE_HEAD', 'rebase-merge', 'rebase-apply', 'CHERRY_PICK_HEAD', 'REVERT_HEAD')
                if self.git_path(worktree, name).exists()]
        if busy:
            self.redirect_note(record, 'uncertain', f"interrupted git operation in feature worktree ({', '.join(busy)}); resolve manually, work left untouched",
                               retain='quarantined')
            return
        if not record['snapshot']:
            head, refs = self.preserve(record, worktree)
            self.db.execute('UPDATE ci_redirects SET original_head=?,snapshot=?,updated=? WHERE id=?',
                            (head, json.dumps(refs), time.time(), record['id']))
            record = self.db.execute('SELECT * FROM ci_redirects WHERE id=?', (record['id'],)).fetchone()
        reason = self.failure_superseded(project, record)
        remote = f"refs/pr-coordinator/ci-redirect/{record['source']}-{record['id']}/remote"
        run(['git', 'fetch', 'origin', f"+refs/heads/{claim['branch']}:{remote}"], cwd=project['root'])
        tip = run(['git', 'rev-parse', remote], cwd=project['root']).strip()
        if reason or tip != record['head']:
            self.redirect_note(record, 'resuming', (reason or 'branch moved after the failed head') + '; resuming without a repair worker')
            return
        self.ticket(project, 'assert', claim['key'], '--token', claim['token'])
        directory = Path(row['directory'])/f"ci-redirect-{record['id']}"/'repair'
        checkout = directory/'checkout'
        if checkout.exists():
            if (run(['git', 'rev-parse', 'HEAD'], cwd=checkout).strip() != record['head']
                    or run(['git', 'status', '--porcelain'], cwd=checkout).strip()):
                self.redirect_note(record, 'uncertain', 'existing repair checkout is not clean at the failed head', retain='quarantined')
                return
        else:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            run(['git', 'worktree', 'add', '--detach', str(checkout), record['head']], cwd=project['root'])
        (directory/'command.json').write_text(json.dumps(self.config['workers'][record['family']]))
        (directory/'prompt.txt').write_text(self.repair_prompt(project, claim, record, checkout, directory))
        # No limits.json: worker.py would kill the model on its own timer, without guards.
        for name in ('process.json', 'exit.json', 'result.json', 'limits.json'):
            (directory/name).unlink(missing_ok=True)
        token = run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', project['github_account']]).strip()
        env = {**os.environ, 'GH_TOKEN': token, 'PR_COORDINATOR_ACTIVE': '1', 'PR_COORDINATOR_CLAIM_TOKEN': claim['token']}
        # Recorded BEFORE spawning: an interrupted launch is never repeated.
        self.redirect_note(record, 'repairing', 'same-claim repair worker in a clean linked checkout', repair_dir=str(directory),
                           repair_deadline=time.time() + CI_REPAIR_TIMEOUT, members=None, signalled=None, killed=0)
        try:
            child = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).with_name('worker.py')), str(directory)],
                                     cwd=checkout, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            self.children[f"ci-repair-{record['id']}"] = child
        except OSError as error:
            self.redirect_note(record, 'orphaned', f'repair launch failed ({type(error).__name__}); never relaunched; claim retained')

    def repair_prompt(self, project, claim, record, checkout, directory):
        return f'''CI REPAIR-FIRST for your existing {claim['agent']} claim {claim['key']} (PR #{record['pr']}, branch {claim['branch']}).
Completed CI failed on exact head {record['head']} with integration base {record['base']}: {record['failures']}
The dispatcher verified and paused your implementation worker, which had continued despite these failures. Its unfinished
uncommitted work stays in {claim['worktree']}; NEVER edit, clean, stash, reset, check out or commit in that worktree.
You are in a clean linked repair checkout {checkout}, detached at {record['head']}. Same claim, session {claim['session']} and branch.
Claim token is PR_COORDINATOR_CLAIM_TOKEN in the environment; never print/post secrets. Assert before edits/commits and heartbeat every five minutes.
Inspect the exact failed runs and logs (gh run view --log-failed), reproduce locally and fix the cause narrowly. Never weaken, skip or waive checks.
Only with concrete evidence of a transient infrastructure failure (not a code or test failure) may you request one rerun of that failed job
(gh run rerun RUN_ID --failed) instead of a code change; then use verdict rerun_requested with that evidence and push nothing.
Commit only the repair, with Tyler Bush <tyler.jonathan.bush@gmail.com> as BOTH author and committer, an itemized body and no model attribution.
Push with: git push origin HEAD:refs/heads/{claim['branch']} (fast-forward only; never force or rewrite history). Leave this checkout clean.
Do not mark Review, block, release, resume or claim tickets; do not merge, create PRs or issues, or start workers. Your unfinished implementation resumes automatically after you exit.
{self.repair_network_scope(project)}
Lock shared build/port resources only during actual use and release them before exiting. Do not touch others' work.
Result file: {directory/'result.json'}
Write it as JSON with keys verdict (fixed, rerun_requested or blocked), model (actual model/version) and evidence (a nonempty string:
cause, commits, exact local test commands and results, pushed head). If you cannot repair it, write blocked with the concrete next step.
Specification snapshot: {json.dumps(claim.get('spec', {}))}
'''

    @staticmethod
    def repair_network_scope(project):
        resources = ', '.join(project.get('ci_redirect_protected_resources') or ['network:suifleet-testnet'])
        return f'''This bounded CI-repair subtask has NO machine-session shared-network or hardware mutation authority: do not lock
{resources}, and do not publish, upgrade, transact, request faucet funds or program devices from this machine. Read-only
network checks and disposable isolated local chains remain authorized. Public-testnet acceptance of the repair runs through
the normal CI triggered by your push. If the repair genuinely needs a machine-session testnet or hardware run, write verdict
blocked naming that need. Your standing authorization returns unchanged when the implementation resumes.'''

    def redirect_repairing(self, project, record):
        row = self.db.execute('SELECT * FROM runs WHERE session=?', (record['source'],)).fetchone()
        claim = json.loads(row['claim'])
        directory = Path(record['repair_dir'])
        if record['guard_session']:
            # Crash/race while fencing an overdue repair before any signal: release first.
            self.release_guards(project, record)
            self.db.execute('UPDATE ci_redirects SET guard_session=NULL,guards=NULL,members=NULL,signalled=NULL,killed=0 WHERE id=?',
                            (record['id'],))
            record = self.reload(record)
        child = self.children.get(f"ci-repair-{record['id']}")
        if child is not None:
            child.poll()
        if not (directory/'process.json').exists():
            if time.time() - record['updated'] > 60:
                self.redirect_note(record, 'orphaned', 'repair launch interrupted before its process receipt; never relaunched; claim retained')
            return
        identity = json.loads((directory/'process.json').read_text())
        if runnable_identity(identity['pid']) == identity['started']:
            if self.owner_state(project, claim) == 'same':
                self.ticket(project, 'heartbeat', claim['key'], '--token', claim['token'])
            if time.time() > (record['repair_deadline'] or float('inf')):
                self.stop_overdue_repair(project, record, claim, identity)
            return
        if not worker_stopped(identity['pid'], identity['started']) or self.claim_helpers(claim):
            if time.time() > (record['repair_deadline'] or float('inf')):
                # Its wrapper exited, so the session cannot be pinned for a safe expansion.
                self.db.execute('UPDATE ci_redirects SET reason=?,updated=? WHERE id=?',
                                ('overdue repair wrapper exited but a session member or claim helper survives; never signalled; '
                                 'inspect and stop it explicitly', time.time(), record['id']))
            return  # A surviving session member or helper keeps the repair live.
        owner = self.owner_state(project, claim)
        if owner != 'same':
            self.redirect_note(record, 'uncertain', f'ownership/status changed during repair ({owner}); a repair worker must not hand off',
                               retain='quarantined')
            return
        failure = None
        try:
            code = json.loads((directory/'exit.json').read_text()).get('returncode')
            result = json.loads((directory/'result.json').read_text())
        except (OSError, ValueError):
            code, result = None, {}
        verdict = result.get('verdict') if isinstance(result, dict) else None
        if code != 0 or verdict not in ('fixed', 'rerun_requested') or not isinstance(result.get('evidence'), str) or not result['evidence'].strip():
            failure = f'repair worker exit {code} with verdict {verdict!r}'
            if record['signalled']:
                failure = f'overdue repair stopped under guards after {CI_REPAIR_TIMEOUT}s; ' + failure
        else:
            remote = f"refs/pr-coordinator/ci-redirect/{record['source']}-{record['id']}/remote"
            run(['git', 'fetch', 'origin', f"+refs/heads/{claim['branch']}:{remote}"], cwd=project['root'])
            tip = run(['git', 'rev-parse', remote], cwd=project['root']).strip()
            checkout = directory/'checkout'
            local = run(['git', 'rev-parse', 'HEAD'], cwd=checkout).strip()
            if run(['git', 'status', '--porcelain'], cwd=checkout).strip():
                failure = 'repair checkout left uncommitted changes'
            elif verdict == 'rerun_requested':
                if tip != record['head'] or local != record['head']:
                    failure = 'rerun_requested but the branch changed'
            elif tip == record['head'] or local != tip:
                failure = 'fixed result without a pushed repair head matching the checkout'
            else:
                try:
                    run(['git', 'merge-base', '--is-ancestor', record['head'], tip], cwd=project['root'])
                    identities = run(['git', 'log', '--format=%an <%ae>|%cn <%ce>', f"{record['head']}..{tip}"],
                                     cwd=project['root']).splitlines()
                    expected = TYLER['GIT_AUTHOR_NAME'] + ' <' + TYLER['GIT_AUTHOR_EMAIL'] + '>'
                    if any(line != expected + '|' + expected for line in identities):
                        failure = 'repair commits are not authored and committed by the configured user identity'
                except subprocess.CalledProcessError:
                    failure = 'pushed repair does not descend from the failed head'
        if failure:
            self.redirect_note(record, 'repair_failed', failure + '; feature work preserved and paused; explicit recovery required',
                               retain='quarantined')
            return
        repaired = None if verdict == 'rerun_requested' else tip
        self.redirect_note(record, 'integrating', 'repair verified; integrating into the paused feature worktree', repaired_head=repaired)
        try:
            run(['git', 'worktree', 'remove', str(directory/'checkout')], cwd=project['root'])
        except subprocess.CalledProcessError:
            print(f"{project['id']}: retained repair checkout {directory/'checkout'}", flush=True)

    def stop_overdue_repair(self, project, record, claim, identity):
        """Past its deadline, stop the live repair exactly like an owner interruption:
        only under held guards on every protected resource, never mid-mutation."""
        later = time.time() + CI_REDIRECT_RECHECK

        def check():
            listed = self.anchored_members(identity['pid'], [identity])
            if listed is None or not listed[0] or listed[0][0] != dict(pid=int(identity['pid']), started=identity['started']):
                return None, 'repair wrapper identity is no longer verified'
            members, foreign = listed
            if foreign:
                return members, "repair session contains another user's process"
            held = self.owner_locks(project, claim)
            if held:
                return members, f"repair holds lock {', '.join(held)}"
            return members, self.sui_activity(project, members)

        members, blocker = check()
        if members is not None and not blocker:
            self.db.execute('UPDATE ci_redirects SET guard_session=?,guards=?,updated=? WHERE id=?',
                            (f"{GUARD_PREFIX}{record['id']}-{uuid.uuid4().hex[:12]}", '[]', time.time(), record['id']))
            record, members, blocker = self.fenced(project, record, check)
        if members is None or blocker:
            self.db.execute('UPDATE ci_redirects SET guard_session=NULL,guards=NULL,retry_after=?,reason=?,updated=? WHERE id=?',
                            (later, f'repair overdue; waiting to stop it safely: {blocker or "identity changed"}; never signalled',
                             time.time(), record['id']))
            return
        # Persist the exact identities BEFORE signalling.
        self.redirect_note(record, 'repair_stopping', 'repair exceeded its deadline; stopping it under held guards',
                           members=json.dumps(members), signalled=None, killed=0)
        self.signal_members(members, signal.SIGTERM)
        self.db.execute('UPDATE ci_redirects SET signalled=?,updated=? WHERE id=?', (time.time(), time.time(), record['id']))

    def redirect_repair_stopping(self, project, record):
        row = self.db.execute('SELECT * FROM runs WHERE session=?', (record['source'],)).fetchone()
        claim = json.loads(row['claim'])

        def stopped(record):
            # Back to normal evaluation: a killed repair has no exit/result and fails visibly.
            self.release_guards(project, record)
            self.redirect_note(record, 'repairing', 'overdue repair verified stopped; guard leases released', guards=None,
                               guard_session=None, repair_deadline=None)

        def revert(record, lost):
            self.release_guards(project, record)
            self.redirect_note(record, 'repairing', f'guard {lost} lost before any signal; overdue repair never signalled',
                               guard_session=None, guards=None, members=None, retry_after=time.time() + CI_REDIRECT_RECHECK)
        self.escalate(project, record, claim, stopped, revert)

    def redirect_integrating(self, project, record):
        row = self.db.execute('SELECT * FROM runs WHERE session=?', (record['source'],)).fetchone()
        claim = json.loads(row['claim'])
        worktree, tip, original = claim['worktree'], record['repaired_head'], record['original_head']
        if self.owner_state(project, claim) != 'same':
            self.redirect_note(record, 'uncertain', 'ownership changed before integration; nothing resumed', retain='quarantined')
            return
        if not tip:
            self.redirect_note(record, 'resuming', 'evidenced rerun requested; no repair commits to integrate')
            return
        if run(['git', 'branch', '--show-current'], cwd=worktree).strip() != claim['branch']:
            self.redirect_note(record, 'uncertain', 'feature worktree left the claimed branch while paused', retain='quarantined')
            return
        current = run(['git', 'rev-parse', 'HEAD'], cwd=worktree).strip()
        snapshot = json.loads(record['snapshot'])['worktree']
        if current == original:
            if self.worktree_tree(worktree) != snapshot['tree']:
                self.redirect_note(record, 'uncertain', 'paused feature worktree changed before integration; nothing merged',
                                   retain='quarantined')
                return
            try:
                run(['git', 'merge-base', '--is-ancestor', original, tip], cwd=worktree)
                fast = True
            except subprocess.CalledProcessError:
                fast = False
            # git refuses to start when the merge would overwrite unfinished
            # tracked or untracked changes; that refusal is a safe conflict.
            try:
                if fast:
                    run(['git', 'merge', '--ff-only', tip], cwd=worktree)
                else:
                    run(['git', 'merge', '--no-edit', '-m', f"Merge CI repair for {record['ticket']} into unfinished feature work",
                         '-m', f"- Integrate same-claim CI repair {tip} for PR #{record['pr']}.\n- Preserve unfinished feature changes in place.",
                         tip], cwd=worktree, env={**os.environ, **TYLER})
            except subprocess.CalledProcessError:
                if self.git_path(worktree, 'MERGE_HEAD').exists():
                    run(['git', 'merge', '--abort'], cwd=worktree)
                preserved = (run(['git', 'rev-parse', 'HEAD'], cwd=worktree).strip() == original
                             and self.worktree_tree(worktree) == snapshot['tree'])
                refs = ', '.join(v['ref'] for v in json.loads(record['snapshot']).values())
                if preserved:
                    self.redirect_note(record, 'conflict', f'repair {tip[:12]} conflicts with unfinished feature changes; work preserved in place '
                                       f'and in {refs}; merge origin/{claim["branch"]} manually, then recover the claim', retain='quarantined')
                else:
                    self.redirect_note(record, 'uncertain', f'merge abort did not restore the paused state; recover from {refs}', retain='quarantined')
                return
        else:
            parents = run(['git', 'rev-list', '--parents', '-n', '1', current], cwd=worktree).split()[1:]
            if current != tip and parents != [original, tip]:
                self.redirect_note(record, 'uncertain', 'feature branch head changed while paused; nothing merged', retain='quarantined')
                return
            # Crash after a completed merge: verify and continue.
        if not self.dirty_preserved(record, worktree):
            self.redirect_note(record, 'uncertain', 'unfinished changes differ from the preserved snapshot after integration; inspect backup refs',
                               retain='quarantined')
            return
        self.redirect_note(record, 'resuming', f'repair {tip[:12]} integrated; unfinished feature changes verified preserved')

    def redirect_resuming(self, project, record):
        row = self.db.execute('SELECT * FROM runs WHERE session=?', (record['source'],)).fetchone()
        claim = json.loads(row['claim'])
        owner = self.owner_state(project, claim)
        if owner != 'same':
            self.redirect_note(record, 'uncertain', f'ownership changed before resume ({owner}); nothing resumed', retain='quarantined')
            return
        self.ticket(project, 'heartbeat', claim['key'], '--token', claim['token'])
        self.ticket(project, 'assert', claim['key'], '--token', claim['token'])
        if run(['git', 'branch', '--show-current'], cwd=claim['worktree']).strip() != claim['branch']:
            raise RuntimeError('feature worktree is not on the claimed branch')
        session = f"{record['source']}-ci{record['id']}"
        directory = Path(row['directory'])/f"ci-redirect-{record['id']}"/'resume'
        self.db.execute('BEGIN IMMEDIATE')
        try:
            # Record the single launch BEFORE starting it; the paused source is archived, not deleted.
            if self.db.execute("UPDATE ci_redirects SET state='resumed',resumed_as=?,updated=? WHERE id=? AND state='resuming'",
                               (session, time.time(), record['id'])).rowcount != 1:
                return
            self.db.execute('INSERT INTO retired_runs(session,original_status,original_message,reason,retired_at) VALUES(?,?,?,?,?)',
                            (row['session'], row['status'], row['message'], 'CI repair-first redirect resumed as '+session, time.time()))
            self.db.execute("UPDATE runs SET status='finished',message=? WHERE session=? AND status='redirected'",
                            ('CI redirect resumed as '+session, row['session']))
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                            (session, project['id'], row['family'], 'launching', str(directory), row['claim'], None, None,
                             f"Resumed implementation after CI repair-first redirect {record['id']}"))
            self.db.execute('COMMIT')
        finally:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
        try:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            refs = ', '.join(v['ref'] for v in json.loads(record['snapshot'] or '{}').values()) or 'none needed'
            names = ', '.join(f.get('name', '?') for f in json.loads(record['failures']))
            if record['repaired_head']:
                repair = (f"A same-claim repair worker pushed commits ending at {record['repaired_head']} to origin/{claim['branch']}; "
                          'the dispatcher merged them into this worktree and verified your unfinished changes are unchanged.')
            else:
                repair = f"No repair commits were integrated: {record['reason']}."
            preface = f"""This resumes your retained claim after a dispatcher CI repair-first redirect. Receipts: {directory.parent}.
PR #{record['pr']} failed completed CI on {record['head']} ({names}); you were paused safely to repair it first. {repair}
Your interrupted unfinished work is preserved in place in this worktree (backup refs: {refs}); do not discard it.
The claim stayed In Progress; do not run ticket resume, claim, recover or release.
First inspect git status/log and the current-head CI, then continue the unfinished implementation and rerun all required tests.
"""
            token = run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', project['github_account']]).strip()
            env = {**os.environ, 'GH_TOKEN': token, 'PR_COORDINATOR_ACTIVE': '1', 'PR_COORDINATOR_CLAIM_TOKEN': claim['token']}
            (directory/'command.json').write_text(json.dumps(self.config['workers'][row['family']]))
            (directory/'prompt.txt').write_text(preface + self.prompt(project, claim, directory))
            child = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).with_name('worker.py')), str(directory)],
                                     cwd=claim['worktree'], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.children[session] = child
            self.db.execute("UPDATE runs SET status='running',pid=?,started=? WHERE session=?",
                            (child.pid, process_identity(child.pid), session))
            print(f"{project['id']}: {row['family']} resumed {claim['key']} after CI redirect {record['id']}", flush=True)
        except Exception as error:
            self.update(session, 'orphaned', f'CI redirect resume launch failed ({type(error).__name__}); inspect before explicit recovery')
            self.db.execute('UPDATE ci_redirects SET reason=? WHERE id=?',
                            (f'resume launch failed ({type(error).__name__}); claim retained, never relaunched', record['id']))

    def record_capacity(self, project, row, claim):
        if row['status'] == 'orphaned':
            return False
        reset = None
        if capacity_failure(row['directory']):
            kind = 'capacity'
        else:
            # Explicit provider quota reset with a validated time; unknown resets stay quarantined.
            kind, reset = 'quota', quota_reset(row['directory'])
            if reset is None:
                return False
        digest = hashlib.sha256(claim['token'].encode()).hexdigest()
        prior = self.db.execute('SELECT * FROM capacity_retries WHERE source=?', (row['session'],)).fetchone()
        if prior:
            return True
        attempt = self.db.execute('SELECT COALESCE(MAX(c.attempt),0)+1 FROM capacity_retries c LEFT JOIN capacity_kinds k '
                                  "ON k.source=c.source WHERE c.claim=? AND COALESCE(k.kind,'capacity')=?",
                                  (digest, kind)).fetchone()[0]
        now = time.time()
        if kind == 'capacity':
            exhausted = attempt > CAPACITY_RETRIES
            delay = CAPACITY_DELAY * 2**min(attempt-1, 4)
            retry_after = now+delay
            reason = ('provider capacity retry budget exhausted; claim retained for explicit recovery' if exhausted
                      else f'provider model at capacity; same-claim retry {attempt}/{CAPACITY_RETRIES} in {delay}s')
        else:
            exhausted = attempt > QUOTA_RETRIES
            # A reset already in the past (for example after downtime) is due now.
            retry_after = max(now, reset+QUOTA_GRACE)
            when = datetime.fromtimestamp(reset).astimezone().isoformat(timespec='minutes')
            reason = ('provider quota retry budget exhausted; claim retained for explicit recovery' if exhausted
                      else f'provider quota limit; same-claim retry {attempt}/{QUOTA_RETRIES} after reset at {when}')
        state = 'exhausted' if exhausted else 'waiting'
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO capacity_retries VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (row['session'], digest, project['id'], row['family'], state, attempt,
                             retry_after, reason, None, now))
            self.db.execute('INSERT INTO capacity_kinds VALUES(?,?)', (row['session'], kind))
            self.update(row['session'], 'capacity_wait' if not exhausted else 'quarantined', reason)
            self.db.execute('COMMIT')
        finally:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
        if kind == 'quota' and not exhausted:
            # The account's quota is shared: new claims for this family would stop the
            # same way. Pause them until the reset; due recoveries run first.
            pause = self.db.execute('SELECT retry_after FROM backoff WHERE project=? AND family=?',
                                    (project['id'], row['family'])).fetchone()
            if not pause or pause[0] < retry_after:
                self.db.execute('INSERT OR REPLACE INTO backoff VALUES(?,?,?,?,?)',
                                (project['id'], row['family'], 0, retry_after, QUOTA_PAUSE))
        return True

    @staticmethod
    def capacity_helpers(claim, started):
        """Any live detached same-claim process, not only heartbeat helpers."""
        return claim_processes(claim['token'], started)

    def capacity_fence(self, project, row, claim):
        """Read-only stop/ownership/worktree fences, repeated immediately before intent."""
        owner = self.ticket(project, 'show', claim['key'])
        fields = ('token', 'session', 'branch', 'worktree', 'agent', 'model')
        if (owner.get('status') != 'in_progress' or owner.get('publish_pending')
                or any(owner.get(k) != claim.get(k) for k in fields)):
            return 'ownership is not the unchanged published In Progress claim'
        self.ticket(project, 'assert', claim['key'], '--token', claim['token'])
        identity = self.stopped_identity(row)
        if identity != dict(pid=row['pid'], started=row['started']):
            return 'original process identity changed or unavailable'
        if identity is None or self.claim_helpers(claim):
            return 'original worker, session descendant or detached claim helper not verified stopped'
        if found := self.capacity_helpers(claim, identity['started']):
            return uninspectable_reason(found) or 'original worker, session descendant or detached claim helper not verified stopped'
        if self.owner_locks(project, claim):
            return 'owner still holds a shared-resource lease'
        if run(['git', 'branch', '--show-current'], cwd=claim['worktree']).strip() != claim['branch']:
            return 'worktree left the original branch'
        for operation in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer', 'index.lock'):
            if self.git_path(claim['worktree'], operation).exists():
                return 'worktree has an unfinished git operation'
        digest = hashlib.sha256(claim['token'].encode()).hexdigest()
        if self.db.execute("SELECT 1 FROM ci_redirects WHERE claim=? AND state NOT IN ("+
                           ','.join('?' for _ in CI_TERMINAL)+")", (digest, *CI_TERMINAL)).fetchone():
            return 'CI redirect owns the claim recovery'
        if self.db.execute("SELECT 1 FROM dependency_resumes WHERE claim=? AND state IN ('waiting','resuming')",
                           (digest,)).fetchone():
            return 'dependency resume owns the claim recovery'
        for other in self.db.execute("SELECT * FROM runs WHERE session!=? AND status!='finished'", (row['session'],)):
            if other['claim'] and json.loads(other['claim']).get('token') == claim['token']:
                if self.stopped_identity(other) is None:
                    return 'another same-claim worker is not verified stopped'
        if self.replacement_repair(project, claim, identity):
            return 'review repair owns the claim recovery'
        return None

    def retry_capacity(self, project, slots, owners):
        families = {slot['family']: slot for slot in slots}
        records = self.db.execute("SELECT c.*,COALESCE(k.kind,'capacity') AS kind,r.directory,r.claim AS original_claim,"
                                  "r.status AS run_status FROM capacity_retries c JOIN runs r ON r.session=c.source "
                                  "LEFT JOIN capacity_kinds k ON k.source=c.source "
                                  "WHERE c.project=? AND c.state='waiting' ORDER BY c.retry_after,c.source",
                                  (project['id'],)).fetchall()
        for record in records:
            row = self.db.execute('SELECT * FROM runs WHERE session=?', (record['source'],)).fetchone()
            claim = json.loads(record['original_claim'])
            try:
                owner = self.ticket(project, 'show', claim['key'])
                fields = ('token', 'session', 'branch', 'worktree', 'agent', 'model')
                unchanged = all(owner.get(k) == claim.get(k) for k in fields)
                if owner.get('status') != 'in_progress' or not unchanged:
                    # Stop retrying authoritative transitions, but do not hide an
                    # unknown/live process. Existing retirement proofs decide
                    # whether the retained receipt can subsequently be archived.
                    if self.stopped_identity(row) is not None and not self.capacity_helpers(claim, self.stopped_identity(row)['started']):
                        self.db.execute("UPDATE capacity_retries SET state='cancelled',reason=?,updated=? WHERE source=?",
                                        ('ownership/status changed; capacity recovery cancelled', time.time(), record['source']))
                        self.update(record['source'], 'quarantined', 'Capacity recovery cancelled after ownership/status transition')
                    continue
                self.ticket(project, 'assert', claim['key'], '--token', claim['token'])
                self.ticket(project, 'heartbeat', claim['key'], '--token', claim['token'])
                slot = families.get(record['family'])
                if (slot is None or slot['model'] != claim.get('model')
                        or claim.get('agent') != record['family']
                        or json.loads((Path(row['directory'])/'command.json').read_text()) != self.config['workers'][record['family']]):
                    reason = 'original configured family/model/command changed or disabled'
                else:
                    reason = self.capacity_fence(project, row, claim)
                if reason:
                    self.db.execute('UPDATE capacity_retries SET reason=?,updated=? WHERE source=?',
                                    (reason, time.time(), record['source']))
                    continue
                self.ticket(project, 'heartbeat', claim['key'], '--token', claim['token'])
                if record['retry_after'] > time.time():
                    continue
                if not self.available(slot, owners, retention=False, recovering=True):
                    self.db.execute('UPDATE capacity_retries SET reason=? WHERE source=?',
                                    ('waiting for configured implementation capacity', record['source']))
                    continue
                # Check after heartbeat/capacity and before the durable single-use
                # intent. Any crash after intent is uncertain, never an auto replay.
                if self.capacity_fence(project, row, claim):
                    continue
                kind = 'quota' if record['kind'] == 'quota' else 'capacity'
                session = record['source']+'-'+kind+'-'+str(record['attempt'])
                directory = Path(row['directory'])/(kind+'-'+str(record['attempt']))
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    changed = self.db.execute("UPDATE capacity_retries SET state='launch_intent',replacement=?,updated=? "
                                              "WHERE source=? AND state='waiting'",
                                              (session, time.time(), record['source'])).rowcount
                    if not changed:
                        continue
                    self.db.execute('INSERT INTO retired_runs VALUES(?,?,?,?,?)',
                                    (row['session'],row['status'],row['message'],kind+' recovery reserved as '+session,time.time()))
                    self.db.execute("UPDATE runs SET status='finished',message=? WHERE session=?",
                                    (kind+' recovery reserved as '+session, record['source']))
                    self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                                    (session, project['id'], record['family'], 'launching', str(directory),
                                     record['original_claim'], None, None, f'same-claim {kind} retry launch reserved'))
                    self.db.execute('COMMIT')
                finally:
                    if self.db.in_transaction:
                        self.db.execute('ROLLBACK')
                try:
                    directory.mkdir(mode=0o700)
                    token = run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', project['github_account']]).strip()
                    env = {**os.environ, 'GH_TOKEN': token, 'PR_COORDINATOR_ACTIVE': '1',
                           'PR_COORDINATOR_CLAIM_TOKEN': claim['token']}
                    (directory/'command.json').write_text(json.dumps(self.config['workers'][record['family']]))
                    cause = 'capacity error' if kind == 'capacity' else 'quota limit whose stated reset time has now passed'
                    preface = (f"Your previous worker stopped on an explicit provider {cause}. Original evidence: {row['directory']}.\n"
                               "Continue the SAME retained implementation claim and existing branch/worktree. Inspect git status first; preserve all staged, dirty and untracked work. "
                               "Do not claim, recover, release, reset, clean or switch models. Review previous receipts and continue unfinished work.\n")
                    (directory/'prompt.txt').write_text(preface+self.prompt(project, claim, directory))
                    child = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).with_name('worker.py')), str(directory)],
                                             cwd=claim['worktree'], env=env, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    self.children[session] = child
                    self.db.execute("UPDATE runs SET status='running',pid=?,started=? WHERE session=?",
                                    (child.pid, process_identity(child.pid), session))
                    self.db.execute('INSERT OR REPLACE INTO backoff VALUES(?,?,?,?,?)',
                                    (project['id'], record['family'], 0, time.time()+CAPACITY_DELAY, 'provider capacity recovery stagger'))
                    self.db.execute("UPDATE capacity_retries SET state='launched',reason=?,updated=? WHERE source=?",
                                    ('same family/model resumed under unchanged claim', time.time(), record['source']))
                except Exception as error:
                    self.update(session, 'orphaned', f'Capacity replacement launch uncertain ({type(error).__name__}); never replayed')
                    self.db.execute("UPDATE capacity_retries SET state='uncertain',reason=?,updated=? WHERE source=?",
                                    ('replacement launch uncertain; explicit recovery required', time.time(), record['source']))
            except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.CalledProcessError):
                self.db.execute('UPDATE capacity_retries SET reason=?,updated=? WHERE source=?',
                                ('verification unavailable; nothing launched', time.time(), record['source']))

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
                if owner['status'] == 'in_progress' and not owner.get('publish_pending') and self.record_capacity(project, row, claim):
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

    def available(self, slot, owners, retention=True, recovering=False):
        pause = self.db.execute('SELECT retry_after,reason FROM backoff WHERE project=? AND family=?', (slot['project'],slot['family'])).fetchone()
        if pause and pause[0] > time.time() and (not recovering or pause['reason'] == 'provider capacity recovery stagger'):
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
            if row['status'] not in ('quarantined', 'parked', 'capacity_wait'):
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
        # A Review claim resumed by a verified live fix job uses review/fix
        # capacity; counting it again would idle an implementation slot.
        manual = sum(1 for owner in owners
                     if owner.get('agent') == slot['family']
                     and owner['status'] in ('in_progress', 'blocked')
                     and (not owner.get('session') or owner['session'] not in managed_sessions)
                     and not self.repair_owned(slot, owner))
        # Retained jobs free execution capacity only while still proven stopped.
        # A dependency resume in flight already holds its slot before launch.
        active = self.db.execute("SELECT count(*) FROM dependency_resumes WHERE project=? AND family=? AND state='resuming'",
                                 (slot['project'], slot['family'])).fetchone()[0]
        for row in rows:
            if row['status'] not in ('quarantined', 'parked', 'capacity_wait'):
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
When local implementation acceptance is ready, push, mark PR Ready and ticket Review with complete evidence, release resources.
{CI_HANDOFF}
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
            # Repair-first work on failed CI precedes audits, resumes and new claims.
            self.redirect_ci_failures(project)
            self.audit_missing_handoff(project)
            slots = [s for s in self.config.get('implementation_slots', [])
                     if s['project'] == project['id'] and s.get('enabled', False)]
            if not slots:
                continue
            owners = self.ticket(project, 'list')
            self.wake_on_change(project, owners)
            # Explicit capacity failures retain the original claim and get first execution capacity.
            self.retry_capacity(project, slots, owners)
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
            w.state AS dependency_resume,w.reason AS dependency_reason,w.attempts AS dependency_attempts,w.session AS resumed_as,
            (SELECT state FROM ci_redirects c WHERE c.source=r.session ORDER BY id DESC LIMIT 1) AS ci_redirect,
            (SELECT 'PR#'||pr||' '||head||': '||reason FROM ci_redirects c WHERE c.source=r.session ORDER BY id DESC LIMIT 1) AS ci_redirect_reason
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
