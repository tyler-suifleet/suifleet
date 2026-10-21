"""SuiFleet function handler runtime v1 (SF-069). Python 3.10+, standard library only.

A function process reads one JSON invocation per line on stdin and writes one
JSON response per line on stdout. On-demand processes receive one invocation
and then end of input; long-lived processes serve invocations sequentially.
Write diagnostics to stderr, never to stdout. This is a native SuiFleet
protocol, not the AWS Lambda runtime API or a Greengrass Lambda SDK.
"""
import json
import sys

__version__ = "1.0.0"
PROTOCOL_VERSION = 1
MAX_ERROR_TYPE, MAX_ERROR_MESSAGE = 128, 1024


class FunctionError(Exception):
    """Raise to return an error response with an explicit type."""

    def __init__(self, error_type, message=''):
        super().__init__(message)
        self.error_type = error_type


def respond(request, result=None, error=None):
    response = dict(version=PROTOCOL_VERSION, invocation_id=request.get('invocation_id'))
    if error is None:
        response.update(status='ok', result=result)
    else:
        response.update(status='error', error=error)
    return json.dumps(response, separators=(',', ':'), allow_nan=False)


def handle(handler, line):
    request = json.loads(line)
    if type(request.get('version')) is not int or request['version'] != PROTOCOL_VERSION:
        return respond(request, error=dict(type='UnsupportedVersion', message=''))
    context = {k: v for k, v in request.items() if k != 'payload'}
    try:
        return respond(request, result=handler(request.get('payload'), context))
    except FunctionError as failure:
        return respond(request, error=dict(type=str(failure.error_type)[:MAX_ERROR_TYPE],
                                           message=str(failure)[:MAX_ERROR_MESSAGE]))
    except Exception as failure:  # noqa: BLE001 - every handler failure is a response
        return respond(request, error=dict(type=type(failure).__name__[:MAX_ERROR_TYPE],
                                           message=str(failure)[:MAX_ERROR_MESSAGE]))


def serve(handler, stdin=None, stdout=None):
    """Call handler(payload, context) for each invocation until end of input."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if line.strip():
            stdout.write(handle(handler, line) + '\n')
            stdout.flush()
