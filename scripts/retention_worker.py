#!/usr/bin/env python3
"""Owner-run bounded retention scheduler. No owner capability or payment key.

Transport protocol is implemented by retention_transport.cjs. State is private,
locked, fsync'd before submission; an uncertain transaction is never replaced.
"""
import argparse
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import ssl
import stat
import time
import urllib.request


class Worker:
    def __init__(self, config, directory, transport=None):
        self.config = config
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise ValueError('worker state directory must be private (0700)')
        self.lock = (self.directory / 'worker.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.lock.close()
            raise
        self.path = self.directory / 'retention.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            'version': 1, 'binding': self.binding(), 'attempts': {}, 'pending': {},
            'spent_ceiling': 0, 'alerts': {}, 'last_success': {}, 'failures': {}}
        if self.state['version'] != 1 or self.state['binding'] != self.binding():
            self.lock.close()
            raise ValueError('state belongs to another executor configuration')
        if len(self.state['pending']) > 100_000 or self.path.exists() and self.path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError('state exceeds disk limit; preserve and inspect it')
        self.transport = transport or self.call

    def binding(self):
        # Never bind to secrets; include every authority/transaction limit.
        return hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()

    def save(self):
        data = json.dumps(self.state, sort_keys=True).encode()
        if len(data) > 128 * 1024 * 1024:
            raise ValueError('retention state full; backpressure, no entries discarded')
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def call(self, action, value=None):
        request = json.dumps({'config': self.config, 'action': action, 'value': value})
        result = subprocess.run(['node', str(Path(__file__).with_name('retention_transport.cjs'))],
                                input=request, text=True, capture_output=True, timeout=90)
        if result.returncode:
            # Transport output can contain provider details; keep diagnostics local/minimal.
            raise RuntimeError('retention transport failed')
        if len(result.stdout) > 64 * 1024 * 1024:
            raise RuntimeError('transport response exceeds limit')
        return json.loads(result.stdout)

    def archive_alerts(self, before_epoch, destination):
        # Explicit owner acknowledgment, including warnings never delivered.
        archived = {key: value for key, value in self.state['alerts'].items()
                    if int(key.rsplit(':', 1)[1]) < before_epoch}
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'binding': self.state['binding'], 'alerts': archived}, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(Path(destination).parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        for key in archived:
            del self.state['alerts'][key]
        self.state.pop('notifier', None)
        self.save()

    def notify(self, code, epoch, now):
        # Payload deliberately excludes blob IDs, fleet metadata and credentials.
        key = f'{code}:{epoch}'
        alerts = self.state['alerts']
        if key not in alerts and len(alerts) >= 256:
            self.state['notifier'] = 'backpressure: 256 alerts retained; owner must export/acknowledge'
            return
        item = alerts.setdefault(key, {'attempts': 0, 'status': 'queued', 'next': 0})
        if item['status'] == 'acknowledged' or item['attempts'] >= 3 or now < item['next']:
            return
        notifier = self.config.get('notifier')
        if not notifier:
            item['status'] = 'unconfigured; no alert delivery guarantee'
            return
        if not notifier['url'].startswith('https://'):
            raise ValueError('notifier requires HTTPS')
        secret_path = Path(notifier['secret_file'])
        descriptor = os.open(secret_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 4096:
                raise ValueError('notifier secret must be a bounded private regular file')
            secret = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        if len(secret) < 32:
            raise ValueError('notifier secret must contain at least 32 bytes')
        payload = json.dumps({'schema': 1, 'id': hashlib.sha256(f'{self.state["binding"]}:{key}'.encode()).hexdigest(),
                              'code': code, 'epoch': epoch, 'timestamp': int(now)}, separators=(',', ':')).encode()
        item.update(attempts=item['attempts'] + 1, status='attempted', next=now + 60 * 2 ** item['attempts'])
        self.save()
        request = urllib.request.Request(notifier['url'], data=payload, method='POST', headers={
            'Content-Type': 'application/json', 'X-SuiFleet-Signature': hmac.new(secret, payload, hashlib.sha256).hexdigest()})
        # Do not follow redirects carrying the authenticated payload elsewhere.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(
                    context=ssl.create_default_context(cafile=notifier.get("ca_file")))).open(request, timeout=10) as response:
                item['status'] = 'acknowledged' if 200 <= response.status < 300 else 'failed'
        except Exception:
            item['status'] = 'failed'

    def tick(self, now=None):
        now = time.time() if now is None else now
        snapshot = self.transport('snapshot')
        blobs = snapshot['blobs']
        if len(blobs) > 100_000:
            raise ValueError('blob limit exceeded; no work discarded')
        self.state.pop('worker_error', None)
        self.state['snapshot'] = snapshot
        for code in snapshot.get('warnings', []):
            self.notify(code, snapshot['epoch'], now)
        for blob in blobs:
            key = blob['id']
            target = blob['target']
            if blob['end'] >= target:
                pending = self.state['pending'].get(key)
                if pending:
                    # Authoritative new expiry alone cannot prove this transaction succeeded.
                    result = self.transport('receipt', pending)
                    if result['status'] == 'success':
                        self.state['last_success'][key] = {'digest': pending['digest'], 'end': blob['end'], 'epoch': snapshot['epoch']}
                        del self.state['pending'][key]
                        self.state['failures'].pop(key, None)
                    elif result['status'] == 'failed':
                        del self.state['pending'][key]
                continue
            if snapshot['epoch'] >= blob['end']:
                self.state['failures'][key] = 'expired: verified customer copy and owner-authorized restore required'
                self.notify('blob_expired', snapshot['epoch'], now)
                continue
            if blob['end'] - snapshot['epoch'] > snapshot['margin']:
                continue
            attempt = self.state['attempts'].setdefault(key, {'count': 0, 'next': 0, 'target': target})
            # New targets require deliberate authorization, but cannot clear uncertain intent.
            if attempt['target'] != target and key not in self.state['pending']:
                attempt.update(count=0, prepared=0, next=0, target=target)
            pending = self.state['pending'].get(key)
            attempt.setdefault('prepared', max(1, attempt['count']) if pending else attempt['count'])
            if attempt['count'] >= 3:
                self.notify('renewal_retry_exhausted', snapshot['epoch'], now)
            if now < attempt['next']:
                continue
            if not pending and attempt.get('prepared', attempt['count']) >= 3:
                continue
            if pending:
                # Receipt outages must not discard intent or bypass retry pacing.
                attempt['next'] = now + min(3600, 60 * 2 ** min(attempt['count'], 6))
                self.save()
                receipt = self.transport('receipt', pending)
                if receipt['status'] == 'failed':
                    del self.state['pending'][key]
                    self.state['failures'][key] = 'renewal failed; bounded retry'
                    self.notify('renewal_failed', snapshot['epoch'], now)
                    self.save()
                    continue
                if receipt['status'] == 'success':
                    # Effects success without expected expiry is a hard discrepancy.
                    self.state['failures'][key] = 'effects/expiry discrepancy; owner inspection required'
                    continue
            else:
                gas = int(self.config['gas_budget'])
                if self.state['spent_ceiling'] + gas > int(self.config['spend_ceiling']):
                    self.state['failures'][key] = 'executor gas allowance exhausted'
                    self.notify('executor_exhausted', snapshot['epoch'], now)
                    continue
                if not blob.get('affordable', False):
                    self.state['failures'][key] = 'retention reserve or epoch allowance exhausted'
                    self.notify('retention_low', snapshot['epoch'], now)
                    continue
                pending = self.transport('prepare', {'blob': key, 'end': blob['end'], 'revision': snapshot['revision']})
                attempt['prepared'] = attempt.get('prepared', attempt['count']) + 1
                self.state['pending'][key] = pending
                self.state['spent_ceiling'] += gas  # refunds/top-ups never replenish run ceiling
            attempt.update(count=attempt['count'] + 1, next=now + min(3600, 60 * 2 ** min(attempt['count'], 6)))
            self.save()  # intent and same signed bytes durable before sending
            try:
                self.transport('submit', pending)
            except Exception:
                self.state['failures'][key] = 'submission uncertain; reconcile original digest only'
                self.notify('renewal_uncertain', snapshot['epoch'], now)
            self.save()
        self.save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--archive-alerts-before', type=int)
    parser.add_argument('--alert-export', type=Path)
    args = parser.parse_args()
    if (args.archive_alerts_before is None) != (args.alert_export is None):
        parser.error('archive requires both --archive-alerts-before and --alert-export')
    worker = Worker(json.loads(args.config.read_text()), args.state_dir)
    if args.alert_export is not None:
        worker.archive_alerts(args.archive_alerts_before, args.alert_export)
        return
    while True:
        try:
            worker.tick()
        except Exception as error:
            worker.state['worker_error'] = type(error).__name__
            try:
                worker.notify('executor_outage', worker.state.get('snapshot', {}).get('epoch', 0), time.time())
            except Exception:
                worker.state['notifier'] = 'failed: inspect private notifier configuration'
            worker.save()
            if args.once:
                raise
        if args.once:
            return
        time.sleep(30)


if __name__ == '__main__':
    main()
