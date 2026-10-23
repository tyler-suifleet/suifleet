"""SF-116: failed CI on active implementation PRs redirects the live owner to
same-claim repair-first work, preserves unfinished changes and resumes it.

Everything is offline: real Git repositories and real worker processes stand in
for GitHub-hosted branches and model CLIs; GitHub reads and ticket calls are fakes.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('dispatcher', ROOT/'dispatcher.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator as c  # noqa: E402
import fake_ticket  # noqa: E402

FAKE = Path(__file__).with_name('fake_cli.py')
dspec = importlib.util.spec_from_file_location('dashboard', ROOT/'dashboard.py')
dash = importlib.util.module_from_spec(dspec)
dspec.loader.exec_module(dash)
IDENTITY = {'GIT_AUTHOR_NAME': 'Tyler Bush', 'GIT_AUTHOR_EMAIL': 'tyler.jonathan.bush@gmail.com',
            'GIT_COMMITTER_NAME': 'Tyler Bush', 'GIT_COMMITTER_EMAIL': 'tyler.jonathan.bush@gmail.com'}
REAL_RUN = d.run


def git(*args, cwd, env=None):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **IDENTITY, **(env or {})}).stdout.strip()


def failure(run=1, conclusion='FAILURE', **fields):
    return {**dict(__typename='CheckRun', name='check-interfaces', workflowName='CI', status='COMPLETED',
                   conclusion=conclusion, detailsUrl=f'https://github.com/owner/repo/actions/runs/{run}/job/{run}',
                   startedAt='2026-01-01T00:00:00Z', completedAt='2026-01-01T00:01:00Z'), **fields}


class Fixture(unittest.TestCase):
    """A dispatcher with one managed live implementation whose PR failed CI."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.state = base/'state'
        self.state.mkdir()
        self.origin, self.root, self.worktree = base/'origin.git', base/'root', base/'feature'
        git('init', '-q', '--bare', '-b', 'main', str(self.origin), cwd=base)
        git('clone', '-q', str(self.origin), str(self.root), cwd=base)
        (self.root/'feature.txt').write_text('base\n')
        git('add', '.', cwd=self.root)
        git('commit', '-q', '-m', 'base', cwd=self.root)
        git('push', '-q', 'origin', 'HEAD:main', cwd=self.root)
        self.base_sha = git('rev-parse', 'HEAD', cwd=self.root)
        git('worktree', 'add', '-q', '-b', 'work/test', str(self.worktree), cwd=self.root)
        (self.worktree/'feature.txt').write_text('first feature step\n')
        git('commit', '-q', '-am', 'feature step', cwd=self.worktree)
        git('push', '-q', 'origin', 'HEAD:refs/heads/work/test', cwd=self.worktree)
        self.head = git('rev-parse', 'HEAD', cwd=self.worktree)
        self.protected = ['hardware:raspberrypi3bplus', 'network:suifleet-testnet']
        self.project = dict(id='suifleet', root=str(self.root), github_account='acct', repository='owner/repo',
                            ci_redirect_protected_resources=list(self.protected))
        self.lock_path = base/'locks.sqlite3'
        self.lockdb = fake_ticket.Locks(str(self.lock_path))
        self.slot = dict(project='suifleet', family='Codex', model='actual-test-model', enabled=True)
        self.config = dict(projects=[self.project], implementation_slots=[self.slot],
                           workers={'Codex': [sys.executable, str(FAKE)]})
        self.store = c.Store(self.state/'reviews.sqlite3')
        self.claim = dict(key='SF-099', token='private-token', session='dispatch-owner', status='in_progress',
                          worktree=str(self.worktree), branch='work/test', agent='Codex', model='actual-test-model',
                          spec={'title': 'feature'})
        self.owner = dict(self.claim)
        self.commands = []
        self.locks = []
        self.pr = dict(number=7, headRefOid=self.head, headRefName='work/test', baseRefName='main',
                       baseRefOid=self.base_sha, state='OPEN', isDraft=True, isCrossRepository=False,
                       statusCheckRollup=[failure()])
        self.worker = self.dispatcher()
        self.processes = []
        self.addCleanup(self.kill_processes)
        self.log = Path(self.temp.name)/'cli.log'
        environment = patch.dict(os.environ, {'FAKE_CLI_LOG': str(self.log), 'FAKE_CLI_SLEEP': '300',
                                              'FAKE_TICKET_DB': str(self.lock_path)})
        environment.start()
        self.addCleanup(environment.stop)

    def dispatcher(self):
        worker = d.Dispatcher(self.config, self.state)
        worker.ticket = Mock(side_effect=self.ticket)
        worker.fresh_pr = Mock(side_effect=lambda project, number: json.loads(json.dumps(self.pr)))
        return worker

    def ticket(self, project, *args):
        self.commands.append(args[0])
        if args[0] == 'show':
            return dict(self.owner)
        if args[0] == 'locks':
            return self.lockdb.list() + list(self.locks)
        if args[0] in ('lock', 'lock-renew', 'lock-assert', 'unlock'):
            try:
                return self.lockdb.command(args)
            except fake_ticket.Refused:
                raise subprocess.CalledProcessError(1, ['ticket', args[0]])
        if args[0] in ('heartbeat', 'assert'):
            if args[2:] != ('--token', 'private-token'):
                raise AssertionError('wrong claim token')
            return dict(self.owner)
        if args[0] == 'list':
            return [dict(self.owner)]
        raise AssertionError(f'unexpected ticket command {args}')

    def observe(self, pr=None):
        pr = pr or self.pr
        failures = c.failed_checks(pr)
        engine_owner = {**self.owner, 'status': self.owner['status']}
        self.store.db.execute('INSERT OR REPLACE INTO ci_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                              ('suifleet', pr['number'], 'SF-099', 'work/test', engine_owner['status'], pr['headRefOid'],
                               pr['baseRefOid'], int(pr['isDraft']), json.dumps(failures),
                               c.repair_feedback({**pr, '_ci_repair': failures}), 'owner', time.time()))

    def start_owner(self, session='dispatch-owner'):
        directory = self.state/'implementations'/session
        directory.mkdir(parents=True)
        (directory/'command.json').write_text(json.dumps(self.config['workers']['Codex']))
        (directory/'prompt.txt').write_text('Implement existing ticket SF-099')
        child = subprocess.Popen(['/usr/bin/python3', str(ROOT/'worker.py'), str(directory)], cwd=self.worktree,
                                 env={**os.environ, 'PR_COORDINATOR_CLAIM_TOKEN': 'private-token'}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        self.processes.append(child.pid)
        self.worker.children[session] = child
        deadline = time.time() + 20
        while not (directory/'process.json').exists() or len(self.worker.session_members(child.pid)[0]) < 2:
            self.assertLess(time.time(), deadline, 'fake worker did not start')
            time.sleep(0.05)
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                               (session, 'suifleet', 'Codex', 'running', str(directory), json.dumps(self.claim),
                                child.pid, d.process_identity(child.pid), None))
        return child, directory

    def kill_processes(self):
        rows = self.worker.db.execute("SELECT pid FROM runs WHERE pid IS NOT NULL").fetchall()
        for pid in {*self.processes, *(r[0] for r in rows)}:
            for member in d.Dispatcher.session_members(pid)[0]:
                try:
                    os.kill(member['pid'], signal.SIGKILL)
                except OSError:
                    pass

    def record(self):
        return self.worker.db.execute('SELECT * FROM ci_redirects ORDER BY id DESC').fetchone()

    def runs(self):
        return {r['session']: r['status'] for r in self.worker.db.execute('SELECT * FROM runs')}

    def advance(self, until, seconds=60, recheck=False):
        deadline = time.time() + seconds
        while True:
            if recheck:  # Collapse the bounded recheck delay so the scan races the owner.
                self.worker.db.execute("UPDATE ci_redirects SET retry_after=0 WHERE state='pending'")
            self.worker.redirect_ci_failures(self.project)
            record = self.record()
            if record and record['state'] in until:
                return record
            self.assertLess(time.time(), deadline, f"stuck in {record['state'] if record else None}: {record['reason'] if record else ''}")
            time.sleep(0.1)

    def run_with_fake_gh(self, args, **kwargs):
        if args[:3] == ['gh', 'auth', 'token']:
            return 'fixture-token\n'
        if args[:1] == ['gh']:
            raise AssertionError('no other GitHub access in redirect tests')
        return REAL_RUN(args, **kwargs)


class EndToEnd(Fixture):
    def test_live_draft_owner_redirect_repairs_preserves_dirty_work_and_resumes(self):
        child, directory = self.start_owner()
        # Unfinished owner work: a tracked edit, a staged new file and an untracked file.
        (self.worktree/'feature.txt').write_text('unfinished second step\n')
        (self.worktree/'staged.txt').write_text('staged work\n')
        git('add', 'staged.txt', cwd=self.worktree)
        (self.worktree/'notes.txt').write_text('untracked notes\n')
        self.observe()
        log = self.log
        with patch.object(d, 'run', side_effect=self.run_with_fake_gh), \
                patch.object(d.Dispatcher, 'claim_helpers', return_value=[]):
            self.assertFalse(self.worker.available(self.slot, [self.owner]))
            # The failure completed long ago: detection and revalidation interrupt in one scan.
            record = self.advance(('interrupting',))
            self.assertEqual(self.runs()['dispatch-owner'], 'redirected')
            members = json.loads(record['members'])
            self.assertEqual(members[0]['pid'], child.pid)
            self.assertGreaterEqual(len(members), 2)  # Wrapper and model CLI, both identity-pinned.
            self.advance(('stopped',))
            self.assertTrue(d.worker_stopped(child.pid, members[0]['started']))
            # Same-claim repair runs in a clean linked checkout; capacity stays one slot.
            with patch.dict(os.environ, {'FAKE_REPAIR_DELAY': '3'}):
                record = self.advance(('repairing',))
            # fleet-status reads the real dispatcher state: the live repair process is a
            # CI-repair worker on the owner's implementation slot; the owner is paused.
            deadline = time.time() + 20
            while not (Path(record['repair_dir'])/'process.json').exists():
                self.assertLess(time.time(), deadline, 'repair never started')
                time.sleep(0.05)
            (Path(self.temp.name)/'config.json').write_text(json.dumps(self.config))
            view = dash.collect(dict(dispatch=self.state/'dispatch.sqlite3', reviews=self.state/'reviews.sqlite3',
                                     tickets=Path(self.temp.name)/'absent.sqlite3', config=Path(self.temp.name)/'config.json',
                                     inventory=Path(self.temp.name)/'inventory'), project='suifleet')
            self.assertEqual([(w['role'], w['ticket'], w['pr'], w['family']) for w in view['workers']],
                             [('ci-repair', 'SF-099', 7, 'Codex')], view['attention'])
            self.assertIn('check-interfaces', view['workers'][0]['summary'])
            self.assertIn(('SF-099', 'paused'), {(a['ticket'], a['state']) for a in view['attention']})
            capacity = next(c for c in view['capacity'] if c['category'] == 'implementation')
            self.assertEqual((capacity['active'], capacity['held']), (1, 0))
            # The repair occupies the owner's slot: one execution reservation, not two.
            self.assertEqual(sum(1 for s in self.runs().values() if s != 'finished'), 1)
            self.assertTrue(self.worker.available({**self.slot, 'max_workers': 2}, [self.owner]))
            self.assertFalse(self.worker.available(self.slot, [self.owner]))
            record = self.advance(('resumed',), seconds=90)
        self.assertIn('integrated; unfinished feature changes verified preserved', record['reason'])
        tip = git('rev-parse', 'refs/heads/work/test', cwd=self.origin)
        self.assertEqual(record['repaired_head'], tip)
        self.assertNotEqual(tip, self.head)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=self.worktree), tip)  # Fast-forwarded in place.
        self.assertEqual((self.worktree/'feature.txt').read_text(), 'unfinished second step\n')
        self.assertEqual((self.worktree/'notes.txt').read_text(), 'untracked notes\n')
        self.assertEqual((self.worktree/'fix.txt').read_text(), 'repaired\n')
        self.assertIn('A  staged.txt', git('status', '--porcelain', cwd=self.worktree))  # Index preserved too.
        snapshot = json.loads(record['snapshot'])
        self.assertEqual(git('show', snapshot['worktree']['ref'] + ':notes.txt', cwd=self.root), 'untracked notes')
        self.assertFalse((Path(record['repair_dir'])/'checkout').exists())
        runs = self.runs()
        self.assertEqual(runs['dispatch-owner'], 'finished')
        self.assertEqual(runs[record['resumed_as']], 'running')
        resumed = self.worker.db.execute('SELECT * FROM runs WHERE session=?', (record['resumed_as'],)).fetchone()
        self.processes.append(resumed['pid'])
        self.assertEqual(json.loads(resumed['claim']), self.claim)
        self.assertIn('CI repair-first redirect', (Path(resumed['directory'])/'prompt.txt').read_text())
        self.assertEqual(sum(1 for s in runs.values() if s != 'finished'), 1)  # Capacity used once.
        self.assertFalse(self.worker.available(self.slot, [self.owner]))
        self.assertEqual(self.worker.db.execute('SELECT reason FROM retired_runs').fetchone()[0],
                         'CI repair-first redirect resumed as ' + record['resumed_as'])
        for forbidden in ('resume', 'review', 'claim', 'release', 'recover', 'block'):
            self.assertNotIn(forbidden, self.commands)
        deadline = time.time() + 20
        while len([line for line in log.read_text().splitlines()]) < 3 and time.time() < deadline:
            time.sleep(0.05)
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([e['mode'] for e in entries], ['implement', 'repair', 'implement'])
        self.assertTrue(all(e['token'] == 'private-token' for e in entries))
        self.assertTrue(entries[1]['cwd'].endswith('checkout') and entries[1]['cwd'] != str(self.worktree))
        self.assertTrue(entries[2]['resumed'])
        authors = git('log', '--format=%an <%ae>|%cn <%ce>', f'{self.head}..{tip}', cwd=self.root).splitlines()
        self.assertEqual(authors, ['Tyler Bush <tyler.jonathan.bush@gmail.com>|Tyler Bush <tyler.jonathan.bush@gmail.com>'])
        # Dispatcher restart adopts terminal state: no duplicate repair or resume.
        restarted = self.dispatcher()
        restarted.redirect_ci_failures(self.project)
        self.assertEqual(restarted.db.execute('SELECT count(*) FROM ci_redirects').fetchone()[0], 1)

    def test_conflicting_repair_keeps_unfinished_work_and_reports_actionable_status(self):
        self.start_owner()
        (self.worktree/'feature.txt').write_text('unfinished edit to the same file\n')
        self.observe()
        with patch.object(d, 'run', side_effect=self.run_with_fake_gh), \
                patch.object(d.Dispatcher, 'claim_helpers', return_value=[]), \
                patch.dict(os.environ, {'FAKE_REPAIR_FILE': 'feature.txt'}):
            record = self.advance(('conflict', 'resumed', 'uncertain'), seconds=90)
        self.assertEqual(record['state'], 'conflict', record['reason'])
        self.assertIn('merge origin/work/test manually', record['reason'])
        self.assertEqual((self.worktree/'feature.txt').read_text(), 'unfinished edit to the same file\n')
        self.assertEqual(git('rev-parse', 'HEAD', cwd=self.worktree), self.head)
        self.assertEqual(self.runs()['dispatch-owner'], 'quarantined')
        self.assertIsNone(record['resumed_as'])
        self.assertIn('CI redirect conflict', self.worker.db.execute('SELECT message FROM runs').fetchone()[0])

    def test_unpushed_local_commit_gets_user_authored_merge_and_resume(self):
        self.start_owner()
        (self.worktree/'local.txt').write_text('local commit\n')
        git('add', 'local.txt', cwd=self.worktree)
        git('commit', '-q', '-m', 'unpushed feature step', cwd=self.worktree)
        local = git('rev-parse', 'HEAD', cwd=self.worktree)
        (self.worktree/'notes.txt').write_text('untracked\n')
        self.observe()
        with patch.object(d, 'run', side_effect=self.run_with_fake_gh), \
                patch.object(d.Dispatcher, 'claim_helpers', return_value=[]):
            record = self.advance(('resumed', 'conflict', 'uncertain', 'repair_failed'), seconds=90)
        self.assertEqual(record['state'], 'resumed', record['reason'])
        parents = git('rev-list', '--parents', '-n', '1', 'HEAD', cwd=self.worktree).split()[1:]
        self.assertEqual(parents, [local, record['repaired_head']])
        self.assertEqual(git('show', '-s', '--format=%an <%ae>|%cn <%ce>', 'HEAD', cwd=self.worktree),
                         'Tyler Bush <tyler.jonathan.bush@gmail.com>|Tyler Bush <tyler.jonathan.bush@gmail.com>')
        self.assertEqual((self.worktree/'notes.txt').read_text(), 'untracked\n')
        self.processes.append(self.worker.db.execute('SELECT pid FROM runs WHERE session=?', (record['resumed_as'],)).fetchone()[0])

    def test_blocked_repair_quarantines_paused_owner_without_resume(self):
        self.start_owner()
        (self.worktree/'notes.txt').write_text('untracked\n')
        self.observe()
        with patch.object(d, 'run', side_effect=self.run_with_fake_gh), \
                patch.object(d.Dispatcher, 'claim_helpers', return_value=[]), \
                patch.dict(os.environ, {'FAKE_REPAIR': 'blocked'}):
            record = self.advance(('repair_failed', 'resumed'), seconds=90)
        self.assertEqual(record['state'], 'repair_failed')
        self.assertEqual(self.runs(), {'dispatch-owner': 'quarantined'})
        self.assertEqual((self.worktree/'notes.txt').read_text(), 'untracked\n')
        # Verified stopped, so the retained claim no longer consumes execution capacity.
        self.assertTrue(self.worker.available(self.slot, [self.owner]))
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM ci_redirects').fetchone()[0], 1)


    def test_lock_obeying_owner_is_never_stopped_mid_mutation(self):
        # A real legacy worker loops shared-wallet mutations under the exclusive lock;
        # the redirect waits for a gap, fences it with real guard leases and stops it.
        with patch.dict(os.environ, {'FAKE_MUTATE': 'network:suifleet-testnet'}):
            child, _ = self.start_owner()
            deadline = time.time() + 20
            while '"begin"' not in (self.log.read_text() if self.log.exists() else ''):
                self.assertLess(time.time(), deadline, 'owner never mutated')
                time.sleep(0.05)
            self.observe()
            with patch.object(d, 'run', side_effect=self.run_with_fake_gh), \
                    patch.object(d.Dispatcher, 'claim_helpers', return_value=[]), \
                    patch.object(d.subprocess, 'Popen', side_effect=self.no_repair_launch):
                record = self.advance(('stopped', 'orphaned'), recheck=True)
        events = [json.loads(line).get('mutation') for line in self.log.read_text().splitlines()]
        events = [e for e in events if e]
        self.assertGreater(events.count('begin'), 0)
        self.assertEqual(events.count('begin'), events.count('end'), events)  # Never interrupted mid-mutation.
        self.assertTrue(d.worker_stopped(child.pid, json.loads(record['members'])[0]['started']))
        self.assertFalse([l for l in self.lockdb.list() if not l['expired']])  # Owner unlocked; guards released.

    def test_overdue_repair_is_never_stopped_mid_mutation(self):
        # Defence in depth: even a repair that (against its offline prompt) loops
        # lock-obeying shared-wallet mutations is stopped only between them.
        directory = self.state/'implementations'/'dispatch-owner'/'ci-redirect-1'/'repair'
        directory.mkdir(parents=True)
        (directory/'command.json').write_text(json.dumps(self.config['workers']['Codex']))
        (directory/'prompt.txt').write_text('Implement existing ticket SF-099')
        with patch.dict(os.environ, {'FAKE_MUTATE': 'network:suifleet-testnet'}):
            child = subprocess.Popen(['/usr/bin/python3', str(ROOT/'worker.py'), str(directory)], cwd=self.worktree,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        self.processes.append(child.pid)
        deadline = time.time() + 20
        while '"begin"' not in (self.log.read_text() if self.log.exists() else ''):
            self.assertLess(time.time(), deadline, 'repair never mutated')
            time.sleep(0.05)
        self.assertFalse((directory/'limits.json').exists())
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                               ('dispatch-owner', 'suifleet', 'Codex', 'redirected', str(self.state), json.dumps(self.claim),
                                None, None, None))
        self.worker.db.execute(
            "INSERT INTO ci_redirects(id,source,project,family,claim,ticket,pr,branch,head,base,fingerprint,failures,state,"
            "reason,repair_dir,repair_deadline,created,updated) VALUES(1,'dispatch-owner','suifleet','Codex','c','SF-099',7,"
            "'work/test',?,?,'f','[]','repairing','x',?,?,0,?)", (self.head, self.base_sha, str(directory), time.time() - 1, time.time()))
        with patch.object(d.Dispatcher, 'claim_helpers', return_value=[]):
            deadline = time.time() + 90
            while self.record()['state'] not in ('repair_failed', 'uncertain'):
                self.assertLess(time.time(), deadline, self.record()['reason'])
                self.worker.db.execute('UPDATE ci_redirects SET retry_after=0')
                self.worker.redirect_ci_failures(self.project)
                time.sleep(0.05)
        record = self.record()
        self.assertEqual(record['state'], 'repair_failed', record['reason'])
        self.assertIn('overdue repair stopped under guards', record['reason'])
        events = [e for e in (json.loads(line).get('mutation') for line in self.log.read_text().splitlines()) if e]
        self.assertGreater(events.count('begin'), 0)
        self.assertEqual(events.count('begin'), events.count('end'), events)  # Never interrupted mid-mutation.
        self.assertTrue(d.worker_stopped(child.pid, json.loads(record['members'])[0]['started']))
        self.assertFalse([l for l in self.lockdb.list() if not l['expired']])
        child.wait()

    REAL_POPEN = subprocess.Popen

    def no_repair_launch(self, args, *rest, **kwargs):
        if any(str(arg).endswith('worker.py') for arg in args):
            raise OSError('repair launch not part of this test')
        return self.REAL_POPEN(args, *rest, **kwargs)


class RealSessions(Fixture):
    """Process-session expansion with real processes; nothing is mocked."""

    def session(self, script):
        child = subprocess.Popen(['/bin/sh', '-c', script], start_new_session=True)
        self.processes.append(child.pid)
        deadline = time.time() + 10
        while len(d.Dispatcher.session_members(child.pid)[0]) < 3:
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)
        return child

    def test_surviving_original_member_anchors_expansion_after_leader_exit(self):
        child = self.session('sleep 60 & sleep 60 & exec sleep 1')
        members, _ = d.Dispatcher.session_members(child.pid)
        leader, survivor = members[0], members[1]
        child.wait()
        self.assertIsNone(d.Dispatcher.anchored_members(child.pid, [leader]))  # A dead leader is no anchor.
        listed, foreign = d.Dispatcher.anchored_members(child.pid, [leader, survivor])
        self.assertFalse(foreign)
        self.assertEqual({m['pid'] for m in listed}, {m['pid'] for m in members[1:]})
        for member in listed:
            os.kill(member['pid'], signal.SIGKILL)

    def test_recycled_wrapper_pid_never_expands_to_an_unrelated_session(self):
        # The recorded wrapper died; its numeric PID/SID now leads an unrelated live session.
        stranger = self.session('sleep 60 & sleep 60 & wait')
        strangers, _ = d.Dispatcher.session_members(stranger.pid)
        recycled = dict(pid=stranger.pid, started='1')  # The stopped worker's original identity.
        self.assertIsNone(d.Dispatcher.anchored_members(stranger.pid, [recycled]))
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                               ('dispatch-owner', 'suifleet', 'Codex', 'redirected', str(self.state), json.dumps(self.claim),
                                stranger.pid, '1', None))
        session = d.GUARD_PREFIX + '1-test'
        guards = [dict(resource=r, token=self.lockdb.lock(r, 'Other', 'm', session, 'guard')['token'], expires=0)
                  for r in self.protected]
        self.worker.db.execute(
            "INSERT INTO ci_redirects(id,source,project,family,claim,ticket,pr,branch,head,base,fingerprint,failures,state,"
            "reason,members,signalled,guard_session,guards,created,updated) VALUES(1,'dispatch-owner','suifleet','Codex',"
            "'c','SF-099',7,'work/test',?,?,'f','[]','interrupting','x',?,?,?,?,0,0)",
            (self.head, self.base_sha, json.dumps([recycled]), time.time() - d.CI_TERM_SECONDS - 1, session, json.dumps(guards)))
        with patch.object(d.Dispatcher, 'claim_helpers', return_value=[]):
            self.worker.redirect_ci_failures(self.project)
            for member in strangers:
                self.assertEqual(d.process_identity(member['pid']), member['started'])  # Never signalled.
            self.assertEqual(json.loads(self.record()['members']), [recycled])  # Nothing appended.
            self.worker.db.execute('UPDATE ci_redirects SET signalled=?,killed=CASE WHEN killed>0 THEN ? ELSE 0 END',
                                   (time.time() - d.CI_STOP_SECONDS - 1, time.time() - d.CI_KILL_SECONDS - 1))
            self.worker.redirect_ci_failures(self.project)
        self.assertEqual(self.record()['state'], 'uncertain')
        for member in strangers:
            self.assertEqual(d.process_identity(member['pid']), member['started'])
        self.assertEqual([l for l in self.lockdb.list() if not l['expired']], [])


    def zombie_leader_with_live_descendant(self):
        """A session leader that exited unreaped (zombie) while a descendant keeps running."""
        child = subprocess.Popen(['/bin/sh', '-c', 'sleep 60 & sleep 0.5; exit 0'], start_new_session=True)
        self.processes.append(child.pid)
        started = d.process_identity(child.pid)
        deadline = time.time() + 10
        while d.runnable_identity(child.pid) is not None:
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)
        survivor = d.Dispatcher.session_members(child.pid)[0]
        self.assertEqual(len(survivor), 1)
        return child, dict(pid=child.pid, started=started), survivor[0]

    def wait_not_runnable(self, member):
        deadline = time.time() + 10
        while d.runnable_identity(member['pid']) == member['started']:
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)

    def test_zombie_wrapper_is_stopped_only_once_live_descendants_stop(self):
        child, wrapper, survivor = self.zombie_leader_with_live_descendant()
        # A zombie keeps its PID (no reuse) and identity, but never runs again.
        self.assertEqual(d.process_identity(child.pid), wrapper['started'])
        self.assertIsNone(d.runnable_identity(child.pid))
        self.assertIsNone(d.Dispatcher.member_session(wrapper))
        self.assertFalse(d.worker_stopped(child.pid, wrapper['started']))  # The live descendant still fences.
        os.kill(survivor['pid'], signal.SIGKILL)
        self.wait_not_runnable(survivor)  # Reaped, or an unreaped zombie under a PID-namespace init.
        self.assertTrue(d.worker_stopped(child.pid, wrapper['started']))
        child.wait()  # Reap last: the wrapper was a zombie throughout.

    def test_interruption_completes_with_zombie_members_but_waits_for_live_ones(self):
        child, wrapper, survivor = self.zombie_leader_with_live_descendant()
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                               ('dispatch-owner', 'suifleet', 'Codex', 'redirected', str(self.state), json.dumps(self.claim),
                                wrapper['pid'], wrapper['started'], None))
        session = d.GUARD_PREFIX + '1-test'
        guards = [dict(resource=r, token=self.lockdb.lock(r, 'Other', 'm', session, 'guard')['token'], expires=0)
                  for r in self.protected]
        self.worker.db.execute(
            "INSERT INTO ci_redirects(id,source,project,family,claim,ticket,pr,branch,head,base,fingerprint,failures,state,"
            "reason,members,signalled,guard_session,guards,created,updated) VALUES(1,'dispatch-owner','suifleet','Codex',"
            "'c','SF-099',7,'work/test',?,?,'f','[]','interrupting','x',?,?,?,?,0,0)",
            (self.head, self.base_sha, json.dumps([wrapper, survivor]), time.time(), session, json.dumps(guards)))
        with patch.object(d.Dispatcher, 'claim_helpers', return_value=[]):
            self.worker.redirect_ci_failures(self.project)
            self.assertEqual(self.record()['state'], 'interrupting')  # Live original descendant: still fenced.
            self.assertEqual(sorted(l['resource'] for l in self.lockdb.list() if not l['expired']), self.protected)
            os.kill(survivor['pid'], signal.SIGKILL)
            self.wait_not_runnable(survivor)
            self.worker.redirect_ci_failures(self.project)
        self.assertEqual(self.record()['state'], 'stopped', self.record()['reason'])
        self.assertEqual([l for l in self.lockdb.list() if not l['expired']], [])
        self.assertIsNone(self.record()['guard_session'])
        child.wait()


class Decisions(Fixture):
    """Unit-level decisions with process identity mocked; nothing is signalled."""

    def owner_row(self, status='running'):
        directory = self.state/'implementations'/'dispatch-owner'
        directory.mkdir(parents=True, exist_ok=True)
        (directory/'process.json').write_text(json.dumps(dict(pid=4242, started='start')))
        self.worker.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                               ('dispatch-owner', 'suifleet', 'Codex', status, str(directory), json.dumps(self.claim),
                                4242, 'start', None))

    def scan(self, alive=True, members=None, on_signal=None):
        members = members if members is not None else [dict(pid=4242, started='start'), dict(pid=4243, started='s2')]
        signals = []
        identity = lambda pid: {4242: 'start', 4243: 's2'}.get(pid) if alive else None
        with patch.object(d, 'process_identity', side_effect=identity), \
                patch.object(d, 'runnable_identity', side_effect=identity), \
                patch.object(d, 'worker_stopped', return_value=not alive), \
                patch.object(d.Dispatcher, 'anchored_members', return_value=(members, False) if alive else None), \
                patch.object(d.Dispatcher, 'claim_helpers', return_value=[]), \
                patch.object(d.Dispatcher, 'signal_members', side_effect=lambda m, n: (on_signal and on_signal(n), signals.append(n))):
            self.worker.redirect_ci_failures(self.project)
        return signals

    def guard_rows(self):
        return [l for l in self.lockdb.list() if str(l['session']).startswith(d.GUARD_PREFIX) and not l['expired']]

    def test_grace_period_then_single_interruption_and_dedup_across_restart(self):
        self.owner_row()
        self.pr['statusCheckRollup'] = [failure(completedAt=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))]
        self.observe()
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.record()['state'], 'pending')
        self.worker.fresh_pr.assert_not_called()  # No GitHub reads during the owner grace period.
        self.worker.db.execute('UPDATE ci_redirects SET retry_after=0')
        self.assertEqual(self.scan(), [signal.SIGTERM])
        self.assertEqual(self.record()['state'], 'interrupting')
        self.worker = self.dispatcher()  # Restart adopts the durable record; never re-signals SIGTERM.
        self.assertEqual(self.scan(), [])
        self.observe()
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM ci_redirects').fetchone()[0], 1)
        self.assertFalse(self.worker.available(self.slot, [self.owner]))

    def test_green_outdated_cancelled_and_rerun_failures_never_interrupt(self):
        cases = {
            'green': [failure(conclusion='SUCCESS')],
            'cancelled': [failure(conclusion='CANCELLED')],
            'rerun': [failure(), {**failure(2), 'status': 'IN_PROGRESS', 'conclusion': None,
                                  'startedAt': '2026-01-01T00:05:00Z'}],
            'new-run': [failure(9)],
        }
        for name, rollup in [*cases.items(), ('moved-head', None), ('closed', None)]:
            with self.subTest(name):
                self.worker.db.execute('DELETE FROM runs')
                self.worker.db.execute('DELETE FROM ci_redirects')
                self.owner_row()
                self.pr['statusCheckRollup'] = [failure()]
                self.observe()
                fresh = dict(self.pr, statusCheckRollup=rollup or [failure()])
                if name == 'moved-head':
                    fresh['headRefOid'] = 'f' * 40
                if name == 'closed':
                    fresh['state'] = 'CLOSED'
                self.worker.fresh_pr = Mock(return_value=fresh)
                self.assertEqual(self.scan(), [])
                self.assertEqual(self.record()['state'], 'superseded')
                self.assertEqual(self.runs()['dispatch-owner'], 'running')

    def test_stopped_parked_quarantined_and_manual_owners_are_not_redirected(self):
        for status in ('quarantined', 'parked', 'orphaned'):
            with self.subTest(status):
                self.worker.db.execute('DELETE FROM runs')
                self.owner_row(status)
                self.observe()
                self.assertEqual(self.scan(alive=False), [])
                self.assertIsNone(self.record())
        self.worker.db.execute('DELETE FROM runs')
        self.assertEqual(self.scan(), [])  # Manual claim: no dispatcher identity, never signalled.
        self.assertIsNone(self.record())

    def test_exited_or_reused_pid_before_interruption_is_left_to_reconciliation(self):
        self.owner_row()
        self.observe()
        self.assertEqual(self.scan(alive=False), [])
        self.assertEqual(self.record()['state'], 'pending')
        self.worker.db.execute("UPDATE runs SET status='quarantined'")
        self.assertEqual(self.scan(alive=False), [])
        self.assertEqual(self.record()['state'], 'superseded')

    def test_changed_owner_or_review_handoff_is_never_interrupted(self):
        for change, state in (({'token': 'replacement'}, 'superseded'), ({'status': 'review'}, 'handed_off'),
                              ({'publish_pending': True}, 'superseded'), ({'session': 'other'}, 'superseded')):
            with self.subTest(change):
                self.worker.db.execute('DELETE FROM runs')
                self.worker.db.execute('DELETE FROM ci_redirects')
                self.owner = dict(self.claim)
                self.owner_row()
                self.observe()  # Observed while in progress; ownership changes before revalidation.
                self.owner = {**self.claim, **change}
                self.assertEqual(self.scan(), [])
                self.assertEqual(self.record()['state'], state)
                self.worker.fresh_pr.assert_not_called()

    def test_owner_locks_defer_interruption_and_guards_are_never_counted(self):
        later = time.time() + 600
        own = [
            dict(resource='build:x', agent='Codex', session='dispatch-owner', ticket=None, purpose='tests', expires=later),
            dict(resource='build:y', agent='Claude', session='other', ticket='suifleet::SF-099', purpose='x', expires=later),
            dict(resource='port:3000', agent='Codex', session='sf-099-dev', ticket=None, purpose='dev server', expires=later),
        ]
        harmless = [
            dict(resource='network:suifleet-testnet', agent='Codex', session='sf-200-run', ticket=None, purpose='SF-200 run', expires=later),
            dict(resource='build:z', agent='Codex', session='dispatch-owner', ticket=None, purpose='old', expires=time.time()-1),
            dict(resource='network:x', agent='Other', session=d.GUARD_PREFIX + '1-abc', ticket=None, purpose='SF-099 CI redirect', expires=later),
        ]
        for lock in own:
            with self.subTest(lock=lock['resource']):
                self.locks = [lock]
                self.assertEqual(self.worker.owner_locks(self.project, self.claim), [lock['resource']])
        self.locks = harmless
        self.assertEqual(self.worker.owner_locks(self.project, self.claim), [])
        # A pending redirect waits visibly, takes no guard and retries with a bounded recheck.
        self.locks = [own[0]]
        self.owner_row()
        self.observe()
        self.assertEqual(self.scan(), [])
        record = self.record()
        self.assertEqual(record['state'], 'pending')
        self.assertIn('build:x', record['reason'])
        self.assertGreater(record['retry_after'], time.time() + d.CI_REDIRECT_RECHECK - 5)
        self.assertEqual(self.lockdb.list(), [])

    def test_unconfigured_protected_resources_disable_forced_interruption(self):
        self.project.pop('ci_redirect_protected_resources')
        self.owner_row()
        self.observe()
        self.assertEqual(self.scan(), [])
        record = self.record()
        self.assertEqual(record['state'], 'pending')
        self.assertIn('forced interruption disabled', record['reason'])
        self.assertNotIn('lock', self.commands)
        self.assertEqual(self.runs()['dispatch-owner'], 'running')

    def test_protected_resource_configuration_is_exact_and_includes_testnet(self):
        grant = dict(network='testnet', chain_identifier='4c78adac', wallet='/w/client.yaml', lock_resource='network:suifleet-testnet')
        for resources in ([], 'network:suifleet-testnet', ['a', 'a'], ['bad name'], [3], ['hardware:raspberrypi3bplus']):
            with self.subTest(resources=resources):
                project = {**self.project, 'ci_redirect_protected_resources': resources, 'testnet_authorization': grant}
                with self.assertRaises(ValueError):
                    d.Dispatcher({**self.config, 'projects': [project]}, self.state)
        project = {**self.project, 'testnet_authorization': grant}
        d.Dispatcher({**self.config, 'projects': [project]}, self.state)  # Exhaustive list accepted.

    def test_owner_cannot_acquire_a_protected_resource_between_safety_check_and_signal(self):
        self.owner_row()
        self.observe()
        refused = []
        def owner_tries(number):
            # The owner races for the shared wallet at the last moment before the signal.
            for resource in self.protected:
                try:
                    self.lockdb.lock(resource, 'Codex', 'm', 'dispatch-owner', 'SF-099 testnet upgrade')
                except fake_ticket.Refused:
                    refused.append(resource)
        self.assertEqual(self.scan(on_signal=owner_tries), [signal.SIGTERM])
        self.assertEqual(refused, self.protected)  # Fenced: both acquisitions refused while guards are held.
        record = self.record()
        self.assertEqual(record['state'], 'interrupting')
        guards = json.loads(record['guards'])
        self.assertEqual(sorted(g['resource'] for g in guards), self.protected)
        self.assertEqual(sorted(l['resource'] for l in self.guard_rows()), self.protected)
        self.assertTrue(all(l['agent'] == 'Other' for l in self.guard_rows()))

    def test_resource_taken_after_safety_check_releases_partial_guards_and_never_signals(self):
        self.owner_row()
        self.observe()
        real = d.Dispatcher.interruptible
        def racing(worker, *args):
            result = real(worker, *args)
            # Acquired right after the read-only safety check, before the guard reaches it.
            if not self.lockdb.list():
                self.lockdb.lock('network:suifleet-testnet', 'Codex', 'm', 'dispatch-owner', 'SF-099 upgrade')
            return result
        with patch.object(d.Dispatcher, 'interruptible', racing):
            self.assertEqual(self.scan(), [])
        record = self.record()
        self.assertEqual(record['state'], 'pending')
        self.assertIn('network:suifleet-testnet is held', record['reason'])
        self.assertEqual(self.guard_rows(), [])  # The hardware guard taken first was released.
        self.assertIn('lock', self.commands)
        self.assertIn('unlock', self.commands)
        self.assertEqual(self.runs()['dispatch-owner'], 'running')
        self.assertIsNone(record['members'])

    def test_crash_with_partial_or_unrecorded_guard_releases_on_restart_without_signal(self):
        self.owner_row()
        self.observe()
        calls = []
        real = d.Dispatcher.save_guards
        def crash(worker, record, guards):
            calls.append(len(guards))
            if len(calls) == 1:
                raise sqlite3.OperationalError('disk I/O error')  # Lease taken, receipt not yet durable.
            return real(worker, record, guards)
        with patch.object(d.Dispatcher, 'save_guards', crash):
            self.assertEqual(self.scan(), [])
        self.assertEqual(self.record()['state'], 'guarding')
        self.assertEqual(len(self.guard_rows()), 1)  # Held with no receipt.
        self.worker = self.dispatcher()  # Restart.
        self.worker.db.execute('UPDATE ci_redirects SET retry_after=0')
        self.assertEqual(self.scan(), [])
        record = self.record()
        self.assertEqual(record['state'], 'pending', record['reason'])
        self.assertIn('guards released', record['reason'])
        self.assertEqual(self.guard_rows(), [])  # Recovered by same-session re-lock, then unlocked.
        self.assertEqual(self.runs()['dispatch-owner'], 'running')
        # The next evaluation interrupts normally under fresh guards.
        self.assertEqual(self.scan(), [signal.SIGTERM])

    def test_crash_between_intent_and_sigterm_signals_once_or_reverts_without_guard(self):
        self.owner_row()
        self.observe()
        self.assertEqual(self.scan(), [signal.SIGTERM])
        self.worker.db.execute('UPDATE ci_redirects SET signalled=NULL')
        self.worker = self.dispatcher()
        self.assertEqual(self.scan(), [signal.SIGTERM])  # Delivered under still-held guards.
        self.assertIsNotNone(self.record()['signalled'])
        self.assertEqual(self.scan(), [])
        # Same crash, but the guard lapsed and another session took the wallet meanwhile.
        self.worker.db.execute('UPDATE ci_redirects SET signalled=NULL')
        self.lockdb.db.execute("UPDATE locks SET expires=0 WHERE resource='network:suifleet-testnet'")
        self.lockdb.lock('network:suifleet-testnet', 'Claude', 'm', 'sf-200-run', 'SF-200 run')
        self.assertEqual(self.scan(), [])
        record = self.record()
        self.assertEqual(record['state'], 'pending')
        self.assertIn('never signalled', record['reason'])
        self.assertEqual(self.runs()['dispatch-owner'], 'running')
        self.assertEqual(self.guard_rows(), [])
        self.assertEqual([l['session'] for l in self.lockdb.list()], ['sf-200-run'])

    def test_escalation_waits_while_a_resource_becomes_held_and_releases_after_stop(self):
        self.owner_row()
        self.observe()
        self.assertEqual(self.scan(), [signal.SIGTERM])
        # Guard leases lapse (e.g. a stalled dispatcher) and the dying owner grabs the wallet.
        self.lockdb.db.execute('UPDATE locks SET expires=0')
        self.lockdb.lock('network:suifleet-testnet', 'Codex', 'm', 'dispatch-owner', 'SF-099 upgrade')
        self.worker.db.execute('UPDATE ci_redirects SET signalled=?', (time.time() - d.CI_TERM_SECONDS - 1,))
        self.assertEqual(self.scan(), [])  # No SIGKILL mid-mutation.
        record = self.record()
        self.assertEqual(record['state'], 'interrupting')
        self.assertEqual(record['killed'], 0)
        self.assertIn('SIGKILL withheld', record['reason'])
        # The hardware guard was re-taken because nobody held it.
        self.assertEqual([l['resource'] for l in self.guard_rows()], ['hardware:raspberrypi3bplus'])
        # The owner finishes its mutation and unlocks: the guard is re-taken, then SIGKILL.
        owner = next(l for l in self.lockdb.list() if l['session'] == 'dispatch-owner')
        self.lockdb.db.execute('DELETE FROM locks WHERE resource=?', (owner['resource'],))
        self.assertEqual(self.scan(), [signal.SIGKILL])
        self.assertEqual(sorted(l['resource'] for l in self.guard_rows()), self.protected)
        # Verified stop releases every guard before any repair step.
        with patch.object(d.subprocess, 'Popen') as spawn:
            self.scan(alive=False)
            spawn.assert_not_called()
        record = self.record()
        self.assertEqual(record['state'], 'stopped')
        self.assertIn('guard leases released', record['reason'])
        self.assertEqual(self.guard_rows(), [])
        self.assertIsNone(record['guards'])

    def test_guard_release_failure_retries_before_repair(self):
        self.owner_row()
        self.observe()
        self.assertEqual(self.scan(), [signal.SIGTERM])
        real = self.lockdb.unlock
        failures = []
        def flaky(resource, token):
            if not failures:
                failures.append(resource)
                raise fake_ticket.Refused('database is locked')
            return real(resource, token)
        with patch.object(self.lockdb, 'unlock', flaky), patch.object(d.subprocess, 'Popen') as spawn:
            self.scan(alive=False)
            record = self.record()
            self.assertEqual(record['state'], 'releasing')
            self.assertIn('guard release incomplete', record['reason'])
            spawn.assert_not_called()  # No repair while any guard remains held.
            self.worker.db.execute('UPDATE ci_redirects SET retry_after=0')
            self.scan(alive=False)
            spawn.assert_not_called()
        self.assertEqual(self.guard_rows(), [])
        self.assertEqual(self.record()['state'], 'stopped')

    def test_uncertain_stop_releases_guards_and_keeps_capacity(self):
        self.owner_row()
        self.observe()
        self.assertEqual(self.scan(), [signal.SIGTERM])
        self.worker.db.execute('UPDATE ci_redirects SET signalled=?', (time.time() - d.CI_TERM_SECONDS - 1,))
        self.assertEqual(self.scan(), [signal.SIGKILL])
        self.worker.db.execute('UPDATE ci_redirects SET signalled=?,killed=CASE WHEN killed>0 THEN ? ELSE 0 END',
                                   (time.time() - d.CI_STOP_SECONDS - 1, time.time() - d.CI_KILL_SECONDS - 1))
        self.assertEqual(self.scan(), [])
        record = self.record()
        self.assertEqual(record['state'], 'uncertain')
        self.assertIn('SIGKILL attempted under held guards', record['reason'])
        self.assertEqual(self.guard_rows(), [])
        self.assertEqual(self.runs()['dispatch-owner'], 'redirected')

    def test_sui_cli_or_wallet_process_in_session_defers_interruption(self):
        folder = Path(self.temp.name)/'bin'
        folder.mkdir()
        (folder/'sui').symlink_to('/bin/sleep')
        wallet = Path(self.temp.name)/'client.yaml'
        project = {**self.project, 'testnet_authorization': dict(network='testnet', chain_identifier='4c78adac',
                                                                wallet=str(wallet), lock_resource='network:suifleet-testnet')}
        for argv in ([str(folder/'sui'), '30'], ['/bin/sh', '-c', f'sleep 30 # {wallet}']):
            with self.subTest(argv=argv[0]):
                child = subprocess.Popen(argv, start_new_session=True)
                self.addCleanup(child.kill)
                time.sleep(0.2)
                members, _ = d.Dispatcher.session_members(child.pid)
                self.assertTrue(members)
                self.assertIsNotNone(self.worker.sui_activity(project, members))
                child.kill()
                child.wait()

    def test_foreign_user_member_or_detached_helper_defers_interruption(self):
        self.owner_row()
        self.observe()
        with patch.object(d, 'process_identity', return_value='start'), \
                patch.object(d, 'runnable_identity', return_value='start'), \
                patch.object(d.Dispatcher, 'anchored_members', return_value=([dict(pid=4242, started='start')], True)), \
                patch.object(d.Dispatcher, 'signal_members') as signals:
            self.worker.redirect_ci_failures(self.project)
            signals.assert_not_called()
        self.assertIn("another user's process", self.record()['reason'])
        self.worker.db.execute('UPDATE ci_redirects SET retry_after=0')
        with patch.object(d, 'process_identity', return_value='start'), \
                patch.object(d, 'runnable_identity', return_value='start'), \
                patch.object(d.Dispatcher, 'anchored_members', return_value=([dict(pid=4242, started='start')], False)), \
                patch.object(d.Dispatcher, 'claim_helpers', return_value=[dict(pid=9, started='x')]), \
                patch.object(d.Dispatcher, 'signal_members') as signals:
            self.worker.redirect_ci_failures(self.project)
            signals.assert_not_called()
        self.assertIn('detached claim helper', self.record()['reason'])

    def test_unstoppable_worker_escalates_once_then_stays_reserved(self):
        self.owner_row()
        self.observe()
        self.assertEqual(self.scan(), [signal.SIGTERM])
        self.assertEqual(self.scan(), [])
        self.worker.db.execute('UPDATE ci_redirects SET signalled=?', (time.time() - d.CI_TERM_SECONDS - 1,))
        self.assertEqual(self.scan(), [signal.SIGKILL])
        self.assertEqual(self.scan(), [])
        self.worker.db.execute('UPDATE ci_redirects SET signalled=?,killed=CASE WHEN killed>0 THEN ? ELSE 0 END',
                                   (time.time() - d.CI_STOP_SECONDS - 1, time.time() - d.CI_KILL_SECONDS - 1))
        self.assertEqual(self.scan(), [])
        record = self.record()
        self.assertEqual(record['state'], 'uncertain')
        self.assertEqual(self.runs()['dispatch-owner'], 'redirected')  # Holds capacity; nothing released.
        self.assertFalse(self.worker.available(self.slot, [self.owner]))
        self.assertEqual(self.scan(), [])
        self.assertNotIn('release', self.commands)

    def test_redirect_limit_per_claim_leaves_later_failures_visible(self):
        self.owner_row()
        for number in range(d.CI_REDIRECTS_PER_CLAIM):
            self.worker.db.execute("INSERT INTO ci_redirects(claim,fingerprint,state,members,source,project) VALUES(?,?,?,?,?,?)",
                                   (__import__('hashlib').sha256(b'private-token').hexdigest(), f'old-{number}', 'resumed', '[]',
                                    'old', 'suifleet'))
        self.observe()
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.record()['state'], 'exhausted')
        self.assertIn('limit', self.record()['reason'])

    def test_github_revalidation_failure_backs_off_and_is_bounded(self):
        self.owner_row()
        self.observe()
        self.worker.fresh_pr = Mock(side_effect=subprocess.CalledProcessError(1, ['gh']))
        for attempt in range(1, d.CI_ATTEMPTS + 1):
            self.assertEqual(self.scan(), [])
            self.worker.db.execute('UPDATE ci_redirects SET retry_after=0')
        record = self.record()
        self.assertEqual(record['state'], 'failed')
        self.assertEqual(self.runs()['dispatch-owner'], 'running')  # Never interrupted.

    REAL_POPEN = subprocess.Popen

    def launch_fails(self, args, *rest, **kwargs):
        # Only the model worker launch fails; git and other commands still run.
        if any(str(arg).endswith('worker.py') for arg in args):
            raise OSError('no cli')
        return self.REAL_POPEN(args, *rest, **kwargs)

    def stopped_record(self, state, **fields):
        self.owner_row()
        self.observe()
        self.assertEqual(self.scan(), [signal.SIGTERM])
        self.assertEqual(self.runs()['dispatch-owner'], 'redirected')
        self.worker.db.execute('UPDATE ci_redirects SET state=?,members=?,signalled=? WHERE 1', (state, json.dumps([dict(pid=4242, started='start')]), time.time()))
        if state != 'interrupting':
            # Later states start after the verified-stop guard release.
            self.lockdb.db.execute('DELETE FROM locks')
            self.worker.db.execute('UPDATE ci_redirects SET guards=NULL,guard_session=NULL')
        for name, value in fields.items():
            self.worker.db.execute(f'UPDATE ci_redirects SET {name}=?', (value,))
        return self.record()

    def test_repair_launch_failure_or_interrupted_launch_is_orphaned_never_relaunched(self):
        self.stopped_record('stopped')
        with patch.object(d, 'run', side_effect=self.run_with_fake_gh), \
                patch.object(d.subprocess, 'Popen', side_effect=self.launch_fails) as spawn:
            self.scan(alive=False)
            self.assertEqual(self.record()['state'], 'orphaned', self.record()['reason'])
            self.scan(alive=False)
            self.assertEqual(sum(1 for call in spawn.call_args_list if 'worker.py' in str(call.args[0])), 1)
        self.assertEqual(self.runs()['dispatch-owner'], 'redirected')
        self.worker.db.execute('DELETE FROM ci_redirects')
        self.worker.db.execute('DELETE FROM runs')
        self.stopped_record('repairing', repair_dir=str(self.state/'never-started'), updated=time.time() - 120)
        self.scan(alive=False)
        self.assertEqual(self.record()['state'], 'orphaned')
        self.assertIn('never relaunched', self.record()['reason'])

    def test_dirty_owner_git_operation_or_lost_branch_stays_untouched(self):
        (self.worktree/'feature.txt').write_text('dirty\n')
        git_dir = Path(git('rev-parse', '--absolute-git-dir', cwd=self.worktree))
        (git_dir/'index.lock').write_text('')
        self.stopped_record('stopped')
        with patch.object(d, 'run', side_effect=self.run_with_fake_gh):
            self.scan(alive=False)
        record = self.record()
        self.assertEqual(record['state'], 'uncertain')
        self.assertIn('index.lock', record['reason'])
        self.assertIsNone(record['snapshot'])
        self.assertEqual((self.worktree/'feature.txt').read_text(), 'dirty\n')
        self.assertEqual(self.runs()['dispatch-owner'], 'quarantined')

    def test_resume_assert_failure_retries_with_bounded_backoff(self):
        self.stopped_record('resuming', snapshot='{}', reason='failure resolved')
        def ticket(project, *args):
            if args[0] == 'assert':
                raise subprocess.CalledProcessError(1, ['ticket'])
            return self.ticket(project, *args)
        self.worker.ticket = Mock(side_effect=ticket)
        with patch.object(d, 'run', side_effect=self.run_with_fake_gh), patch.object(d.subprocess, 'Popen') as spawn:
            for _ in range(d.CI_ATTEMPTS):
                self.scan(alive=False)
                self.worker.db.execute('UPDATE ci_redirects SET retry_after=0')
            spawn.assert_not_called()
        self.assertEqual(self.record()['state'], 'failed')
        self.assertEqual(self.runs()['dispatch-owner'], 'quarantined')

    def test_repair_worker_handoff_or_ownership_change_is_uncertain(self):
        directory = self.state/'repair'
        directory.mkdir()
        (directory/'process.json').write_text(json.dumps(dict(pid=5555, started='r')))
        (directory/'exit.json').write_text('{"returncode": 0}')
        (directory/'result.json').write_text(json.dumps({'verdict': 'fixed', 'model': 'm', 'evidence': 'x'}))
        self.stopped_record('repairing', repair_dir=str(directory))
        self.owner = {**self.claim, 'status': 'review'}
        self.scan(alive=False)
        self.assertEqual(self.record()['state'], 'uncertain')
        self.assertIn('must not hand off', self.record()['reason'])

    def test_live_repair_worker_is_heartbeated_and_keeps_capacity(self):
        directory = self.state/'repair'
        directory.mkdir()
        (directory/'process.json').write_text(json.dumps(dict(pid=4243, started='s2')))
        self.stopped_record('repairing', repair_dir=str(directory))
        self.scan(alive=True)
        self.assertEqual(self.record()['state'], 'repairing')
        self.assertIn('heartbeat', self.commands)
        self.assertFalse(self.worker.available(self.slot, [self.owner]))

    def test_crash_between_kill_intent_and_sigkill_retries_and_never_claims_delivery(self):
        class Crash(BaseException):
            pass

        def crash_on_kill(number):
            if number == signal.SIGKILL:
                raise Crash()
        self.owner_row()
        self.observe()
        self.assertEqual(self.scan(), [signal.SIGTERM])
        self.worker.db.execute('UPDATE ci_redirects SET signalled=?', (time.time() - d.CI_TERM_SECONDS - 1,))
        with self.assertRaises(Crash):  # The dispatcher dies after the durable intent, before the signal returns.
            self.scan(on_signal=crash_on_kill)
        record = self.record()
        self.assertEqual(record['state'], 'interrupting')
        self.assertFalse(record['killed'])  # Intent only: never recorded as attempted or delivered.
        self.assertIn('SIGKILL pending', record['reason'])
        self.assertNotIn('attempted', record['reason'])
        self.assertEqual(sorted(l['resource'] for l in self.guard_rows()), self.protected)
        # Even past the stop deadline, recovery first retries the guarded SIGKILL instead of quarantining.
        self.worker = self.dispatcher()
        self.worker.db.execute('UPDATE ci_redirects SET signalled=?', (time.time() - d.CI_STOP_SECONDS - 1,))
        self.assertEqual(self.scan(), [signal.SIGKILL])
        record = self.record()
        self.assertEqual(record['state'], 'interrupting')
        self.assertGreater(record['killed'], 0)
        self.assertIn('SIGKILL attempted', record['reason'])
        self.assertEqual(self.scan(), [])  # Brief post-SIGKILL wait; never repeated.
        self.assertEqual(self.record()['state'], 'interrupting')
        # Members that died after the attempt complete the stop normally.
        with patch.object(d.subprocess, 'Popen') as spawn:
            self.scan(alive=False)
            spawn.assert_not_called()
        self.assertEqual(self.record()['state'], 'stopped')
        self.assertEqual(self.guard_rows(), [])

    def test_repair_is_scoped_offline_and_has_no_wrapper_kill_timer(self):
        grant = dict(network='testnet', chain_identifier='4c78adac', wallet='/wallet/client.yaml',
                     lock_resource='network:suifleet-testnet')
        project = {**self.project, 'testnet_authorization': grant}
        record = dict(pr=7, head=self.head, base=self.base_sha, failures='[]')
        repair = self.worker.repair_prompt(project, self.claim, record, Path('/checkout'), Path('/repair'))
        self.assertNotIn(c.network_authorization(project), repair)
        self.assertNotIn('/wallet/client.yaml', repair)
        self.assertIn('NO machine-session shared-network or hardware mutation authority', repair)
        self.assertIn('hardware:raspberrypi3bplus, network:suifleet-testnet', repair)
        self.assertNotIn('hardware resources only', repair)
        # The resumed implementation keeps its standing authorization unchanged.
        resumed = self.worker.prompt(project, self.claim, Path('/resume'))
        self.assertIn(c.network_authorization(project), resumed)

    def test_overdue_repair_waits_for_owner_locks_and_guards_before_signals(self):
        directory = self.state/'repair'
        directory.mkdir()
        (directory/'process.json').write_text(json.dumps(dict(pid=4242, started='start')))
        self.stopped_record('repairing', repair_dir=str(directory), repair_deadline=time.time() + 60)
        self.assertEqual(self.scan(), [])  # Before the deadline: heartbeat only.
        self.worker.db.execute('UPDATE ci_redirects SET repair_deadline=?', (time.time() - 1,))
        # The repair session holds the wallet: a guard is unavailable, so nothing is signalled.
        self.lockdb.lock('network:suifleet-testnet', 'Codex', 'm', 'dispatch-owner', 'SF-099 mutation')
        self.assertEqual(self.scan(), [])
        record = self.record()
        self.assertEqual(record['state'], 'repairing')
        self.assertIn('waiting to stop it safely', record['reason'])
        self.assertIsNone(record['guard_session'])
        self.assertEqual(self.guard_rows(), [])
        self.lockdb.db.execute('DELETE FROM locks')
        self.worker.db.execute('UPDATE ci_redirects SET retry_after=0')
        self.assertEqual(self.scan(), [signal.SIGTERM])
        record = self.record()
        self.assertEqual(record['state'], 'repair_stopping')
        self.assertEqual(sorted(l['resource'] for l in self.guard_rows()), self.protected)
        # Escalation is the same guard-fenced path, then normal (failed) result evaluation.
        self.worker.db.execute('UPDATE ci_redirects SET signalled=?', (time.time() - d.CI_TERM_SECONDS - 1,))
        self.assertEqual(self.scan(), [signal.SIGKILL])
        self.scan(alive=False)
        self.assertEqual(self.record()['state'], 'repairing')
        self.assertEqual(self.guard_rows(), [])
        self.scan(alive=False)
        record = self.record()
        self.assertEqual(record['state'], 'repair_failed', record['reason'])
        self.assertIn('overdue repair stopped under guards', record['reason'])
        self.assertEqual(self.runs()['dispatch-owner'], 'quarantined')

    def test_fresh_pr_reads_exact_head_and_current_base(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[:3] == ['gh', 'auth', 'token']:
                return 'token\n'
            if args[:3] == ['gh', 'pr', 'view']:
                return json.dumps({**self.pr, 'baseRefOid': 'stale'})
            if args[:2] == ['gh', 'api']:
                return json.dumps({'object': {'sha': 'current-base'}})
            raise AssertionError(args)
        worker = d.Dispatcher(self.config, self.state)
        with patch.object(d, 'run', side_effect=run):
            pr = worker.fresh_pr(self.project, 7)
        self.assertEqual(pr['baseRefOid'], 'current-base')
        self.assertIn('statusCheckRollup', calls[1][-1])
        self.assertEqual(calls[2], ['gh', 'api', 'repos/owner/repo/git/ref/heads/main'])

    def test_status_reports_redirect_state(self):
        self.owner_row()
        self.observe()
        self.scan()
        with patch.object(sys, 'argv', ['dispatcher.py', '--config', str(self.write_config()), '--state', str(self.state), '--status']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            d.main()
        row = json.loads(out.getvalue())[0]
        self.assertEqual(row['ci_redirect'], 'interrupting')
        self.assertIn('PR#7', row['ci_redirect_reason'])
        self.assertNotIn('private-token', out.getvalue())

    def write_config(self):
        path = Path(self.temp.name)/'config.json'
        path.write_text(json.dumps(self.config))
        return path


class CoordinatorObservations(unittest.TestCase):
    def setUp(self):
        import test_coordinator as previous
        self.previous = previous
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def scan(self, pr, owner, execute=True):
        engine, project, _ = self.previous.CheckFreshnessTests().setup_scan(self.temp.name, pr)
        self.previous.CheckFreshnessTests().configure_scan(engine, pr, owner)
        with patch.object(engine, 'launch') as launch, patch('sys.stdout', new_callable=io.StringIO) as out:
            engine.tick(execute)
        rows = [dict(r) for r in engine.store.db.execute('SELECT * FROM ci_observations')]
        return engine, launch, json.loads(out.getvalue().splitlines()[-1]), rows

    def fixture(self, **pr_fields):
        pr = self.previous.fixture()[0]
        pr.update(statusCheckRollup=[failure()], headRefName='work/test', **pr_fields)
        owner = self.previous.fixture()[1]
        owner.update(key='T1', token='local', branch='work/test', spec={'priority': 'P1'}, status='in_progress',
                     model='m', expired=False)
        return pr, owner

    def test_draft_and_in_progress_failures_are_recorded_with_actual_responsibility(self):
        for draft in (True, False):
            with self.subTest(draft=draft):
                pr, owner = self.fixture(isDraft=draft)
                engine, launch, line, rows = self.scan(pr, owner)
                launch.assert_not_called()  # Never a coordinator worker for a live implementation claim.
                self.assertEqual(rows[0]['owner_status'], 'in_progress')
                self.assertEqual(rows[0]['draft'], int(draft))
                self.assertEqual(json.loads(rows[0]['failures'])[0]['detailsUrl'], failure()['detailsUrl'])
                self.assertIn('dispatcher redirects', line['ci_responsibility'])
                self.assertEqual(rows[0]['fingerprint'], c.repair_feedback({**pr, '_ci_repair': c.failed_checks(pr)}))
                engine.store.db.close()

    def test_green_or_superseded_snapshot_clears_and_read_only_scan_writes_nothing(self):
        pr, owner = self.fixture(isDraft=True)
        engine, _, _, rows = self.scan(pr, owner)
        self.assertEqual(len(rows), 1)
        pr['statusCheckRollup'] = [failure(conclusion='SUCCESS')]
        with patch.object(engine, 'launch'), patch('sys.stdout', new_callable=io.StringIO):
            engine.tick(True)
        self.assertEqual(engine.store.db.execute('SELECT count(*) FROM ci_observations').fetchone()[0], 0)
        pr['statusCheckRollup'] = [failure()]
        with patch.object(engine, 'launch'), patch('sys.stdout', new_callable=io.StringIO) as out:
            engine.tick(False)
        self.assertEqual(engine.store.db.execute('SELECT count(*) FROM ci_observations').fetchone()[0], 0)
        self.assertIn('dispatcher redirects', json.loads(out.getvalue().splitlines()[-1])['ci_responsibility'])

    def test_closed_prs_are_dropped_but_unreadable_open_snapshots_are_kept(self):
        pr, owner = self.fixture()
        engine, _, _, _ = self.scan(pr, owner)
        engine.store.db.execute("INSERT INTO ci_observations(project,pr) VALUES('test', 99)")
        engine.store.db.execute("INSERT INTO ci_observations(project,pr) VALUES('test', 98)")
        def prs(project):
            engine.open_numbers[project['id']] = {pr['number'], 98}
            return [pr]
        engine.prs = prs
        with patch.object(engine, 'launch'), patch('sys.stdout', new_callable=io.StringIO):
            engine.tick(True)
        self.assertEqual(sorted(r[0] for r in engine.store.db.execute('SELECT pr FROM ci_observations')), sorted([pr['number'], 98]))

    def test_responsibility_names_every_owner_case(self):
        pr, owner = self.fixture()
        failures = c.failed_checks(pr)
        cases = [
            (None, 'no verified ticket owner'),
            ({**owner, 'expired': True}, 'never automatic'),
            ({**owner, 'publish_pending': True}, 'mirror pending'),
            ({**owner, 'status': 'blocked'}, 'explicit recovery'),
            ({**owner, 'status': 'review'}, 'coordinator CI repair'),
        ]
        for value, expected in cases:
            self.assertIn(expected, c.ci_responsibility(pr, value, failures))
        self.assertIn('marked ready', c.ci_responsibility({**pr, 'isDraft': True}, {**owner, 'status': 'review'}, failures))
        self.assertIn('not an open same-repository', c.ci_responsibility({**pr, 'isCrossRepository': True}, owner, failures))
        self.assertIsNone(c.ci_responsibility(pr, owner, []))


if __name__ == '__main__':
    unittest.main()
