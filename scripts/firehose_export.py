#!/usr/bin/env python3
"""Clean-room Firehose exporter v1. Platform-managed, scoped SF-057 credentials."""
import argparse
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))
from suifleet_ipc import Client, IpcError

MAX_RECORD = 1000 * 1024
MAX_BATCH = 4 * 1024 * 1024


def config(value):
    required = {'version', 'socket', 'component', 'stream', 'region', 'delivery_stream', 'state_dir'}
    if set(value) != required or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('invalid configuration/version')
    if any(not isinstance(value[k], str) or not value[k] for k in required - {'version'}):
        raise ValueError('invalid configuration value')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value['delivery_stream']):
        raise ValueError('invalid delivery stream')
    if not re.fullmatch(r'[a-z0-9-]+', value['region']):
        raise ValueError('invalid region')
    if not Path(value['socket']).is_absolute() or not Path(value['state_dir']).is_absolute():
        raise ValueError('paths must be absolute')
    return value


def aws_client(cfg):
    # Use ONLY the scoped container provider, never ambient profiles/static keys.
    # The pinned SDK logs refresh exceptions at WARNING with raw provider text.
    # This executable emits only its own bounded, redacted diagnostics.
    sdk_logger = logging.getLogger('botocore')
    sdk_logger.addHandler(logging.NullHandler())
    sdk_logger.propagate = False
    sdk_logger.setLevel(logging.CRITICAL + 1)
    import botocore.session
    from botocore.config import Config
    from botocore.credentials import ContainerProvider
    if not os.environ.get('AWS_CONTAINER_CREDENTIALS_FULL_URI') or not os.environ.get('AWS_CONTAINER_AUTHORIZATION_TOKEN'):
        raise ValueError('scoped credentials required')
    credentials = ContainerProvider(environ={
        key: os.environ[key] for key in ('AWS_CONTAINER_CREDENTIALS_FULL_URI',
                                       'AWS_CONTAINER_AUTHORIZATION_TOKEN')}).load()
    if credentials is None:
        raise ValueError('scoped credentials required')
    session = botocore.session.get_session()
    session._credentials = credentials
    return session.create_client('firehose', region_name=cfg['region'],
                                 api_version='2015-08-04',
                                 config=Config(connect_timeout=5, read_timeout=20,
                                               retries={'total_max_attempts': 1},
                                               ignore_configured_endpoint_urls=True))


class Exporter:
    def __init__(self, cfg, ipc, firehose):
        self.cfg, self.ipc, self.firehose = config(cfg), ipc, firehose
        self.directory = Path(cfg['state_dir'])
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.directory.lstat()
        if self.directory.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError('state directory must be private and owned')
        self.lock = open(self.directory / 'lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path = self.directory / 'checkpoint.json'
            self.binding = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
            self.checkpoint = -1
            if self.path.exists():
                state = json.loads(self.path.read_text())
                if (set(state) != {'version', 'binding', 'checkpoint'} or type(state['version']) is not int or state['version'] != 1
                        or state['binding'] != self.binding or type(state['checkpoint']) is not int
                        or state['checkpoint'] < -1):
                    raise ValueError('checkpoint mismatch/corruption')
                self.checkpoint = state['checkpoint']
        except Exception:
            self.lock.close()
            raise

    def close(self):
        self.lock.close()

    def save(self, checkpoint):
        fd, name = tempfile.mkstemp(dir=self.directory, prefix='.checkpoint-')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(dict(version=1, binding=self.binding, checkpoint=checkpoint), output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
            directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.checkpoint = checkpoint
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def step(self):
        description = self.ipc.stream_describe(self.cfg['stream'])
        if description['persistence'] != 'File' or description.get('export') is not None:
            raise ValueError('requires file stream without native export')
        # A missing retained prefix must never be silently acknowledged. Sequence
        # gaps after crashes are also explicit operator reconciliation events.
        batch, size, cursor = [], 0, self.checkpoint + 1
        while len(batch) < 500:
            try:
                messages = self.ipc.stream_read(self.cfg['stream'], start=cursor)
            except IpcError as error:
                if str(error) == 'not-enough-messages':
                    break
                raise
            message = messages[0]
            if message['sequence'] != cursor:
                raise ValueError('stream gap: reconcile retained data before resuming')
            # Version 1 is newline-delimited compact JSON, not raw/binary IPC.
            data = (json.dumps(message['payload'], ensure_ascii=False, separators=(',', ':'),
                               allow_nan=False) + '\n').encode()
            if len(data) > MAX_RECORD:
                raise ValueError('record too large')
            if size + len(data) > MAX_BATCH:
                break
            batch.append({'Data': data})
            size += len(data)
            cursor += 1
        if not batch:
            return 'idle'
        response = self.firehose.put_record_batch(DeliveryStreamName=self.cfg['delivery_stream'], Records=batch)
        results, failed = response.get('RequestResponses'), response.get('FailedPutCount')
        if not isinstance(results, list) or len(results) != len(batch) or type(failed) is not int:
            raise ValueError('ambiguous response')
        successes = []
        for result in results:
            if not isinstance(result, dict):
                raise ValueError('ambiguous record response')
            success = isinstance(result.get('RecordId'), str) and bool(result['RecordId']) and 'ErrorCode' not in result
            failure = isinstance(result.get('ErrorCode'), str) and bool(result['ErrorCode']) and 'RecordId' not in result
            if not (success or failure):
                raise ValueError('ambiguous record response')
            successes.append(success)
        if failed != successes.count(False):
            raise ValueError('inconsistent failure count')
        prefix = next((i for i, ok in enumerate(successes) if not ok), len(batch))
        if prefix:
            self.save(self.checkpoint + prefix)
        return 'partial' if failed else 'exported'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        cfg = config(json.loads(Path(args.config).read_text()))
        exporter = Exporter(cfg, Client(cfg['socket'], cfg['component']), aws_client(cfg))
    except Exception:
        sys.exit('Firehose startup rejected; inspect protected configuration/state/credentials')
    delay = 1
    try:
        while True:
            try:
                outcome = exporter.step()
                if outcome == 'partial':
                    raise RuntimeError('partial')
                delay = 1
                if outcome == 'idle':
                    time.sleep(1)
            except Exception:
                # Never echo SDK error text, payload, endpoint, paths or credentials.
                print('Firehose export retained; retry pending', file=sys.stderr, flush=True)
                time.sleep(delay)
                delay = min(60, delay * 2)
    finally:
        exporter.close()


if __name__ == '__main__':
    main()
