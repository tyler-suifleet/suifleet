#!/usr/bin/env python3
"""Offline progress monitor and singleton recovery-agent supervisor."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

MODULE_DIR = Path(__file__).resolve().parent

def atomic(path, value):
    fd, name = tempfile.mkstemp(prefix='.watchdog-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def identity(pid, proc=Path('/proc')):
    try:
        fields = (proc/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in ('Z', 'X'):
            return None
        return dict(pid=int(pid), started=fields[19], session=int(fields[3]))
    except (FileNotFoundError, ProcessLookupError):
        return None


def survivors(run, proc=Path('/proc')):
    """Fence session members and escaped helpers carrying this run's nonce."""
    found = []
    marker = ('SUIFLEET_RECOVERY_RUN='+run['nonce']).encode()
    sessions = {run['pid']}
    child = read(Path(run['directory'])/'child.json')
    if child:
        sessions.add(child['pid'])
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            item = identity(int(path.name), proc)
            if not item or int(item['started']) < int(run['started']):
                continue
            if item['session'] in sessions or marker in (path/'environ').read_bytes().split(b'\0'):
                found.append(item['pid'])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # New uninspectable helpers are an uncertain outcome, never free capacity.
            found.append(int(path.name))
    return found


def settings(config):
    value = config.get('recovery_watchdog', {})
    if type(value.get('enabled', False)) is not bool:
        raise ValueError('watchdog enabled must be boolean')
    result = dict(enabled=value.get('enabled', False), projects=value.get('projects', []),
                  stall_seconds=value.get('stall_seconds', 900),
                  cooldown_seconds=value.get('cooldown_seconds', 1800),
                  timeout_seconds=value.get('timeout_seconds', 1200))
    known = {p['id'] for p in config['projects']}
    if (not isinstance(result['projects'], list) or len(set(result['projects'])) != len(result['projects'])
            or any(p not in known for p in result['projects'])
            or (result['enabled'] and not result['projects'])):
        raise ValueError('watchdog projects must explicitly name configured projects')
    for name, minimum, maximum in [('stall_seconds', 60, 86400),
                                    ('cooldown_seconds', 300, 86400), ('timeout_seconds', 120, 3600)]:
        if type(result[name]) is not int or not minimum <= result[name] <= maximum:
            raise ValueError('invalid '+name)
    command = config['workers'].get('Codex')
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise ValueError('a configured Codex command is required')
    return result


def project_view(model, project, excluded=()):
    inventory = model.get('inventory', {}).get(project, {})
    counts = dict(inventory.get('breakdown', {}))
    for ticket in model.get('_watchdog_tickets', {}).get(project, []):
        if ticket['key'] in excluded:
            status = ticket['status']
            counts[status] = max(0, counts.get(status, 0)-1)
    workers = [dict(id=w.get('id'), role=w.get('role'), ticket=w.get('ticket'), pr=w.get('pr'))
               for w in model.get('workers', []) if w.get('project') == project]
    # Exclude timestamp-only refreshes and obsolete retry rows from progress.
    attention = [dict(state=a.get('state'), ticket=a.get('ticket'), pr=a.get('pr'))
                 for a in model.get('attention', []) if a.get('project') == project
                 and a.get('ticket') not in excluded
                 and a.get('state') in ('waiting', 'manual', 'uncertain', 'orphaned', 'ci-failed', 'ci-stale')]
    return dict(project=project, counts={k:counts.get(k, 0) for k in
                ('backlog', 'in_progress', 'review', 'blocked', 'done')},
                workers=workers, attention=attention,
                inventory_available=inventory.get('state') == 'ok')


def reason(view):
    if not view['inventory_available']:
        return 'status inventory unavailable; diagnose observability and tracker refresh'
    if view['attention']:
        return 'waiting repair, uncertain ownership or CI failure needs investigation'
    count = view['counts']
    if not view['workers'] and count['backlog']+count['in_progress']+count['review']:
        return 'open development work has no verified worker'
    return None


def result_record(path, now):
    result = read(path)
    if (not isinstance(result, dict) or result.get('outcome') not in
            ('recovered', 'waiting', 'blocked', 'no_action')
            or not all(isinstance(result.get(k), list) and
                       all(isinstance(v, str) for v in result[k]) for k in ('actions', 'verification', 'blockers'))
            or type(result.get('next_check_at')) not in (int, float)
            or not 0 <= result['next_check_at'] <= now+86400):
        raise ValueError('invalid recovery result')
    return result


class Watchdog:
    def __init__(self, config, state, now=None):
        self.config, self.options = config, settings(config)
        self.parent = Path(state)
        self.root = self.parent/'recovery-watchdog'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root/'state.json'
        self.state = read(self.path, {'observations': {}})
        self.now = time.time() if now is None else now

    def save(self, status, **details):
        self.state['report'] = dict(checked_at=self.now, status=status, **details)
        atomic(self.path, self.state)
        atomic(self.root/'latest.json', self.state['report'])
        report = self.state['report']
        lines = ['Recovery watchdog: '+status, time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(self.now))]
        for key, value in details.items():
            lines.append(key+': '+json.dumps(value, sort_keys=True))
        target = self.root/'latest.txt'
        fd, name = tempfile.mkstemp(prefix='.report-', dir=self.root)
        with os.fdopen(fd, 'w') as stream:
            stream.write('\n'.join(lines)+'\n')
        os.replace(name, target)
        return report

    def reconcile(self):
        run = self.state.get('run')
        if not run:
            return False
        directory = Path(run['directory'])
        if run['phase'] == 'launching':
            receipt = read(directory/'process.json')
            if not receipt:
                self.save('uncertain launch; operator inspection required')
                return True
            run.update(pid=receipt['pid'], started=receipt['started'], phase='running')
        if survivors(run):
            self.save('recovery agent or descendant still active', run_directory=str(directory))
            return True
        exit_record = read(directory/'exit.json')
        if not exit_record:
            self.save('uncertain exit; operator inspection required', run_directory=str(directory))
            return True
        try:
            if exit_record.get('returncode') != 0:
                raise ValueError('recovery worker failed or timed out')
            result = result_record(directory/'result.json', self.now)
        except (ValueError, TypeError):
            result = dict(outcome='blocked', actions=[], verification=[],
                          blockers=['Recovery worker failed, timed out or returned invalid evidence; inspect private run receipts.'],
                          next_check_at=self.now+self.options['cooldown_seconds'])
        self.state['last_result'] = result
        self.state['last_run'] = run
        del self.state['run']
        self.state['not_before'] = max(self.state.get('not_before', 0), result['next_check_at'])
        self.save('recovery completed; verification on next observation', result=result)
        return False

    def launch(self, candidates):
        nonce = uuid.uuid4().hex
        directory = self.root/('run-'+nonce)
        directory.mkdir(mode=0o700)
        command = self.config['workers']['Codex']
        atomic(directory/'command.json', command)
        atomic(directory/'limits.json', {'timeout_seconds':self.options['timeout_seconds']})
        atomic(directory/'snapshot.json', candidates)
        result = directory/'result.json'
        prompt = f'''You are the user-authorized fleet recovery agent. Get work moving and verify the result.
Read AGENTS.md in each affected repository. Do not create another recovery agent or recurring watcher.
Affected projects and observed stalls: {json.dumps(candidates)}
The snapshot is offline evidence, not proof of current CI failure or current ownership. Re-read targeted live state as needed.
Inspect services, dispatch/review receipts, pending ticket mirrors, CI jobs, dependencies and stopped workers.
Perform concrete authorized recovery; preserve live workers and their branches. Never clear an expired claim without proving
the original worker and same-claim helpers stopped. Use installed ticket commands and the sole existing coordinator for
claims, repair/review dispatch and merges. Do not race its authority locks or edit its SQLite database directly.
Do not personally implement a feature without a valid issue claim. Do not duplicate an active implementation or review.
Preserve independent exact-head/base review, actual CI/security/testnet evidence and merge gates. No fabricated approval,
forced merge, account rotation, new paid capacity, production credentials/DNS/mainnet/hardware changes without the user's
existing explicit authorization. Follow dedicated testnet grants and spending/resource limits exactly.
Respect authentication/quota reset times. Legitimate running CI, a live resource lease or genuinely deferred acceptance is
not a reason to launch idle models. Do not poll/wait for CI; leave a specific next check instead. Never change the user's
feature decisions to manufacture eligibility. Report product/credential blockers plainly.
Recovery means taking action, not only diagnosing and leaving a handoff. A missing operational route is a repairable
software gap, not an external blocker. For a stopped Blocked/draft implementation with concrete code/CI repairs, use:
python3 {MODULE_DIR/'recovery_control.py'} --project PROJECT --ticket TICKET --reason 'specific authorized repair and evidence needed'
This supported command preserves the original model/claim/branch, proves old workers/helpers stopped, checks dependencies,
resource leases and competing repairs, reserves a durable replacement, and hands supervision back to the dispatcher.
Do not move a ticket to Review or mark a draft Ready just to dispatch it. Do not resume a genuine credential/product/
hardware/authorization blocker. Re-read existing notes and specify only work that can proceed under current authorization.
Check ALL affected queues, not only the first failed PR; a blocked treasury decision must not prevent unrelated reviews.
If the command or another necessary operational route is missing/broken, repair that infrastructure under its existing
issue claim (or publish/claim a scoped Codex issue if none exists), implement and test the contained fix, then hand it
to normal PR review. Installation is permitted only through the reviewed merged-only path; never install unreviewed
working-tree changes. Record the pending review as the next action. Never bypass ownership or acceptance gates. Verify launched worker identities and actual results; report failures honestly.
Prefer bounded operational fixes. Service restarts must preserve child workers and respect maintenance holds.
Verify actual PID/start identities and changed tracker/PR state; a successful command alone is insufficient evidence.
Do not expose credentials or put raw logs into the report. Write {result} as one JSON object:
{{"outcome":"recovered|waiting|blocked|no_action","actions":["concrete actions"],"verification":["observed evidence"],
"blockers":["remaining blockers"],"next_check_at":UNIX_TIMESTAMP}}
Use a next-check time between 5 minutes and 24 hours after completion; align known provider waits with their reset.
Keep the existing local status report current. Finish this bounded recovery pass, write the result, then EXIT.
'''
        with os.fdopen(os.open(directory/'prompt.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            stream.write(prompt)
        self.state['run'] = dict(phase='launching', nonce=nonce, directory=str(directory))
        self.state['not_before'] = self.now+self.options['cooldown_seconds']
        self.save('launch intent recorded', candidates=candidates)
        env = {k:v for k,v in os.environ.items() if k not in
               ('GH_TOKEN', 'GITHUB_TOKEN', 'PR_COORDINATOR_ACTIVE', 'PR_COORDINATOR_CLAIM_TOKEN')}
        env.update(SUIFLEET_RECOVERY_ACTIVE='1', SUIFLEET_RECOVERY_RUN=nonce)
        project = next(p for p in self.config['projects'] if p['id'] == candidates[0]['view']['project'])
        child = subprocess.Popen([sys.executable, str(MODULE_DIR/'recovery_worker.py'), str(directory)],
                                 cwd=project['root'], env=env, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        item = identity(child.pid)
        if item:
            self.state['run'].update(pid=child.pid, started=item['started'], phase='running')
        # If the wrapper exited immediately, process.json adopts its durable identity next tick.
        return self.save('recovery agent launched', run_directory=str(directory), candidates=candidates)

    def tick(self, model, execute=False, kick=False):
        if self.reconcile():
            return self.state['report']
        candidates, changed, activity_changed, waiting, active = [], [], [], [], []
        for project in self.options['projects']:
            project_config = next(p for p in self.config['projects'] if p['id'] == project)
            excluded = project_config.get('acceptance_policy', {}).get('development_defaults', {}).get('acceptance_only_tickets', [])
            view = project_view(model, project, excluded)
            fingerprint = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()
            old = self.state['observations'].get(project, {})
            activity = old.get('fingerprint') != fingerprint
            if old and activity:
                activity_changed.append(project)
            # Starts, exits, nonterminal state flips and CI-notice churn are
            # activity, not completion. Otherwise retrying failures can extend
            # the grace period forever without completing a single ticket.
            done = view['counts']['done']
            completed = bool(old.get('inventory_available') and view['inventory_available']
                             and done > old.get('done', done))
            if completed:
                changed.append(project)
            progress = self.now if not old or completed else old['progress_at']
            self.state['observations'][project] = dict(
                fingerprint=fingerprint, progress_at=progress, done=done,
                inventory_available=view['inventory_available'],
                activity_at=self.now if activity else old.get('activity_at', progress))
            diagnosis = reason(view)
            due = progress + self.options['stall_seconds']
            if diagnosis:
                if kick or self.now >= due:
                    candidates.append(dict(reason=diagnosis, no_completion_seconds=self.now-progress, view=view))
                else:
                    waiting.append(dict(project=project, reason=diagnosis, recovery_due_at=due))
            elif view['workers']:
                active.append(project)
        if self.state.get('last_result'):
            self.state['snapshot_verification'] = dict(checked_at=self.now, changed_projects=changed,
                activity_changed_projects=activity_changed,
                detail='Observed completed-ticket progress' if changed else
                       'No completed-ticket progress; worker/notice activity is not proof of recovery')
        if not self.options['enabled'] or (self.root/'disabled').exists():
            return self.save('disabled')
        if self.now < self.state.get('not_before', 0):
            return self.save('cooldown or reported provider wait', next_check_at=self.state['not_before'],
                             pending_projects=[c['view']['project'] for c in candidates])
        if not candidates:
            if waiting:
                return self.save('observation grace; recovery scheduled', projects=waiting,
                                 next_check_at=min(p['recovery_due_at'] for p in waiting))
            if active:
                return self.save('workers active; supervision continues', projects=active)
            return self.save('no actionable development work')
        if not execute:
            return self.save('would launch recovery agent', candidates=candidates)
        # Activation and explicit maintenance holds block new recovery sessions.
        with (self.parent/'activation.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self.save('runtime maintenance in progress')
            return self.launch(candidates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path, default=Path.home()/'.local/state/pr-coordinator')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--kick', action='store_true', help='Skip initial observation grace, never singleton/cooldown/maintenance guards')
    args = parser.parse_args()
    if os.environ.get('SUIFLEET_RECOVERY_ACTIVE'):
        raise SystemExit('Recursive recovery is disabled')
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state/'recovery-watchdog.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Recovery observation already running')
            return
        watchdog = Watchdog(read(args.config), args.state)
        # Offline only: the existing status reporter owns periodic remote refresh.
        observed = subprocess.run(['fleet-status', '--json'], capture_output=True, text=True, timeout=60)
        if observed.returncode:
            model = {}  # Missing inventory itself becomes a stalled observability diagnosis.
        else:
            model = json.loads(observed.stdout)
        model['_watchdog_tickets'] = {}
        for project in watchdog.options['projects']:
            # Local ownership reads only; omit paths, evidence and all claim tokens.
            tickets = subprocess.run(['ticket', '--project', project, 'list'],
                                     capture_output=True, text=True, timeout=45)
            if tickets.returncode == 0:
                model['_watchdog_tickets'][project] = [dict(key=t['key'], status=t['status'])
                                                     for t in json.loads(tickets.stdout)]
        print(json.dumps(watchdog.tick(model, args.execute, args.kick)))


if __name__ == '__main__':
    main()
