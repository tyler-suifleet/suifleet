"""SF-145: same-claim recovery after explicit provider quota resets, and CI handoff (offline)."""
from datetime import datetime
import hashlib
import json
import os
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
from test_ci_redirects import Fixture, ROOT, git, d, c, REAL_RUN

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

    def test_deferred_coordinator_repair_owns_the_claim(self):
        self.stopped(); self.recorded(); self.due(); self.worker.db.execute('DELETE FROM backoff')
        intent = dict(claim=hashlib.sha256(self.claim['token'].encode()).hexdigest(), key=self.claim['key'],
                      branch=self.claim['branch'], worktree=self.claim['worktree'], head=self.head)
        self.store.db.execute("INSERT INTO jobs(project,pr,family,kind,head,feedback,status,directory,repair_claim) "
                              "VALUES('suifleet',7,'Codex','fix',?,'f','quota_wait','x',?)", (self.head, json.dumps(intent)))
        self.run_retry()
        self.assertEqual(self.worker.db.execute('SELECT reason FROM capacity_retries').fetchone()[0],
                         'review repair owns the claim recovery')


if __name__ == '__main__':
    unittest.main()
