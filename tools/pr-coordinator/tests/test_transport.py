import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('transport_coordinator', Path(__file__).resolve().parents[1]/'coordinator.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class ReadRetries(unittest.TestCase):
    def test_transient_read_retries_then_returns(self):
        args=['git','ls-remote','origin','refs/heads/main']
        with patch.object(c.subprocess,'run',side_effect=[subprocess.CalledProcessError(128,args,stderr='TLS handshake timeout'),Mock(stdout='ok')]) as run, patch.object(c.time,'sleep') as sleep:
            self.assertEqual(c.run(args),'ok')
            self.assertEqual(run.call_count,2)
            sleep.assert_called_once()
            self.assertEqual(run.call_args.kwargs['timeout'],45)

    def test_timeout_exhausts_three_attempts(self):
        with patch.object(c.subprocess,'run',side_effect=subprocess.TimeoutExpired('gh',45)) as run, patch.object(c.time,'sleep'):
            with self.assertRaises(subprocess.TimeoutExpired): c.run(['gh','pr','view','1'])
            self.assertEqual(run.call_count,3)

    def test_permanent_errors_and_mutations_never_retry(self):
        for args,message in [(['gh','api','repos/a/b'],'HTTP 403 forbidden'),(['gh','api','repos/a/b'],'HTTP 401 bad credentials'),(['git','push','origin','main'],'TLS handshake timeout'),(['ticket','--project','x','claim'],'TLS handshake timeout'),(['gh','api','graphql','-f','query=mutation { x }'],'HTTP 503'),(['gh','api','repos/a/b','-f','body=x'],'HTTP 503')]:
            with self.subTest(args=args,message=message), patch.object(c.subprocess,'run',side_effect=subprocess.CalledProcessError(1,args,stderr=message)) as run, patch.object(c.time,'sleep') as sleep:
                with self.assertRaises(subprocess.CalledProcessError): c.run(args)
                self.assertEqual(run.call_count,1);sleep.assert_not_called()

    def test_graphql_query_and_refresh_are_read_only(self):
        self.assertTrue(c.retryable_read(['gh','api','graphql','-f','query=query { viewer { login } }']))
        self.assertTrue(c.retryable_read(['ticket','--project','suifleet','refresh']))
        self.assertFalse(c.retryable_read(['gh','api','graphql','--input','-']))

    def test_retry_after_is_respected_without_long_scan_sleep(self):
        err=lambda text:subprocess.CalledProcessError(1,['gh'],stderr=text)
        self.assertEqual(c.transient_delay(err('HTTP 429 Retry-After: 9'),0),9)
        self.assertIsNone(c.transient_delay(err('HTTP 429 Retry-After: 120'),0))
        self.assertIsNone(c.transient_delay(err('HTTP 404 TLS handshake timeout'),0))


class ScanIsolation(unittest.TestCase):
    def engine(self,directory):
        return c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(directory))

    def test_bad_snapshot_does_not_hide_later_pr(self):
        with tempfile.TemporaryDirectory() as d:
            engine=self.engine(d)
            with patch.object(engine,'gh',return_value=json.dumps([[{'number':1},{'number':2}]])),patch.object(engine,'pr',side_effect=[RuntimeError('transport'),{'number':2}]):
                self.assertEqual(engine.prs({'id':'test','repository':'a/b'}),[{'number':2}])

    def test_discovery_outage_does_not_prevent_open_pr_scan(self):
        with tempfile.TemporaryDirectory() as d:
            engine=self.engine(d)
            with patch.object(engine,'owners',return_value=[]),patch.object(engine,'collect'),patch.object(engine,'discover_merged',side_effect=RuntimeError('transport')),patch.object(engine,'prs',return_value=[]) as prs:
                engine.tick_project({'id':'test'},True)
                prs.assert_called_once()

    def test_failed_heartbeat_does_not_skip_next_worker(self):
        with tempfile.TemporaryDirectory() as d:
            engine=self.engine(d)
            for n in (1,2):
                engine.store.db.execute("INSERT INTO jobs(project,pr,family,kind,head,feedback,status,directory,pid,started,created) VALUES('test',?,'Claude','fix','head','feedback','running',?,12,'start',0)",(n,d))
            owner={'branch':'work','status':'in_progress','key':'T1','token':'private'}
            with patch.object(c,'process_identity',return_value='start'),patch.object(engine,'pr',side_effect=[RuntimeError('transport'),{'headRefName':'work'}]),patch.object(engine,'ticket') as ticket,patch.object(engine,'publish_results'):
                engine.collect({'id':'test'},[owner])
                ticket.assert_called_once()
            self.assertEqual(engine.store.db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0],2)
