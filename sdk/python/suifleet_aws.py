#!/usr/bin/env python3
"""Start one platform-managed component with scoped AWS container credentials.

The wrapper must already run as its dedicated component UID. It execs the command
so the kernel pidfd keeps the token bound to exactly this start's lifetime.
"""
import argparse
import json
import os
import socket


def environment(path, component):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(path)
        connection.sendall(json.dumps(dict(version=1, operation='start', component=component)).encode() + b'\n')
        with connection.makefile('rb') as source:
            raw = source.readline(16385)
        if len(raw) > 16384 or not raw.endswith(b'\n'):
            raise RuntimeError('credential bootstrap failed')
        value = json.loads(raw)
        if value.get('ok') is not True:
            raise RuntimeError('credential bootstrap denied')
        return value['data']['env']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--component', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('command required')
    env = dict(os.environ)
    # Reject accidental precedence from another AWS provider/identity. An operator
    # may pass region/service configuration, but never inherited credential sources.
    for key in list(env):
        if key.startswith(('AWS_CONTAINER_', 'AWS_ACCESS_KEY', 'AWS_SECRET_', 'AWS_SESSION_', 'AWS_PROFILE',
                           'AWS_ROLE_', 'AWS_WEB_IDENTITY_', 'AWS_SHARED_CREDENTIALS_', 'AWS_CONFIG_FILE',
                           'AWS_DEFAULT_PROFILE', 'AWS_SECURITY_TOKEN', 'AWS_CREDENTIAL_FILE', 'BOTO_CONFIG')):
            del env[key]
    env['AWS_EC2_METADATA_DISABLED'] = 'true'
    env.update(environment(args.socket, args.component))
    os.execvpe(command[0], command, env)


if __name__ == '__main__':
    main()
