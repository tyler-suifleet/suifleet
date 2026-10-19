#!/usr/bin/env python3
"""Operator-configured durable Kinesis/S3 exporter. Run under suifleet_aws.py."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlparse

from suifleet_ipc import Client, IpcError


def encode(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True).encode()


def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as target:
        target.write(encode(value))
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate(config):
    required = {'service', 'region', 'stream', 'component', 'socket', 'state_dir',
                'destination', 'source_id', 'batch_messages', 'batch_bytes', 'bytes_per_second',
                'poll_seconds', 'max_backoff_seconds'}
    if not required <= config.keys() or config.keys() - required - {'endpoint', 'part_bytes'}:
        raise ValueError('invalid exporter configuration fields')
    if config['service'] not in ('kinesis', 's3'):
        raise ValueError('unsupported exporter service')
    for key in ('region', 'stream', 'component', 'destination', 'source_id'):
        if not isinstance(config[key], str) or not config[key] or len(config[key]) > 255:
            raise ValueError('invalid exporter identifier')
    for key in ('socket', 'state_dir'):
        if not isinstance(config[key], str) or not Path(config[key]).is_absolute():
            raise ValueError('exporter paths must be absolute')
    for key, low, high in [('batch_messages', 1, 500), ('batch_bytes', 1024, 16 << 20),
                           ('bytes_per_second', 1, 1 << 30), ('poll_seconds', 1, 3600),
                           ('max_backoff_seconds', 1, 3600)]:
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError('invalid exporter limit')
    if config['service'] == 'kinesis' and config['batch_bytes'] > 5 << 20:
        raise ValueError('Kinesis batch limit is conservatively 5 MiB')
    if config['service'] == 's3':
        # destination is bucket/prefix. Prefix must be exclusive to this exporter.
        bucket, separator, prefix = config['destination'].partition('/')
        if not separator or not bucket or not prefix or not prefix.endswith('/'):
            raise ValueError('S3 destination must be bucket/exclusive-prefix/')
        part = config.get('part_bytes', 5 << 20)
        if type(part) is not int or not 5 << 20 <= part <= 16 << 20:
            raise ValueError('part_bytes must be 5..16 MiB')
    if 'endpoint' in config:
        url = urlparse(config['endpoint'])
        if (url.username or url.password or url.query or url.fragment or
            url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')):
            raise ValueError('endpoint overrides are restricted to loopback HTTP emulators')


class Exporter:
    def __init__(self, config, source=None, aws=None, sleep=time.sleep):
        validate(config)
        self.config, self.sleep = config, sleep
        directory = Path(config['state_dir'])
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700 or directory.resolve() != directory):
            raise ValueError('state directory must be private, owned, and not symlinked')
        self.lock = open(directory / 'lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path = directory / 'checkpoint.json'
            self.source = source or Client(config['socket'], config['component'])
            binding = hashlib.sha256(encode(config)).hexdigest()
            self.state = json.loads(self.path.read_bytes()) if self.path.exists() else {
                'schema': 1, 'binding': binding, 'next': 0, 'pending': None}
            if self.state['schema'] != 1 or self.state['binding'] != binding:
                raise ValueError('checkpoint/config mismatch; drain before reconfiguration')
            # No SDK default credential-chain fallback: this service requires SF-057.
            if aws is None:
                import boto3
                from botocore.config import Config
                session = boto3.Session(region_name=config['region'])
                credentials = session.get_credentials()
                if credentials is None or credentials.method != 'container-role':
                    raise ValueError('scoped container credentials required')
                aws = session.client(config['service'], endpoint_url=config.get('endpoint'),
                                     config=Config(connect_timeout=5, read_timeout=30,
                                                   retries={'total_max_attempts': 1},
                                                   proxies={}, s3={'addressing_style': 'path'},
                                                   request_checksum_calculation='when_required',
                                                   response_checksum_validation='when_required'))
            self.aws = aws
        except BaseException:
            self.lock.close()
            raise

    def save(self):
        atomic(self.path, self.state)

    def snapshot(self):
        cfg = self.config
        description = self.source.stream_describe(cfg['stream'])
        if (not description.get('external_export') or description['persistence'] != 'File'
                or description['strategy_on_full'] != 'RejectNewData'
                or description['time_to_live_seconds'] is not None
                or description['corrupt_segments']):
            raise ValueError('source is not a lossless external-export stream')
        # New local state can safely resume a confirmed daemon checkpoint.
        self.state['next'] = max(self.state['next'], description['exported'] + 1)
        messages, total, cursor = [], 0, self.state['next']
        for _ in range(cfg['batch_messages']):
            try:
                result = self.source.stream_read(cfg['stream'], start=max(cursor, description['oldest']), max_messages=1)
            except IpcError as error:
                if str(error) == 'not-enough-messages':
                    break
                raise
            message = result[0]
            body = encode({'schema': 1, 'source_id': cfg['source_id'], 'stream': cfg['stream'],
                           'sequence_gap_before': message['sequence'] - cursor, **message})
            if cfg['service'] == 'kinesis':
                # Partition key is fixed per local stream; PutRecords has no ordering guarantee.
                size = len(body) + len(self.partition_key())
                if size > 1 << 20:
                    raise ValueError('record exceeds conservative Kinesis 1 MiB limit')
            else:
                size = len(body) + 1
            if size > cfg['batch_bytes']:
                raise ValueError('record exceeds configured batch_bytes; checkpoint unchanged')
            if total + size > cfg['batch_bytes']:
                break
            messages.append({'sequence': message['sequence'], 'body': base64.b64encode(body).decode()})
            total += size
            cursor = message['sequence'] + 1
        if not messages:
            return False
        self.state['pending'] = {'records': messages, 'done': [False] * len(messages),
                                 'delivered': False, 'parts': []}
        # Durable snapshot precedes any external mutation.
        self.save()
        return True

    def partition_key(self):
        return hashlib.sha256(encode([self.config['source_id'], self.config['stream']])).hexdigest()

    def throttle(self, size):
        # No unmetered SDK retries. Conservative payload pacing (no initial burst).
        self.sleep(size / self.config['bytes_per_second'])

    def kinesis(self, pending):
        remaining = [(i, base64.b64decode(record['body'])) for i, record in
                     enumerate(pending['records']) if not pending['done'][i]]
        if not remaining:
            return  # All records were persisted just before a previous process died.
        self.throttle(sum(len(body) + len(self.partition_key()) for _, body in remaining))
        response = self.aws.put_records(StreamName=self.config['destination'],
                                        Records=[{'Data': body, 'PartitionKey': self.partition_key()}
                                                 for _, body in remaining])
        results = response.get('Records', [])
        if len(results) != len(remaining):
            raise ValueError('malformed PutRecords result')
        failed = sum('ErrorCode' in result for result in results)
        if response.get('FailedRecordCount') != failed:
            raise ValueError('inconsistent PutRecords result')
        if any('ErrorCode' not in r and (not r.get('SequenceNumber') or not r.get('ShardId')) for r in results):
            raise ValueError('unconfirmed PutRecords result')
        for (index, _), result in zip(remaining, results):
            if 'ErrorCode' not in result:
                pending['done'][index] = True
        self.save()
        if failed:
            raise RuntimeError('Kinesis partial failure; unconfirmed records retained')

    def s3_key_body(self, pending):
        body = b''.join(base64.b64decode(record['body']) + b'\n' for record in pending['records'])
        digest = hashlib.sha256(body).hexdigest()
        bucket, prefix = self.config['destination'].split('/', 1)
        key = (prefix + self.partition_key() + '/' +
               str(pending['records'][0]['sequence']) + '-' +
               str(pending['records'][-1]['sequence']) + '-' + digest + '.jsonl')
        return bucket, key, body, digest

    def object_exists(self, bucket, key, body, digest):
        from botocore.exceptions import ClientError
        try:
            result = self.aws.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if error.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
                return False
            raise
        if result.get('ContentLength') != len(body) or result.get('Metadata', {}).get('sha256') != digest:
            raise ValueError('S3 object identity mismatch')
        return True

    def cleanup(self, bucket, key):
        # Handles a CreateMultipartUpload committed remotely whose response/save was lost.
        # Exclusive prefix + local flock are mandatory; never abort another exporter.
        paginator = self.aws.get_paginator('list_multipart_uploads')
        for page in paginator.paginate(Bucket=bucket, Prefix=key):
            for upload in page.get('Uploads', []):
                if upload['Key'] == key:
                    self.aws.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload['UploadId'])

    def s3(self, pending):
        from botocore.exceptions import ClientError
        bucket, key, body, digest = self.s3_key_body(pending)
        if self.object_exists(bucket, key, body, digest):
            self.cleanup(bucket, key)
            return
        if 'upload' not in pending:
            self.cleanup(bucket, key)
            response = self.aws.create_multipart_upload(
                Bucket=bucket, Key=key, ContentType='application/x-ndjson', Metadata={'sha256': digest})
            pending['upload'] = response['UploadId']
            self.save()
        common = dict(Bucket=bucket, Key=key, UploadId=pending['upload'])
        part_size = self.config.get('part_bytes', 5 << 20)
        try:
            for offset in range(len(pending['parts']) * part_size, len(body), part_size):
                part = body[offset:offset + part_size]
                number = offset // part_size + 1
                self.throttle(len(part))
                response = self.aws.upload_part(**common, PartNumber=number, Body=part)
                pending['parts'].append({'PartNumber': number, 'ETag': response['ETag']})
                self.save()
            self.aws.complete_multipart_upload(**common, MultipartUpload={'Parts': pending['parts']})
        except ClientError as error:
            if error.response['Error']['Code'] == 'NoSuchUpload':
                # Lifecycle/operator abort or expired upload; retain snapshot and restart.
                pending.pop('upload', None)
                pending['parts'] = []
                self.save()
            raise
        # Independently verify completed object even if completion returned success.
        if not self.object_exists(bucket, key, body, digest):
            raise RuntimeError('S3 completion not visible')

    def abort(self):
        pending = self.state['pending']
        if pending and self.config['service'] == 's3':
            bucket, key, _, _ = self.s3_key_body(pending)
            self.cleanup(bucket, key)
            pending.pop('upload', None)
            pending['parts'] = []
            self.save()
        # Never advances checkpoint or drops snapshot. A later start resumes.

    def step(self):
        pending = self.state['pending']
        if pending is None:
            if not self.snapshot():
                return False
            pending = self.state['pending']
        self.save()  # Retry any previous failed snapshot/fsync before cloud mutations.
        if not pending['delivered']:
            if self.config['service'] == 'kinesis':
                self.kinesis(pending)
            else:
                self.s3(pending)
            pending['delivered'] = True
            self.save()
        last = pending['records'][-1]['sequence']
        # Delivered marker persists before the local ack. Retrying an ambiguous
        # ack cannot cause a cloud resend; local ack is monotonic/idempotent.
        self.source.stream_export_ack(self.config['stream'], last)
        self.state['next'] = last + 1
        self.state['pending'] = None
        self.save()
        return True

    def run(self):
        backoff = 1
        while True:
            try:
                delivered = self.step()
                backoff = 1
                if not delivered:
                    self.sleep(self.config['poll_seconds'])
            except Exception:
                # No SDK exceptions/URLs/payloads/credential strings in logs.
                print('stream export failed; pending data retained', flush=True)
                self.sleep(backoff)
                backoff = min(backoff * 2, self.config['max_backoff_seconds'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--abort', action='store_true', help='abort pending S3 uploads, retain data/checkpoint')
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_bytes())
        exporter = Exporter(config)
        exporter.abort() if args.abort else exporter.run()
    except Exception:
        parser.exit(1, 'stream exporter startup/cleanup failed; inspect protected configuration/state\n')


if __name__ == '__main__':
    main()
