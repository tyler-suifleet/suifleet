"""SF-145: same-claim recovery after explicit provider quota resets, and CI handoff (offline)."""
from datetime import datetime
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_capacity_retry import Recovery
from test_ci_redirects import Fixture, ROOT, git, d, c, REAL_RUN, TEST_UNINSPECTABLE

# Observed verbatim in stopped Claude workers on 2026-10-23.
OBSERVED = "You've hit your session limit · resets 4:30pm (America/Los_Angeles)"
LA = ZoneInfo('America/Los_Angeles')


def at(text, zone=LA):
    return datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=zone).timestamp()


def clock(ts, zone='UTC'):
    """Format a reset exactly as the CLI does: 4:30pm, 9am."""
    local = datetime.fromtimestamp(ts, ZoneInfo(zone))
    minute = f':{local.minute:02d}' if local.minute else ''
    return f"{local.hour % 12 or 12}{minute}{'am' if local.hour < 12 else 'pm'}"


def session_limit(reset, zone='UTC'):
    return f"You've hit your session limit · resets {clock(reset, zone)} ({zone})"


def receipt(path, text, code=1, stopped=None):
    (path/'worker.log').write_text(text)
    (path/'exit.json').write_text(json.dumps({'returncode': code}))
    if stopped is not None:
        os.utime(path/'exit.json', (stopped, stopped))


class Classification(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def reset(self, text, code=1, stopped=None, now=None):
        stopped = at('2026-10-23 13:05') if stopped is None else stopped
        receipt(self.path, text, code, stopped)
        return c.quota_reset(self.path, now=stopped+10 if now is None else now)

    def test_observed_claude_message_with_its_repeated_terminal_lines(self):
        expected = at('2026-10-23 16:30')
        self.assertEqual(self.reset(OBSERVED), expected)
        observed = f'Waiting for CI.\n\nRe-armed.\n\n{OBSERVED}\n\n{OBSERVED}\n'
        self.assertEqual(self.reset(observed), expected)
        envelope = json.dumps({'type': 'result', 'is_error': True, 'result': OBSERVED})
        self.assertEqual(self.reset(envelope), expected)

    def test_reset_is_anchored_to_the_exit_receipt_and_bounded(self):
        # Exit just after the stated time still means that window (bounded clock skew).
        self.assertEqual(self.reset(OBSERVED, stopped=at('2026-10-23 16:32')), at('2026-10-23 16:30'))
        # Past midnight: the next day's occurrence.
        late = OBSERVED.replace('4:30pm', '1am')
        self.assertEqual(self.reset(late, stopped=at('2026-10-23 22:00')), at('2026-10-24 01:00'))
        # A session window never resets more than six hours ahead: unknown, never guessed.
        self.assertIsNone(self.reset(OBSERVED, stopped=at('2026-10-23 10:00')))
        self.assertIsNone(self.reset(OBSERVED, stopped=at('2026-10-23 16:40')))
        # A receipt from the future is not trusted.
        self.assertIsNone(self.reset(OBSERVED, now=at('2026-10-23 12:00')))

    def test_dated_weekly_limits(self):
        weekly = "You've hit your weekly limit · resets Oct 27, 9am (America/Los_Angeles)"
        self.assertEqual(self.reset(weekly), at('2026-10-27 09:00'))
        rollover = "You've hit your Opus weekly limit · resets Jan 2, 12:15am (UTC)"
        self.assertEqual(self.reset(rollover, stopped=at('2026-12-30 08:00', ZoneInfo('UTC'))),
                         at('2027-01-02 00:15', ZoneInfo('UTC')))
        self.assertIsNone(self.reset(weekly.replace('Oct 27', 'Dec 27')), 'beyond a week ahead')
        self.assertIsNone(self.reset("You've hit your weekly limit · resets 9am (America/Los_Angeles)"),
                          'weekly reset without its day is unknown')

    def test_rejects_unrelated_auth_quoted_signal_and_successful_output(self):
        rejected = [
            ('> '+OBSERVED, 1), ('Claude said: '+OBSERVED, 1), (OBSERVED+'.', 1),
            (OBSERVED+'\nInvalid API key · Please run /login', 1),
            ('Invalid API key · Please run /login', 1),
            ('OAuth token has expired. Please obtain a new token or refresh your existing token.', 1),
            ("You've hit your session limit", 1),
            ("You've hit your session limit · resets soon", 1),
            ('Claude AI usage limit reached|1792800000', 1),
            (OBSERVED.replace('America/Los_Angeles', 'Mars/Olympus_Mons'), 1),
            (OBSERVED.replace('4:30pm', '13:30pm'), 1),
            (OBSERVED.replace('session', 'monthly'), 1),
            (OBSERVED+'\nunknown trailing error', 1),
            (json.dumps({'type': 'assistant', 'message': OBSERVED}), 1),
            (json.dumps({'type': 'result', 'is_error': False, 'result': OBSERVED}), 1),
            ('ERROR: Selected model is at capacity. Please try a different model.', 1),
            (OBSERVED, 0), (OBSERVED, -15), (OBSERVED, 124), (OBSERVED, 137),
            ('x'*40000+'\n'+OBSERVED[:20], 1),
        ]
        for text, code in rejected:
            with self.subTest(text=text[:70], code=code):
                self.assertIsNone(self.reset(text, code))
        receipt(self.path, OBSERVED, 1, at('2026-10-23 13:05'))
        (self.path/'result.json').write_text('{"verdict":"review_ready"}')
        self.assertIsNone(c.quota_reset(self.path, now=at('2026-10-23 13:06')), 'a handoff receipt is never a failure')
        (self.path/'result.json').unlink()
        (self.path/'exit.json').write_text('{"returncode":1,"signal":9}')
        self.assertIsNone(c.quota_reset(self.path, now=at('2026-10-23 13:06')))
        # Capacity and quota classifications are disjoint.
        receipt(self.path, OBSERVED, 1)
        self.assertFalse(d.capacity_failure(self.path))


NONDUMPABLE = ('import ctypes,sys,time\n'
               'ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE=0\n'
               'print("ready", flush=True)\n'
               'time.sleep(300)\n')


def nondumpable(test, token):
    """A live detached same-user process carrying `token` whose environ cannot be read."""
    child = subprocess.Popen([sys.executable, '-c', NONDUMPABLE], env={**os.environ, 'PR_COORDINATOR_CLAIM_TOKEN': token},
                             stdout=subprocess.PIPE, start_new_session=True)
    TEST_UNINSPECTABLE.add(child.pid)
    test.addCleanup(lambda: (child.kill(), child.wait(), child.stdout.close(), TEST_UNINSPECTABLE.discard(child.pid)))
    test.assertEqual(child.stdout.readline(), b'ready\n')
    try:
        Path(f'/proc/{child.pid}/environ').read_bytes()
    except PermissionError:
        return child
    test.skipTest('nondumpable process environ remains readable (privileged test runner)')


def comm(child):
    return Path(f'/proc/{child.pid}/comm').read_text().strip()


class Inspection(unittest.TestCase):
    def test_denied_inspection_is_reported_never_skipped(self):
        token = uuid.uuid4().hex
        child = nondumpable(self, token)
        started = c.process_identity(os.getpid())
        found = [p for p in c.claim_processes(token, started) if p['pid'] == child.pid]
        self.assertEqual(found, [dict(pid=child.pid, started=c.process_identity(child.pid), uninspectable=True)])
        child.kill(); child.wait()
        self.assertEqual([p for p in c.claim_processes(token, started) if p['pid'] == child.pid], [])


class Implementation(Recovery):
    """Dispatcher implementation workers: same claim, after the stated reset only."""

    def stopped(self, source='dispatch-owner', code=1, reset=None, stopped=None):
        row = super().stopped(source, code)
        directory = Path(row['directory'])
        self.reset_at = time.time()+2*3600 if reset is None else reset
        receipt(directory, f'Working on it.\n\n{session_limit(self.reset_at)}\n\n{session_limit(self.reset_at)}\n', code,
                time.time()-60 if stopped is None else stopped)
        return row

    def kind(self, record):
        row = self.worker.db.execute('SELECT kind FROM capacity_kinds WHERE source=?', (record['source'],)).fetchone()
        return row[0] if row else 'capacity'

    # Inherited capacity fences (helpers, git operations, owner/model changes,
    # launch-intent replay, slot capacity, CI redirect/dependency fences) all run
    # again against quota receipts. These two are capacity-specific budgets/pauses.
    def test_stable_claim_budget_exhaustion(self):
        self.test_quota_budget_is_bounded_and_separate_from_capacity()

    def test_due_recovery_bypasses_successor_idle_backoff(self):
        self.stopped(); self.recorded(); self.due()
        self.worker.db.execute("UPDATE backoff SET retry_after=?,reason='idle: no eligible ticket'", (time.time()+900,))
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0], 'launched')

    def test_future_reset_waits_heartbeats_pauses_family_then_resumes_same_claim(self):
        self.stopped()
        record = self.recorded()
        self.assertEqual((self.kind(record), record['state'], record['attempt']), ('quota', 'waiting', 1))
        self.assertEqual([r[1] for r in self.worker.db.execute('PRAGMA table_info(capacity_retries)')],
                         ['source', 'claim', 'project', 'family', 'state', 'attempt', 'retry_after', 'reason',
                          'replacement', 'updated'], 'unchanged SF-117 layout keeps activation rollback compatible')
        expected = int(self.reset_at//60)*60 + d.QUOTA_GRACE
        self.assertEqual(record['retry_after'], expected)
        self.assertEqual(self.worker.db.execute('SELECT status FROM runs').fetchone()[0], 'capacity_wait')
        pause = self.worker.db.execute('SELECT retry_after,reason FROM backoff').fetchone()
        self.assertEqual(tuple(pause), (expected, d.QUOTA_PAUSE))
        self.assertFalse(self.worker.available(self.slot, [self.owner], retention=False), 'no new claims before reset')
        (self.worktree/'untracked.txt').write_text('unfinished work')
        snapshot = git('status', '--porcelain=v1', cwd=self.worktree)
        self.worker = self.dispatcher()  # restarts keep the reset schedule
        self.commands.clear(); self.run_retry()
        self.assertIn('heartbeat', self.commands)
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0], 'waiting')
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0], 1, 'no retry before reset')
        self.due(); self.worker.db.execute('UPDATE backoff SET retry_after=0')
        self.run_retry()
        record = self.worker.db.execute('SELECT * FROM capacity_retries').fetchone()
        self.assertEqual(record['state'], 'launched')
        self.assertEqual(record['replacement'], 'dispatch-owner-quota-1')
        replacement = self.worker.db.execute('SELECT * FROM runs WHERE session=?', (record['replacement'],)).fetchone()
        self.assertEqual(json.loads(replacement['claim']), self.claim)
        prompt = (Path(replacement['directory'])/'prompt.txt').read_text()
        self.assertIn('quota limit whose stated reset time has now passed', prompt)
        self.assertIn('SAME retained implementation claim', prompt)
        self.assertEqual(git('status', '--porcelain=v1', cwd=self.worktree), snapshot)
        self.run_retry()
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0], 1,
                         'never a duplicate replacement')

    def test_past_reset_is_due_immediately(self):
        now = time.time()
        self.stopped(reset=now-600, stopped=now-3600)
        record = self.recorded()
        self.assertLessEqual(record['retry_after'], time.time())
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0], 'launched')

    def test_unknown_reset_stays_quarantined_for_explicit_recovery(self):
        row = self.stopped()
        (Path(row['directory'])/'worker.log').write_text("You've hit your session limit · resets soon\n")
        self.assertIsNone(self.recorded())
        self.assertEqual(self.worker.db.execute('SELECT status FROM runs').fetchone()[0], 'quarantined')

    def test_quota_budget_is_bounded_and_separate_from_capacity(self):
        capacity = super().stopped('capacity-source')
        self.worker.record_capacity(self.project, capacity, self.claim)
        self.worker.db.execute("UPDATE runs SET status='finished' WHERE session='capacity-source'")
        for number in range(d.QUOTA_RETRIES+1):
            source = 'quota-'+str(number)
            self.worker.record_capacity(self.project, self.stopped(source), self.claim)
            self.worker.db.execute("UPDATE runs SET status='finished' WHERE session=?", (source,))
        rows = [tuple(r) for r in self.worker.db.execute(
            "SELECT COALESCE(k.kind,'capacity') AS kind,c.attempt,c.state FROM capacity_retries c "
            'LEFT JOIN capacity_kinds k ON k.source=c.source ORDER BY kind,c.attempt')]
        self.assertEqual(rows, [('capacity', 1, 'waiting')] + [('quota', n, 'waiting') for n in range(1, d.QUOTA_RETRIES+1)]
                         + [('quota', d.QUOTA_RETRIES+1, 'exhausted')])
        reason = self.worker.db.execute("SELECT reason FROM capacity_retries WHERE state='exhausted'").fetchone()[0]
        self.assertIn('explicit recovery', reason)

    def test_changed_ownership_live_descendant_and_shared_lock_refuse(self):
        self.stopped(); self.recorded(); self.due(); self.worker.db.execute('DELETE FROM backoff')
        self.locks.append(dict(resource='network:suifleet-testnet', session=self.claim['session'], expires=time.time()+500))
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state,reason FROM capacity_retries').fetchone()[1],
                         'owner still holds a shared-resource lease')
        self.locks.clear()
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(300)'],
                                 env={**os.environ, 'PR_COORDINATOR_CLAIM_TOKEN': self.claim['token']}, start_new_session=True)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0], 'waiting')
        child.kill(); child.wait()
        self.owner.update(token='reclaimed-generation')
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0], 'cancelled')
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0], 1)

    def test_uninspectable_detached_claim_process_fences_recovery(self):
        self.stopped(); self.recorded(); self.due(); self.worker.db.execute('DELETE FROM backoff')
        child = nondumpable(self, self.claim['token'])
        self.run_retry(); self.run_retry()
        self.assertEqual(tuple(self.worker.db.execute('SELECT state,reason FROM capacity_retries').fetchone()),
                         ('waiting', f'an uninspectable same-user process may carry the claim token (pid {child.pid} {comm(child)}); '
                                     'stop it or recover explicitly'))
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM runs').fetchone()[0], 1, 'no replacement')
        child.kill(); child.wait()
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT state FROM capacity_retries').fetchone()[0], 'launched')

    def test_deferred_coordinator_repair_owns_the_claim(self):
        self.stopped(); self.recorded(); self.due(); self.worker.db.execute('DELETE FROM backoff')
        intent = dict(claim=hashlib.sha256(self.claim['token'].encode()).hexdigest(), key=self.claim['key'],
                      branch=self.claim['branch'], worktree=self.claim['worktree'], head=self.head)
        self.store.db.execute("INSERT INTO jobs(project,pr,family,kind,head,feedback,status,directory,repair_claim) "
                              "VALUES('suifleet',7,'Codex','fix',?,'f','quota_wait','x',?)", (self.head, json.dumps(intent)))
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT reason FROM capacity_retries').fetchone()[0],
                         'review repair owns the claim recovery')


SCRIPT = '''import os,pathlib,sys,time
marker=pathlib.Path(os.environ["QUOTA_MARKER"])
if not marker.exists():
    marker.write_text("stopped once")
    print("Pushed a partial fix.\\n", flush=True)
    print(os.environ["QUOTA_MESSAGE"], flush=True)
    sys.exit(1)
pathlib.Path(os.environ["QUOTA_MARKER"]+".resumed").write_text(os.environ.get("PR_COORDINATOR_CLAIM_TOKEN",""))
time.sleep(300)
'''


class Repair(Fixture):
    """Coordinator review-feedback/CI repair workers on a resumed Review claim."""

    def setUp(self):
        super().setUp()
        self.token = uuid.uuid4().hex
        self.owner.update(token=self.token, status='in_progress')
        script = Path(self.temp.name)/'quota_cli.py'
        script.write_text(SCRIPT)
        self.marker = Path(self.temp.name)/'quota-marker'
        self.coordinator_config = dict(projects=[self.project],
                                       workers={'Codex': [sys.executable, str(script)], 'Claude': ['unused']})
        self.engine = self.coordinator()
        self.calls = []

    def coordinator(self):
        engine = c.Coordinator(self.coordinator_config, self.state)
        engine.ticket = self.coordinator_ticket
        engine.gh = lambda project, args: json.dumps(dict(state='OPEN', isCrossRepository=False, headRefName='work/test'))
        return engine

    def coordinator_ticket(self, project, *args, timeout=None):
        self.calls.append(args[0])
        if args[0] == 'show':
            return json.dumps(self.owner)
        if args[0] == 'locks':
            return json.dumps(self.locks)
        if args[0] in ('heartbeat', 'assert'):
            if args[2:] != ('--token', self.owner['token']):
                raise AssertionError('wrong claim token')
            return json.dumps(self.owner)
        raise AssertionError(f'unexpected ticket command {args}')

    def kill_processes(self):
        super().kill_processes()
        for engine in [self.engine, *getattr(self, 'engines', [])]:
            for child in engine.children.values():
                if child.poll() is None:
                    # The wrapper and CLI share a dedicated session. Killing only
                    # the wrapper leaves that CLI racing TemporaryDirectory cleanup.
                    members, foreign = d.Dispatcher.session_members(child.pid)
                    self.assertFalse(foreign)
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
                    deadline = time.monotonic()+5
                    while any(d.Dispatcher.member_session(member) is not None for member in members):
                        self.assertLess(time.monotonic(), deadline, 'test worker descendants did not stop')
                        time.sleep(.01)
                else:
                    child.wait(timeout=5)

    def stopped_repair(self, reset=None, stopped=None):
        self.reset_at = time.time()+2*3600 if reset is None else reset
        directory = self.state/'suifleet'/'pr-7-fix'
        directory.mkdir(parents=True)
        (directory/'snapshot.json').write_text(json.dumps({**self.pr, 'ticket': {k: self.owner[k] for k in ('key', 'agent', 'model')}}))
        (directory/'command.json').write_text(json.dumps(self.coordinator_config['workers']['Codex']))
        (directory/'prompt.txt').write_text('repair feedback')
        env = {**os.environ, 'QUOTA_MARKER': str(self.marker), 'QUOTA_MESSAGE': session_limit(self.reset_at),
               'PR_COORDINATOR_CLAIM_TOKEN': self.token}
        child = subprocess.Popen([sys.executable, str(ROOT/'worker.py'), str(directory)], cwd=self.worktree, env=env,
                                 start_new_session=True)
        child.wait(timeout=10)
        if stopped is not None:
            os.utime(directory/'exit.json', (stopped, stopped))
        identity = json.loads((directory/'process.json').read_text())
        intent = dict(claim=hashlib.sha256(self.token.encode()).hexdigest(), key='SF-099', branch='work/test',
                      worktree=str(self.worktree), head=self.head)
        self.job = self.engine.store.db.execute(
            "INSERT INTO jobs(project,pr,family,kind,head,feedback,status,directory,pid,started,created,repair_claim) "
            "VALUES('suifleet',7,'Codex','fix',?,'f'||?,'running',?,?,?,?,?)",
            (self.head, uuid.uuid4().hex, str(directory), identity['pid'], identity['started'], time.time(),
             json.dumps(intent))).lastrowid
        os.environ.update(QUOTA_MARKER=str(self.marker), QUOTA_MESSAGE=session_limit(self.reset_at))
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ('QUOTA_MARKER', 'QUOTA_MESSAGE')])
        return directory

    def collect(self):
        def fake(argv, **kwargs):
            return 'private-gh-token' if argv[:3] == ['gh', 'auth', 'token'] else REAL_RUN(argv, **kwargs)
        with patch.object(c, 'run', side_effect=fake):
            self.engine.collect(self.project, [self.owner])

    def job_row(self):
        return self.engine.store.db.execute('SELECT * FROM jobs WHERE id=?', (self.job,)).fetchone()

    def due(self):
        self.engine.store.db.execute('UPDATE jobs SET retry_after=0 WHERE id=?', (self.job,))

    def test_observed_stop_waits_for_reset_then_resumes_same_claim_once(self):
        source = self.stopped_repair()
        (self.worktree/'feature.txt').write_text('dirty repair work\n')
        snapshot = git('status', '--porcelain=v1', cwd=self.worktree)
        self.collect()
        row = self.job_row()
        self.assertEqual(row['status'], 'quota_wait')
        self.assertEqual(row['retry_after'], int(self.reset_at//60)*60 + c.QUOTA_GRACE)
        self.assertIn('same-claim repair retry 1/', row['error'])
        # No retry before the reset; the retained claim stays alive; the PR stays reserved.
        self.engine = self.coordinator()
        self.calls.clear(); self.collect()
        self.assertEqual(self.job_row()['status'], 'quota_wait')
        self.assertIn('heartbeat', self.calls)
        self.assertFalse(Path(str(self.marker)+'.resumed').exists())
        self.assertIsNone(self.engine.store.reserve('suifleet', self.pr, 'Claude', 'review', self.state/'other'))
        self.due(); self.collect()
        row = self.job_row()
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['directory'], str(source/'quota-1'))
        deadline = time.time()+10
        resumed = Path(str(self.marker)+'.resumed')
        while not resumed.exists():
            self.assertLess(time.time(), deadline); time.sleep(.02)
        self.assertEqual(resumed.read_text(), self.token, 'the same claim token is inherited')
        self.assertEqual(git('status', '--porcelain=v1', cwd=self.worktree), snapshot)
        prompt = (source/'quota-1'/'prompt.txt').read_text()
        self.assertIn('explicit provider quota limit', prompt)
        self.assertIn(c.CI_HANDOFF, prompt)
        self.assertTrue((source/'worker.log').exists(), 'original evidence retained')
        self.collect()
        self.assertEqual(len(self.engine.children), 1, 'never a duplicate')
        record = self.engine.store.db.execute('SELECT state FROM quota_recoveries').fetchone()[0]
        self.assertEqual(record, 'launched')

    def test_past_reset_is_due_immediately(self):
        now = time.time()
        self.stopped_repair(reset=now-600, stopped=now-3600)
        self.collect()
        self.assertEqual(self.job_row()['status'], 'quota_wait')
        self.collect()
        self.assertEqual(self.job_row()['status'], 'running')

    def test_cleanup_stops_resumed_cli_before_removing_its_directory(self):
        now = time.time()
        self.stopped_repair(reset=now-600, stopped=now-3600)
        self.collect(); self.collect()
        child = next(iter(self.engine.children.values()))
        resumed = Path(str(self.marker)+'.resumed')
        deadline = time.monotonic()+10
        while not resumed.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        members, foreign = d.Dispatcher.session_members(child.pid)
        self.assertFalse(foreign)
        self.assertGreaterEqual(len(members), 2, 'exercise wrapper and real CLI child')
        self.kill_processes()
        self.assertTrue(all(d.Dispatcher.member_session(member) is None for member in members))
        self.assertIsNotNone(child.returncode)

    def test_fences_lock_live_claim_process_and_changed_ownership(self):
        self.stopped_repair(); self.collect(); self.due()
        self.locks.append(dict(resource='network:suifleet-testnet', session='other', ticket='suifleet::SF-099',
                               expires=time.time()+500))
        self.collect()
        self.assertEqual(self.job_row()['status'], 'quota_wait')
        self.assertEqual(self.engine.store.db.execute('SELECT reason FROM quota_recoveries').fetchone()[0],
                         'the claim still holds a shared-resource lease')
        self.locks.clear()
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(300)'],
                                 env={**os.environ, 'PR_COORDINATOR_CLAIM_TOKEN': self.token}, start_new_session=True)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        self.collect()
        self.assertEqual(self.engine.store.db.execute('SELECT reason FROM quota_recoveries').fetchone()[0],
                         'a live process still carries the claim token')
        child.kill(); child.wait()
        self.owner.update(status='review')
        self.collect()
        self.assertEqual(self.job_row()['status'], 'quarantined')
        self.assertEqual(self.engine.store.db.execute('SELECT state FROM quota_recoveries').fetchone()[0], 'cancelled')
        self.assertFalse(Path(str(self.marker)+'.resumed').exists())

    def test_uninspectable_detached_claim_process_fences_recovery(self):
        self.stopped_repair(); self.collect(); self.due()
        child = nondumpable(self, self.token)
        self.collect(); self.collect()
        self.assertEqual(self.job_row()['status'], 'quota_wait')
        self.assertEqual(self.engine.store.db.execute('SELECT reason FROM quota_recoveries').fetchone()[0],
                         f'an uninspectable same-user process may carry the claim token (pid {child.pid} {comm(child)}); '
                         'stop it or recover explicitly')
        self.assertEqual(self.engine.children, {}, 'no replacement')
        self.assertFalse(Path(str(self.marker)+'.resumed').exists())
        child.kill(); child.wait()
        self.collect()
        self.assertEqual(self.job_row()['status'], 'running')

    def test_uncertain_launch_is_held_and_never_replayed(self):
        self.stopped_repair(); self.collect(); self.due()
        original = subprocess.Popen
        def popen(argv, **kwargs):
            if str(ROOT/'worker.py') in argv:
                raise OSError('ambiguous spawn')
            return original(argv, **kwargs)
        with patch.object(c.subprocess, 'Popen', side_effect=popen):
            self.collect()
        row = self.job_row()
        self.assertEqual((row['status'], row['pid']), ('orphaned', None))
        self.engine = self.coordinator()
        self.collect(); self.collect()
        self.assertEqual(self.job_row()['status'], 'orphaned', 'held for explicit recovery, never quarantined or replayed')
        self.assertEqual(self.engine.store.db.execute('SELECT state FROM quota_recoveries').fetchone()[0], 'uncertain')

    def test_interrupted_intent_after_restart_is_held(self):
        self.stopped_repair(); self.collect(); self.due()
        self.engine.store.db.execute("UPDATE jobs SET status='quota_launch' WHERE id=?", (self.job,))
        self.engine.store.db.execute("UPDATE quota_recoveries SET state='launch_intent',replacement=?",
                                     (str(self.state/'never-started'),))
        self.collect()
        self.assertEqual(self.job_row()['status'], 'orphaned')
        self.assertFalse(Path(str(self.marker)+'.resumed').exists())

    def test_budget_exhaustion_requires_explicit_recovery(self):
        self.stopped_repair()
        for attempt in range(c.QUOTA_RETRIES):
            self.engine.store.db.execute("INSERT INTO quota_recoveries VALUES(?,?,?,?,?,?,?,?)",
                                         (self.job, attempt+1, 'old', 0, 'launched', None, 'old', 0))
        self.collect()
        row = self.job_row()
        self.assertEqual(row['status'], 'orphaned')
        self.assertIn('quota retry budget exhausted', row['error'])
        self.collect()
        self.assertEqual(self.job_row()['status'], 'quarantined')

    def test_review_quota_retry_waits_for_the_reset(self):
        self.stopped_repair()
        self.engine.store.db.execute("UPDATE jobs SET kind='review',repair_claim=NULL WHERE id=?", (self.job,))
        self.collect()
        row = self.job_row()
        self.assertEqual(row['status'], 'retry')
        self.assertGreaterEqual(row['retry_after'], self.reset_at)


if __name__ == '__main__':
    unittest.main()
