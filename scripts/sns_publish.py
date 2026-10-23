#!/usr/bin/env python3
"""Clean-room SNS publisher v1. Platform-managed, scoped SF-057 credentials.

Reads SNS publish requests from a durable QoS 1 local pub/sub subscription
(SF-032), enforces per-sender topic grants and publishes with SNS Publish.
"""
import argparse
import base64
import binascii
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

CONTRACT_VERSION = 1
MAX_MESSAGE = 256 * 1024
MAX_ATTRIBUTES = 10
TOPIC_ARN = re.compile(r'arn:aws(?:-[a-z]+)*:sns:([a-z0-9-]+):[0-9]{12}:([A-Za-z0-9_-]{1,251})(\.fifo)?')
ATTRIBUTE_NAME = re.compile(r'[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,254})')
ATTRIBUTE_TYPES = ('String', 'Number', 'String.Array', 'Binary')
# Fixed SNS error codes that retrying the same request cannot fix. Everything
# else (throttling, internal errors, IAM/credential failures) is retried.
PERMANENT = frozenset({'InvalidParameter', 'InvalidParameterValue', 'ParameterValueInvalid',
                       'ValidationError', 'Validation', 'ValidationException', 'NotFound',
                       'EndpointDisabled', 'PlatformApplicationDisabled', 'KMSDisabled',
                       'KMSInvalidState', 'KMSNotFound', 'KMSOptInRequired'})
# Failures before a request reaches the service cannot have published.
UNSENT = ('NoCredentialsError', 'CredentialRetrievalError', 'PartialCredentialsError',
          'EndpointConnectionError', 'ConnectTimeoutError', 'UnknownCredentialError')
REQUEST_KEYS = {'message', 'subject', 'sns_topic_arn', 'message_structure',
                'message_attributes', 'message_group_id', 'message_deduplication_id'}


def valid_topic(topic, wildcards):
    if not isinstance(topic, str) or not topic or len(topic.encode()) > 256 or '\0' in topic:
        return False
    levels = topic.split('/')
    for index, level in enumerate(levels):
        if not wildcards and ('+' in level or '#' in level):
            return False
        if ('+' in level and level != '+') or ('#' in level and (level != '#' or index != len(levels) - 1)):
            return False
    return True


def matches(pattern, topic):
    """MQTT 3.1.1 section 4.7 filter matching (both arguments valid)."""
    if pattern[:1] in ('+', '#') and topic.startswith('$'):
        return False
    filters, levels = pattern.split('/'), topic.split('/')
    for index, level in enumerate(filters):
        if level == '#':
            return True
        if index >= len(levels) or (level != '+' and level != levels[index]):
            return False
    return len(filters) == len(levels)


def topic_arn(value, region):
    match = TOPIC_ARN.fullmatch(value) if isinstance(value, str) else None
    # SNS Publish only reaches topics in the client's own Region.
    return match is not None and match.group(1) == region


def config(value):
    required = {'version', 'socket', 'component', 'region', 'request_topic', 'status_topic',
                'default_topic_arn', 'grants', 'max_attempts', 'max_publishes_per_second', 'state_dir'}
    if not isinstance(value, dict) or set(value) != required or type(value['version']) is not int \
            or value['version'] != CONTRACT_VERSION:
        raise ValueError('invalid configuration/version')
    if any(not isinstance(value[k], str) or not value[k] for k in ('socket', 'component', 'region', 'state_dir')):
        raise ValueError('invalid configuration value')
    if not re.fullmatch(r'[a-z0-9-]+', value['region']):
        raise ValueError('invalid region')
    if not Path(value['socket']).is_absolute() or not Path(value['state_dir']).is_absolute():
        raise ValueError('paths must be absolute')
    if not valid_topic(value['request_topic'], wildcards=True):
        raise ValueError('invalid request topic')
    status = value['status_topic']
    if status is not None and (not valid_topic(status, wildcards=False) or matches(value['request_topic'], status)):
        raise ValueError('invalid status topic')
    if value['default_topic_arn'] is not None and not topic_arn(value['default_topic_arn'], value['region']):
        raise ValueError('invalid default topic')
    grants = value['grants']
    if not isinstance(grants, dict) or not grants or any(
            not isinstance(sender, str) or not sender or not isinstance(arns, list) or not arns
            or len(set(arns)) != len(arns) or not all(topic_arn(arn, value['region']) for arn in arns)
            for sender, arns in grants.items()):
        raise ValueError('invalid grants')
    if type(value['max_attempts']) is not int or not 1 <= value['max_attempts'] <= 100:
        raise ValueError('invalid max attempts')
    rate = value['max_publishes_per_second']
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0.01 <= rate <= 1000:
        raise ValueError('invalid publish rate')
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
    return session.create_client('sns', region_name=cfg['region'], api_version='2010-03-31',
                                 config=Config(connect_timeout=5, read_timeout=20,
                                               retries={'total_max_attempts': 1},
                                               ignore_configured_endpoint_urls=True))


class PublishError(ValueError):
    """Only fixed, payload-free diagnostic codes may reach the service log."""


class Rejected(Exception):
    """A request that can never be published; reported, then acknowledged."""

    def __init__(self, code, text):
        super().__init__(code)
        self.code, self.text = code, text


def diagnostic(error):
    if isinstance(error, PublishError):
        return str(error)
    return 'retry-pending'


def printable(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and all(33 <= ord(c) <= 126 for c in value)


def utf8(value):
    try:
        return value.encode('utf-8')
    except UnicodeEncodeError:
        raise Rejected('InvalidInputException', 'text is not valid UTF-8') from None


def unique_object(text):
    def pairs(items):
        keys = [key for key, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate key')
        return dict(items)
    value = json.loads(text, object_pairs_hook=pairs)
    if not isinstance(value, dict) or not isinstance(value.get('default'), str):
        raise ValueError('json message structure')
    return value


def correlation(payload):
    """The caller's request id, copied to the status when well formed."""
    value = payload.get('id') if isinstance(payload, dict) else None
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


def prepare(cfg, message):
    """Validate one received pub/sub message; return (request id, Publish arguments).

    Every check, including the sender's topic grant, precedes network I/O."""
    payload = message.get('payload')
    request_id = correlation(payload)
    request = payload.get('request') if isinstance(payload, dict) else None
    if not isinstance(request, dict):
        raise Rejected('InvalidInputException', 'request envelope is invalid')
    target = request.get('sns_topic_arn', cfg['default_topic_arn'])
    # The authenticated SO_PEERCRED sender, never a payload field, is the
    # caller. Denial precedes all other processing of its content.
    sender = message.get('sender')
    if not isinstance(sender, str) or not isinstance(target, str) or target not in cfg['grants'].get(sender, ()):
        raise Rejected('AccessDenied', 'sender is not granted this topic')
    if not set(payload) <= {'version', 'id', 'request'} or request_id is None:
        raise Rejected('InvalidInputException', 'request envelope is invalid')
    if 'version' in payload and (type(payload['version']) is not int or payload['version'] != CONTRACT_VERSION):
        raise Rejected('UnsupportedVersion', 'request version is unsupported')
    if not set(request) <= REQUEST_KEYS:
        raise Rejected('InvalidInputException', 'request fields are invalid')
    text = request.get('message')
    if not isinstance(text, str) or not text:
        raise Rejected('InvalidInputException', 'message is required')
    size = len(utf8(text))
    kwargs = dict(TopicArn=target, Message=text)
    if 'subject' in request:
        subject = request['subject']
        if not isinstance(subject, str) or not 0 < len(subject) < 100 or \
                any(ord(c) < 32 or ord(c) == 127 for c in subject) or not subject[0].isprintable() or subject[0] == ' ':
            raise Rejected('InvalidInputException', 'subject is invalid')
        utf8(subject)
        kwargs['Subject'] = subject
    structure = request.get('message_structure')
    if 'message_structure' in request:
        if structure != 'json':
            raise Rejected('InvalidInputException', 'message structure is invalid')
        try:
            unique_object(text)
        except ValueError:
            raise Rejected('InvalidInputException', 'json message needs a string default') from None
        kwargs['MessageStructure'] = 'json'
    if 'message_attributes' in request:
        attributes = request['message_attributes']
        # SNS forwards attributes only with string message structure.
        if structure == 'json' or not isinstance(attributes, dict) or not 0 < len(attributes) <= MAX_ATTRIBUTES:
            raise Rejected('InvalidInputException', 'message attributes are invalid')
        converted = {}
        for name, attribute in attributes.items():
            if not isinstance(name, str) or not ATTRIBUTE_NAME.fullmatch(name) or name.endswith('.') \
                    or '..' in name or name.lower().startswith(('aws.', 'amazon.')):
                raise Rejected('InvalidInputException', 'message attribute name is invalid')
            if not isinstance(attribute, dict) or attribute.get('data_type') not in ATTRIBUTE_TYPES:
                raise Rejected('InvalidInputException', 'message attribute type is invalid')
            kind = attribute['data_type']
            size += len(name) + len(kind)
            if kind == 'Binary':
                if set(attribute) != {'data_type', 'binary_value'} or not isinstance(attribute['binary_value'], str):
                    raise Rejected('InvalidInputException', 'binary attribute is invalid')
                try:
                    data = base64.b64decode(attribute['binary_value'], validate=True)
                except (binascii.Error, ValueError):
                    raise Rejected('InvalidInputException', 'binary attribute is invalid') from None
                if not data:
                    raise Rejected('InvalidInputException', 'binary attribute is invalid')
                converted[name] = dict(DataType=kind, BinaryValue=data)
                size += len(data)
                continue
            value = attribute.get('string_value')
            if set(attribute) != {'data_type', 'string_value'} or not isinstance(value, str) or not value:
                raise Rejected('InvalidInputException', 'string attribute is invalid')
            if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                raise Rejected('InvalidInputException', 'string attribute is invalid')
            if kind == 'Number':
                try:
                    number = float(value)
                except ValueError:
                    number = math.nan
                if not re.fullmatch(r'-?[0-9]+(\.[0-9]{1,5})?', value) or not -1e9 <= number <= 1e9:
                    raise Rejected('InvalidInputException', 'number attribute is invalid')
            if kind == 'String.Array':
                try:
                    array = json.loads(value)
                except ValueError:
                    array = None
                if not isinstance(array, list):
                    raise Rejected('InvalidInputException', 'array attribute is invalid')
            converted[name] = dict(DataType=kind, StringValue=value)
            size += len(utf8(value))
        kwargs['MessageAttributes'] = converted
    fifo = target.endswith('.fifo')
    if 'message_group_id' in request:
        if not printable(request['message_group_id'], 128):
            raise Rejected('InvalidInputException', 'message group id is invalid')
        kwargs['MessageGroupId'] = request['message_group_id']
    elif fifo:
        raise Rejected('InvalidInputException', 'FIFO topics need a message group id')
    if 'message_deduplication_id' in request:
        if not fifo or not printable(request['message_deduplication_id'], 128):
            raise Rejected('InvalidInputException', 'message deduplication id is invalid')
        kwargs['MessageDeduplicationId'] = request['message_deduplication_id']
    if size > MAX_MESSAGE:
        raise Rejected('InvalidInputException', 'message exceeds 256 KiB')
    return request_id, kwargs


class Publisher:
    def __init__(self, cfg, ipc, sns, clock=time.monotonic):
        self.cfg, self.ipc, self.sns, self.clock = config(cfg), ipc, sns, clock
        self.directory = Path(cfg['state_dir'])
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.directory.lstat()
        if self.directory.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError('state directory must be private and owned')
        self.lock = open(self.directory / 'lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path = self.directory / 'ledger.json'
            self.binding = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
            self.state = dict(completed=-1, status=None, inflight=None)
            if self.path.exists():
                state = json.loads(self.path.read_text())
                if not self.valid(state):
                    raise ValueError('ledger mismatch/corruption')
                self.state = {k: state[k] for k in ('completed', 'status', 'inflight')}
            # Durable and idempotent: the subscription and its queue survive restarts.
            self.ipc.subscribe(cfg['request_topic'], qos=1)
        except Exception:
            self.lock.close()
            raise
        self.tokens, self.refilled = self.burst, self.clock()

    @property
    def burst(self):
        return max(1.0, float(self.cfg['max_publishes_per_second']))

    def valid(self, state):
        if not isinstance(state, dict) or set(state) != {'version', 'binding', 'completed', 'status', 'inflight'}:
            return False
        if type(state['version']) is not int or state['version'] != 1 or state['binding'] != self.binding:
            return False
        if type(state['completed']) is not int or state['completed'] < -1:
            return False
        if state['status'] is not None and not isinstance(state['status'], dict):
            return False
        inflight = state['inflight']
        return inflight is None or (isinstance(inflight, dict) and set(inflight) == {'id', 'attempts', 'ambiguous'}
                                    and type(inflight['id']) is int and inflight['id'] > state['completed']
                                    and type(inflight['attempts']) is int and inflight['attempts'] >= 1
                                    and type(inflight['ambiguous']) is bool)

    def close(self):
        self.lock.close()

    def save(self, **changes):
        state = dict(self.state, **changes)
        fd, name = tempfile.mkstemp(dir=self.directory, prefix='.ledger-')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(dict(state, version=1, binding=self.binding), output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
            directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.state = state
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def wait(self):
        """Seconds until the local publish-rate bucket holds a token again."""
        return max(0.01, (1 - self.tokens) / float(self.cfg['max_publishes_per_second']))

    def take_token(self):
        now = self.clock()
        rate = float(self.cfg['max_publishes_per_second'])
        self.tokens = min(self.burst, self.tokens + (now - self.refilled) * rate)
        self.refilled = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    def finish(self, message_id, status):
        """Persist the outcome before reporting/acknowledging it, so a redelivery
        after a crash, lost ack or status backpressure never republishes."""
        if status is not None:
            self.save(completed=message_id, status=status, inflight=None)
        if message_id == self.state['completed'] and self.state['status'] is not None \
                and self.cfg['status_topic'] is not None:
            try:
                self.ipc.publish(self.cfg['status_topic'], self.state['status'])
            except (IpcError, OSError):
                # Typically status-subscriber backpressure: hold the request queue.
                raise PublishError('status-pending') from None
        self.ipc.ack(self.cfg['request_topic'], message_id)

    def step(self):
        message = self.ipc.receive(self.cfg['request_topic'])
        if message is None:
            return 'idle'
        message_id = message.get('id') if isinstance(message, dict) else None
        if type(message_id) is not int or message_id < 0:
            # Volatile legacy inbox messages carry no id and are already removed.
            raise PublishError('unsupported-message')
        if message_id <= self.state['completed']:
            self.finish(message_id, None)
            return 'duplicate'
        try:
            request_id, kwargs = prepare(self.cfg, message)
        except Rejected as rejection:
            self.finish(message_id, dict(id=correlation(message.get('payload')), response=dict(
                status='fail', error=rejection.code, error_message=rejection.text)))
            return 'denied' if rejection.code == 'AccessDenied' else 'rejected'
        inflight = self.state['inflight']
        if inflight is None or inflight['id'] != message_id:
            inflight = dict(id=message_id, attempts=0, ambiguous=False)
        if inflight['attempts'] >= self.cfg['max_attempts']:
            code = 'DeliveryUnknown' if inflight['ambiguous'] else 'RetryExhausted'
            text = ('retry bound reached after an ambiguous attempt; it may have been published'
                    if inflight['ambiguous'] else 'retry bound reached; not published')
            self.finish(message_id, dict(id=request_id, response=dict(status='fail', error=code, error_message=text)))
            return 'exhausted'
        if not self.take_token():
            return 'throttled'
        if kwargs['TopicArn'].endswith('.fifo') and 'MessageDeduplicationId' not in kwargs:
            # Stable across retries and restarts: SNS FIFO drops a duplicate
            # within its deduplication interval. Standard topics cannot.
            kwargs['MessageDeduplicationId'] = hashlib.sha256(f'{self.binding}:{message_id}'.encode()).hexdigest()
        # Count the attempt durably before sending; a crash during the call is ambiguous.
        self.save(inflight=dict(id=message_id, attempts=inflight['attempts'] + 1, ambiguous=True))
        try:
            result = self.sns.publish(**kwargs)
        except Exception as error:
            details = getattr(error, 'response', None)
            code = details.get('Error', {}).get('Code') if isinstance(details, dict) else None
            definite = isinstance(code, str) or type(error).__name__ in UNSENT
            if definite:
                self.save(inflight=dict(self.state['inflight'], ambiguous=inflight['ambiguous']))
            if code in PERMANENT:
                self.finish(message_id, dict(id=request_id, response=dict(
                    status='fail', error=code, error_message='SNS rejected the request')))
                return 'rejected'
            if isinstance(code, str) and ('Throttl' in code or code == 'TooManyRequestsException'):
                raise PublishError('throttled') from None
            raise PublishError('retry-pending' if definite else 'ambiguous') from None
        message_ref = result.get('MessageId') if isinstance(result, dict) else None
        if not isinstance(message_ref, str) or not 0 < len(message_ref) <= 100:
            raise PublishError('ambiguous response')
        response = dict(status='success', sns_message_id=message_ref)
        if isinstance(result.get('SequenceNumber'), str):
            response['sns_sequence_number'] = result['SequenceNumber']
        self.finish(message_id, dict(id=request_id, response=response))
        return 'published'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        cfg = config(json.loads(Path(args.config).read_text()))
        publisher = Publisher(cfg, Client(cfg['socket'], cfg['component']), aws_client(cfg))
    except Exception:
        sys.exit('SNS publisher startup rejected; inspect protected configuration/state/credentials')
    delay = 1
    try:
        while True:
            try:
                outcome = publisher.step()
                delay = 1
                if outcome == 'idle':
                    time.sleep(1)
                elif outcome == 'throttled':
                    time.sleep(publisher.wait())
            except Exception as error:
                # Never echo SDK error text, payload, endpoint, paths or credentials.
                print('SNS request retained; ' + diagnostic(error), file=sys.stderr, flush=True)
                time.sleep(delay)
                delay = min(60, delay * 2)
    finally:
        publisher.close()


if __name__ == '__main__':
    main()
