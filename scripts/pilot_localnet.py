#!/usr/bin/env python3
"""SF-090 isolated-chain sample; refuses all non-loopback network interfaces."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import sui_toolchain


def main():
    if not Path('/.dockerenv').exists() or {p.name for p in Path('/sys/class/net').iterdir()} != {'lo'}:
        raise SystemExit('Requires disposable Docker --network none')
    for port in (9000, 9123):
        with socket.socket() as connection:
            if connection.connect_ex(('127.0.0.1', port)) == 0:
                raise SystemExit('Refusing existing local service')
    Path(os.environ.get('SF090_CHAIN_RECEIPT', '/tmp/sf090-chain.json')).unlink(missing_ok=True)
    tool = sui_toolchain.check_tools('sui', sui_toolchain.NETWORK_DEFAULT)['network']['path']
    with tempfile.TemporaryDirectory(prefix='sf090-chain-') as temp:
        env = dict(os.environ, SUI_CONFIG_DIR=temp, PROTOCOL_TEST_DISPOSABLE='1',
                   PROTOCOL_TEST_RPC='http://127.0.0.1:9000', PROTOCOL_TEST_FAUCET='http://127.0.0.1:9123')
        with (Path(temp) / 'node.log').open('w') as log:
            node = subprocess.Popen([tool, 'start', '--with-faucet', '--force-regenesis'], env=env, stdout=log, stderr=log)
            try:
                for _ in range(120):
                    if node.poll() is not None:
                        raise RuntimeError('Disposable node exited; private logs suppressed')
                    ready = subprocess.run(['suifleet-edge/target/debug/examples/chain_facts', env['PROTOCOL_TEST_RPC']], capture_output=True, timeout=5)
                    if ready.returncode == 0:
                        break
                    time.sleep(.5)
                else:
                    raise RuntimeError('Disposable node startup timeout')
                subprocess.run(['node', 'scripts/pilot_chain.cjs'], check=True, env=env)
            finally:
                node.terminate()
                try:
                    node.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    node.kill()
                    node.wait()


if __name__ == '__main__':
    main()
