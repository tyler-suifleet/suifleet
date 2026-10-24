import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dashboard', ROOT/'dashboard.py')
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)

NOW = 2_000_000.0
TOKEN = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'   # 32-hex claim token shape
LOCK_TOKEN = 'f0e1d2c3b4a5968778695a4b3c2d1e0f'
SECRETS = [TOKEN, LOCK_TOKEN, 'PROMPT-SECRET-TEXT', 'WORKER-LOG-SECRET', 'PR-BODY-SECRET',
           'COMMENT-SECRET', 'ghp_abcdefghijklmnopqrstuvwxyz0123']


def stat_line(pid, state='S', session=None, start=500):
    return f"{pid} (worker) {state} 1 {pid} {session or pid} " + ' '.join(['0']*15) + f" {start} 0 0\n"


class Fixture:
    def __init__(self, base):
        self.base = Path(base)
        self.proc = self.base/'proc'
        self.proc.mkdir()
        (self.proc/'stat').write_text('cpu 0\nbtime 1000000\n')
        self.paths = dict(tickets=self.base/'coordination.sqlite3', dispatch=self.base/'dispatch.sqlite3',
                          reviews=self.base/'reviews.sqlite3', config=self.base/'config.json', inventory=self.base/'inventory')
        self.paths['config'].write_text(json.dumps({
            'workers': {'Codex': ['codex'], 'Claude': ['claude']},
            'projects': [{'id': 'suifleet'}, {'id': 'vortech'}],
            'implementation_slots': [
                {'project': 'suifleet', 'family': 'Claude', 'model': 'claude-test', 'enabled': True, 'max_workers': 2},
                {'project': 'suifleet', 'family': 'Codex', 'model': 'codex-test', 'enabled': True, 'max_workers': 1}],
            'review_worker_limits': {'Codex': {'review': 2, 'fix': 1}}}))
        db = sqlite3.connect(self.paths['tickets'])
        db.executescript('''
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE tickets (key TEXT PRIMARY KEY, project TEXT NOT NULL DEFAULT '', spec TEXT NOT NULL, remote TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'backlog', token TEXT, agent TEXT, model TEXT, session TEXT, heartbeat REAL,
                expires REAL, worktree TEXT, branch TEXT, evidence TEXT, revision INTEGER NOT NULL DEFAULT 0,
                published_revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE events (id INTEGER PRIMARY KEY, at REAL NOT NULL, ticket TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE locks (resource TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, agent TEXT NOT NULL, model TEXT NOT NULL,
                session TEXT NOT NULL, project TEXT NOT NULL, ticket TEXT, purpose TEXT NOT NULL, acquired REAL NOT NULL,
                heartbeat REAL NOT NULL, expires REAL NOT NULL);''')
        db.execute("INSERT INTO metadata VALUES('snapshot_at:suifleet',?)", (str(NOW-120),))
        db.execute('INSERT INTO events VALUES(1,?,?,?,?,?)', (NOW, 'suifleet::SF-001', 's', 'claim', f'token={TOKEN}'))
        db.execute('INSERT INTO locks VALUES(?,?,?,?,?,?,?,?,?,?,?)', ('network:suifleet-testnet', LOCK_TOKEN, 'Claude', 'claude-test',
                   'sess-impl', 'suifleet', 'suifleet::SF-001', 'testnet upgrade run', NOW-60, NOW-30, NOW+600))
        db.commit(); db.close()
        db = sqlite3.connect(self.paths['dispatch'])
        db.execute('CREATE TABLE runs (session TEXT PRIMARY KEY, project TEXT, family TEXT, status TEXT, directory TEXT, claim TEXT, pid INTEGER, started TEXT, message TEXT)')
        db.execute('CREATE TABLE backoff (project TEXT, family TEXT, attempts INTEGER, retry_after REAL, reason TEXT, PRIMARY KEY(project,family))')
        db.commit(); db.close()
        db = sqlite3.connect(self.paths['reviews'])
        db.execute('''CREATE TABLE jobs (id INTEGER PRIMARY KEY, project TEXT, pr INTEGER, family TEXT, kind TEXT, head TEXT, feedback TEXT,
            status TEXT, directory TEXT, pid INTEGER, started TEXT, result TEXT, error TEXT, created REAL, retry_after REAL,
            attempts INTEGER DEFAULT 0, comment_id TEXT, review_id TEXT, publication_done INTEGER DEFAULT 0,
            publication_attempts INTEGER DEFAULT 0, launch_intent TEXT, UNIQUE(project,pr,kind,head,feedback))''')
        db.execute('CREATE TABLE integrations (project TEXT, pr INTEGER, head TEXT, commit_id TEXT, reconciled INTEGER DEFAULT 0, PRIMARY KEY(project,pr))')
        db.execute('CREATE TABLE quarantine_backoff (family TEXT, kind TEXT, attempts INTEGER, retry_after REAL, PRIMARY KEY(family,kind))')
        db.commit(); db.close()
        self.job_id = 0

    def process(self, pid, state='S', session=None, start=500):
        folder = self.proc/str(pid)
        folder.mkdir(exist_ok=True)
        (folder/'stat').write_text(stat_line(pid, state, session, start))

    def ticket(self, key, status='in_progress', agent='Claude', session='sess-impl', project='suifleet', evidence=None, branch=None):
        spec = {'key': key, 'title': f'Title of {key}', 'description': 'desc'}
        with sqlite3.connect(self.paths['tickets']) as db:
            db.execute('INSERT INTO tickets(key,project,spec,remote,status,token,agent,model,session,heartbeat,expires,branch,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (f'{project}::{key}', project, json.dumps(spec), json.dumps({'url': 'https://example.invalid/1'}), status,
                        TOKEN if status in ('in_progress', 'blocked', 'review') else None, agent, 'model-'+agent.lower(),
                        session, NOW-50, NOW+800, branch or f'work/{key.lower()}', evidence))

    def run(self, session, key, status='running', pid=4242, start=500, family='Claude', process_json=True):
        folder = self.base/session
        folder.mkdir()
        (folder/'prompt.txt').write_text('PROMPT-SECRET-TEXT')
        (folder/'worker.log').write_text('WORKER-LOG-SECRET')
        if process_json:
            (folder/'process.json').write_text(json.dumps({'pid': pid, 'started': str(start)}))
        claim = {'key': key, 'token': TOKEN, 'session': session, 'model': 'claude-test', 'branch': f'work/{key.lower()}',
                 'spec': {'title': f'Title of {key}'}, 'worktree': '/private/worktree'}
        with sqlite3.connect(self.paths['dispatch']) as db:
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)', (session, 'suifleet', family, status, str(folder),
                       json.dumps(claim), pid, str(start), 'Verified stopped; claim retained' if status != 'running' else None))
        return folder

    def job(self, pr, kind='review', status='running', pid=None, start=500, family='Codex', ticket='SF-001',
            checks=('SUCCESS',), attempts=0, error=None, retry_after=None, snapshot=True, project='suifleet'):
        self.job_id += 1
        folder = self.base/f'job-{self.job_id}'
        folder.mkdir()
        (folder/'prompt.txt').write_text('PROMPT-SECRET-TEXT')
        (folder/'worker.log').write_text('WORKER-LOG-SECRET')
        if snapshot:
            (folder/'snapshot.json').write_text(json.dumps({
                'number': pr, 'title': f'{ticket}: PR title', 'body': 'PR-BODY-SECRET', 'headRefName': f'work/{ticket.lower()}',
                'headRefOid': hashlib.sha1(str(pr).encode()).hexdigest(), 'isDraft': False,
                'statusCheckRollup': [{'name': f'c{i}', 'conclusion': c} for i, c in enumerate(checks)],
                'comments': [{'body': 'COMMENT-SECRET'}], '_testnet_required': False,
                'ticket': {'key': ticket, 'agent': 'Claude', 'model': 'm', 'evidence': f'token={TOKEN}', 'spec': {'title': f'Title of {ticket}'}}}))
        with sqlite3.connect(self.paths['reviews']) as db:
            db.execute('INSERT INTO jobs(id,project,pr,family,kind,head,feedback,status,directory,pid,started,error,created,retry_after,attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (self.job_id, project, pr, family, kind, hashlib.sha1(str(pr).encode()).hexdigest(), str(self.job_id), status,
                        str(folder), pid, str(start) if pid else None, error, NOW-300, retry_after, attempts))
        return folder

    def redirect(self, source, state, pr=7, reason='x', repair=None, resumed_as=None, fingerprint='fp', ticket='SF-001'):
        """A dispatcher SF-116 CI repair-first record (schema of dispatcher.py)."""
        with sqlite3.connect(self.paths['dispatch']) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ci_redirects (
                id INTEGER PRIMARY KEY, source TEXT, project TEXT, family TEXT, claim TEXT, ticket TEXT,
                pr INTEGER, branch TEXT, head TEXT, base TEXT, fingerprint TEXT, failures TEXT,
                state TEXT, reason TEXT, attempts INTEGER DEFAULT 0, retry_after REAL DEFAULT 0,
                members TEXT, signalled REAL, killed REAL DEFAULT 0, guard_session TEXT, guards TEXT, original_head TEXT,
                snapshot TEXT, repair_dir TEXT, repair_deadline REAL, repaired_head TEXT, resumed_as TEXT,
                created REAL, updated REAL, UNIQUE(claim, fingerprint))''')
            db.execute('''INSERT INTO ci_redirects(source,project,family,claim,ticket,pr,branch,head,fingerprint,failures,state,reason,
                          guard_session,guards,repair_dir,resumed_as,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (source, 'suifleet', 'Claude', hashlib.sha256(TOKEN.encode()).hexdigest(), ticket, pr, f'work/{ticket.lower()}',
                        'c'*40, fingerprint, json.dumps([{'name': 'check-interfaces', 'detailsUrl': 'https://x/runs/1'}]), state, reason,
                        'ci-redirect-guard-1', json.dumps([{'resource': 'network:suifleet-testnet', 'token': LOCK_TOKEN}]),
                        str(repair) if repair else None, resumed_as, NOW-200, NOW-100))

    def observation(self, pr=7, ticket='SF-001', owner_status='in_progress', fingerprint='fp', responsibility='Claude implementation owner: dispatcher redirects'):
        with sqlite3.connect(self.paths['reviews']) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ci_observations (
                project TEXT, pr INTEGER, ticket TEXT, branch TEXT, owner_status TEXT, head TEXT, base TEXT, draft INTEGER,
                failures TEXT, fingerprint TEXT, responsibility TEXT, observed REAL, PRIMARY KEY(project,pr))''')
            db.execute('INSERT OR REPLACE INTO ci_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('suifleet', pr, ticket, f'work/{ticket.lower()}', owner_status, 'c'*40, 'b'*40, 1,
                        json.dumps([{'name': 'check-interfaces'}]), fingerprint, responsibility, NOW-90))

    def collect(self, **kw):
        return dash.collect(self.paths, now=NOW, proc=self.proc, **kw)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)

    def states(self, model):
        return {(a.get('ticket'), a['role'], a['state']) for a in model['attention']}

    def test_capacity_backoff_exhaustion_and_resumed_capacity_are_visible(self):
        f=self.f
        f.ticket('SF-001');f.run('sess-impl','SF-001',status='capacity_wait',pid=101)
        with sqlite3.connect(f.paths['dispatch']) as db:
            db.execute('CREATE TABLE capacity_retries (source TEXT,claim TEXT,state TEXT,attempt INTEGER,retry_after REAL,reason TEXT,replacement TEXT,updated REAL)')
            db.execute('INSERT INTO capacity_retries VALUES(?,?,?,?,?,?,?,?)',
                       ('sess-impl',TOKEN,'waiting',2,NOW+120,'explicit capacity failure',None,NOW-5))
        model=f.collect()
        alert=next(a for a in model['attention'] if a['state']=='capacity-backoff')
        self.assertEqual(alert['retry_after'],NOW+120)
        self.assertIn('2/3',alert['summary'])
        self.assertEqual(model['workers'],[])
        self.assertNotIn(TOKEN,json.dumps(model))
        with sqlite3.connect(f.paths['dispatch']) as db:
            db.execute("UPDATE capacity_retries SET state='exhausted',reason='budget exhausted'")
            db.execute("UPDATE runs SET status='quarantined',message='budget exhausted'")
        self.assertIn(('SF-001','implementation','capacity-exhausted'),self.states(f.collect()))
        with sqlite3.connect(f.paths['dispatch']) as db:
            db.execute("UPDATE runs SET status='finished'")
            db.execute("UPDATE capacity_retries SET state='launched',replacement='replacement'")
        f.run('replacement','SF-001',pid=202);f.process(202)
        model=f.collect()
        self.assertEqual(len(model['workers']),1)
        self.assertFalse(any(a['state']=='capacity-backoff' for a in model['attention']))

    def test_quota_waits_are_visible_for_implementation_and_repair(self):
        source = importlib.util.spec_from_file_location('coordinator_for_dashboard', ROOT/'coordinator.py')
        coordinator = importlib.util.module_from_spec(source)
        source.loader.exec_module(coordinator)
        self.assertEqual(dash.QUOTA_RETRIES, coordinator.QUOTA_RETRIES)
        f=self.f
        f.ticket('SF-001');f.run('sess-impl','SF-001',status='capacity_wait',pid=101)
        with sqlite3.connect(f.paths['dispatch']) as db:
            db.execute('CREATE TABLE capacity_retries (source TEXT,claim TEXT,state TEXT,attempt INTEGER,retry_after REAL,reason TEXT,replacement TEXT,updated REAL)')
            db.execute('CREATE TABLE capacity_kinds (source TEXT PRIMARY KEY, kind TEXT NOT NULL)')
            db.execute('INSERT INTO capacity_retries VALUES(?,?,?,?,?,?,?,?)',
                       ('sess-impl',TOKEN,'waiting',1,NOW+3600,'provider quota limit; after reset',None,NOW-5))
            db.execute("INSERT INTO capacity_kinds VALUES('sess-impl','quota')")
        f.ticket('SF-002')
        f.job(8, kind='fix', ticket='SF-002', status='quota_wait', pid=102, retry_after=NOW+600,
              error='provider quota limit; same-claim repair retry 1/4 after reset')
        model=f.collect()
        alerts={a['ticket']:a for a in model['attention'] if a['state']=='quota-wait'}
        self.assertEqual(set(alerts),{'SF-001','SF-002'})
        self.assertIn('provider quota retry 1/4',alerts['SF-001']['summary'])
        self.assertEqual(alerts['SF-002']['retry_after'],NOW+600)
        self.assertEqual(alerts['SF-002']['role'],'repair')
        self.assertEqual(model['workers'],[])
        text='\n'.join(dash.render(model,140))
        self.assertIn('[quota-wait]',text)
        self.assertIn('retry in',text)
        for secret in SECRETS:
            self.assertNotIn(secret,json.dumps(model))

    def test_only_pid_and_start_time_verified_processes_are_active(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', pid=101, start=500); f.process(101, start=500)
        f.ticket('SF-002', session='sess-reused'); f.run('sess-reused', 'SF-002', pid=102, start=500); f.process(102, session=1, start=999)  # unrelated process reused the PID
        f.ticket('SF-003', session='sess-zombie'); f.run('sess-zombie', 'SF-003', pid=103); f.process(103, state='Z')
        f.ticket('SF-004', session='sess-linger'); f.run('sess-linger', 'SF-004', pid=104); f.process(9104, session=104)
        f.ticket('SF-005', session='sess-gone'); f.run('sess-gone', 'SF-005', pid=105)
        f.ticket('SF-006', session='sess-noid'); f.run('sess-noid', 'SF-006', pid=None, start='', process_json=False)
        model = f.collect()
        self.assertEqual([w['ticket'] for w in model['workers']], ['SF-001'])
        self.assertEqual(model['workers'][0]['since'], 1000000 + 500/os.sysconf('SC_CLK_TCK'))
        states = self.states(model)
        for expected in [('SF-002', 'implementation', 'stale'), ('SF-003', 'implementation', 'stale'),
                         ('SF-004', 'implementation', 'lingering'), ('SF-005', 'implementation', 'stale'),
                         ('SF-006', 'implementation', 'unverified')]:
            self.assertIn(expected, states)
        claude = next(c for c in model['capacity'] if c['family'] == 'Claude' and c['category'] == 'implementation')
        self.assertEqual((claude['active'], claude['held'], claude['limit']), (1, 5, 2))

    def test_retained_and_manual_reservations_are_never_active(self):
        f = self.f
        for i, status in enumerate(['orphaned', 'quarantined', 'parked']):
            f.ticket(f'SF-01{i}', status='blocked' if status == 'parked' else 'in_progress', session=f's{i}')
            f.run(f's{i}', f'SF-01{i}', status=status, pid=200+i); f.process(200+i)  # even if a PID matches
        f.ticket('SF-020', session='manual-session', agent='Codex')
        model = f.collect()
        self.assertEqual(model['workers'], [])
        states = self.states(model)
        self.assertTrue({('SF-010', 'implementation', 'orphaned'), ('SF-011', 'implementation', 'quarantined'),
                         ('SF-012', 'implementation', 'parked'), ('SF-012', 'implementation', 'blocked'),
                         ('SF-020', 'implementation', 'manual')} <= states)
        codex = next(c for c in model['capacity'] if c['family'] == 'Codex' and c['category'] == 'implementation')
        self.assertEqual((codex['active'], codex['held']), (0, 1))

    def test_unreadable_process_table_verifies_nothing(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', pid=101); f.process(101)
        model = dash.collect(f.paths, now=NOW, proc=f.base/'no-proc')
        self.assertEqual(model['workers'], [])
        self.assertTrue(any('Process table unreadable' in w for w in model['warnings']))
        self.assertIn(('SF-001', 'implementation', 'unverified'), self.states(model))

    def test_missing_and_corrupt_stores_are_unavailable_not_empty(self):
        f = self.f
        f.paths['tickets'].unlink()
        f.paths['dispatch'].write_bytes(b'not a sqlite database at all' * 50)
        f.paths['config'].write_text('{broken')
        model = f.collect()
        self.assertEqual(model['sources']['tickets'], {'state': 'unavailable', 'detail': 'missing'})
        self.assertEqual(model['sources']['dispatch']['state'], 'unavailable')
        self.assertEqual(model['sources']['config']['state'], 'unavailable')
        self.assertEqual(model['sources']['reviews']['state'], 'ok')
        self.assertFalse(f.paths['tickets'].exists(), 'a missing store must never be created')
        text = '\n'.join(dash.render(model, 120))
        self.assertIn('tickets UNAVAILABLE: missing', text)
        self.assertIn('Unknown: ticket store unavailable (not an empty queue)', text)
        self.assertNotIn('Empty:', text)

    def test_empty_queue_is_reported_as_empty(self):
        text = '\n'.join(dash.render(self.f.collect(), 120))
        self.assertIn('Empty: no tickets for this project filter.', text)
        self.assertIn('None verified.', text)
        self.assertIn('Nothing waiting.', text)

    def test_corrupt_receipts_are_isolated(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', pid=101); f.process(101)
        with sqlite3.connect(f.paths['dispatch']) as db:
            db.execute("INSERT INTO runs VALUES('bad','suifleet','Claude','running','/nonexistent','{not json',NULL,NULL,NULL)")
        folder = f.job(7, pid=301); f.process(301)
        (folder/'snapshot.json').write_text('{truncated')
        model = f.collect()
        self.assertEqual(len(model['workers']), 2)
        self.assertTrue(any('unreadable claim' in w for w in model['warnings']))

    def test_source_io_failures_are_unavailable_not_fatal(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', pid=101); f.process(101)
        real = Path.read_text
        config = f.paths['config']

        def denied(path, *args, **kwargs):
            if path == config:
                raise PermissionError(13, 'Permission denied')
            return real(path, *args, **kwargs)
        with patch.object(Path, 'read_text', denied):
            model = f.collect()
        self.assertEqual(model['sources']['config'], {'state': 'unavailable', 'detail': 'unreadable (PermissionError)'})
        self.assertEqual(model['sources']['tickets']['state'], 'ok')
        self.assertEqual(len(model['workers']), 1, 'healthy stores still refresh')
        # A store that disappears between the existence check and the read.
        with patch.object(dash.sqlite3, 'connect', side_effect=FileNotFoundError(2, 'gone')):
            model = f.collect()
        self.assertEqual({model['sources'][n]['state'] for n in ('tickets', 'dispatch', 'reviews')}, {'unavailable'})
        self.assertEqual(model['sources']['config']['state'], 'ok')
        config.write_text(json.dumps({'implementation_slots': {'not': 'a list'}}))
        self.assertEqual(f.collect()['sources']['config']['state'], 'unavailable')
        config.write_text(json.dumps({'review_worker_limits': {'Codex': 3}}))
        self.assertEqual(f.collect()['sources']['config']['state'], 'unavailable')

    def test_cli_reports_unreadable_source_without_traceback(self):
        f = self.f
        f.paths['config'].write_text(json.dumps({'implementation_slots': [None]}))
        out = self.cli('--json')
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertEqual(json.loads(out.stdout)['sources']['config']['state'], 'unavailable')

    def test_malformed_projects_are_unavailable_without_traceback(self):
        for projects in (5, None, {}, 'suifleet', [None], [5]):
            with self.subTest(projects=projects):
                self.f.paths['config'].write_text(json.dumps({'projects': projects}))
                out = self.cli('--json')
                self.assertEqual(out.returncode, 3, out.stderr)
                self.assertNotIn('Traceback', out.stderr)
                self.assertEqual(json.loads(out.stdout)['sources']['config']['state'], 'unavailable')

    def test_malformed_nested_records_are_isolated(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', pid=101); f.process(101)
        f.ticket('SF-002', session='s2', status='review')
        with sqlite3.connect(f.paths['tickets']) as db:
            db.execute("UPDATE tickets SET spec='[]' WHERE key='suifleet::SF-002'")
        bad_checks = f.job(7, pid=301, ticket='SF-003'); f.process(301)
        data = json.loads((bad_checks/'snapshot.json').read_text())
        data['statusCheckRollup'] = [None]
        (bad_checks/'snapshot.json').write_text(json.dumps(data))
        bad_branch = f.job(8, pid=302, ticket='SF-004'); f.process(302)
        data = json.loads((bad_branch/'snapshot.json').read_text())
        data['headRefName'] = ['not', 'hashable']
        (bad_branch/'snapshot.json').write_text(json.dumps(data))
        with sqlite3.connect(f.paths['dispatch']) as db:
            db.execute("INSERT INTO runs VALUES('bad','suifleet','Claude','running','/nonexistent',?,NULL,NULL,NULL)",
                       (json.dumps({'key': 'SF-009', 'session': ['x'], 'branch': {}}),))
        model = f.collect()
        self.assertEqual({s['state'] for s in model['sources'].values()}, {'ok'})
        self.assertEqual(len(model['workers']), 3, 'healthy implementation and review workers remain visible')
        self.assertTrue(any('SF-002 has an unreadable specification' in w for w in model['warnings']))
        self.assertTrue(any('unreadable claim' in w for w in model['warnings']))
        review = [a for a in model['attention'] if a.get('ticket') == 'SF-002']
        self.assertEqual(len(review), 1)
        self.assertIsNone(dash.ci_state(dash.SnapshotCache().get(str(bad_checks))))
        self.assertIn('PR snapshot', '\n'.join(dash.render(model, 80)))

    def test_malformed_process_identities_are_unverified_not_fatal(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', pid=101); f.process(101)
        f.ticket('SF-002', session='s-dict')
        bad = f.run('s-dict', 'SF-002', pid=102); f.process(102)  # DB identity would verify; the receipt must not
        (bad/'process.json').write_text(json.dumps({'pid': {'bad': 102}, 'started': '500'}))
        f.ticket('SF-003', session='s-list')
        bad = f.run('s-list', 'SF-003', pid=103); f.process(103)
        (bad/'process.json').write_text(json.dumps({'pid': 103, 'started': ['500']}))
        f.ticket('SF-004', session='s-bool')
        bad = f.run('s-bool', 'SF-004', pid=104); f.process(104)
        (bad/'process.json').write_text(json.dumps({'pid': True, 'started': '500'}))
        f.ticket('SF-005', status='review')
        f.job(9, pid=301, ticket='SF-005'); f.process(301)
        with sqlite3.connect(f.paths['reviews']) as db:
            db.execute("UPDATE jobs SET pid='-301', started='5e2' WHERE pr=9")
        model = f.collect()
        self.assertEqual({s['state'] for s in model['sources'].values()}, {'ok'})
        self.assertEqual([w['ticket'] for w in model['workers']], ['SF-001'])
        states = self.states(model)
        for key, role in [('SF-002', 'implementation'), ('SF-003', 'implementation'), ('SF-004', 'implementation'), ('SF-005', 'review')]:
            self.assertIn((key, role, 'unverified'), states)
        self.assertEqual(sum('unreadable process identity' in w for w in model['warnings']), 3)
        claude = next(c for c in model['capacity'] if c['family'] == 'Claude' and c['category'] == 'implementation')
        self.assertEqual((claude['active'], claude['held']), (1, 3))
        self.assertEqual(json.loads(json.dumps(dash.public(model)))['workers'][0]['ticket'], 'SF-001')

    def test_falsy_or_missing_receipt_identity_never_falls_back_to_db(self):
        f = self.f
        receipts = [{'pid': False, 'started': '500'}, {'pid': 0, 'started': '500'}, {'pid': [], 'started': '500'},
                    {'pid': {}, 'started': '500'}, {'pid': None, 'started': '500'}, {'pid': '', 'started': '500'},
                    {'pid': 'PID', 'started': False}, {'pid': 'PID', 'started': 0}, {'pid': 'PID', 'started': ''},
                    {'pid': 'PID', 'started': []}, {'pid': 'PID'}, {'started': '500'}, {}, [], 'text']
        for n, value in enumerate(receipts):
            pid = 110 + n
            key, session = f'SF-{n + 10:03d}', f's-bad-{n}'
            f.ticket(key, session=session)
            # The persisted DB identity matches a live process; only the receipt is malformed.
            folder = f.run(session, key, pid=pid); f.process(pid)
            if isinstance(value, dict):
                value = {k: (pid if v == 'PID' else v) for k, v in value.items()}
            (folder/'process.json').write_text(json.dumps(value))
        f.ticket('SF-001', session='s-db'); f.run('s-db', 'SF-001', pid=101, process_json=False); f.process(101)
        model = f.collect()
        self.assertEqual([w['ticket'] for w in model['workers']], ['SF-001'], 'missing receipt uses the DB identity')
        states = self.states(model)
        for n in range(len(receipts)):
            self.assertIn((f'SF-{n + 10:03d}', 'implementation', 'unverified'), states)
        self.assertEqual(sum('unreadable process identity' in w for w in model['warnings']), len(receipts))
        claude = next(c for c in model['capacity'] if c['family'] == 'Claude' and c['category'] == 'implementation')
        self.assertEqual((claude['active'], claude['held']), (1, len(receipts)))

    def test_orphaned_implementation_holds_capacity_like_dispatcher(self):
        f = self.f
        f.ticket('SF-001', agent='Codex', session='s-orphan')
        f.run('s-orphan', 'SF-001', status='orphaned', pid=201, family='Codex'); f.process(201)  # PID still matches
        f.ticket('SF-002', agent='Codex', session='s-quar')
        f.run('s-quar', 'SF-002', status='quarantined', pid=202, family='Codex')
        model = f.collect()
        self.assertEqual(model['workers'], [])
        codex = next(c for c in model['capacity'] if c['family'] == 'Codex' and c['category'] == 'implementation')
        self.assertEqual((codex['limit'], codex['active'], codex['held'], codex['retained']), (1, 0, 1, 1))
        self.assertGreaterEqual(codex['active'] + codex['held'], codex['limit'], 'saturated: dispatcher cannot launch')
        self.assertIn(('SF-001', 'implementation', 'orphaned'), self.states(model))

    def test_unmanaged_blocked_claim_holds_capacity_like_dispatcher(self):
        f = self.f
        f.ticket('SF-001', agent='Codex', session='manual', status='blocked')
        codex = next(c for c in f.collect()['capacity'] if c['family'] == 'Codex' and c['category'] == 'implementation')
        self.assertEqual((codex['active'], codex['held']), (0, 1))

    def test_duplicate_and_recovered_jobs(self):
        f = self.f
        f.ticket('SF-001', status='review')
        f.job(9, kind='fix', status='quarantined', ticket='SF-001')
        f.job(9, kind='fix', status='running', pid=401, attempts=1, ticket='SF-001'); f.process(401)
        f.ticket('SF-002', session='sess-a'); f.run('sess-a', 'SF-002', status='quarantined', pid=402)
        f.run('sess-b', 'SF-002', pid=403); f.process(403)
        model = f.collect()
        active = [(w['role'], w.get('pr'), w['ticket']) for w in model['workers']]
        self.assertEqual(sorted(active), [('implementation', None, 'SF-002'), ('repair', 9, 'SF-001')])
        repair = next(w for w in model['workers'] if w['role'] == 'repair')
        self.assertIn('attempt 2', repair['summary'])
        self.assertTrue(any('PR #9: 2 open jobs' in w for w in model['warnings']))
        self.assertTrue(any('SF-002: 2 retained implementation receipts' in w for w in model['warnings']))
        # A ticket under live repair is not also listed as awaiting review.
        self.assertNotIn(('SF-001', 'implementation', 'awaiting-review'), self.states(model))
        fix = next(c for c in model['capacity'] if c['family'] == 'Codex' and c['category'] == 'repair')
        self.assertEqual((fix['active'], fix['retained'], fix['limit']), (1, 1, 1))

    def test_resumed_repair_claim_is_not_a_manual_claim(self):
        f = self.f
        f.ticket('SF-001', session='original-dispatch')  # Review claim resumed in_progress by a repair job
        f.job(3, kind='fix', status='reserved', ticket='SF-001')
        states = self.states(f.collect())
        self.assertIn(('SF-001', 'repair', 'waiting'), states)
        self.assertNotIn(('SF-001', 'implementation', 'manual'), states)

    def test_relative_receipt_directory_is_never_read_from_cwd(self):
        f = self.f
        f.ticket('SF-001')
        f.process(101)
        with sqlite3.connect(f.paths['dispatch']) as db:
            db.execute("INSERT INTO runs VALUES('sess-impl','suifleet','Claude','running','',?,NULL,NULL,NULL)",
                       (json.dumps({'key': 'SF-001'}),))
        cwd = os.getcwd()
        os.chdir(f.base)
        try:
            (f.base/'process.json').write_text(json.dumps({'pid': 101, 'started': '500'}))
            model = f.collect()
        finally:
            os.chdir(cwd)
        self.assertEqual(model['workers'], [])
        self.assertIn(('SF-001', 'implementation', 'unverified'), self.states(model))

    def implementation_capacity(self, model, family='Claude'):
        c = next(c for c in model['capacity'] if c['family'] == family and c['category'] == 'implementation')
        return c['active'], c['held'], c['retained']

    def review_repair_capacity(self, model, family='Claude'):
        c = next(c for c in model['capacity'] if c['family'] == family and c['category'] == 'repair')
        return c['active'], c['held']

    def test_ci_repair_requested_on_live_owner_is_visible_without_double_count(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', pid=101); f.process(101)
        f.redirect('sess-impl', 'pending', reason='failed CI observed; owner grace period before revalidation')
        f.observation()
        model = f.collect()
        self.assertEqual([(w['ticket'], w['role']) for w in model['workers']], [('SF-001', 'implementation')])
        requested = [a for a in model['attention'] if a['state'] == 'ci-repair-requested']
        self.assertEqual(len(requested), 1)
        self.assertEqual((requested[0]['ticket'], requested[0]['pr']), ('SF-001', 7))
        self.assertIn('check-interfaces', requested[0]['summary'])
        self.assertNotIn('ci-failed', {a['state'] for a in model['attention']})  # One notice, not two.
        self.assertEqual(self.implementation_capacity(model), (1, 0, 0))
        # Closed/green PR: the coordinator dropped its observation, so no stale alert remains.
        with sqlite3.connect(f.paths['reviews']) as db:
            db.execute('DELETE FROM ci_observations')
        model = f.collect()
        self.assertFalse([a for a in model['attention'] if a['state'] in ('ci-repair-requested', 'ci-failed')])

    def test_guarded_interruption_pauses_the_owner_instead_of_an_active_editor(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', status='redirected', pid=101); f.process(101)
        f.redirect('sess-impl', 'interrupting', reason='verified live owner paused for repair-first work under held guards')
        model = f.collect()
        self.assertEqual(model['workers'], [])
        self.assertIn(('SF-001', 'implementation', 'pausing'), self.states(model))
        self.assertNotIn('unverified', {a['state'] for a in model['attention']})
        self.assertEqual(self.implementation_capacity(model), (0, 1, 0))
        self.assertNotIn('manual', {a['state'] for a in model['attention']})

    def test_live_ci_repair_is_a_repair_worker_using_the_owner_slot_once(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', status='redirected', pid=101)  # Original owner stopped.
        repair = f.base/'repair'
        repair.mkdir()
        (repair/'process.json').write_text(json.dumps({'pid': 301, 'started': '700'}))
        (repair/'prompt.txt').write_text('PROMPT-SECRET-TEXT')
        f.process(301, start=700)
        f.redirect('sess-impl', 'repairing', repair=repair, reason='same-claim repair worker in a clean linked checkout')
        f.observation()
        model = f.collect()
        self.assertEqual(len(model['workers']), 1)
        worker = model['workers'][0]
        self.assertEqual((worker['role'], worker['family'], worker['model'], worker['ticket'], worker['pr']),
                         ('ci-repair', 'Claude', 'model-claude', 'SF-001', 7))
        self.assertIn('check-interfaces', worker['summary'])
        self.assertIn('implementation paused', worker['summary'])
        self.assertIn(('SF-001', 'implementation', 'paused'), self.states(model))
        self.assertNotIn('ci-failed', {a['state'] for a in model['attention']})
        self.assertEqual(self.implementation_capacity(model), (1, 0, 0))  # Same claim counted once.
        self.assertEqual(self.review_repair_capacity(model), (0, 0))      # Not a global review repair slot.
        text = '\n'.join(dash.render(model, 160))
        self.assertIn('ci-repair', text)
        self.assertIn('[paused]', text)
        # Repair exited (awaiting evaluation) or launch receipt missing: held, never active.
        (f.proc/'301').rename(f.proc/'gone')
        model = f.collect()
        self.assertEqual(model['workers'], [])
        self.assertIn(('SF-001', 'ci-repair', 'stale'), self.states(model))
        self.assertEqual(self.implementation_capacity(model), (0, 1, 0))
        (repair/'process.json').unlink()
        self.assertIn(('SF-001', 'ci-repair', 'waiting'), self.states(f.collect()))

    def test_uncertain_orphaned_and_failed_redirects_stay_visible_and_reserved(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', status='redirected', pid=101)
        f.redirect('sess-impl', 'uncertain', reason='stopping worker/session did not stop; inspect recorded PIDs')
        f.ticket('SF-002', session='sess-b'); f.run('sess-b', 'SF-002', status='redirected', pid=102)
        f.redirect('sess-b', 'orphaned', pr=8, ticket='SF-002', fingerprint='fp2', reason='repair launch failed; never relaunched')
        f.ticket('SF-003', session='sess-c'); f.run('sess-c', 'SF-003', status='quarantined', pid=103)
        with sqlite3.connect(f.paths['dispatch']) as db:
            db.execute("UPDATE runs SET message='CI redirect repair_failed: repair worker exit 1' WHERE session='sess-c'")
        f.redirect('sess-c', 'repair_failed', pr=9, ticket='SF-003', fingerprint='fp3', reason='repair worker exit 1')
        model = f.collect()
        states = self.states(model)
        self.assertIn(('SF-001', 'implementation', 'uncertain'), states)
        self.assertIn(('SF-002', 'implementation', 'orphaned'), states)
        self.assertIn(('SF-003', 'implementation', 'quarantined'), states)
        quarantined = next(a for a in model['attention'] if a['ticket'] == 'SF-003')
        self.assertIn('CI redirect repair_failed', quarantined['summary'])
        self.assertEqual(model['workers'], [])
        self.assertEqual(self.implementation_capacity(model), (0, 2, 1))

    def test_resumed_feature_work_is_one_implementation_worker(self):
        f = self.f
        # The fixture's claim session follows the run; the dispatcher keeps the same claim for the resumed run.
        f.ticket('SF-001', session='sess-impl-ci1'); f.run('sess-impl-ci1', 'SF-001', pid=111); f.process(111)
        f.run('sess-impl', 'SF-001', status='finished', pid=101)
        f.redirect('sess-impl', 'resumed', resumed_as='sess-impl-ci1', reason='repair integrated')
        model = f.collect()
        self.assertEqual([(w['role'], w['ticket']) for w in model['workers']], [('implementation', 'SF-001')])
        self.assertIn('resumed after CI repair-first on PR #7', model['workers'][0]['summary'])
        self.assertFalse([a for a in model['attention'] if a['ticket'] == 'SF-001'])
        self.assertEqual(self.implementation_capacity(model), (1, 0, 0))

    def test_ci_observations_show_cause_and_actual_responsibility(self):
        f = self.f
        f.ticket('SF-001', session='manual-session')
        f.observation(responsibility='Claude implementation owner: manual or stopped owners need their own repair')
        f.ticket('SF-002', status='review', session='rev')
        f.observation(pr=8, ticket='SF-002', owner_status='review', responsibility='Claude coordinator CI repair')
        model = f.collect()
        failed = [a for a in model['attention'] if a['state'] == 'ci-failed']
        self.assertEqual([(a['ticket'], a['pr']) for a in failed], [('SF-001', 7)])
        self.assertIn('check-interfaces', failed[0]['summary'])
        self.assertIn('(draft)', failed[0]['summary'])
        self.assertIn('manual or stopped owners need their own repair', failed[0]['summary'])

    def test_redirect_receipts_never_expose_tokens(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', status='redirected', pid=101)
        f.redirect('sess-impl', 'interrupting', reason=f'leaked {TOKEN} in a reason')
        model = f.collect()
        output = json.dumps(dash.public(model)) + '\n'.join(dash.render(model, 200))
        for secret in (TOKEN, LOCK_TOKEN, hashlib.sha256(TOKEN.encode()).hexdigest()):
            self.assertNotIn(secret, output)

    def test_waiting_retry_and_ci_states_are_distinct(self):
        f = self.f
        f.ticket('SF-001', status='review'); f.job(1, ticket='SF-001', status='done', checks=('SUCCESS', 'FAILURE'))
        f.ticket('SF-002', status='review'); f.job(2, ticket='SF-002', status='done', checks=('SUCCESS', None))
        f.ticket('SF-003', status='review'); f.job(3, ticket='SF-003', status='done', checks=('SUCCESS',))
        f.ticket('SF-004', status='review')
        f.ticket('SF-005', status='blocked', evidence=f'Needs operator; token={TOKEN}')
        f.job(5, ticket='SF-006', status='reserved')
        f.job(6, ticket='SF-007', status='retry', retry_after=NOW+120, attempts=2, error='usage limit; ghp_abcdefghijklmnopqrstuvwxyz0123')
        model = f.collect()
        states = self.states(model)
        for expected in [('SF-001', 'implementation', 'ci-failed'), ('SF-002', 'implementation', 'ci-pending'),
                         ('SF-003', 'implementation', 'awaiting-review'), ('SF-004', 'implementation', 'review'),
                         ('SF-005', 'implementation', 'blocked'), ('SF-006', 'review', 'waiting'), ('SF-007', 'review', 'retry')]:
            self.assertIn(expected, states)
        text = '\n'.join(dash.render(model, 200))
        self.assertIn('retry in 2m', text)
        self.assertIn('no local PR snapshot yet', text)
        self.assertIn('checks 1 ok/0 pending/1 failed', text)

    def test_sensitive_fields_never_reach_any_output(self):
        f = self.f
        f.ticket('SF-001', evidence=f'handoff token={TOKEN} ghp_abcdefghijklmnopqrstuvwxyz0123', status='blocked')
        f.run('sess-impl', 'SF-001', pid=101); f.process(101)
        f.job(4, kind='fix', pid=102, ticket='SF-001', error=f'failed with {TOKEN}'); f.process(102)
        f.job(5, status='orphaned', ticket='SF-001', error=f'GH_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123 {TOKEN}')
        model = f.collect()
        delta = dash.changes(dash.collect(f.paths, now=NOW, proc=f.base/'none'), model)
        outputs = [json.dumps(dash.public(model, delta)), '\n'.join(dash.render(model, 300, delta))]
        for output in outputs:
            for secret in SECRETS:
                self.assertNotIn(secret, output)
            self.assertNotIn('/private/worktree', output)
        self.assertIn('[redacted]', outputs[1])

    def test_reads_are_read_only_and_offline(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', pid=101); f.process(101); f.job(3, pid=102); f.process(102)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in f.base.rglob('*') if p.is_file()}
        listing = set(f.base.rglob('*'))

        def no_network(*args, **kwargs):
            raise AssertionError('dashboard attempted a network connection')
        with patch.object(socket.socket, 'connect', no_network), patch.object(subprocess, 'Popen', side_effect=AssertionError('no subprocesses')):
            for _ in range(2):
                dash.collect(f.paths, now=NOW, proc=f.proc)
        self.assertEqual(listing, set(f.base.rglob('*')))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in f.base.rglob('*') if p.is_file()})

    def test_project_filter(self):
        f = self.f
        f.ticket('SF-001'); f.ticket('VT-001', project='vortech', session='v')
        f.job(8, ticket='VT-001', project='vortech', status='reserved')
        model = f.collect(project='suifleet')
        self.assertEqual(set(model['queue']), {'suifleet'})
        self.assertFalse(any(a['project'] == 'vortech' for a in model['attention']))
        self.assertEqual(set(f.collect(project='vortech')['queue']), {'vortech'})

    def test_rendering_fits_small_terminals_and_reports_changes(self):
        f = self.f
        f.ticket('SF-001'); f.run('sess-impl', 'SF-001', pid=101); f.process(101)
        first = f.collect()
        f.process(101, session=1, start=777)  # worker exited; PID reused
        f.job(2, pid=500); f.process(500)
        second = f.collect()
        delta = dash.changes(first, second)
        self.assertTrue(any(d.startswith('+ started: Codex review') for d in delta))
        self.assertTrue(any('no longer active: Claude implementation SF-001' in d for d in delta))
        self.assertTrue(any(d.startswith('+ stale: Claude implementation SF-001') for d in delta))
        for width in (20, 40, 80):
            lines = dash.render(second, width, delta, 5)
            self.assertTrue(all(len(line) <= width for line in lines), width)
        lines = dash.render(second, 120, delta, 5)
        self.assertIn('CHANGES SINCE LAST REFRESH', lines)
        self.assertTrue(any('Title of SF-001' in line for line in lines))
        self.assertTrue(any('network:suifleet-testnet' in line for line in lines))

    def cli(self, *args):
        f = self.f
        paths = [x for name, p in f.paths.items() for x in (f'--{name}-path', str(p))]
        return subprocess.run([sys.executable, str(ROOT/'dashboard.py'), *paths, *args],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)

    def test_cli_non_tty_json_and_live_refusal(self):
        self.f.ticket('SF-001', status='blocked')
        default = self.cli()
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn('read-only snapshot', default.stdout)
        self.assertNotIn('\x1b', default.stdout)
        data = json.loads(self.cli('--json').stdout)
        self.assertEqual(data['queue'], {'suifleet': {'blocked': 1}})
        live = self.cli('--live')
        self.assertEqual(live.returncode, 2)
        self.assertIn('needs an interactive terminal', live.stderr)
        self.f.paths['reviews'].unlink()
        self.assertEqual(self.cli('--once').returncode, 3)



class GitHubInventory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)
        self.f.paths['inventory'] = self.f.base/'inventory'
        config = json.loads(self.f.paths['config'].read_text())
        for p in config['projects']:
            p.update(repository='owner/'+p['id'], github_account='account-'+p['id'])
        self.f.paths['config'].write_text(json.dumps(config))

    def snapshot(self, project='suifleet', issues=None, prs=None, stamp=NOW-10):
        path = dash.inventory_path(self.f.paths['inventory'], project)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(schema=1, project=project, repository='owner/'+project,
                                       fetched_at=stamp, issues=issues or [], prs=prs or [])))
        return path

    def ticket(self, number, status='backlog', project='suifleet'):
        key = f'SF-{number:03}'
        self.f.ticket(key, status=status, project=project, session='session-'+str(number))
        with sqlite3.connect(self.f.paths['tickets']) as db:
            db.execute('UPDATE tickets SET remote=? WHERE key=?',
                       (json.dumps({'url': f'https://github.com/owner/{project}/issues/{number}'}), f'{project}::{key}'))

    def test_github_totals_separate_workers_prs_and_completed_tickets(self):
        statuses = ['backlog']*8 + ['blocked']*2 + ['in_progress']*4 + ['review']*3
        for n, status in enumerate(statuses, 1):
            self.ticket(n, status)
        self.ticket(18, 'done')
        for n in range(11, 15):
            self.f.run('session-'+str(n), f'SF-{n:03}', pid=100+n)
            self.f.process(100+n)
        self.snapshot(issues=[dict(number=n, title=f'[SF-{n:03}] issue') for n in range(1, 18)],
                      prs=[dict(number=100+n, title=f'SF-{n:03}: work', isDraft=n==1) for n in range(1, 9)]
                          + [dict(number=n, title='old maintenance') for n in (53, 150)])
        model = self.f.collect(project='suifleet')
        entry = model['inventory']['suifleet']
        self.assertEqual((entry['github_open_issues'], entry['tracked_open'], entry['github_open_prs']), (17, 17, 10))
        self.assertEqual(entry['other_prs'], [53, 150])
        self.assertEqual(len(model['workers']), 4)
        output = '\n'.join(dash.render(model, 200))
        for expected in ['17 open issues · 10 open PRs', '17 = 8 backlog + 4 in progress + 3 review + 2 blocked',
                         'Running workers: 4 verified', 'do not add to issue totals', '1 done']:
            self.assertIn(expected, output)
        self.assertEqual(entry['untracked_issues'], [])
        self.assertEqual(entry['absent_from_github'], [])

    def test_discrepancies_staleness_and_project_filter(self):
        self.ticket(1); self.ticket(2)
        self.snapshot(issues=[dict(number=1, title='one'), dict(number=99, title='untracked')], stamp=NOW-2000)
        self.snapshot(project='vortech', issues=[dict(number=4, title='other')])
        model = self.f.collect(project='suifleet')
        self.assertEqual(set(model['inventory']), {'suifleet'})
        entry = model['inventory']['suifleet']
        self.assertEqual(entry['state'], 'stale')
        self.assertEqual(entry['untracked_issues'], [99])
        self.assertEqual(entry['absent_from_github'], ['SF-002'])
        self.assertIn('STALE', '\n'.join(dash.render(model, 160)))

    def test_title_links_ticket_after_unrelated_numbered_tokens(self):
        self.ticket(1)
        self.snapshot(prs=[dict(number=99, title='Fix UTF-8 and CVE-2024 in SF-001'),
                           dict(number=100, title='SF-0010 is a different ticket')])
        entry = self.f.collect()['inventory']['suifleet']
        self.assertEqual(entry['linked_prs'], 1)
        self.assertEqual(entry['other_prs'], [100])

    def test_missing_corrupt_and_wrong_repository_are_unknown_not_zero(self):
        self.ticket(1)
        self.assertIsNone(self.f.collect()['inventory']['suifleet']['github_open_prs'])
        path = self.snapshot()
        for bad in ['garbage', json.dumps(dict(schema=1, project='suifleet', repository='wrong/repo')),
                    json.dumps(dict(schema=1, project='suifleet', repository='owner/suifleet', fetched_at=NOW,
                                    issues=[dict(number=True)], prs=[]))]:
            path.write_text(bad)
            model = self.f.collect()
            self.assertEqual(model['inventory']['suifleet']['state'], 'unavailable')
            self.assertIn('open PRs ? (no usable snapshot)', '\n'.join(dash.render(model, 200)))

    @staticmethod
    def ci_response(number=2, checks=None, testnet=False, head='a'*40, base='b'*40, more=False):
        return {'data': {'repository': {'testnet': {'oid': 'c'*40} if testnet else None,
            'p'+str(number): dict(number=number, state='OPEN', headRefOid=head, baseRefOid=base,
                headRefName='work/sf-001', isDraft=False, title='SF-001',
                commits={'nodes': [{'commit': {'oid': head, 'statusCheckRollup': {'contexts': {
                    'nodes': checks or [], 'pageInfo': {'hasNextPage': more}}}}}]})}}}

    def test_explicit_refresh_paginates_filters_prs_and_uses_project_account(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            if args[1] == 'auth':
                return subprocess.CompletedProcess(args, 0, stdout='private-token\n')
            if args[2] == 'graphql':
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(self.ci_response()))
            if '/issues?' in args[2]:
                rows = [[dict(number=1, title='issue', state='open')],
                        [dict(number=2, title='PR', state='open', pull_request={})]]
            else:
                rows = [[dict(number=2, title='PR '+SECRETS[-1], body='COMMENT-SECRET', state='open', isDraft=True)]]
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(rows))
        with patch.object(subprocess, 'run', side_effect=run):
            dash.refresh_github(self.f.paths, 'suifleet')
        self.assertEqual(calls[0][0][-1], 'account-suifleet')
        self.assertEqual(len(calls), 4)
        for args, options in calls[1:]:
            if args[2] != 'graphql':
                self.assertIn('--paginate', args)
            self.assertEqual(options['env']['GH_TOKEN'], 'private-token')
            self.assertEqual(options['timeout'], 60)
        raw = dash.inventory_path(self.f.paths['inventory'], 'suifleet').read_text()
        data = json.loads(raw)
        self.assertEqual(len(data['issues']), 1)
        self.assertEqual(len(data['prs']), 1)
        self.assertTrue(data['prs'][0]['draft'])
        for secret in ['private-token', 'COMMENT-SECRET', SECRETS[-1]]:
            self.assertNotIn(secret, raw)

    def test_live_failure_replaces_worker_pending_and_offline_view_stays_offline(self):
        self.ticket(1, 'review')
        folder = self.f.job(7, status='done', ticket='SF-001', checks=('PENDING',))
        os.utime(folder/'snapshot.json', (NOW-7200, NOW-7200))
        checks = [dict(__typename='CheckRun', name='check', status='COMPLETED', conclusion='SUCCESS'),
                  dict(__typename='StatusContext', context='testnet/package-upgrade', state='FAILURE',
                       description='base:'+'b'*40+' class:uncertain')]
        with patch.object(dash.time, 'time', return_value=NOW-10):
            ci = dash.fetch_ci('owner/suifleet', [dict(number=7)],
                lambda *a, **kw: json.dumps(self.ci_response(7, checks, True)), {})
        path = self.snapshot(prs=[dict(number=7, title='SF-001')])
        raw = json.loads(path.read_text()); raw['ci'] = ci; path.write_text(json.dumps(raw))
        with patch.object(subprocess, 'run', side_effect=AssertionError('offline collect made a network call')):
            model = self.f.collect()
        alert = next(a for a in model['attention'] if a.get('ticket') == 'SF-001')
        self.assertEqual(alert['state'], 'ci-failed')
        self.assertIn('testnet failure', alert['summary'])
        self.assertIn('GitHub; observed 10s ago', alert['summary'])
        stale = dash.collect(self.f.paths, now=NOW+1000, proc=self.f.proc)
        self.assertEqual(next(a for a in stale['attention'] if a.get('ticket') == 'SF-001')['state'], 'ci-stale')

    def test_unmatched_base_missing_checks_and_partial_responses_are_not_success(self):
        check = dict(context='testnet/package-upgrade', state='SUCCESS', description='base:'+'f'*40)
        response = self.ci_response(2, [check], True)
        ci = dash.fetch_ci('owner/suifleet', [dict(number=2)], lambda *a, **kw: json.dumps(response), {})
        self.assertEqual(dash.ci_state(ci['2']), 'ci-unknown')
        for response in [dict(errors=[dict(message='private error')]), self.ci_response(more=True),
                         self.ci_response(head='bad'), {'data': {'repository': {'p2': None}}}]:
            with self.subTest(response=response), self.assertRaises((ValueError, KeyError)):
                dash.fetch_ci('owner/suifleet', [dict(number=2)], lambda *a, **kw: json.dumps(response), {})

    def test_old_worker_evidence_is_marked_stale_and_merged_retry_is_hidden(self):
        self.ticket(1, 'review')
        folder = self.f.job(7, status='done', checks=('PENDING',))
        os.utime(folder/'snapshot.json', (NOW-4000, NOW-4000))
        alert = next(a for a in self.f.collect()['attention'] if a.get('ticket') == 'SF-001')
        self.assertEqual(alert['state'], 'ci-stale')
        self.assertIn('worker-start snapshot', alert['summary'])
        self.f.job(8, status='retry', retry_after=NOW-1)
        with sqlite3.connect(self.f.paths['reviews']) as db:
            db.execute("INSERT INTO integrations VALUES('suifleet',8,'head','commit',1)")
        self.assertFalse(any(a['state'] == 'retry' and a.get('pr') == 8 for a in self.f.collect()['attention']))

    def test_failed_refresh_preserves_previous_snapshot_and_redacts_errors(self):
        path = self.snapshot(issues=[dict(number=1, title='old')])
        before = path.read_bytes()
        with patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'gh', stderr=TOKEN)):
            with self.assertRaises(dash.Unavailable) as error:
                dash.refresh_github(self.f.paths, 'suifleet')
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertEqual(path.read_bytes(), before)

    def test_failed_serialization_removes_temporary_file(self):
        path = self.snapshot(issues=[dict(number=1, title='old')])
        before = path.read_bytes()
        response = subprocess.CompletedProcess(['gh'], 0, stdout='[[{"number":1,"title":"one","state":"open"}]]')
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(self.ci_response(1))) if args[2] == 'graphql' else response
        with patch.object(subprocess, 'run', side_effect=run), patch.object(dash.json, 'dump', side_effect=OSError('disk full')):
            with self.assertRaises(dash.Unavailable):
                dash.refresh_github(self.f.paths, 'suifleet')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
