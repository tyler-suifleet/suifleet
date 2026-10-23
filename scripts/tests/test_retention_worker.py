import importlib.util
from pathlib import Path
import tempfile
import hashlib
import hmac
import json
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest

spec = importlib.util.spec_from_file_location('retention_worker', Path(__file__).parents[1] / 'retention_worker.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Chain:
    def __init__(self):
        self.end = 5
        self.epoch = 4
        self.affordable = True
        self.submitted = 0
        self.prepared = 0
        self.receipt = 'unknown'
        self.outage = False

    def __call__(self, action, value=None):
        if action == 'snapshot':
            return {'epoch': self.epoch, 'margin': 2, 'revision': '1',
                    'blobs': [{'id': 'blob', 'end': self.end, 'target': 7, 'affordable': self.affordable}]}
        if action == 'prepare':
            self.prepared += 1
            return {'digest': 'digest', 'bytes': 'same-signed-bytes', 'signature': 'executor'}
        if action == 'submit':
            self.submitted += 1
            if self.outage:
                raise OSError('uncertain')
            self.end = 7
            self.receipt = 'success'
            return {'status': 'submitted'}
        if action == 'receipt':
            return {'status': self.receipt}
        raise AssertionError(action)


class RetentionTests(unittest.TestCase):
    def worker(self, directory, chain):
        return module.Worker({'gas_budget': '10', 'spend_ceiling': '20'}, directory, chain)

    def test_uncertain_restart_reuses_original_transaction_and_confirms_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = Chain()
            chain.outage = True
            worker = self.worker(directory, chain)
            worker.tick(0)
            self.assertIn('blob', worker.state['pending'])
            worker.lock.close()
            worker = self.worker(directory, chain)
            chain.outage = False
            worker.tick(300)
            self.assertEqual(chain.prepared, 1)
            self.assertEqual(worker.state['spent_ceiling'], 10)
            worker.tick(301)
            self.assertEqual(worker.state['last_success']['blob']['end'], 7)
            self.assertEqual(worker.state['pending'], {})
            worker.tick(999)
            self.assertEqual(chain.submitted, 2)
            worker.lock.close()

    def test_two_blobs_shared_gas_invalidates_then_reprepares_after_restart(self):
        class SharedGas:
            def __init__(self):
                self.version = 1
                self.ends = {'A': 5, 'B': 5}
                self.prepared = []
                self.effects = set()
                self.outage = True

            def __call__(self, action, value=None):
                if action == 'snapshot':
                    return {'epoch': 4, 'margin': 2, 'revision': '1', 'blobs': [
                        {'id': key, 'end': end, 'target': 7, 'affordable': True}
                        for key, end in self.ends.items()]}
                if action == 'prepare':
                    intent = {'digest': f"{value['blob']}@{self.version}",
                              'blob': value['blob'], 'version': self.version}
                    self.prepared.append(intent)
                    return intent
                if action == 'receipt':
                    return {'status': 'success' if value['digest'] in self.effects else
                            'failed' if value['version'] < self.version else 'unknown'}
                if action == 'submit':
                    if value['blob'] == 'A' and self.outage:
                        raise OSError('timeout before execution')
                    if value['version'] != self.version:
                        raise OSError('stale gas')
                    self.version += 1
                    self.ends[value['blob']] = 7
                    self.effects.add(value['digest'])
                    return {'status': 'submitted'}
                raise AssertionError(action)

        with tempfile.TemporaryDirectory() as directory:
            chain = SharedGas()
            config = {'gas_budget': '10', 'spend_ceiling': '30'}
            worker = module.Worker(config, directory, chain)
            worker.tick(0)  # B consumes A's gas reference while A is uncertain.
            self.assertEqual(chain.ends, {'A': 5, 'B': 7})
            worker.lock.close()
            worker = module.Worker(config, directory, chain)
            chain.outage = False
            worker.tick(300)  # Definitively invalidated A is cleared.
            worker.tick(10000)  # Fresh A uses B's new gas version.
            worker.tick(10001)
            self.assertEqual(chain.ends, {'A': 7, 'B': 7})
            self.assertEqual(len(chain.prepared), 3)
            self.assertEqual(worker.state['spent_ceiling'], 30)
            self.assertEqual(worker.state['pending'], {})
            self.assertEqual(worker.state['last_success']['A']['digest'], 'A@2')
            worker.lock.close()

    def test_purchase_decline_has_no_credit_and_topup_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = Chain()
            chain.affordable = False
            worker = self.worker(directory, chain)
            worker.tick(0)
            self.assertEqual(chain.prepared, 0)
            # Provider callbacks cannot credit resources; only fresh chain reads.
            worker.state['purchase'] = 'declined'
            worker.tick(100)
            self.assertEqual(chain.prepared, 0)
            chain.affordable = True
            worker.tick(200)
            worker.tick(201)
            self.assertEqual(chain.prepared, 1)
            self.assertEqual(worker.state['purchase'], 'declined')
            worker.lock.close()

    def test_expired_blob_never_renews_or_installs(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = Chain()
            chain.epoch = 5
            worker = self.worker(directory, chain)
            worker.tick(0)
            self.assertEqual(chain.prepared, 0)
            self.assertIn('restore required', worker.state['failures']['blob'])
            worker.lock.close()

    def test_expiry_without_successful_effects_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = Chain()
            chain.outage = True
            worker = self.worker(directory, chain)
            worker.tick(0)
            chain.end = 7
            worker.tick(200)
            self.assertEqual(worker.state['last_success'], {})
            self.assertIn('blob', worker.state['pending'])
            worker.lock.close()

    def test_retries_and_notifications_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = Chain()
            chain.outage = True
            worker = self.worker(directory, chain)
            for now in range(0, 10000, 300):
                worker.tick(now)
            self.assertGreater(chain.submitted, 3)
            self.assertEqual(chain.prepared, 1)
            self.assertEqual(len(worker.state['alerts']), 2)
            self.assertIn('unconfigured', next(iter(worker.state['alerts'].values()))['status'])
            worker.lock.close()

    def test_long_submit_outage_recovers_original_intent_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = Chain()
            chain.outage = True
            worker = self.worker(directory, chain)
            for now in (0, 60, 180, 420, 900, 1860):
                worker.tick(now)
            self.assertGreater(chain.submitted, 3)
            self.assertIn('renewal_retry_exhausted:4', worker.state['alerts'])
            original = dict(worker.state['pending']['blob'])
            worker.lock.close()
            worker = self.worker(directory, chain)
            chain.outage = False
            worker.tick(10000)
            worker.tick(10001)
            self.assertEqual(worker.state['last_success']['blob']['digest'], original['digest'])
            self.assertEqual(chain.prepared, 1)
            self.assertEqual(worker.state['spent_ceiling'], 10)
            worker.lock.close()

    def test_retry_stops_at_exclusive_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = Chain()
            chain.outage = True
            worker = self.worker(directory, chain)
            worker.tick(0)
            chain.epoch = 5
            worker.tick(10000)
            self.assertEqual(chain.submitted, 1)
            self.assertIn('blob_expired:5', worker.state['alerts'])
            worker.lock.close()

    def test_full_alert_queue_export_acknowledgment_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory, Chain())
            for epoch in range(256):
                worker.notify('old_warning', epoch, epoch)
            worker.notify('blob_expired', 300, 300)
            self.assertNotIn('blob_expired:300', worker.state['alerts'])
            export = Path(directory) / 'alerts-export.json'
            worker.archive_alerts(256, export)
            self.assertEqual(len(json.loads(export.read_text())['alerts']), 256)
            self.assertEqual(export.stat().st_mode & 0o777, 0o600)
            worker.lock.close()
            worker = self.worker(directory, Chain())
            worker.notify('blob_expired', 300, 301)
            self.assertIn('blob_expired:300', worker.state['alerts'])
            # Refuse overwriting an export; never acknowledge without exporting.
            with self.assertRaises(FileExistsError):
                worker.archive_alerts(301, export)
            self.assertIn('blob_expired:300', worker.state['alerts'])
            worker.lock.close()

    def test_notifier_rejects_symlink_and_public_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / 'secret'
            secret.write_bytes(b'x' * 32)
            secret.chmod(0o600)
            link = root / 'link'
            link.symlink_to(secret)
            config = {'gas_budget': '10', 'spend_ceiling': '20',
                      'notifier': {'url': 'https://localhost/alerts', 'secret_file': str(link)}}
            worker = module.Worker(config, root / 'state', Chain())
            try:
                with self.assertRaises(OSError): worker.notify('low', 1, 0)
                link.unlink()
                secret.rename(link)
                link.chmod(0o644)
                with self.assertRaises(ValueError): worker.notify('low', 1, 1)
            finally:
                worker.lock.close()

    def test_real_https_notifier_authentication_acknowledgment_and_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = root / 'cert.pem', root / 'tls.key'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(key), '-out', str(cert), '-days', '1',
                            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                           check=True, capture_output=True)
            secret = root / 'notifier.key'
            secret.write_bytes(b'test-only-notifier-secret-32-bytes')
            secret.chmod(0o600)
            payloads = []
            class Receiver(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_POST(self):
                    payload = self.rfile.read(int(self.headers['Content-Length']))
                    expected = hmac.new(secret.read_bytes(), payload, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(self.headers.get('X-SuiFleet-Signature', ''), expected):
                        self.send_error(401)
                        return
                    payloads.append(json.loads(payload))
                    self.send_response(204)
                    self.end_headers()
            server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                worker = module.Worker({'gas_budget': '10', 'spend_ceiling': '20', 'notifier': {
                    'url': f'https://localhost:{server.server_port}/alerts', 'secret_file': str(secret), 'ca_file': str(cert)}}, root / 'state', Chain())
                worker.notify('retention_low', 4, 100)
                worker.notify('retention_low', 4, 200)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(set(payloads[0]), {'schema', 'id', 'code', 'epoch', 'timestamp'})
                self.assertEqual(worker.state['alerts']['retention_low:4']['status'], 'acknowledged')
                worker.save()
                config = worker.config
                worker.lock.close()
                # Same durable configuration still suppresses duplicates after restart.
                worker = module.Worker(config, root / 'state', Chain())
                worker.notify('retention_low', 4, 300)
                self.assertEqual(len(payloads), 1)
                worker.lock.close()
                # A second fleet may share this authenticated endpoint and secret.
                # Its condition must not collide with the first fleet's message ID.
                other = module.Worker({**config, 'storage': 'another-fleet'}, root / 'other-state', Chain())
                other.notify('retention_low', 4, 300)
                self.assertEqual(len(payloads), 2)
                self.assertNotEqual(payloads[0]['id'], payloads[1]['id'])
                self.assertEqual(set(payloads[1]), {'schema', 'id', 'code', 'epoch', 'timestamp'})
                other.lock.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_exclusive_lock_and_configuration_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory, Chain())
            worker.save()
            with self.assertRaises(BlockingIOError):
                self.worker(directory, Chain())
            worker.lock.close()
            with self.assertRaises(ValueError):
                module.Worker({'gas_budget': '11', 'spend_ceiling': '20'}, directory, Chain())


if __name__ == '__main__':
    unittest.main()
