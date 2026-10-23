"""SF-118: a Review claim resumed by a verified live fix job is repair work, not
a second manual implementation owner (port of untracked PR #150).

Uses the real coordinator review schema, real repair_claim proofs and a real
live child process for the repair identity; only the ticket CLI is faked.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
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
import coordinator  # noqa: E402


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class RepairAccounting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.project = dict(id='suifleet', root=str(self.state), github_account='project-account')
        self.slot = dict(project='suifleet', family='Codex', model='actual-test-model', enabled=True, max_workers=1)
        self.config = dict(projects=[self.project, dict(id='other', root=str(self.state), github_account='a')],
                           implementation_slots=[self.slot], workers={'Codex': ['test-cli']})
        self.worker = d.Dispatcher(self.config, self.state)
        self.addCleanup(self.worker.db.close)
        coordinator.Store(self.state/'reviews.sqlite3').db.close()  # Real current review schema.
        self.owners = {}
        self.worker.ticket = Mock(side_effect=self.ticket)
        self.repaired = self.owner('SF-101', 'token-101')

    def ticket(self, project, *args):
        if args[0] == 'show':
            return dict(self.owners[(project['id'], args[1])])
        raise AssertionError(args)

    def owner(self, key, token, project='suifleet', **fields):
        # The original dispatch run already finished at its Review handoff, so
        # the resumed claim's session is no longer a managed dispatcher session.
        full = dict(key=key, token=token, agent='Codex', model='actual-test-model', status='in_progress',
                    session='finished-dispatch-'+key, branch='work/'+key.lower(), worktree=str(self.state/key),
                    expired=False, publish_pending=False, revision=7, published_revision=7, **fields)
        self.owners[(project, key)] = full
        return {k: v for k, v in full.items() if k not in ('token', 'revision', 'published_revision')}

    def live_process(self):
        child = subprocess.Popen(['sleep', '120'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        started = d.process_identity(child.pid)
        self.assertIsNotNone(started)
        return child, started

    def fix_job(self, owner, token=None, status='running', project='suifleet', family='Codex', kind='fix',
                proof=None, receipt=True, pr=None):
        child, started = self.live_process()
        folder = self.state/f"job-{len(list(self.state.glob('job-*')))}"
        folder.mkdir()
        if receipt:
            (folder/'process.json').write_text(json.dumps(dict(pid=child.pid, started=started)))
        claim = proof if proof is not None else dict(
            claim=digest(token or self.owners[(project, owner['key'])]['token']), key=owner['key'],
            branch=owner['branch'], worktree=owner['worktree'], head='a'*40)
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            cursor = db.execute('INSERT INTO jobs(project,pr,family,kind,head,feedback,status,directory,pid,started,created,repair_claim) '
                                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                (project, pr or 100+len(list(self.state.glob('job-*'))), family, kind, 'a'*40, 'f'*64,
                                 status, str(folder), child.pid, started, time.time(),
                                 json.dumps(claim) if claim is not False else None))
        return cursor.lastrowid, child

    def jobs(self, sql, *args):
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            db.execute(sql, args)

    def test_verified_live_repair_frees_only_its_duplicate_implementation_count(self):
        self.fix_job(self.repaired)
        self.assertTrue(self.worker.available(self.slot, [self.repaired]), 'repair is counted by fix capacity')
        # Its verification used the authoritative current generation, not the list snapshot.
        self.worker.ticket.assert_called_with(self.project, 'show', 'SF-101')
        other = self.owner('SF-102', 'token-102')
        self.assertFalse(self.worker.available(self.slot, [self.repaired, other]), 'genuine manual owner still counts')
        self.assertFalse(self.worker.available(self.slot, [{**self.repaired, 'status': 'blocked'}]),
                         'blocked work keeps its reservation')
        # Repair ownership never adds capacity beyond the configured limit.
        self.assertFalse(self.worker.available({**self.slot, 'max_workers': 1}, [self.repaired, other]))
        self.assertTrue(self.worker.available({**self.slot, 'max_workers': 2}, [self.repaired, other]))

    def test_ci_repair_fix_job_with_the_same_fenced_proof_is_repair_category(self):
        token = self.owners[('suifleet', 'SF-101')]['token']
        proof = dict(claim=digest(token), key='SF-101', branch=self.repaired['branch'],
                     worktree=self.repaired['worktree'], head='a'*40, base='b'*40, ci_repair=[{'name': 'check'}])
        self.fix_job(self.repaired, proof=proof)
        self.assertTrue(self.worker.available(self.slot, [self.repaired]))

    def test_unverified_or_stale_proof_keeps_the_reservation(self):
        token = self.owners[('suifleet', 'SF-101')]['token']
        good = dict(claim=digest(token), key='SF-101', branch=self.repaired['branch'],
                    worktree=self.repaired['worktree'], head='a'*40)
        cases = {
            'no fenced proof (historical snapshot only)': dict(proof=False),
            'other token generation': dict(proof={**good, 'claim': digest('old-token')}),
            'other branch': dict(proof={**good, 'branch': 'work/other'}),
            'other worktree': dict(proof={**good, 'worktree': str(self.state/'elsewhere')}),
            'other ticket key': dict(proof={**good, 'key': 'SF-999'}),
            'reserved, not launched': dict(status='reserved'),
            'orphaned launch': dict(status='orphaned'),
            'finished job': dict(status='done'),
            'review job': dict(kind='review'),
            'other family': dict(family='Claude'),
            'other project namespace': dict(project='other', token='token-101'),
            'missing process receipt': dict(receipt=False),
        }
        for name, options in cases.items():
            with self.subTest(name):
                self.jobs('DELETE FROM jobs')
                self.fix_job(self.repaired, **options)
                self.assertFalse(self.worker.available(self.slot, [self.repaired]))

    def test_process_identity_must_be_the_live_receipt(self):
        job, child = self.fix_job(self.repaired)
        self.assertTrue(self.worker.available(self.slot, [self.repaired]))
        folder = Path(self.state/'job-0')
        receipt = json.loads((folder/'process.json').read_text())
        (folder/'process.json').write_text(json.dumps({**receipt, 'started': '1'}))
        self.assertFalse(self.worker.available(self.slot, [self.repaired]), 'mismatched receipt')
        (folder/'process.json').write_text('{broken')
        self.assertFalse(self.worker.available(self.slot, [self.repaired]), 'malformed receipt')
        (folder/'process.json').write_text(json.dumps(receipt))
        self.jobs('UPDATE jobs SET started=? WHERE id=?', '1', job)
        (folder/'process.json').write_text(json.dumps({**receipt, 'started': '1'}))
        self.assertFalse(self.worker.available(self.slot, [self.repaired]), 'reused PID / wrong start identity')
        self.jobs('UPDATE jobs SET started=? WHERE id=?', receipt['started'], job)
        (folder/'process.json').write_text(json.dumps(receipt))
        child.kill()
        child.wait()
        self.assertFalse(self.worker.available(self.slot, [self.repaired]), 'exited repair worker')

    def test_unreaped_zombie_repair_is_not_live(self):
        job, child = self.fix_job(self.repaired)
        self.assertTrue(self.worker.repair_owned(self.slot, self.repaired))
        child.terminate()
        stat = Path(f'/proc/{child.pid}/stat')
        deadline = time.monotonic() + 10
        # Deliberately do not wait()/poll(): the exited child stays an unreaped zombie.
        while stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z':
            self.assertLess(time.monotonic(), deadline, 'child did not become a zombie')
            time.sleep(0.01)
        self.assertEqual(d.process_identity(child.pid), json.loads((self.state/'job-0'/'process.json').read_text())['started'],
                         'zombie keeps its PID/start receipt')
        self.assertFalse(self.worker.repair_owned(self.slot, self.repaired), 'zombie repair worker')
        self.assertFalse(self.worker.available(self.slot, [self.repaired]), 'zombie keeps the reservation')
        child.wait()  # Deterministic reaping; the cleanup kill/wait is then a no-op.
        self.assertFalse(self.worker.available(self.slot, [self.repaired]), 'reaped repair worker')

    def test_current_owner_generation_must_match(self):
        self.fix_job(self.repaired)
        current = self.owners[('suifleet', 'SF-101')]
        changes = [('token', 'replacement-token'), ('token', None), ('session', 'someone-else'),
                   ('branch', 'work/moved'), ('worktree', str(self.state/'moved')), ('agent', 'Claude'),
                   ('status', 'review'), ('expired', True), ('publish_pending', True), ('published_revision', 6)]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                self.owners[('suifleet', 'SF-101')] = {**current, field: value}
                self.assertFalse(self.worker.available(self.slot, [self.repaired]))
        self.owners[('suifleet', 'SF-101')] = current
        self.worker.ticket.side_effect = subprocess.CalledProcessError(1, ['ticket'])
        self.assertFalse(self.worker.available(self.slot, [self.repaired]), 'unavailable authority fails closed')

    def test_list_snapshot_must_match_the_live_proof(self):
        self.fix_job(self.repaired)
        for field, value in [('branch', 'work/x'), ('worktree', '/elsewhere'), ('session', 'other'),
                             ('expired', True), ('publish_pending', True)]:
            with self.subTest(field):
                self.assertFalse(self.worker.available(self.slot, [{**self.repaired, field: value}]))

    def test_ambiguous_repairs_for_one_ticket_fail_closed(self):
        self.fix_job(self.repaired, pr=1)
        self.fix_job(self.repaired, pr=2)
        self.assertFalse(self.worker.available(self.slot, [self.repaired]), 'two live repairs')
        self.jobs('DELETE FROM jobs WHERE pr=2')
        # A stale earlier-generation repair still running beside the current one.
        self.fix_job(self.repaired, token='earlier-token', pr=3)
        self.assertFalse(self.worker.available(self.slot, [self.repaired]))
        self.jobs('DELETE FROM jobs WHERE pr=3')
        self.assertTrue(self.worker.available(self.slot, [self.repaired]))
        # An unparseable proof cannot be attributed to any ticket: fail closed.
        self.jobs("INSERT INTO jobs(project,pr,family,kind,head,feedback,status,directory,repair_claim) "
                  "VALUES('suifleet',4,'Codex','fix','h','f','running','/nonexistent','{broken')")
        self.assertFalse(self.worker.available(self.slot, [self.repaired]))

    def test_same_key_in_another_project_is_a_separate_namespace(self):
        self.owner('SF-101', 'token-101', project='other')
        self.fix_job(self.repaired, project='other')
        self.assertFalse(self.worker.available(self.slot, [self.repaired]))
        self.fix_job(self.repaired)
        self.assertTrue(self.worker.available(self.slot, [self.repaired]))

    def test_historical_review_schema_without_repair_claim_fails_closed(self):
        with sqlite3.connect(self.state/'reviews.sqlite3') as db:
            db.execute('DROP TABLE jobs')
            db.execute('CREATE TABLE jobs(project TEXT,family TEXT,kind TEXT,status TEXT,directory TEXT)')
            db.execute("INSERT INTO jobs VALUES('suifleet','Codex','fix','running','/x')")
        self.assertFalse(self.worker.available(self.slot, [self.repaired]))
        (self.state/'reviews.sqlite3').unlink()
        self.assertFalse(self.worker.available(self.slot, []), 'missing review database still fails closed')

    def test_sf116_ci_redirect_of_the_claim_keeps_its_slot_accounting(self):
        self.fix_job(self.repaired)
        claim = digest(self.owners[('suifleet', 'SF-101')]['token'])
        for state in ('pending', 'guarding', 'interrupting', 'repairing', 'uncertain', 'orphaned', 'conflict'):
            with self.subTest(state):
                self.worker.db.execute('DELETE FROM ci_redirects')
                self.worker.db.execute('INSERT INTO ci_redirects(source,project,family,claim,ticket,fingerprint,state) '
                                       'VALUES(?,?,?,?,?,?,?)', ('s', 'suifleet', 'Codex', claim, 'SF-101', 'f', state))
                self.assertFalse(self.worker.available(self.slot, [self.repaired]))
        self.worker.db.execute("UPDATE ci_redirects SET state='resumed'")
        self.assertTrue(self.worker.available(self.slot, [self.repaired]))

    def test_sf116_managed_paused_owner_and_redirect_still_hold_one_slot(self):
        # The SF-116 owner is a managed run: its slot is held through runs, not
        # the manual count, and an unrelated verified repair cannot free it.
        claim = {k: self.owners[('suifleet', 'SF-101')][k] for k in ('key', 'token', 'session', 'branch', 'worktree')}
        self.worker.db.execute("INSERT INTO runs(session,project,family,status,directory,claim) VALUES(?,?,?,?,?,?)",
                               (claim['session'], 'suifleet', 'Codex', 'running', str(self.state), json.dumps(claim)))
        other = self.owner('SF-102', 'token-102')
        self.fix_job(other)
        self.assertFalse(self.worker.available(self.slot, [self.repaired, other]))
        self.assertTrue(self.worker.available({**self.slot, 'max_workers': 2}, [self.repaired, other]))

    def test_sf117_capacity_recovery_coexists_without_weakening_retry_safety(self):
        self.fix_job(self.repaired)
        claim = digest(self.owners[('suifleet', 'SF-101')]['token'])
        for state in ('waiting', 'launch_intent', 'uncertain', 'unknown-future-state'):
            with self.subTest(state):
                self.worker.db.execute('DELETE FROM capacity_retries')
                self.worker.db.execute('INSERT INTO capacity_retries(source,claim,project,family,state) VALUES(?,?,?,?,?)',
                                       ('src', claim, 'suifleet', 'Codex', state))
                self.assertFalse(self.worker.available(self.slot, [self.repaired], retention=False, recovering=True))
        for state in ('cancelled', 'exhausted', 'launched'):
            with self.subTest(state):
                self.worker.db.execute('UPDATE capacity_retries SET state=?', (state,))
                self.assertTrue(self.worker.available(self.slot, [self.repaired], retention=False, recovering=True))
        # A recovering capacity retry of another claim may use the slot a verified
        # repair no longer duplicates, bypassing idle backoff but never the stagger.
        self.worker.db.execute("INSERT INTO backoff VALUES('suifleet','Codex',1,?,'idle: no eligible ticket')",
                               (time.time()+60,))
        self.assertTrue(self.worker.available(self.slot, [self.repaired], retention=False, recovering=True))
        self.assertFalse(self.worker.available(self.slot, [self.repaired]), 'ordinary claims still back off')
        self.worker.db.execute("UPDATE backoff SET reason='provider capacity recovery stagger'")
        self.assertFalse(self.worker.available(self.slot, [self.repaired], retention=False, recovering=True))

    def test_tick_launches_a_successor_only_when_the_repair_is_verified(self):
        launched = []
        self.worker.ticket = Mock(side_effect=lambda project, *args: (
            [self.repaired] if args[0] == 'list' else {} if args[0] == 'refresh' else self.ticket(project, *args)))
        with patch.object(self.worker, 'launch', side_effect=lambda project, slot: launched.append(slot['family'])), \
                patch.object(self.worker, 'reconcile'), patch.object(self.worker, 'redirect_ci_failures'), \
                patch.object(self.worker, 'audit_missing_handoff'), patch.object(self.worker, 'wake_on_change'):
            self.worker.tick(True)
            self.assertEqual(launched, [], 'unverified resumed claim still holds the slot')
            self.fix_job(self.repaired)
            self.worker.tick(True)
        self.assertEqual(launched, ['Codex'])


if __name__ == '__main__':
    unittest.main()
