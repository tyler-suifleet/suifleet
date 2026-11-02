#!/usr/bin/env python3
"""Measure the actual ordinary package/wallet suites, never public networks.

Run in a disposable --network none dev container with warm Move dependencies,
the built daemon/chain_facts and installed frontend dependencies. No production
guard or budget is patched. Local Walrus stores and returns the actual bytes;
its storage is a stand-in, not Walrus certification evidence.
"""
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

import deployment_manifest as dm
import live_common
import live_package_upgrade as upgrade
import live_wallet_gate as wallet_gate
import sui_toolchain
from testnet_budget import Spent


class LocalStorage(live_common.Walrus):
    """Loopback HTTP storage supporting the real browser upload/read path."""
    instances = []

    def __init__(self, _mode):
        self.mode, self.blobs = 'local', {}
        blobs = self.blobs

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, body):
                self.send_response(status)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, PUT, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.reply(200, b'')

            def do_PUT(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                blob = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip('=')
                blobs[blob] = body
                self.reply(200, json.dumps({'newlyCreated': {'blobObject': {'blobId': blob}}}).encode())

            def do_GET(self):
                body = blobs.get(self.path.removeprefix('/v1/blobs/'))
                self.reply(200 if body is not None else 404, body or b'')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.publisher = self.aggregator = f'http://127.0.0.1:{self.server.server_port}'
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.instances.append(self)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()


def temporal_ready():
    try:
        dm.chain_facts('http://127.0.0.1:9000')
        return True
    except Exception:
        return False


def measure_temporal(options, output, root):
    """Read the original aged decision and charge its optional retirement.

    Separate measurement ledger, never a continuation/reset of a failed fresh run.
    The production reader must observe the node's actual retention floor.
    """
    import rollout_audit_acceptance as audit
    import testnet_reconcile as reconciler
    live = live_common.Live(options['wallet'], 'localnet', 1_600_000_000)
    live.preflight()
    state_path = Path(options['state'])
    state = upgrade.load_state(state_path, live.chain, live.operator)
    receipts = []
    for key, fixture in state['fixtures'].items():
        retained = fixture.get('rollout_audit')
        if not retained:
            continue
        assert not retained.get('pending_operation'), 'unresolved audit operation must be reconciled first'
        manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url,
            package=fixture['head_package'], upgrade_cap=fixture['upgrade_cap'],
            registry=fixture['registry'], network='localnet',
            publisher='http://127.0.0.1:1', aggregator='http://127.0.0.1:1'))
        before = dict(retained)
        # Bounded wait for this actual node to prune; no stand-in floor or event data.
        deadline = time.monotonic() + 180
        def step(label, ptb, calls):
            return audit.checkpointed_step(state_path, state, retained, label, ptb, calls,
                client=live.client, sign=live.signer(None, live.operator), mutate=live.execute,
                ledger=live.ledger, operator=live.operator, chain=reconciler.Chain(live.rpc_url))
        while True:
            try:
                audit.run(step, live.client, manifest, fixture['device'], live.evidence, root,
                    fixture=retained, save=lambda: upgrade.save_state(state_path, state),
                    operator=live.operator, fixture_key=key, retire=True)
                break
            except audit.RetentionPending:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(5)
        assert retained['record'] == before['record'] and retained['checkpoint'] == before['checkpoint']
        assert retained['retired'] and retained['proof']['audit']['complete']
        receipts.append(dict(fixture=key, record=retained['record'], checkpoint=retained['checkpoint'],
            proof=retained['proof'], cleanup_digest=retained['cleanup_digest']))
    assert receipts, 'no retained audit to measure'
    for tx in live.evidence['transactions']:
        result = live.client('tx-block', tx['digest'])
        tx['gas_used'] = result['effects']['gasUsed']
    live.evidence.pop('rollout_audit_pending', None)
    live.evidence.update(success=True, allowance_used_mist=live.ledger.used,
                         retained_audits=receipts, end_balance_mist=live.balance())
    (output / 'temporal.json').write_text(json.dumps(live.evidence, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-ref', required=True)
    parser.add_argument('--head-ref', default='HEAD')
    parser.add_argument('--next-head-ref', help='exercise same-lane identity reuse across a second exact package pair')
    parser.add_argument('--evidence-dir', required=True)
    parser.add_argument('--compact-lifecycle', action='store_true')
    parser.add_argument('--with-security', action='store_true',
                        help='also execute fresh/cached upgrade-security suites against this checkout HEAD')
    parser.add_argument('--with-temporal', action='store_true',
                        help='restart the disposable node with real checkpoint pruning, prove retained audit and measure retirement')
    args = parser.parse_args()
    if not Path('/.dockerenv').exists() or {p.name for p in Path('/sys/class/net').iterdir()} != {'lo'}:
        raise SystemExit('requires disposable Docker --network none')
    for port in (9000, 9123):
        with socket.socket() as connection:
            if connection.connect_ex(('127.0.0.1', port)) == 0:
                raise SystemExit(f'refusing existing local service on {port}')
    output = Path(args.evidence_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sf221-local-') as temporary:
        root = Path(temporary)
        clone = root / 'source'
        refs = [args.head_ref] + ([args.next_head_ref] if args.next_head_ref else [])
        heads = [subprocess.check_output(['git', '-C', str(upgrade.ROOT), 'rev-parse', ref], text=True).strip() for ref in refs]
        if args.with_security and heads[-1] != subprocess.check_output(
                ['git', '-C', str(upgrade.ROOT), 'rev-parse', 'HEAD'], text=True).strip():
            raise SystemExit('--with-security requires the final head to equal the tested checkout HEAD')
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-hardlinks', str(upgrade.ROOT), str(clone)], check=True)
        subprocess.run(['git', '-C', str(clone), 'checkout', '--quiet', '--detach', args.head_ref], check=True)
        # Only the package source/history moves: the harness and built clients
        # remain this checkout's actual code. Each exact pair keeps its own IDs.
        upgrade.ROOT = clone
        os.environ['SUI_CONFIG_DIR'] = str(root / 'node')
        node_log = (root / 'node.log').open('w')
        node_dir = root / 'node'
        if args.with_temporal:
            node_dir.mkdir()
            subprocess.run([sui_toolchain.NETWORK_DEFAULT, 'genesis', '--working-dir', str(node_dir),
                            '--epoch-duration-ms', '10000', '--with-faucet'],
                           check=True, stdout=node_log, stderr=subprocess.STDOUT)
        node_command = [sui_toolchain.NETWORK_DEFAULT, 'start', '--with-faucet']
        node_command += ['--network.config', str(node_dir)] if args.with_temporal else ['--force-regenesis']
        node = subprocess.Popen(node_command, stdout=node_log, stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                if node.poll() is not None:
                    raise RuntimeError('local node exited; private logs suppressed')
                try:
                    dm.chain_facts('http://127.0.0.1:9000')
                    break
                except Exception:
                    time.sleep(1)
            else:
                raise RuntimeError('local node startup timeout')
            # Generate and fund only disposable local keys through the local faucet.
            setup = r"""
const fs = require('fs'); const {appRequire} = require('./scripts/frontend_test_utils.cjs');
const {Ed25519Keypair} = appRequire('@mysten/sui/keypairs/ed25519');
const {decodeSuiPrivateKey} = appRequire('@mysten/sui/cryptography');
const {requestSuiFromFaucetV2} = appRequire('@mysten/sui/faucet');
(async()=>{const k=Ed25519Keypair.generate(), dir=process.argv[1];
fs.writeFileSync(dir+'/sui.keystore',JSON.stringify([Buffer.concat([Buffer.from([0]),decodeSuiPrivateKey(k.getSecretKey()).secretKey]).toString('base64')]),{mode:0o600});
fs.writeFileSync(dir+'/client.yaml',`keystore:\n  File: ${dir}/sui.keystore\nenvs:\n- alias: localnet\n  rpc: http://127.0.0.1:9000\nactive_env: localnet\nactive_address: ${k.toSuiAddress()}\n`,{mode:0o600});
await requestSuiFromFaucetV2({host:'http://127.0.0.1:9123',recipient:k.toSuiAddress()});
})().catch(()=>process.exit(1));
"""
            subprocess.run(['node', '-e', setup, str(root)], check=True)
            options = dict(wallet=str(root / 'client.yaml'), network='localnet', base_ref=args.base_ref,
                max_spend_mist=1_600_000_000, required_balance_mist=None, state=str(root / 'state.json'),
                faucet_tool=None, build_sui='sui', network_sui=sui_toolchain.NETWORK_DEFAULT,
                transaction_gas_budget=60_000_000, compact_lifecycle=args.compact_lifecycle, fresh=False, walrus='local', retire_rollout_audit=False)
            outcomes = []
            for pair, head in enumerate(heads):
                subprocess.run(['git', '-C', str(clone), 'checkout', '--quiet', '--detach', head], check=True)
                for phase in ('fresh', 'retained'):
                    evidence = output / f'pair-{pair}-{phase}.json'
                    try:
                        upgrade.run(SimpleNamespace(**options, evidence=str(evidence)))
                        outcomes.append({'pair': pair, 'phase': phase, 'success': True})
                    except Spent as error:
                        outcomes.append({'pair': pair, 'phase': phase, 'success': False, 'error': str(error)})
                    # Independent effects read records gross storage as well as
                    # net ledger charges; reservations are never reported as fees.
                    receipt = json.loads(evidence.read_text())
                    for tx in receipt['transactions']:
                        if tx.get('digest'):
                            effects = upgrade.package_client(sui_toolchain.NETWORK_DEFAULT,
                                options['wallet'], 'tx-block', tx['digest'])['effects']
                            tx['gas_used'] = effects['gasUsed']
                            assert tx['status'] == effects['status']['status']
                    evidence.write_text(json.dumps(receipt, indent=2) + '\n')
                    # Retained run reuses SAME pair; no identical republication.
            if args.with_security:
                import live_upgrade_security as security
                security.Walrus = LocalStorage
                for phase in ('fresh', 'retained'):
                    security.run(SimpleNamespace(wallet=options['wallet'], network='localnet',
                        base_ref=security.DEFAULT_BASE, walrus='local', fresh=False,
                        state=str(root / 'security-state.json'), max_spend_mist=1_500_000_000,
                        evidence=str(output / f'security-{phase}.json')))
            # Wallet uses the same actual browser suite with local blob transport.
            wallet_gate.Walrus = LocalStorage
            wallet_gate.contracts_digest = lambda _: upgrade.contracts_digest(clone / 'contracts')
            for phase in ('fresh', 'retained'):
                wallet_gate.run(SimpleNamespace(wallet=options['wallet'], network='localnet',
                    state=options['state'], max_spend_mist=150_000_000,
                    evidence=str(output / f'wallet-{phase}.json')))
            if args.with_temporal:
                # Finish all historical effects reads before enabling actual pruning.
                # This changes only this disposable node, never the public reader or proof.
                node.terminate()
                node.wait(timeout=30)
                config = node_dir / 'fullnode.yaml'
                text = config.read_text()
                text = text.replace('authority-store-pruning-config:\n',
                    'authority-store-pruning-config:\n  num-epochs-to-retain-for-checkpoints: 2\n'
                    '  pruning-run-delay-seconds: 1\n  smooth: false\n')
                config.write_text(text)
                node = subprocess.Popen(node_command, stdout=node_log, stderr=subprocess.STDOUT)
                live_common.wait_for(lambda: temporal_ready(), 'pruning node did not restart', timeout=120)
                measure_temporal(options, output, root)
            (output / 'outcomes.json').write_text(json.dumps(outcomes, indent=2) + '\n')
        finally:
            for storage in LocalStorage.instances:
                storage.close()
            node.terminate()
            try:
                node.wait(timeout=15)
            except subprocess.TimeoutExpired:
                node.kill()
                node.wait()
            node_log.close()


if __name__ == '__main__':
    main()
