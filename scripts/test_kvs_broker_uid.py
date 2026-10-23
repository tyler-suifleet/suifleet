#!/usr/bin/env python3
"""Real Linux TCP socket-owner authentication across separate Unix users.

Run only in the disposable network-none test container as root with SETUID,
SETGID and DAC_OVERRIDE for loading the read-only checkout before dropping UID.
No AWS request, broker key, production token or host credential is used.
"""
import datetime as dt
import http.server
import json
import os
from pathlib import Path
import select
import signal
import sys
import time
import unittest

import kvs_video as video

BASE = json.loads((Path(__file__).resolve().parents[1]/'interfaces/compat/video/example.json').read_text())
BROKER_UID, CLIENT_UID = 1101, 1102


def drop(uid):
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)


def receive(fd):
    if not select.select([fd], [], [], 10)[0]:
        raise AssertionError('test child deadline')
    raw = os.read(fd, 4096)
    os.close(fd)
    return json.loads(raw)


class BrokerIdentity(unittest.TestCase):
    def probe(self, expected_uid, accept_delay=0):
        self.assertEqual(os.geteuid(), 0, 'run the real-UID test through make test-video')
        port_r, port_w = os.pipe()
        seen_r, seen_w = os.pipe()
        broker = os.fork()
        if broker == 0:
            try:
                os.close(port_r)
                os.close(seen_r)
                drop(BROKER_UID)
                seen = []
                class Handler(http.server.BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass
                    def do_GET(self):
                        seen.append(self.headers.get('Authorization') == 'fake-local-token')
                        body = json.dumps(dict(AccessKeyId='fake-access', SecretAccessKey='fake-secret',
                            Token='fake-session', Expiration=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1)).isoformat())).encode()
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
                server.timeout = 7
                os.write(port_w, json.dumps(server.server_port).encode())
                os.close(port_w)
                time.sleep(accept_delay)
                server.handle_request()
                server.server_close()
                os.write(seen_w, json.dumps(seen).encode())
                os.close(seen_w)
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(port_w)
        os.close(seen_w)
        client = None
        try:
            port = receive(port_r)
            result_r, result_w = os.pipe()
            client = os.fork()
            if client == 0:
                try:
                    os.close(result_r)
                    drop(CLIENT_UID)
                    os.environ['AWS_CONTAINER_CREDENTIALS_FULL_URI'] = f'http://127.0.0.1:{port}/2016-11-01/credentialprovider/'
                    os.environ['AWS_CONTAINER_AUTHORIZATION_TOKEN'] = 'fake-local-token'
                    cfg = video.Config(dict(BASE, credential_provider_uid=expected_uid))
                    try:
                        credentials = video.credentials(cfg)
                        accepted = credentials['AccessKeyId'] == 'fake-access'
                    except (OSError, ValueError):
                        accepted = False
                    os.write(result_w, json.dumps(accepted).encode())
                    os.close(result_w)
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            os.close(result_w)
            accepted = receive(result_r)
            seen = receive(seen_r)
            for pid in (client, broker):
                _, status = os.waitpid(pid, 0)
                self.assertEqual(status, 0)
            client, broker = None, None
            return accepted, seen
        finally:
            for pid in (client, broker):
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                    except ProcessLookupError:
                        pass

    def test_expected_broker_uid_with_distinct_client(self):
        self.assertEqual(self.probe(BROKER_UID), (True, [True]))

    def test_delayed_accept_from_expected_broker(self):
        self.assertEqual(self.probe(BROKER_UID, accept_delay=0.25), (True, [True]))

    def test_unexpected_broker_uid_never_receives_token(self):
        # Before accept, Linux reports UID 0/inode 0 even for this UID1101
        # listener. That placeholder must not authenticate a root broker.
        self.assertEqual(self.probe(0, accept_delay=0.25), (False, []))


if __name__ == '__main__':
    unittest.main()
