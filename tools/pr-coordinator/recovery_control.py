#!/usr/bin/env python3
"""Resume a verified stopped implementation under its retained claim.

Run only from the watchdog/operator, never from a normal model worker. The
existing dispatcher owns the new receipt and subsequent heartbeat/handoff.
"""
import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import uuid

FIELDS = ('token', 'session', 'branch', 'worktree', 'agent', 'model')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fence(dispatcher, project, row, claim):
    owner = dispatcher.ticket(project, 'show', claim['key'])
    require(owner.get('status') in ('blocked', 'in_progress'), 'owner is not Blocked/In Progress')
    require(not owner.get('publish_pending'), 'ownership publication pending')
    require(all(owner.get(k) == claim.get(k) for k in FIELDS), 'claim identity changed')
    require(not owner.get('remote', {}).get('external_blockers'), 'external development dependency remains')
    for key in set(owner.get('spec', {}).get('depends_on', [])) | set(owner.get('remote', {}).get('depends_on', [])):
        dependency = dispatcher.ticket(project, 'show', key)
        require(dependency['status'] == 'done' and not dependency.get('publish_pending'), 'development dependency incomplete')
    identity = dispatcher.stopped_identity(row)
    require(identity is not None, 'original worker/session/helpers not verified stopped')
    require(not dispatcher.capacity_helpers(claim, identity['started']), 'same-claim helper alive or uninspectable')
    require(not dispatcher.owner_locks(project, claim), 'owner still holds a resource lease')
    require(not dispatcher.replacement_repair(project, claim, identity), 'coordinator repair already owns recovery')
    from coordinator import run
    require(run(['git', 'branch', '--show-current'], cwd=claim['worktree']).strip() == claim['branch'], 'original branch changed')
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer', 'index.lock'):
        require(not dispatcher.git_path(claim['worktree'], name).exists(), 'unfinished git operation')
    digest = hashlib.sha256(claim['token'].encode()).hexdigest()
    require(not dispatcher.db.execute("SELECT 1 FROM dependency_resumes WHERE claim=? AND state IN ('waiting','resuming')", (digest,)).fetchone(), 'dependency resumption owns recovery')
    from dispatcher import CI_TERMINAL
    require(not dispatcher.db.execute("SELECT 1 FROM ci_redirects WHERE claim=? AND state NOT IN (" + ','.join('?' for _ in CI_TERMINAL) + ')', (digest, *CI_TERMINAL)).fetchone(), 'CI redirect owns recovery')
    require(not dispatcher.db.execute("SELECT 1 FROM capacity_retries WHERE claim=? AND state IN ('waiting','launch_intent')", (digest,)).fetchone(), 'provider retry owns recovery')
    for other in dispatcher.db.execute("SELECT * FROM runs WHERE session!=? AND status!='finished'", (row['session'],)):
        if other['claim'] and json.loads(other['claim']).get('token') == claim['token']:
            require(dispatcher.stopped_identity(other) is not None, 'another same-claim launch is live or uncertain')
    return owner


def resume(dispatcher, project, slot, key, reason, worker_path):
    """Caller holds activation, daemon and dispatcher authority locks."""
    require(isinstance(reason, str) and len(reason.strip()) >= 20, 'specific actionable repair instruction required')
    owner = dispatcher.ticket(project, 'show', key)
    require(owner.get('agent') == slot['family'] and owner.get('model') == slot['model'], 'configured original family/model unavailable')
    candidates = [r for r in dispatcher.db.execute("SELECT * FROM runs WHERE project=? AND status IN ('parked','quarantined')", (project['id'],))
                  if r['claim'] and json.loads(r['claim']).get('token') == owner.get('token')]
    require(len(candidates) == 1, 'need exactly one retained implementation receipt')
    row = candidates[0]
    claim = json.loads(row['claim'])
    require(json.loads((Path(row['directory'])/'command.json').read_text()) == dispatcher.config['workers'][slot['family']], 'original command changed')
    fence(dispatcher, project, row, claim)
    owners = dispatcher.ticket(project, 'list')
    require(dispatcher.available(slot, owners, retention=False, recovering=True), 'no configured implementation capacity')
    # A durable one-use reservation precedes any ticket transition or spawn.
    # Failures stay visible as orphaned; no automatic replay of uncertain intent.
    session = 'recovery-' + uuid.uuid4().hex
    directory = dispatcher.state/'implementations'/session
    dispatcher.db.execute('BEGIN IMMEDIATE')
    try:
        changed = dispatcher.db.execute("UPDATE runs SET status='finished',message=? WHERE session=? AND status=?", ('explicit same-claim recovery reserved as '+session, row['session'], row['status'])).rowcount
        require(changed == 1, 'source reservation changed')
        dispatcher.db.execute('INSERT INTO retired_runs VALUES(?,?,?,?,?)', (row['session'], row['status'], row['message'], reason, time.time()))
        dispatcher.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)', (session, project['id'], slot['family'], 'launching', str(directory), json.dumps(claim), None, None, 'explicit retained-claim recovery intent'))
        dispatcher.db.execute('COMMIT')
    finally:
        if dispatcher.db.in_transaction:
            dispatcher.db.execute('ROLLBACK')
    try:
        directory.mkdir(parents=True, mode=0o700)
        from coordinator import run, process_identity
        # Resolve credentials before changing ticket status; never write them.
        token = run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', project['github_account']]).strip()
        dispatcher.ticket(project, 'heartbeat', key, '--token', claim['token'])
        if owner['status'] == 'blocked':
            dispatcher.ticket(project, 'resume', key, '--token', claim['token'])
        dispatcher.ticket(project, 'assert', key, '--token', claim['token'])
        fresh = dispatcher.ticket(project, 'show', key)
        require(fresh['status'] == 'in_progress' and not fresh.get('publish_pending') and all(fresh.get(k) == claim.get(k) for k in FIELDS), 'claim changed during resume')
        # Repeat stop/ownership fences immediately before launch. The reserved
        # replacement is the only intentional uncertain receipt at this point.
        require(dispatcher.stopped_identity(row) is not None and not dispatcher.capacity_helpers(claim, dispatcher.stopped_identity(row)['started']), 'worker appeared during recovery')
        require(not dispatcher.replacement_repair(project, claim, dispatcher.stopped_identity(row)), 'competing repair appeared')
        require(not dispatcher.owner_locks(project, claim), 'resource lease appeared during recovery')
        from recovery_watchdog import atomic
        atomic(directory/'command.json', dispatcher.config['workers'][slot['family']])
        prompt = ('Explicit user-authorized recovery of your SAME stopped implementation. Do the repair, not merely another handoff.\n'
                  + 'Actionable recovery instruction: '+reason+'\n'
                  + 'Previous receipts: '+row['directory']+'\n'
                  + 'Preserve existing dirty/staged work. Do not reset, release, reclaim or switch models. '
                  + 'The service resumed your original claim; do not mark Review until actual acceptance is ready.\n'
                  + dispatcher.prompt(project, fresh, directory))
        fd = os.open(directory/'prompt.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(prompt)
        env = {k:v for k,v in os.environ.items() if k not in ('SUIFLEET_RECOVERY_ACTIVE', 'SUIFLEET_RECOVERY_RUN', 'GITHUB_TOKEN')}
        env.update(GH_TOKEN=token, PR_COORDINATOR_ACTIVE='1', PR_COORDINATOR_CLAIM_TOKEN=claim['token'])
        child = subprocess.Popen(['/usr/bin/python3', str(worker_path), str(directory)], cwd=claim['worktree'], env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        dispatcher.children[session] = child
        started = process_identity(child.pid)
        dispatcher.db.execute("UPDATE runs SET status='running',pid=?,started=? WHERE session=?", (child.pid, started, session))
        return dict(project=project['id'], ticket=key, family=slot['family'], session=session, pid=child.pid, started=started, directory=str(directory))
    except Exception:
        dispatcher.update(session, 'orphaned', 'Explicit recovery launch uncertain; inspect receipt before retry')
        raise


@contextmanager
def interruption_cleanup():
    """Unwind authority locks and restart parents on catchable termination.

    Ignore subsequent termination signals while the finally block restores
    services. SIGKILL/host loss cannot be handled by a Python cleanup block.
    """
    numbers = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    previous = {number: signal.getsignal(number) for number in numbers}
    def interrupted(number, frame):
        for pending in numbers:
            signal.signal(pending, signal.SIG_IGN)
        raise InterruptedError('recovery control terminated')
    try:
        for number in numbers:
            signal.signal(number, interrupted)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home()/'.config/pr-coordinator/config.json')
    parser.add_argument('--state', type=Path, default=Path.home()/'.local/state/pr-coordinator')
    parser.add_argument('--runtime', type=Path, default=Path.home()/'.local/lib/pr-coordinator/current')
    parser.add_argument('--project', required=True)
    parser.add_argument('--ticket', required=True)
    parser.add_argument('--reason', required=True, help='Concrete authorized implementation repairs; never override external decisions')
    args = parser.parse_args()
    require(not os.environ.get('PR_COORDINATOR_ACTIVE'), 'normal implementation/review workers cannot dispatch replacements')
    os.umask(0o077)
    require(not (args.state/'recovery-watchdog'/'disabled').exists(), 'recovery explicitly disabled for maintenance')
    config = json.loads(args.config.read_text())
    from recovery_watchdog import settings
    require(args.project in settings(config)['projects'] and settings(config)['enabled'], 'project recovery not enabled')
    project = next(p for p in config['projects'] if p['id'] == args.project)
    # Import only the installed runtime, never the possibly stale working tree.
    sys.path.insert(0, str(args.runtime.resolve()))
    from dispatcher import Dispatcher
    services = ('ticket-dispatcher.service', 'pr-coordinator.service')
    require(all(subprocess.run(['systemctl','--user','is-active','--quiet',s]).returncode == 0 for s in services), 'services must already be active; respect operator maintenance')
    require(all(subprocess.check_output(['systemctl','--user','show',s,'--property=KillMode','--value'],text=True).strip() == 'process' for s in services), 'parent stops must preserve children')
    with interruption_cleanup(), ExitStack() as stack:
        for name in ('activation.lock',):
            lock = stack.enter_context((args.state/name).open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            subprocess.run(['systemctl','--user','stop',*services],check=True)
            for name in ('dispatcher.lock','daemon.lock'):
                lock = stack.enter_context((args.state/name).open('a'))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            dispatcher = Dispatcher(config,args.state)
            stack.callback(dispatcher.db.close)
            owner = dispatcher.ticket(project,'show',args.ticket)
            slot = next(s for s in config['implementation_slots'] if s['project'] == args.project and s['family'] == owner['agent'])
            print(json.dumps(resume(dispatcher,project,slot,args.ticket,args.reason,args.runtime.resolve()/'worker.py')))
        finally:
            # A first signal during restoration must not abort parent restart.
            for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                signal.signal(number, signal.SIG_IGN)
            # Release authority before restarting their sole normal owners.
            stack.close()
            subprocess.run(['systemctl','--user','start',*services],check=True)


if __name__ == '__main__':
    main()
