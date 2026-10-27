#!/usr/bin/env python3
"""Publish/update/rollback a verified static export on dedicated Testnet.

The upstream builder is checksum/version pinned. Its legacy wallet transport is
not used: the pinned-contract gRPC adapter signs only with an isolated publisher,
whose whole remaining allowance is charged before it runs. No server is started.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile

import build_walrus_site as site
import deployment_manifest as dm
from live_common import Live, key_wallet, pending_key
from live_fleet_budget import EXCHANGE_PACKAGE, EXCHANGE, net_gas

FUNDING = 700_000_000


def refresh_effects(saved, request, state, executed):
    """Fence a readback-failed plan before giving its recovery a new intent label."""
    for key in ('files', 'rpc', 'owner', 'journal', 'key'):
        if saved.get(key) != request.get(key):
            raise ValueError('refresh cannot change the pending export or publisher boundary')
    if any(intent.get('status') != 'success' for intent in state['intents'].values()):
        raise ValueError('reconcile unknown/failed publisher intents before refresh')
    operations = saved.get('operations')
    if not operations or not request.get('operations') or operations == request['operations']:
        raise ValueError('refresh requires a distinct explicit operation')
    verified = []
    for operation in operations:
        intent = state['intents'].get('site/' + operation['name'])
        if not intent or intent.get('status') != 'success':
            raise ValueError('the pending Site transaction is not confirmed successful')
        result = executed(intent['digest'])
        if not result or result.get('digest') != intent['digest'] or result.get('effects', {}).get('status', {}).get('status') != 'success':
            raise ValueError('pending Site effects could not be independently verified')
        verified.append(intent['digest'])
    return verified


def run(args):
    if not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
        raise RuntimeError('serialized wallet resource required')
    os.umask(0o077)
    builder_pin = site.verified_builder(args.builder)
    walrus_pin = json.loads((site.ROOT / 'tools/walrus-sites/walrus-toolchain.json').read_text())
    if hashlib.sha256(args.walrus.read_bytes()).hexdigest() != walrus_pin['binary_sha256']:
        raise ValueError('Walrus binary differs from reviewed pin')
    if subprocess.check_output([str(args.walrus), '--version'], text=True).strip() != walrus_pin['version_output']:
        raise ValueError('Walrus version differs from reviewed pin')
    config = site.ROOT / 'tools/walrus-sites' / walrus_pin['client_config']['path']
    if hashlib.sha256(config.read_bytes()).hexdigest() != walrus_pin['client_config']['sha256']:
        raise ValueError('Walrus Testnet config differs from reviewed pin')
    receipt = json.loads(args.receipt.read_text())
    if site.inventory(args.directory) != receipt['files']:
        raise ValueError('static export differs from its build receipt')
    manifest = json.loads((args.directory / 'deployment-manifest.json').read_text())
    dm.validate(manifest)
    if manifest['network'] != 'testnet' or manifest['chain_identifier'] != '4c78adac':
        raise ValueError('only the dedicated Testnet is authorized')
    live = Live(args.wallet, 'testnet', args.max_spend_mist)
    live.preflight()
    dm.verify(manifest)
    prior = json.loads(args.resume_evidence.read_text()) if args.resume_evidence else None
    if prior and (prior['chain'] != live.chain or prior['operator'] != live.operator):
        raise ValueError('foreign publisher receipt')
    root = Path(prior['runtime']) if prior else Path(tempfile.mkdtemp(prefix='sf082-site-', dir=args.runtime_dir))
    funding_file = root / 'publisher-funding.json'
    journal = root / 'grpc-journal.json'
    key_path = live.wallet.parent / 'sf082-site.key'
    receipt_hash = hashlib.sha256(args.receipt.read_bytes()).hexdigest()
    staged = root / 'releases' / receipt_hash
    if not staged.exists():
        staged.parent.mkdir(exist_ok=True)
        shutil.copytree(args.directory, staged)
    if site.inventory(staged) != receipt['files']:
        raise ValueError('retained release differs from build receipt')
    if prior:
        funding = json.loads(funding_file.read_text())
        wallet = root / 'site-wallet/client.yaml'
        if (root / 'site-wallet/test.key').read_bytes() != key_path.read_bytes():
            raise ValueError('reserved publisher identity changed')
        address = live.client('active-address', json_output=False, config=wallet).stdout.strip()
        if address != funding['owner']:
            raise ValueError('publisher wallet address changed')
        existing = json.loads(journal.read_text())
        live.ledger.grant(existing['limit'] - existing['used'] + funding['swap_mist'])
    else:
        if key_path.exists():
            raise ValueError('an isolated publisher remains reserved; resume its receipt instead of replacing its key')
        seed = secrets.token_hex(32)
        pending_key(key_path, seed)
        wallet, address = key_wallet(live, seed, root, 'site-wallet')
        funding = {'owner': address, 'grant_mist': FUNDING, 'swap_mist': 20_000_000, 'initial_used': 0}
    live.evidence.update(runtime=str(root), builder=builder_pin, walrus=walrus_pin,
                         build_receipt_sha256=receipt_hash, site_owner=address,
                         adapter='pinned Site ABI + Sui/Walrus SDK over gRPC; upstream 2.12.2 JSON-RPC wallet unsupported')
    def owner_transfer(label, amount, marker, before_limit=0):
        if marker.exists():
            record = json.loads(marker.read_text())
            if record.get('status') != 'success':
                raise RuntimeError('publisher funding outcome uncertain; reconcile effects before retrying')
            return record
        record = {'status': 'unknown', 'amount': amount, 'before_limit': before_limit}
        marker.write_text(json.dumps(record))
        r = live.transaction(label, '--split-coins', 'gas', f'[{amount}u64]', '--assign', 'float',
                             '--transfer-objects', '[float]', f'@{address}', budget=15_000_000, funding=amount)
        record.update(status='success', digest=r['digest'], operator=live.operator)
        marker.write_text(json.dumps(record))
        return record

    try:
        if not prior:
            owner_transfer('fund isolated Site publisher', FUNDING, root / 'initial-funding.json')
            child = live.child(wallet, FUNDING)
            swap = root / 'swap-pending.json'
            if swap.exists():
                raise RuntimeError('reconcile the retained WAL purchase before retrying')
            swap.write_text(json.dumps({'status': 'unknown'}))
            r = live.transaction('buy bounded Site WAL', '--split-coins', 'gas', '[20000000u64]', '--assign', 'sui',
                                 '--move-call', f'{EXCHANGE_PACKAGE}::wal_exchange::exchange_all_for_wal',
                                 f'@{EXCHANGE}', 'sui', '--assign', 'wal', '--transfer-objects', '[wal]', f'@{address}',
                                 budget=15_000_000, funding=20_000_000, sender=child)
            funding['initial_used'] = max(0, net_gas(r)) + funding['swap_mist']
            funding_file.write_text(json.dumps(funding))
            swap.unlink()
        state = json.loads(journal.read_text()) if journal.exists() else {'phases': [], 'limit': FUNDING}
        expansion = None
        if args.add_funding_mist:
            marker = root / f'expansion-{len(state["phases"])}-{receipt_hash}.json'
            record = owner_transfer('explicit publisher allowance expansion', args.add_funding_mist, marker, state['limit'])
            expansion = {'digest': record['digest'], 'amount': record['amount'], 'operator': record['operator']}
        request = {'rpc': live.rpc_url, 'directory': str(staged), 'files': receipt['files'], 'journal': str(journal),
                   'key': str(root / 'site-wallet/test.key'), 'owner': address,
                   'allowance': (record['before_limit'] + record['amount']) if expansion else state['limit'], 'initialUsed': funding['initial_used'],
                   'walrusPackage': '0x7d3b2086808a62bbc44d18c6b1d407973d77dd26946735873092c2f5650b4611'}
        if expansion:
            request['expansion'] = expansion
        if args.operation != 'rehearse':
            if args.operation != 'publish' and not state.get('siteId'):
                raise ValueError('update/rollback requires the retained owned Site journal')
            request['operations'] = [{'name': f'{args.operation}/{len(state["phases"])}/{receipt_hash}',
                                      'directory': str(staged), 'files': receipt['files']}]
        plan = root / 'pending-plan.json'
        if plan.exists():
            saved = json.loads(plan.read_text())
            if saved.get('files') != request['files']:
                raise ValueError('reconcile the pending publication before selecting another release')
            if args.operation == 'refresh' and saved.get('operations') != request.get('operations'):
                verified = refresh_effects(saved, request, state, lambda digest: live.executed(digest, config=wallet))
                # Archive the original intent and error verbatim; never mark its readback successful.
                archive = root / ('reconciled-plan-' + hashlib.sha256(plan.read_bytes()).hexdigest() + '.json')
                if archive.exists():
                    raise ValueError('refresh archive already exists; inspect recovery before retrying')
                error = Path(str(plan) + '.error')
                if error.exists():
                    error.rename(Path(str(archive) + '.error'))
                plan.rename(archive)
                plan.write_text(json.dumps(request))
                live.evidence['reconciled_site_intents'] = verified
                live.evidence['archived_pending_plan'] = archive.name
            else:
                request = saved
        else:
            plan.write_text(json.dumps(request))
        live.assert_lock()
        subprocess.run(['node', 'scripts/live_walrus_site.cjs', str(plan)], check=True, timeout=1200)
        result = json.loads(journal.read_text())
        evidence = []
        for intent in result['intents'].values():
            r = live.executed(intent['digest'], config=wallet)
            if not r or r['effects']['status']['status'] != intent['status']:
                raise RuntimeError('publisher effects could not be independently verified')
            if max(0, net_gas(r)) != intent['netGasMist']:
                raise RuntimeError('publisher gas differs from independent effects')
            evidence.append({'digest': r['digest'], 'status': intent['status'], 'net_gas_mist': net_gas(r),
                             'gas': r['effects']['gasUsed']})
        if funding['initial_used'] + sum(max(0, r['net_gas_mist']) for r in evidence) != result['used']:
            raise RuntimeError('publisher allowance accounting mismatch')
        live.evidence.update(site_id=result['siteId'], phases=result['phases'], native_transactions=evidence,
                             publisher_allowance_mist=result['limit'], publisher_used_mist=result['used'],
                             retained_publisher_identity=True)
        live.check('Sui effects, full resource bytes/ranges/headers and routing readback verified')
        plan.unlink()
    finally:
        live.finish(args.evidence)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallet', required=True)
    parser.add_argument('--builder', type=Path, required=True)
    parser.add_argument('--walrus', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--max-spend-mist', type=int, default=800_000_000)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--resume-evidence', type=Path)
    parser.add_argument('--operation', choices=['publish', 'update', 'rollback', 'refresh', 'rehearse'], default='rehearse')
    parser.add_argument('--add-funding-mist', type=int, default=0)
    run(parser.parse_args())
