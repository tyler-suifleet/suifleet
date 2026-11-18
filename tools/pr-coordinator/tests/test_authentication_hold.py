import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from coordinator import Coordinator, Store

class AuthenticationHold(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.c=Coordinator.__new__(Coordinator)
        self.c.store=Store(self.path/'reviews.sqlite3'); self.addCleanup(self.c.store.db.close)
        self.c.config={'workers':{'Claude':['claude','-p']}}
        self.stamp=patch.object(self.c,'credential_stamp',return_value='old'); self.stamp.start(); self.addCleanup(self.stamp.stop)
        self.c.store.db.execute("INSERT INTO jobs(id,family,kind,status) VALUES(1,'Claude','review','retry')")
        self.row={'id':1,'kind':'review'}
    def test_hold_prevents_retry_without_spawning_provider(self):
        self.c.hold_authentication(self.row)
        with patch('coordinator.subprocess.run') as run:
            self.assertFalse(self.c.authentication_ready('Claude')); run.assert_not_called()
        row=self.c.store.db.execute('SELECT status,error FROM jobs WHERE id=1').fetchone()
        self.assertEqual(tuple(row),('retry',self.c.AUTH_REQUIRED))
        self.assertTrue(self.c.authentication_ready('Codex'))
    def test_changed_credentials_restore_waiting_reviews(self):
        self.c.hold_authentication(self.row)
        self.c.store.db.execute('UPDATE worker_authentication SET checked=0')
        with patch.object(self.c,'credential_stamp',return_value='new'), patch('coordinator.subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout='{"loggedIn":true}')) as run:
            self.assertTrue(self.c.authentication_ready('Claude'))
            self.assertEqual(run.call_args.args[0],['claude','auth','status'])
        row=self.c.store.db.execute('SELECT retry_after,error FROM jobs WHERE id=1').fetchone()
        self.assertEqual(row[0],0); self.assertIn('restored',row[1])
    def test_failed_probe_retains_hold_and_stopped_repairs(self):
        self.c.store.db.execute("UPDATE jobs SET kind='fix' WHERE id=1")
        self.c.hold_authentication({'id':1,'kind':'fix'})
        self.c.store.db.execute('UPDATE worker_authentication SET checked=0')
        with patch('coordinator.subprocess.run',return_value=types.SimpleNamespace(returncode=1,stdout='')):
            self.assertFalse(self.c.authentication_ready('Claude'))
        self.assertEqual(self.c.store.db.execute('SELECT status FROM jobs').fetchone()[0],'orphaned')
    def test_new_expiration_resets_previous_restored_stamp(self):
        self.c.auth_table()
        self.c.store.db.execute("INSERT INTO worker_authentication VALUES('Claude',0,0,'earlier','restored')")
        self.c.hold_authentication(self.row)
        self.assertEqual(self.c.store.db.execute('SELECT stamp FROM worker_authentication').fetchone()[0],'old')
    def test_unchanged_metadata_still_gets_bounded_periodic_probe(self):
        self.c.hold_authentication(self.row)
        self.c.store.db.execute('UPDATE worker_authentication SET checked=0')
        with patch('coordinator.subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout='{"loggedIn":true}')) as run:
            self.assertTrue(self.c.authentication_ready('Claude'))
            self.assertEqual(run.call_count,1)
    def test_invalid_status_never_releases_hold(self):
        self.c.hold_authentication(self.row)
        self.c.store.db.execute('UPDATE worker_authentication SET checked=0')
        with patch('coordinator.subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout='invalid')):
            self.assertFalse(self.c.authentication_ready('Claude'))
        self.assertEqual(self.c.store.db.execute('SELECT blocked FROM worker_authentication').fetchone()[0],1)
    def test_only_latest_attempt_classified(self):
        log=self.path/'worker.log'
        log.write_text('Failed to authenticate: OAuth session expired and could not be refreshed')
        self.assertTrue(self.c.auth_failure(self.path))
        with log.open('a') as f:f.write('\n[pr-coordinator worker attempt]\nnormal result')
        self.assertFalse(self.c.auth_failure(self.path))
        self.assertFalse(self.c.auth_failure(self.path/'missing'))

class DashboardAuthentication(unittest.TestCase):
    def test_authentication_action_visible_without_private_credentials(self):
        from test_dashboard import Fixture, dash
        with tempfile.TemporaryDirectory() as root:
            f=Fixture(root)
            import sqlite3
            with sqlite3.connect(f.paths['reviews']) as db:
                db.execute('CREATE TABLE worker_authentication(family TEXT,blocked INTEGER,checked REAL,stamp TEXT,diagnostic TEXT)')
                db.execute('INSERT INTO worker_authentication VALUES(?,?,?,?,?)',('Claude',1,0,'metadata-only',Coordinator.AUTH_REQUIRED))
            model=f.collect()
            self.assertEqual(model['authentication'][0]['family'],'Claude')
            output='\n'.join(dash.render(model,width=140))
            self.assertIn('AUTHENTICATION',output)
            self.assertIn('claude auth login --claudeai',output)
            self.assertNotIn('metadata-only',output)
