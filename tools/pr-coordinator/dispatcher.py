#!/usr/bin/env python3
"""Dispatch authorized implementation slots; the ticket database owns tickets."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

from coordinator import network_authorization, process_identity, worker_stopped, run


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
        for slot in config.get('implementation_slots', []):
            if slot['family'] not in ('Codex', 'Claude') or not slot['model'].strip():
                raise ValueError('Each slot needs a known family and actual model')
            if slot['project'] not in {p['id'] for p in config['projects']}:
                raise ValueError('Unknown slot project')
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

    def reconcile(self, project):
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
                ownership_changed = owner.get('token') != claim['token'] and not completed
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
                    continue
                # Exit receipt alone is insufficient: the wrapper can write it just before exiting.
                if receipt.exists() and json.loads(receipt.read_text()).get('returncode') == 0:
                    result_path = directory/'result.json'
                    result = json.loads(result_path.read_text()) if result_path.exists() else {}
                    if (owner['status'] in ('review', 'done') and not owner.get('publish_pending')
                            and result.get('verdict') == 'review_ready' and result.get('evidence')):
                        self.update(row['session'], 'finished', 'Implementation handed off; review coordinator owns integration')
                        continue
                if (receipt.exists() and json.loads(receipt.read_text()).get('returncode') == 0
                        and owner['status'] == 'blocked' and not owner.get('publish_pending')
                        and result.get('verdict') == 'blocked' and result.get('evidence')):
                    self.update(row['session'], 'parked', 'Verified stopped blocked worker; ticket retained, execution capacity freed')
                    continue
                self.update(row['session'], 'quarantined', 'Verified stopped without handoff; claim retained for explicit recovery')
            except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
                # A corrupt receipt or one unavailable ticket must not starve
                # unrelated slots. Keep its existing capacity/ownership intact.
                print(f"{project['id']}: reconciliation deferred for {row['session']} ({type(error).__name__})", flush=True)

    def available(self, slot, owners):
        pause = self.db.execute('SELECT retry_after FROM backoff WHERE project=? AND family=?', (slot['project'],slot['family'])).fetchone()
        if pause and pause[0] > time.time():
            return False
        # Review and repair workers have their own capacity. Require the review
        # service database, but a lengthy review/repair must not pause a family.
        if not (self.state/'reviews.sqlite3').exists():
            return False
        rows = self.db.execute("SELECT session,claim,status FROM runs WHERE project=? AND family=? AND status!='finished'",
                               (slot['project'], slot['family'])).fetchall()
        managed_sessions = {r['session'] for r in rows}
        # Real ticket responses preserve the dispatch session. Tests/legacy state
        # may preserve it only in the persisted claim; use both identities.
        managed_sessions.update(json.loads(r['claim']).get('session') for r in rows if r['claim'])
        manual = sum(1 for owner in owners
                     if owner.get('agent') == slot['family']
                     and owner['status'] in ('in_progress', 'blocked')
                     and (not owner.get('session') or owner['session'] not in managed_sessions))
        # Quarantined/parked jobs retain ownership, but have no runnable worker.
        active = sum(r['status'] not in ('quarantined', 'parked') for r in rows)
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
            self.db.execute('DELETE FROM backoff WHERE project=? AND family=?', (project['id'],slot['family']))
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
Immediately heartbeat/assert; heartbeat at least every five minutes and assert after long commands and before edits/commits/push.
Read ticket show for the full specification/dependencies/notes; coordinate shared APIs in existing notes/issues.
Implement, run required meaningful tests, record exact results and remaining acceptance gaps. Do not fake acceptance or ignore failures.
User-authorized implementation acceptance scope: {json.dumps(project.get('acceptance_policy',{}),sort_keys=True)}
Only when that scope names your ticket, defer its listed external integration checks to the linked acceptance issues. The user permits code-reviewed implementation merge before those checks; local tests, isolated-chain upgrade/compatibility and source correctness remain required. Record limits and links without claiming external tests passed. This newer user scope overrides blanket live/hardware requirements for those listed checks only.
Use small scoped itemized commits with Tyler Bush <tyler.jonathan.bush@gmail.com> as BOTH author and committer; verify metadata.
No model authorship/coauthor/signoff attribution. Push your branch and create a PR with Closes followed by this ticket's existing issue URL.
Authorized: local builds, simulators and disposable network-none local Sui chains; publication on those disposable chains is test setup.
{network_authorization(project)}
Lock shared build/port/hardware resources only during actual use, renew while used and release before Review. Do not touch others' work.
Do not create any new issues, start model workers, merge PRs or relax test/review gates. Report newly discovered work in this issue.
If genuine external acceptance is unavailable, record it honestly and retain/block your claim; do not claim the next ticket yourself.
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
            slots = [s for s in self.config.get('implementation_slots', [])
                     if s['project'] == project['id'] and s.get('enabled', False)]
            if not slots:
                continue
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
        print(json.dumps([dict(r) for r in dispatcher.db.execute('SELECT session,project,family,status,message FROM runs')], indent=2))
        return
    with open(args.state/'dispatcher.lock','w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
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
