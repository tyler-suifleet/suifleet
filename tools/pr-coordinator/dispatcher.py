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

from coordinator import process_identity, run


class Dispatcher:
    def __init__(self, config, state):
        self.config, self.state = config, Path(state)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.state/'dispatch.sqlite3', isolation_level=None)
        self.children = {}
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS runs (
            session TEXT PRIMARY KEY, project TEXT, family TEXT, status TEXT,
            directory TEXT, claim TEXT, pid INTEGER, started TEXT, message TEXT)''')
        for slot in config.get('implementation_slots', []):
            if slot['family'] not in ('Codex', 'Claude') or not slot['model'].strip():
                raise ValueError('Each slot needs a known family and actual model')
            if slot['project'] not in {p['id'] for p in config['projects']}:
                raise ValueError('Unknown slot project')
            command = config['workers'].get(slot['family'])
            if not isinstance(command, list) or not command or not all(isinstance(s, str) and s for s in command):
                raise ValueError('Worker command must be a nonempty argv list')
        pairs = [(s['project'], s['family']) for s in config.get('implementation_slots', [])]
        if len(set(pairs)) != len(pairs):
            raise ValueError('Only one implementation slot per project/family')

    def ticket(self, project, *args):
        try:
            return json.loads(run(['ticket', '--project', project['id'], *args]))
        except subprocess.CalledProcessError as error:
            print(f"{project['id']}: ticket {args[0]} failed (exit {error.returncode}); scan retained ownership", flush=True)
            raise

    def update(self, session, status, message):
        self.db.execute('UPDATE runs SET status=?,message=? WHERE session=?', (status, message, session))

    def reconcile(self, project):
        rows = self.db.execute("SELECT * FROM runs WHERE project=? AND status IN ('claiming','launching','running')", (project['id'],)).fetchall()
        for row in rows:
            directory = Path(row['directory'])
            child = self.children.get(row['session'])
            if child is not None:
                child.poll()  # Reap our exited wrappers before checking /proc identity.
            if row['status'] == 'claiming':
                # Claim may have succeeded before its response was persisted. Never repeat it.
                self.update(row['session'], 'orphaned', 'Interrupted claim; inspect ticket session and worktree before recovery')
                continue
            claim = json.loads(row['claim'])
            owner = self.ticket(project, 'show', claim['key'])
            completed = (owner['status'] == 'done' and owner.get('token') is None
                         and owner.get('session') == claim['session'] and owner.get('branch') == claim['branch'])
            if owner.get('token') != claim['token'] and not completed:
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
                if owner['status'] == 'in_progress':
                    self.ticket(project, 'heartbeat', claim['key'], '--token', claim['token'])
                continue
            # Exit receipt alone is insufficient: the wrapper can write it just before exiting.
            if receipt.exists() and json.loads(receipt.read_text()).get('returncode') == 0:
                result_path = directory/'result.json'
                result = json.loads(result_path.read_text()) if result_path.exists() else {}
                if (owner['status'] in ('review', 'done') and not owner.get('publish_pending')
                        and result.get('verdict') == 'review_ready' and result.get('evidence')):
                    self.update(row['session'], 'finished', 'Implementation handed off; review coordinator owns integration')
                    continue
            self.update(row['session'], 'orphaned', 'Worker stopped without verified handoff; claim retained, slot stopped')

    def review_busy(self, family):
        path = self.state/'reviews.sqlite3'
        if not path.exists():
            return True  # Do not outrun a missing review service.
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
            return db.execute("SELECT 1 FROM jobs WHERE family=? AND status IN ('reserved','running','retry','orphaned') LIMIT 1", (family,)).fetchone() is not None

    def available(self, slot, owners):
        if self.db.execute("SELECT 1 FROM runs WHERE project=? AND family=? AND status!='finished' LIMIT 1", (slot['project'], slot['family'])).fetchone():
            return False
        # Expired manual implementations and blocked work still occupy their slot.
        if any(o.get('agent') == slot['family'] and o['status'] in ('in_progress','blocked') for o in owners):
            return False
        return not self.review_busy(slot['family'])

    def launch(self, project, slot):
        session = 'dispatch-' + uuid.uuid4().hex
        directory = self.state/'implementations'/session
        directory.mkdir(parents=True, mode=0o700)
        self.db.execute('INSERT INTO runs(session,project,family,status,directory) VALUES(?,?,?,?,?)',
                        (session, project['id'], slot['family'], 'claiming', str(directory)))
        try:
            # Fetch before claiming; the immutable base is explicit, never stale local main.
            run(['git','fetch','origin','main'], cwd=project['root'])
            try:
                claim = self.ticket(project, 'claim', '--agent', slot['family'], '--model', slot['model'],
                                    '--session', session, '--base', 'origin/main')
            except subprocess.CalledProcessError as error:
                if 'No eligible ticket:' in (error.stderr or ''):
                    self.db.execute('DELETE FROM runs WHERE session=?', (session,))
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
Immediately heartbeat/assert; heartbeat at least every five minutes and assert after long commands and before edits/commits/push.
Read ticket show for the full specification/dependencies/notes; coordinate shared APIs in existing notes/issues.
Implement, run required meaningful tests, record exact results and remaining acceptance gaps. Do not fake acceptance or ignore failures.
Use small scoped itemized commits with Tyler Bush <tyler.jonathan.bush@gmail.com> as BOTH author and committer; verify metadata.
No model authorship/coauthor/signoff attribution. Push your branch and create a PR with Closes followed by this ticket's existing issue URL.
Authorized: local builds, simulators and disposable network-none local Sui chains; publication on those disposable chains is test setup.
Not authorized: public-chain transactions, production deployment, physical hardware programming or release-signing credentials.
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
            self.ticket(project, 'refresh')
            for slot in slots:
                owners = self.ticket(project, 'list')
                if self.available(slot, owners):
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
        while True:
            try:
                dispatcher.tick(args.execute)
            except Exception as error:
                print(f'Dispatch scan stopped ({type(error).__name__}); no claims recovered', flush=True)
                if not args.watch:
                    raise SystemExit(1)
            if not args.watch:
                break
            time.sleep(max(10, args.interval))


if __name__ == '__main__':
    main()
