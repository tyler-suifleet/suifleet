"""SuiFleet native IPC v1 SDK. Python 3.10+, standard library only."""
import argparse
import json
import socket

__version__ = "1.0.0"
PROTOCOL_VERSION = 1
MAX_FRAME = 65536


class IpcError(RuntimeError):
    """Rejected request. A transport timeout has an unknown mutation outcome."""


class Client:
    def __init__(self, path='/run/suifleet/ipc.sock', component='', timeout=120):
        self.path, self.component, self.timeout = path, component, timeout

    def request(self, operation, *, component=None, topic='', payload=None):
        request = dict(version=PROTOCOL_VERSION, operation=operation,
                       component=self.component if component is None else component,
                       topic=topic, payload=payload)
        frame = json.dumps(request, separators=(',', ':')).encode() + b'\n'
        if len(frame) > MAX_FRAME:
            raise ValueError('request exceeds IPC frame limit')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.path)
            connection.sendall(frame)
            response = bytearray()
            while b'\n' not in response:
                chunk = connection.recv(min(4096, MAX_FRAME + 1 - len(response)))
                if not chunk:
                    raise IpcError('truncated response')
                response.extend(chunk)
                if len(response) > MAX_FRAME:
                    raise IpcError('response exceeds IPC frame limit')
        value = json.loads(response.split(b'\n', 1)[0])
        if value.get('version') != PROTOCOL_VERSION:
            raise IpcError('unsupported response version')
        if value.get('ok') is not True:
            raise IpcError(value.get('error', 'request_failed'))
        return value['data']

    def install(self, deployment):
        return self.request('install', payload=deployment)

    def list(self):
        return self.request('list')

    def start(self):
        return self.request('start')

    def stop(self):
        return self.request('stop')

    def restart(self):
        return self.request('restart')

    def status(self):
        return self.request('status')

    def logs(self):
        return self.request('logs')['tail']

    def configuration(self):
        return self.request('config')['configuration']

    def health(self, state):
        return self.request('health', payload=state)

    def publish(self, topic, payload):
        return self.request('publish', topic=topic, payload=payload)

    def receive(self, topic):
        return self.request('receive', topic=topic)

    def subscribe(self, topic_filter, qos=1):
        """Durable subscription (MQTT filter); QoS 1 messages wait for ack."""
        return self.request('subscribe', topic=topic_filter, payload={'qos': qos})

    def unsubscribe(self, topic_filter):
        return self.request('unsubscribe', topic=topic_filter)

    def ack(self, topic_filter, message_id):
        return self.request('ack', topic=topic_filter, payload=message_id)

    def messaging(self):
        """Administrator only: subscription and bridge counters."""
        return self.request('messaging')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default='/run/suifleet/ipc.sock')
    parser.add_argument('--component', default='')
    parser.add_argument('operation', choices=['install', 'list', 'start', 'stop',
                                            'restart', 'status', 'logs', 'config',
                                            'health', 'publish', 'receive',
                                            'subscribe', 'unsubscribe', 'ack',
                                            'messaging'])
    parser.add_argument('--topic', default='')
    parser.add_argument('--payload', help='JSON value; install takes a quoted deployment ID')
    args = parser.parse_args()
    try:
        result = Client(args.socket, args.component).request(
            args.operation, topic=args.topic,
            payload=json.loads(args.payload) if args.payload is not None else None)
        print(json.dumps(result, indent=2))
    except (IpcError, OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
