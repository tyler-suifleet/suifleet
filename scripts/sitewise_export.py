#!/usr/bin/env python3
"""Native SiteWise TQV publisher v1; clean-room, SF-034 IPC / SF-057 credentials."""
import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))
from suifleet_ipc import Client, IpcError

SCHEMA = 1
INTEGER_BITS = {'sbyte': (8, True), 'byte': (8, False), 'int16': (16, True), 'uint16': (16, False),
                'int32': (32, True), 'uint32': (32, False), 'int64': (64, True), 'uint64': (64, False)}
NUMERIC = {'sbyte', 'byte', 'int16', 'uint16', 'int32', 'uint32', 'int64', 'uint64', 'float32', 'float64', 'double', 'float'}

class ExportError(ValueError):
    """Fixed payload-free diagnostic."""


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def config(value):
    required = {'version', 'socket', 'component', 'stream', 'region', 'state_dir', 'mappings'}
    if not isinstance(value, dict) or set(value) != required or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('invalid configuration/version')
    if any(not isinstance(value[k], str) or not value[k] for k in required - {'version', 'mappings'}):
        raise ValueError('invalid configuration value')
    if not re.fullmatch(r'[a-z0-9-]+', value['region']):
        raise ValueError('invalid region')
    if not Path(value['socket']).is_absolute() or not Path(value['state_dir']).is_absolute():
        raise ValueError('absolute paths required')
    if not isinstance(value['mappings'], list) or not 1 <= len(value['mappings']) <= 256:
        raise ValueError('invalid mappings')
    seen = set()
    for m in value['mappings']:
        if not isinstance(m, dict) or set(m) - {'source', 'point', 'propertyAlias', 'assetId', 'propertyId', 'valueType', 'scale', 'offset'}:
            raise ValueError('invalid mapping')
        if any(not isinstance(m.get(k), str) or not m[k] for k in ('source', 'point')):
            raise ValueError('invalid source/point')
        if m.get('valueType') not in ('D', 'I', 'B', 'S'):
            raise ValueError('invalid mapping valueType')
        if ('scale' in m or 'offset' in m) and m['valueType'] not in ('D', 'I'):
            raise ValueError('non-numeric transformation')
        key = (m['source'], m['point'])
        if key in seen:
            raise ValueError('duplicate mapping')
        seen.add(key)
        if 'propertyAlias' in m:
            alias = m['propertyAlias']
            if ('assetId' in m or 'propertyId' in m or not isinstance(alias, str)
                    or not 1 <= len(alias) <= 1000 or any(ord(c) < 32 or ord(c) == 127 for c in alias)):
                raise ValueError('invalid alias')
        else:
            for k in ('assetId', 'propertyId'):
                if (not isinstance(m.get(k), str) or not re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', m[k])
                        or m[k] == '00000000-0000-0000-0000-000000000000'):
                    raise ValueError('invalid property identity')
        if any(not finite(m.get(k, default)) for k, default in [('scale', 1), ('offset', 0)]):
            raise ValueError('invalid transformation')
    return value


def aws_client(cfg):
    logger = logging.getLogger('botocore')
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL + 1)
    import botocore.session
    from botocore.config import Config
    from botocore.credentials import ContainerProvider
    keys = ('AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_CONTAINER_AUTHORIZATION_TOKEN')
    if any(not os.environ.get(k) for k in keys):
        raise ValueError('scoped credentials required')
    credentials = ContainerProvider(environ={k: os.environ[k] for k in keys}).load()
    if credentials is None:
        raise ValueError('scoped credentials required')
    session = botocore.session.get_session()
    session._credentials = credentials
    return session.create_client('iotsitewise', region_name=cfg['region'], api_version='2019-12-02',
        config=Config(connect_timeout=5, read_timeout=20, retries={'total_max_attempts': 1},
                      ignore_configured_endpoint_urls=True))


class Exporter:
    def __init__(self, cfg, ipc, sitewise):
        self.cfg, self.ipc, self.sitewise = config(cfg), ipc, sitewise
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

    def entries(self, payload):
        if (not isinstance(payload, dict) or type(payload.get('schema')) is not int or payload.get('schema') != SCHEMA
                or not isinstance(payload.get('records'), list) or not 1 <= len(payload['records']) <= 1024):
            raise ExportError('invalid-payload')
        entries = []
        for record in payload['records']:
            # Status/gap events cannot be mistaken for successful delivery.
            if not isinstance(record, dict) or record.get('kind') not in ('sample', 'status'):
                raise ExportError('non-sample-record')
            if record['kind'] == 'status':
                continue  # Control-plane status has no SiteWise TQV; gaps fail closed.
            mapping = next((m for m in self.cfg['mappings'] if m['source'] == record.get('source')
                            and m['point'] == record.get('point')), None)
            if mapping is None:
                raise ExportError('unmapped-record')
            quality, ns, value = record.get('quality'), record.get('timestamp_ns'), record.get('value')
            if quality not in ('GOOD', 'BAD', 'UNCERTAIN') or type(ns) is not int or not 1000000000 <= ns <= 9223372036854774999999999:
                raise ExportError('invalid-timestamp-quality')
            # Preserve source timestamps, including expired data: let service refusal
            # retain the record; never retimestamp an outage backlog to make it pass.
            typ = record.get('type')
            destination = mapping['valueType']
            if value is None:
                # OPC UA disconnects can lack a source type; the operator's
                # destination declaration remains authoritative for nulls.
                # A GOOD source status does not make an absent scalar valid.
                # Real OPC UA null String/unset DateTime samples reach here.
                if quality == 'GOOD':
                    quality = 'BAD'
                variant = {'nullValue': {'valueType': destination}}
            elif destination == 'B' and typ in ('bool', 'boolean') and type(value) is bool:
                variant = {'booleanValue': value}
            elif destination == 'S' and (
                    (typ == 'string' and isinstance(value, str))
                    or (typ == 'datetime' and type(value) is int)):
                # SF-070 emits datetime as integer Unix nanoseconds. Preserve
                # them exactly as decimal text instead of rounding to double.
                value = str(value)
                if len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                    raise ExportError('invalid-string-value')
                variant = {'stringValue': value}
            elif destination in ('D', 'I') and typ in NUMERIC and finite(value):
                if type(value) is int and abs(value) > 2**53:
                    raise ExportError('invalid-numeric-value')
                if typ in INTEGER_BITS:
                    bits, signed = INTEGER_BITS[typ]
                    low, high = (-(2**(bits-1)), 2**(bits-1)-1) if signed else (0, 2**bits-1)
                    if type(value) is not int or not low <= value <= high:
                        raise ExportError('invalid-integer-value')
                try:
                    result = value * mapping.get('scale', 1) + mapping.get('offset', 0)
                    if not finite(result):
                        raise ValueError()
                    if destination == 'I':
                        if not -(2**31) <= result <= 2**31-1 or int(result) != result:
                            raise ValueError()
                        variant = {'integerValue': int(result)}
                    else:
                        if not -10**10 <= result <= 10**10:
                            raise ValueError()
                        variant = {'doubleValue': float(result)}
                except (OverflowError, ValueError):
                    raise ExportError('invalid-numeric-value') from None
            else:
                raise ExportError('invalid-value-type')
            entry = {k: mapping[k] for k in ('propertyAlias', 'assetId', 'propertyId') if k in mapping}
            entry.update(entryId=str(len(entries)), propertyValues=[dict(value=variant, quality=quality,
                         timestamp=dict(timeInSeconds=ns // 1000000000, offsetInNanos=ns % 1000000000))])
            entries.append(entry)
        return entries

    def step(self):
        description = self.ipc.stream_describe(self.cfg['stream'])
        if (description['persistence'] != 'File' or description.get('export') is not None
                or not description.get('external_export') or description.get('strategy_on_full') != 'RejectNewData'
                or description.get('time_to_live_seconds') is not None):
            raise ExportError('requires-lossless-external-stream')
        if description['corrupt_segments']:
            raise ExportError('corrupt-stream')
        if description['exported'] > self.checkpoint:
            raise ExportError('checkpoint-behind-source')
        # Local durable delivery checkpoint precedes retirement. Repair a lost ack
        # before reading any new data; never resend a confirmed message for ack loss.
        if self.checkpoint > description['exported']:
            self.ipc.stream_export_ack(self.cfg['stream'], self.checkpoint)
        # This operator-only stream forbids TTL/overwrite; retired messages were
        # acknowledged above. Oldest may skip unused crash-reservation numbers.
        cursor = max(self.checkpoint + 1, description['oldest'])
        try:
            messages = self.ipc.stream_read(self.cfg['stream'], start=cursor)
        except IpcError as error:
            if str(error) == 'not-enough-messages':
                return 'idle'
            raise
        message = messages[0]
        sequence = message['sequence']
        if type(sequence) is not int or sequence <= self.checkpoint:
            raise ExportError('invalid-sequence')
        # Validate the whole source message before any delivery. One source message
        # is the atomic checkpoint unit; successful chunks may repeat after failure.
        entries = self.entries(message.get('payload'))
        for offset in range(0, len(entries), 10):
            batch = entries[offset:offset + 10]
            response = self.sitewise.batch_put_asset_property_value(entries=batch, enablePartialEntryProcessing=False)
            errors = response.get('errorEntries')
            if not isinstance(errors, list):
                raise ExportError('ambiguous-response')
            ids = {entry['entryId'] for entry in batch}
            seen = set()
            for error in errors:
                if (not isinstance(error, dict) or error.get('entryId') not in ids
                        or error['entryId'] in seen or not isinstance(error.get('errors'), list) or not error['errors']):
                    raise ExportError('ambiguous-response')
                seen.add(error['entryId'])
                if any(not isinstance(e, dict) or not isinstance(e.get('errorCode'), str) or not e['errorCode'] for e in error['errors']):
                    raise ExportError('ambiguous-response')
            if errors:
                return 'partial'
        self.save(sequence)
        self.ipc.stream_export_ack(self.cfg['stream'], sequence)
        return 'exported'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        cfg = config(json.loads(Path(args.config).read_text()))
        exporter = Exporter(cfg, Client(cfg['socket'], cfg['component']), aws_client(cfg))
    except Exception:
        sys.exit('SiteWise startup rejected; inspect protected configuration/state/credentials')
    delay = 1
    try:
        while True:
            try:
                result = exporter.step()
                if result == 'partial':
                    raise ExportError('rejected-record')
                delay = 1
                if result == 'idle':
                    time.sleep(1)
            except Exception as error:
                code = str(error) if isinstance(error, ExportError) else 'retry-pending'
                print('SiteWise export retained; ' + code, file=sys.stderr, flush=True)
                time.sleep(delay)
                delay = min(60, delay * 2)
    finally:
        exporter.close()

if __name__ == '__main__':
    main()
