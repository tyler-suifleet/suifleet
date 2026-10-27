#!/usr/bin/env python3
"""SF-129 storage reclaim lifecycle on a disposable network-none localnet.

Publishes the base contracts, creates real archives and deployment records,
upgrades core and fleet_data in place, then reclaims through the new APIs and
records measured rebates. Refuses every non-loopback interface and existing
local services; temporary keys/state never leave the container.
"""
import os
from pathlib import Path
import socket
import subprocess
import tempfile

import sui_toolchain


def main():
    if not Path('/.dockerenv').exists() or {p.name for p in Path('/sys/class/net').iterdir()} != {'lo'}:
        raise SystemExit('Requires a disposable Docker container with --network none')
    for port in (9000, 9123):
        with socket.socket() as connection:
            if connection.connect_ex(('127.0.0.1', port)) == 0:
                raise SystemExit('Refusing existing local service on port %d' % port)
    tool = sui_toolchain.check_tools('sui', sui_toolchain.NETWORK_DEFAULT)['network']['path']
    with tempfile.TemporaryDirectory(prefix='sf129-chain-') as temp:
        env = dict(os.environ, SUI_CONFIG_DIR=temp, PROTOCOL_TEST_DISPOSABLE='1',
                   PROTOCOL_TEST_RPC='http://127.0.0.1:9000', PROTOCOL_TEST_FAUCET='http://127.0.0.1:9123')
        with (Path(temp) / 'node.log').open('w') as log:
            node = subprocess.Popen([tool, 'start', '--with-faucet', '--force-regenesis'], env=env, stdout=log, stderr=log)
            try:
                # The harness waits for gRPC readiness itself and fails on timeout.
                subprocess.run(['node', 'scripts/test_reclaim_localnet.cjs'], check=True, env=env)
            finally:
                node.terminate()
                try:
                    node.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    node.kill()
                    node.wait()


if __name__ == '__main__':
    main()
