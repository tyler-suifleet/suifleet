"""Actual signed SDK HTTP requests to a local SNS/credential emulator."""
import base64
import hashlib
import hmac
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import sns_publish as sp

ACCOUNT = '123456789012'
ALERTS = f'arn:aws:sns:us-east-1:{ACCOUNT}:alerts'
OPS = f'arn:aws:sns:us-east-1:{ACCOUNT}:ops'
ORDERED = f'arn:aws:sns:us-east-1:{ACCOUNT}:ordered.fifo'
MISSING = f'arn:aws:sns:us-east-1:{ACCOUNT}:missing'
XMLNS = 'http://sns.amazonaws.com/doc/2010-03-31/'


def form_attributes(form):
    attributes, index = {}, 1
    while f'MessageAttributes.entry.{index}.Name' in form:
        prefix = f'MessageAttributes.entry.{index}.'
        value = {'DataType': form[prefix + 'Value.DataType']}
        if prefix + 'Value.StringValue' in form:
            value['StringValue'] = form[prefix + 'Value.StringValue']
        if prefix + 'Value.BinaryValue' in form:
            value['BinaryValue'] = base64.b64decode(form[prefix + 'Value.BinaryValue'], validate=True)
        attributes[form[prefix + 'Name']] = value
        index += 1
    return attributes


@contextmanager
def emulator():
    """SNS Query API 2010-03-31 Publish, modelling scoped IAM, topics and FIFO
    deduplication. `deliveries` is what subscribers would receive."""
    state = dict(requests=[], deliveries=[], responses=[], credentials=True, dedup=set(),
                 iam={ALERTS, OPS, ORDERED, MISSING}, topics={ALERTS, OPS, ORDERED})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, content='text/xml'):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Type', content)
            self.end_headers()
            self.wfile.write(data)

        def error(self, code, kind):
            self.reply(code, f'<ErrorResponse xmlns="{XMLNS}"><Error><Type>Sender</Type><Code>{kind}</Code>'
                             f'<Message>local emulator</Message></Error><RequestId>r</RequestId></ErrorResponse>')

        def do_GET(self):
            if not state['credentials']:
                return self.reply(503, {}, 'application/json')
            if self.headers.get('Authorization') != 'local-scoped-token':
                return self.reply(403, {}, 'application/json')
            self.reply(200, dict(AccessKeyId='local-test-access', SecretAccessKey='local-test-secret',
                                Token='local-test-session',
                                Expiration=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()),
                       'application/json')

        def do_POST(self):
            assert 'Credential=local-test-access/' in self.headers['Authorization']
            assert '/us-east-1/sns/aws4_request' in self.headers['Authorization']
            assert self.headers['X-Amz-Security-Token'] == 'local-test-session'
            raw = self.rfile.read(int(self.headers['Content-Length']))
            auth = self.headers['Authorization']
            signed = auth.split('SignedHeaders=')[1].split(',')[0]
            scope = auth.split('Credential=')[1].split(',')[0].split('/', 1)[1]
            canonical_headers = ''.join(name + ':' + ' '.join(self.headers[name].split()) + '\n'
                                        for name in signed.split(';'))
            canonical = '\n'.join(('POST', '/', '', canonical_headers, signed, hashlib.sha256(raw).hexdigest()))
            string_to_sign = '\n'.join(('AWS4-HMAC-SHA256', self.headers['X-Amz-Date'], scope,
                                        hashlib.sha256(canonical.encode()).hexdigest()))
            key = b'AWS4local-test-secret'
            for part in scope.split('/'):
                key = hmac.new(key, part.encode(), hashlib.sha256).digest()
            expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
            assert hmac.compare_digest(expected, auth.split('Signature=')[1])
            form = {k: v[0] for k, v in parse_qs(raw.decode(), keep_blank_values=True, strict_parsing=True).items()}
            assert form['Action'] == 'Publish' and form['Version'] == '2010-03-31'
            request = dict(form, MessageAttributes=form_attributes(form))
            state['requests'].append(request)
            if state['responses']:
                code, value = state['responses'].pop(0)
                if value == 'disconnect':
                    self.connection.shutdown(2)
                    return
                if value == 'deliver-then-disconnect':
                    # Accepted by the service, response lost in transit.
                    self.wfile = open(os.devnull, 'wb')
                    self.accept(request, form)
                    self.connection.shutdown(2)
                    return
                if isinstance(value, str):
                    return self.error(code, value)
                return self.reply(code, value)
            self.accept(request, form)

        def accept(self, request, form):
            topic = form['TopicArn']
            if topic not in state['iam']:
                return self.error(403, 'AuthorizationError')
            if topic not in state['topics']:
                return self.error(404, 'NotFound')
            sequence = ''
            if topic.endswith('.fifo'):
                dedup = (topic, form['MessageDeduplicationId'])
                sequence = f'<SequenceNumber>{len(state["deliveries"]) + 10000000000000000000}</SequenceNumber>'
                if dedup in state['dedup']:
                    # Accepted, but not delivered again within the interval.
                    return self.reply(200, self.result(len(state['requests']), sequence))
                state['dedup'].add(dedup)
            state['deliveries'].append(request)
            self.reply(200, self.result(len(state['requests']), sequence))

        def result(self, number, sequence):
            return (f'<PublishResponse xmlns="{XMLNS}"><PublishResult><MessageId>msg-{number}</MessageId>'
                    f'{sequence}</PublishResult><ResponseMetadata><RequestId>r</RequestId>'
                    '</ResponseMetadata></PublishResponse>')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield state, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Broker:
    """In-memory model of the SF-032 durable QoS 1 subscription contract:
    ordered ids, the head is redelivered until acknowledged."""

    def __init__(self):
        self.queue, self.next, self.statuses, self.subscribed = [], 0, [], []
        self.status_failures, self.ack_failures, self.acks = 0, 0, []

    def add(self, payload, sender='sensor', topic='sns/message'):
        self.queue.append(dict(id=self.next, topic=topic, sender=sender, payload=payload, published_ms=1))
        self.next += 1
        return self.next - 1

    def subscribe(self, topic_filter, qos=1):
        assert qos == 1
        self.subscribed.append(topic_filter)

    def receive(self, topic):
        assert topic == 'sns/message'
        return json.loads(json.dumps(self.queue[0])) if self.queue else None

    def ack(self, topic, message_id):
        if self.ack_failures:
            self.ack_failures -= 1
            raise sp.IpcError('truncated response')
        self.acks.append(message_id)
        self.queue = [m for m in self.queue if m['id'] != message_id]

    def publish(self, topic, payload):
        assert topic == 'sns/message/status'
        if self.status_failures:
            self.status_failures -= 1
            raise sp.IpcError('request_failed')
        self.statuses.append(json.loads(json.dumps(payload)))


def configuration(directory, **changes):
    return dict(dict(version=1, socket='/run/suifleet/ipc.sock', component='sns-publisher', region='us-east-1',
                     request_topic='sns/message', status_topic='sns/message/status', default_topic_arn=ALERTS,
                     grants={'sensor': [ALERTS, ORDERED, MISSING], 'auditor': [OPS]}, max_attempts=3,
                     max_publishes_per_second=1000, state_dir=str(directory / 'sns')), **changes)


def client(cfg, url):
    # Production factory deliberately offers no endpoint override. Only the test
    # swaps create_client's endpoint to the network-none local emulator.
    import botocore.session
    original = botocore.session.Session.create_client

    def local(session, *args, **kwargs):
        return original(session, *args, **kwargs, endpoint_url=url)

    with patch.dict(os.environ, {'AWS_CONTAINER_CREDENTIALS_FULL_URI': url + '/credentials',
                                 'AWS_CONTAINER_AUTHORIZATION_TOKEN': 'local-scoped-token'}), \
            patch.object(botocore.session.Session, 'create_client', local):
        return sp.aws_client(cfg)


def request(message='hello', **fields):
    return dict(id=fields.pop('id', 'request-1'), request=dict(message=message, **fields))


class SnsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = configuration(Path(self.temp.name))

    def publisher(self, broker, url, cfg=None, **kwargs):
        cfg = cfg or self.cfg
        result = sp.Publisher(cfg, broker, client(cfg, url), **kwargs)
        self.addCleanup(result.close)
        return result

    def test_message_subject_attributes_and_status(self):
        with emulator() as (state, url):
            broker = Broker()
            broker.add(request('hello é', subject='Alarm: door', message_attributes={
                'kind': dict(data_type='String', string_value='door'),
                'level': dict(data_type='Number', string_value='-3.25'),
                'zones': dict(data_type='String.Array', string_value='["a", 1]'),
                'blob': dict(data_type='Binary', binary_value=base64.b64encode(b'\0\1').decode())}))
            broker.add(request('{"default": "d", "email": "e"}', id='request-2', message_structure='json',
                               sns_topic_arn=ALERTS))
            publisher = self.publisher(broker, url)
            self.assertEqual(broker.subscribed, ['sns/message'])
            self.assertEqual(publisher.step(), 'published')
            self.assertEqual(publisher.step(), 'published')
            self.assertEqual(publisher.step(), 'idle')
            first, second = state['requests']
            self.assertEqual((first['TopicArn'], first['Message'], first['Subject']), (ALERTS, 'hello é', 'Alarm: door'))
            self.assertNotIn('MessageStructure', first)
            self.assertEqual(first['MessageAttributes'], {
                'kind': {'DataType': 'String', 'StringValue': 'door'},
                'level': {'DataType': 'Number', 'StringValue': '-3.25'},
                'zones': {'DataType': 'String.Array', 'StringValue': '["a", 1]'},
                'blob': {'DataType': 'Binary', 'BinaryValue': b'\0\1'}})
            self.assertEqual(second['MessageStructure'], 'json')
            self.assertEqual(broker.statuses, [
                dict(id='request-1', response=dict(status='success', sns_message_id='msg-1')),
                dict(id='request-2', response=dict(status='success', sns_message_id='msg-2'))])
            self.assertEqual(broker.acks, [0, 1])
            self.assertEqual(publisher.state['completed'], 1)

    def test_caller_and_topic_denial_precedes_network(self):
        with emulator() as (state, url):
            broker = Broker()
            broker.add(request('x'), sender='intruder')
            broker.add(request('x'), sender='@bridge/LocalMqtt')
            broker.add(request('x', sns_topic_arn=ALERTS), sender='auditor')
            broker.add(request('x', sns_topic_arn=OPS))
            broker.add(request('x', id='payload-sender', sender='sensor'), sender='intruder')
            # Ungranted ARNs, including other Regions/services, never reach SNS.
            broker.add(request('m', sns_topic_arn='arn:aws:sns:eu-west-1:123456789012:alerts'))
            broker.add(request('m', sns_topic_arn='arn:aws:sqs:us-east-1:123456789012:alerts'))
            broker.add(dict(id='default', request=dict(message='m')))
            publisher = self.publisher(broker, url)
            for _ in range(5):
                self.assertEqual(publisher.step(), 'denied')
            self.assertEqual(state['requests'], [])
            self.assertEqual(publisher.step(), 'denied')
            self.assertEqual(publisher.step(), 'denied')
            self.assertEqual(publisher.step(), 'published')  # default topic is granted to sensor
            self.assertEqual({s['response']['error'] for s in broker.statuses[:-1]}, {'AccessDenied'})
            self.assertEqual(broker.acks, list(range(8)))
            state['requests'].clear()
            # A granted sender reaches only its granted topic.
            broker.add(request('ok', sns_topic_arn=OPS), sender='auditor')
            self.assertEqual(publisher.step(), 'published')
            self.assertEqual(state['requests'][0]['TopicArn'], OPS)

    def test_invalid_requests_and_versions_are_reported_locally(self):
        cases = [
            'not-an-object', dict(id='x'), dict(request=dict(message='m')), dict(id='', request=dict(message='m')),
            dict(id='x', request=dict(message='m'), extra=1), dict(id='x', request=dict(message='m', unknown=1)),
            request(''), request(7), request('m', subject=''), request('m', subject='line\nbreak'),
            request('m', subject='x' * 100), request('m', message_structure='text'),
            request('{"other": "x"}', message_structure='json'), request('[1]', message_structure='json'),
            request('{"default": "a", "default": "b"}', message_structure='json'),
            request('{"default": "a"}', message_structure='json',
                    message_attributes={'a': dict(data_type='String', string_value='x')}),
            request('m', message_attributes={}),
            request('m', message_attributes={f'a{i}': dict(data_type='String', string_value='x') for i in range(11)}),
            request('m', message_attributes={'AWS.reserved': dict(data_type='String', string_value='x')}),
            request('m', message_attributes={'a..b': dict(data_type='String', string_value='x')}),
            request('m', message_attributes={'.a': dict(data_type='String', string_value='x')}),
            request('m', message_attributes={'a': dict(data_type='Custom', string_value='x')}),
            request('m', message_attributes={'a': dict(data_type='String', string_value='')}),
            request('m', message_attributes={'a': dict(data_type='Number', string_value='1e3')}),
            request('m', message_attributes={'a': dict(data_type='Number', string_value='2000000000')}),
            request('m', message_attributes={'a': dict(data_type='String.Array', string_value='{}')}),
            request('m', message_attributes={'a': dict(data_type='Binary', binary_value='!!')}),
            request('m', message_attributes={'a': dict(data_type='Binary', string_value='x')}),
            request('m', message_attributes={'a': dict(data_type='String', string_value='\ud83d')}),
            request('\ud83d'),
            request('m', sns_topic_arn=ORDERED),
            request('m', sns_topic_arn=ORDERED, message_group_id='bad group'),
            request('m', message_deduplication_id='standard-topic'),
            request('x' * (sp.MAX_MESSAGE + 1)),
        ]
        with emulator() as (state, url):
            broker = Broker()
            for case in cases:
                broker.add(case)
            broker.add(dict(version=2, id='future', request=dict(message='m')))
            broker.add(dict(version=True, id='bool', request=dict(message='m')))
            publisher = self.publisher(broker, url)
            for _ in cases:
                self.assertEqual(publisher.step(), 'rejected')
            self.assertEqual(publisher.step(), 'rejected')
            self.assertEqual(publisher.step(), 'rejected')
            self.assertEqual(state['requests'], [])
            errors = [s['response']['error'] for s in broker.statuses]
            self.assertEqual(errors[:-2], ['InvalidInputException'] * len(cases))
            self.assertEqual(errors[-2:], ['UnsupportedVersion'] * 2)
            self.assertEqual(broker.statuses[-2]['id'], 'future')
            self.assertIsNone(broker.statuses[0]['id'])
            self.assertEqual(len(broker.acks), len(cases) + 2)

    def test_service_throttling_and_local_rate_limit(self):
        with emulator() as (state, url):
            broker = Broker()
            broker.add(request('a'))
            broker.add(request('b'))
            state['responses'] = [(400, 'Throttling')]
            now = [0.0]
            cfg = dict(self.cfg, max_publishes_per_second=0.5)
            publisher = self.publisher(broker, url, cfg, clock=lambda: now[0])
            with self.assertRaisesRegex(sp.PublishError, 'throttled'):
                publisher.step()
            self.assertEqual(broker.acks, [])
            self.assertEqual(publisher.state['inflight'], dict(id=0, attempts=1, ambiguous=False))
            # Local bucket (0.5/s, burst 1): no request until a token accrues.
            self.assertEqual(publisher.step(), 'throttled')
            self.assertAlmostEqual(publisher.wait(), 2.0)
            self.assertEqual(len(state['requests']), 1)
            now[0] = 2.0
            self.assertEqual(publisher.step(), 'published')
            self.assertEqual(publisher.step(), 'throttled')
            now[0] = 4.0
            self.assertEqual(publisher.step(), 'published')
            self.assertEqual([r['Message'] for r in state['requests']], ['a', 'a', 'b'])
            self.assertEqual(broker.acks, [0, 1])

    def test_lost_response_duplicates_standard_but_fifo_dedups(self):
        with emulator() as (state, url):
            broker = Broker()
            broker.add(request('standard'))
            broker.add(request('ordered', sns_topic_arn=ORDERED, message_group_id='g-1'))
            state['responses'] = [(200, 'deliver-then-disconnect')]
            publisher = self.publisher(broker, url)
            with self.assertRaisesRegex(sp.PublishError, 'ambiguous'):
                publisher.step()
            self.assertEqual(publisher.state['inflight'], dict(id=0, attempts=1, ambiguous=True))
            self.assertEqual(publisher.step(), 'published')
            # Standard topics have no deduplication: at least once, duplicated.
            self.assertEqual([d['Message'] for d in state['deliveries']], ['standard', 'standard'])
            state['responses'] = [(200, 'deliver-then-disconnect')]
            with self.assertRaisesRegex(sp.PublishError, 'ambiguous'):
                publisher.step()
            publisher.close()
            # Restart: the derived deduplication id is stable, SNS FIFO drops the copy.
            resumed = self.publisher(broker, url)
            self.assertEqual(resumed.state['inflight'], dict(id=1, attempts=1, ambiguous=True))
            self.assertEqual(resumed.step(), 'published')
            fifo = [r for r in state['requests'] if r['TopicArn'] == ORDERED]
            self.assertEqual(len(fifo), 2)
            self.assertEqual(fifo[0]['MessageDeduplicationId'], fifo[1]['MessageDeduplicationId'])
            self.assertEqual(fifo[0]['MessageGroupId'], 'g-1')
            self.assertEqual([d['Message'] for d in state['deliveries']], ['standard', 'standard', 'ordered'])
            self.assertTrue(broker.statuses[-1]['response']['sns_sequence_number'].isdigit())
            # An explicit sender deduplication id is passed through unchanged.
            broker.add(request('explicit', sns_topic_arn=ORDERED, message_group_id='g-1',
                               message_deduplication_id='client-1'))
            self.assertEqual(resumed.step(), 'published')
            self.assertEqual(state['requests'][-1]['MessageDeduplicationId'], 'client-1')

    def test_bounded_retries_report_exhaustion(self):
        with emulator() as (state, url):
            broker = Broker()
            broker.add(request('outage', id='a'))
            broker.add(request('lost', id='b'))
            broker.add(request('next', id='c'))
            state['responses'] = [(500, 'InternalError'), (403, 'AuthorizationError'), (503, 'ServiceUnavailable'),
                                  (200, 'disconnect'), (500, 'InternalError'), (500, 'InternalError')]
            publisher = self.publisher(broker, url)
            for _ in range(3):
                with self.assertRaises(sp.PublishError):
                    publisher.step()
            self.assertEqual(broker.acks, [])
            publisher.close()
            # The attempt count is durable: a restart does not reset the bound.
            publisher = self.publisher(broker, url)
            self.assertEqual(publisher.step(), 'exhausted')
            self.assertEqual(broker.statuses[-1], dict(id='a', response=dict(
                status='fail', error='RetryExhausted', error_message='retry bound reached; not published')))
            for _ in range(3):
                with self.assertRaises(sp.PublishError):
                    publisher.step()
            self.assertEqual(publisher.step(), 'exhausted')
            self.assertEqual(broker.statuses[-1]['response']['error'], 'DeliveryUnknown')
            self.assertEqual(publisher.step(), 'published')
            self.assertEqual(len(state['requests']), 7)
            self.assertEqual(broker.acks, [0, 1, 2])

    def test_permanent_service_errors_are_reported_once(self):
        with emulator() as (state, url):
            broker = Broker()
            broker.add(request('m'))
            broker.add(request('m', sns_topic_arn=MISSING))
            state['responses'] = [(400, 'InvalidParameter')]
            publisher = self.publisher(broker, url)
            self.assertEqual(publisher.step(), 'rejected')
            self.assertEqual(publisher.step(), 'rejected')
            self.assertEqual([s['response']['error'] for s in broker.statuses], ['InvalidParameter', 'NotFound'])
            self.assertEqual(len(state['requests']), 2)
            self.assertEqual(broker.acks, [0, 1])

    def test_lost_ack_and_status_backpressure_never_republish(self):
        with emulator() as (state, url):
            broker = Broker()
            broker.add(request('once'))
            broker.status_failures = 2
            publisher = self.publisher(broker, url)
            for _ in range(2):
                with self.assertRaisesRegex(sp.PublishError, 'status-pending'):
                    publisher.step()
            self.assertEqual(broker.acks, [])
            broker.ack_failures = 1
            with self.assertRaises(sp.IpcError):
                publisher.step()
            publisher.close()
            resumed = self.publisher(broker, url)
            self.assertEqual(resumed.step(), 'duplicate')
            self.assertEqual(len(state['requests']), 1)
            self.assertEqual(broker.acks, [0])
            # Status is at least once; consumers correlate it by request id.
            self.assertEqual([s['id'] for s in broker.statuses], ['request-1', 'request-1'])
            # An older completed id is acknowledged without moving the ledger back.
            broker.add(request('newer'))
            self.assertEqual(resumed.step(), 'published')
            broker.queue.append(dict(id=0, topic='sns/message', sender='sensor', payload=request('once')))
            self.assertEqual(resumed.step(), 'duplicate')
            self.assertEqual(resumed.state['completed'], 1)
            self.assertEqual(len(broker.statuses), 3)
            self.assertEqual(len(state['requests']), 2)

    def test_process_crash_after_publish_before_ack(self):
        with emulator() as (state, url):
            code = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
from test_sns_publish import sp, Broker, client, request
cfg = json.loads(sys.argv[2])
broker = Broker()
broker.add(request('crash'))
broker.ack = lambda *a: os._exit(0)  # abrupt exit after the persisted outcome
sp.Publisher(cfg, broker, client(cfg, sys.argv[3])).step()
"""
            subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts/tests'), json.dumps(self.cfg), url],
                           check=True, timeout=30)
            broker = Broker()
            broker.add(request('crash'))
            publisher = self.publisher(broker, url)
            self.assertEqual(publisher.state['completed'], 0)
            self.assertEqual(publisher.step(), 'duplicate')
            self.assertEqual(len(state['requests']), 1)
            self.assertEqual(broker.statuses[0]['response']['sns_message_id'], 'msg-1')

    def test_credentials_expiry_refresh_and_scope(self):
        with emulator() as (state, url):
            broker = Broker()
            broker.add(request('m'))
            publisher = self.publisher(broker, url)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, 'scoped credentials'):
                    sp.aws_client(self.cfg)
            publisher.sns._request_signer._credentials._expiry_time = datetime.now(timezone.utc) - timedelta(seconds=1)
            state['credentials'] = False
            with self.assertNoLogs(), self.assertRaisesRegex(sp.PublishError, 'retry-pending'):
                publisher.step()
            self.assertEqual(state['requests'], [])
            self.assertEqual(publisher.state['inflight']['ambiguous'], False)
            state['credentials'] = True
            with patch.dict(os.environ, {'AWS_CONTAINER_AUTHORIZATION_TOKEN': 'local-scoped-token'}):
                self.assertEqual(publisher.step(), 'published')
            # IAM scoped narrower than the local grant: retained and retried, not dropped.
            state['iam'].discard(ALERTS)
            broker.add(request('m'))
            with self.assertRaisesRegex(sp.PublishError, 'retry-pending'):
                publisher.step()
            self.assertEqual(broker.acks, [0])

    def test_state_lock_binding_corruption_and_failed_sync(self):
        with emulator() as (state, url):
            broker = Broker()
            broker.add(request('m'))
            publisher = self.publisher(broker, url)
            with self.assertRaises(BlockingIOError):
                sp.Publisher(self.cfg, broker, client(self.cfg, url))
            with patch.object(sp.os, 'fsync', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    publisher.step()
            self.assertEqual(state['requests'], [])
            self.assertEqual(publisher.step(), 'published')
            publisher.close()
            changed = dict(self.cfg, grants={'sensor': [ALERTS]})
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                sp.Publisher(changed, broker, client(changed, url))
            publisher.path.write_text('{bad-json')
            with self.assertRaises(ValueError):
                sp.Publisher(self.cfg, broker, client(self.cfg, url))

    def test_configuration_versions_and_redacted_diagnostics(self):
        for changes in [dict(version=True), dict(version=2), dict(version='1'), dict(extra=1),
                        dict(region='US'), dict(state_dir='relative'), dict(request_topic='a/#/b'),
                        dict(status_topic='sns/+'), dict(status_topic='sns/message'),
                        dict(request_topic='sns/#', status_topic='sns/message/status'),
                        dict(default_topic_arn='arn:aws:sns:eu-west-1:123456789012:x'),
                        dict(grants={}), dict(grants={'sensor': []}), dict(grants={'sensor': [ALERTS, ALERTS]}),
                        dict(grants={'sensor': ['*']}), dict(max_attempts=0), dict(max_attempts=101),
                        dict(max_attempts=True), dict(max_publishes_per_second=0),
                        dict(max_publishes_per_second=float('inf'))]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                sp.config(dict(self.cfg, **changes))
        sp.config(dict(self.cfg, status_topic=None, default_topic_arn=None))
        self.assertEqual(sp.diagnostic(ValueError('secret payload')), 'retry-pending')
        self.assertEqual(sp.diagnostic(sp.PublishError('throttled')), 'throttled')

    def test_no_status_topic_and_legacy_message(self):
        with emulator() as (state, url):
            broker = Broker()
            broker.queue.append(dict(sender='sensor', payload=request('legacy')))
            broker.add(request('m'))
            broker.add(request('m'))
            publisher = self.publisher(broker, url, dict(self.cfg, status_topic=None, default_topic_arn=None))
            with self.assertRaisesRegex(sp.PublishError, 'unsupported-message'):
                publisher.step()
            broker.queue.pop(0)
            # Without a configured default every request must name a granted topic.
            self.assertEqual(publisher.step(), 'denied')
            broker.add(request('m', sns_topic_arn=ALERTS))
            self.assertEqual(publisher.step(), 'denied')
            self.assertEqual(publisher.step(), 'published')
            self.assertEqual(broker.statuses, [])
            self.assertEqual(broker.acks, [0, 1, 2])


if __name__ == '__main__':
    unittest.main()
