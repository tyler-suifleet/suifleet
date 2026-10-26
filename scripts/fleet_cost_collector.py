#!/usr/bin/env python3
"""Read-only, hash-pinned observation ingestion. SQLite is the durable checkpoint."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3

import deployment_manifest as dm
from fleet_costs import CATEGORIES, calculate, effects_cost, integer

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result


def positive(value):
    if integer(value) < 1:
        raise ValueError('positive integer required')


def validate(run):
    if run['schema_version'] != 1:
        raise ValueError('unsupported run schema')
    deployment = dm.validate(run['deployment'])
    if deployment['network'] not in ('mainnet', 'testnet', 'localnet'):
        raise ValueError('unsupported accounting network')
    for key in ('run_id', 'fleet_id', 'provenance'):
        if not isinstance(run[key], str) or not run[key].strip():
            raise ValueError('missing run identity/provenance')
    accounts = run['accounts']
    if not accounts or len(set(accounts)) != len(accounts):
        raise ValueError('empty/duplicate consolidated accounts')
    for account in accounts:
        if dm.object_id(account) != account:
            raise ValueError('use canonical account IDs')
    for revision in ('code', 'workload'):
        if not re.fullmatch('[0-9a-f]{40}', run['revisions'][revision]):
            raise ValueError('exact git revisions required')
    for key in ('devices', 'shared_releases', 'report_seconds', 'upload_seconds',
                'retention_epochs', 'poll_seconds', 'max_transactions', 'max_records'):
        positive(run['workload'][key])
    start, end = map(timestamp, (run['interval']['start'], run['interval']['end']))
    if not 0 < (end - start).total_seconds() <= 31 * 86400:
        raise ValueError('observation interval must be bounded to 31 days')
    # Unknown required services may never silently disappear.
    if set(run['required_categories']) != CATEGORIES:
        raise ValueError('all accounting categories must be explicit')
    if len(run.get('transactions', [])) > run['workload']['max_transactions']:
        raise ValueError('digest plan exceeds transaction bound')
    for tx in run.get('transactions', []):
        if tx['category'] not in CATEGORIES:
            raise ValueError('invalid planned category')
    for source in run['sources']:
        if (not re.fullmatch('[0-9a-f]{64}', source['sha256'])
                or not source['provenance'].strip() or not source['approved_by'].strip()):
            raise ValueError('trusted source pin and out-of-band approver required')
    return run


def validate_record(row, run):
    if not row.get('id') or not row.get('source'):
        raise ValueError('record ID/source required')
    time = timestamp(row['at'])
    if not timestamp(run['interval']['start']) <= time <= timestamp(run['interval']['end']):
        raise ValueError('record outside run interval')
    kind = row['type']
    if kind == 'transaction':
        if row['category'] not in CATEGORIES or row['status'] not in ('unknown', 'success', 'failure'):
            raise ValueError('invalid transaction category/status')
        if not re.fullmatch('[1-9A-HJ-NP-Za-km-z]{32,44}', row['id']):
            raise ValueError('invalid transaction digest')
        if row['status'] != 'unknown':
            effects_cost(row['gas'])
            if dm.object_id(row['sender']) != row['sender']:
                raise ValueError('canonical sender required')
            if dm.object_id(row['gas_owner']) != row['gas_owner']:
                raise ValueError('canonical gas owner required')
            # External funding relevance is checked against linked transfers below.
    elif kind == 'walrus':
        if row['operation'] not in ('purchase', 'renewal', 'reclaim'):
            raise ValueError('invalid Walrus operation')
        if row['category'] not in ('walrus_storage', 'walrus_write', 'renewal', 'cleanup', 'recipe_storage'):
            raise ValueError('invalid Walrus accounting category')
        positive(row['encoded_bytes'])
        if integer(row['end_epoch']) <= integer(row['start_epoch']):
            raise ValueError('invalid purchased retention')
        if row['frost'] is not None:
            integer(row['frost'])
        if not row['digest'] or not row['blob_id']:
            raise ValueError('Walrus transaction/blob linkage required')
    elif kind == 'requests':
        if row['basis'] not in ('measured', 'estimated', 'unavailable'):
            raise ValueError('invalid request basis')
        for key in ('requests', 'download_bytes'):
            if row[key] is not None:
                integer(row[key])
        if row['basis'] == 'unavailable' and any(row[k] is not None for k in ('requests', 'download_bytes')):
            raise ValueError('unavailable counts must be null')
        if row['provider_usd'] is not None:
            # Provider invoices are recorded but not auto-booked without scope review.
            from decimal import Decimal
            amount = Decimal(str(row['provider_usd']))
            if not amount.is_finite() or amount < 0:
                raise ValueError('invalid provider charge')
    elif kind == 'balance':
        if row['account'] not in run['accounts'] or row['unit'] not in ('MIST', 'FROST'):
            raise ValueError('balance outside consolidated scope')
        integer(row['amount'])
        if row['boundary'] not in ('start', 'end'):
            raise ValueError('invalid balance boundary')
    elif kind == 'transfer':
        if row['unit'] not in ('MIST', 'FROST'):
            raise ValueError('invalid transfer currency')
        integer(row['amount'])
        if any(dm.object_id(row[k]) != row[k] for k in ('from', 'to')):
            raise ValueError('canonical transfer accounts required')
        if row['from'] not in run['accounts'] and row['to'] not in run['accounts']:
            raise ValueError('transfer outside scope')
        if not row['digest']:
            raise ValueError('transfer needs transaction linkage')
    elif kind == 'capital':
        if row['unit'] not in ('MIST', 'FROST'):
            raise ValueError('invalid capital currency')
        integer(row['amount'])
        if row['kind'] not in ('refundable_storage', 'gas_float', 'wal_reserve'):
            raise ValueError('invalid capital kind')
        if not row['object_or_account']:
            raise ValueError('capital identity required')
    else:
        raise ValueError('unknown record type')


def ingest(run_path, database):
    run_path = Path(run_path)
    run = validate(json.loads(run_path.read_text()))
    with sqlite3.connect(database) as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS run (json TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS sources (hash TEXT PRIMARY KEY, provenance TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, json TEXT NOT NULL, PRIMARY KEY(kind,id))')
        db.execute('CREATE TABLE IF NOT EXISTS history (kind TEXT, id TEXT, json TEXT, source_hash TEXT)')
        # Serialize concurrent resume processes before checking run/source/record state.
        db.execute('BEGIN IMMEDIATE')
        identity = dict(run)
        identity.pop('sources')
        encoded = canonical(identity)
        previous = db.execute('SELECT json FROM run').fetchone()
        if previous and previous[0] != encoded:
            raise ValueError('resume run/deployment/workload changed')
        if not previous:
            db.execute('INSERT INTO run VALUES (?)', (encoded,))
        for source in run['sources']:
            raw = (run_path.parent / source['path']).read_bytes()
            if sha(raw) != source['sha256']:
                raise ValueError('source hash mismatch')
            if db.execute('SELECT 1 FROM sources WHERE hash=?', (source['sha256'],)).fetchone():
                continue
            batch = json.loads(raw)
            if (batch['network'] != run['deployment']['network']
                    or batch['chain_identifier'] != run['deployment']['chain_identifier']
                    or batch['run_id'] != run['run_id']):
                raise ValueError('source network/chain/run mismatch')
            if len(batch['records']) > run['workload']['max_records']:
                raise ValueError('source record bound exceeded')
            for row in batch['records']:
                validate_record(row, run)
                key = (row['type'], row['id'])
                value = canonical(row)
                old = db.execute('SELECT json FROM records WHERE kind=? AND id=?', key).fetchone()
                if old and old[0] != value:
                    prior = json.loads(old[0])
                    if (row['type'] == 'transaction' and prior['status'] == row['status'] == 'unknown'
                            and prior['category'] == row['category']):
                        continue
                    # Only an unresolved digest can advance to final effects.
                    if not (row['type'] == 'transaction' and prior['status'] == 'unknown'
                            and row['status'] != 'unknown'
                            and all(prior[k] == row[k] for k in ('id', 'type', 'category'))):
                        raise ValueError('conflicting duplicate observation')
                if not old or old[0] != value:
                    db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)', (*key, value))
                    db.execute('INSERT INTO history VALUES (?,?,?,?)', (*key, value, source['sha256']))
            if db.execute('SELECT count(*) FROM records').fetchone()[0] > run['workload']['max_records']:
                raise ValueError('run record bound exceeded')
            if db.execute("SELECT count(*) FROM records WHERE kind='transaction'").fetchone()[0] > run['workload']['max_transactions']:
                raise ValueError('transaction bound exceeded')
            db.execute('INSERT INTO sources VALUES (?,?)', (source['sha256'], canonical(source)))
        # Validate derived reconciliation before committing any checkpoint.
        result = report(database, connection=db)
    return result


def report(database, connection=None):
    from contextlib import nullcontext
    with nullcontext(connection) if connection is not None else sqlite3.connect(database) as db:
        run = json.loads(db.execute('SELECT json FROM run').fetchone()[0])
        rows = [json.loads(r[0]) for r in db.execute('SELECT json FROM records ORDER BY kind,id')]
        sources = [json.loads(r[0]) for r in db.execute('SELECT provenance FROM sources ORDER BY hash')]
    network = run['deployment']['network']
    txs = {r['id']: r for r in rows if r['type'] == 'transaction'}
    # Renamed observations must not double-book the same economic event.
    for kind, fields in (
        ('transfer', ('digest', 'unit', 'from', 'to')),
        ('walrus', ('digest', 'blob_id', 'operation', 'category')),
        ('capital', ('object_or_account', 'kind', 'unit', 'at')),
    ):
        keys = [tuple(r[k] for k in fields) for r in rows if r['type'] == kind]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate economic observation: ' + kind)
    linked_funding = {r['digest'] for r in rows if r['type'] == 'transfer'}
    lines, gaps = [], []
    for tx in txs.values():
        if (tx['status'] != 'unknown' and tx['sender'] not in run['accounts']
                and tx['gas_owner'] not in run['accounts'] and tx['id'] not in linked_funding):
            raise ValueError('transaction outside workload accounts')
        if tx['status'] == 'unknown':
            gaps.append('unresolved transaction: ' + tx['id'])
        elif tx['gas_owner'] in run['accounts']:
            lines.append(dict(name=tx['id'], category=tx['category'], kind='measured',
                              unit='MIST', count=1, network=network, source=tx['source'],
                              digest=tx['id'], status=tx['status'], effects=tx['gas']))
    for row in rows:
        if row['type'] == 'walrus':
            tx = txs.get(row['digest'])
            if row['frost'] is None or not tx or tx['status'] != 'success':
                gaps.append('unsettled WAL observation: ' + row['id'])
            else:
                lines.append(dict(name=row['id'], category=row['category'], kind='measured',
                                  unit='FROST', count=1, network=network,
                                  source=row['source'], amount=row['frost']))
        elif row['type'] == 'requests':
            if row['basis'] != 'measured' or row['requests'] is None or row['download_bytes'] is None:
                gaps.append('unmeasured request/download coverage: ' + row['id'])
            if row['provider_usd'] is None:
                gaps.append('unavailable provider charge: ' + row['id'])
    if not any(r['type'] == 'requests' for r in rows):
        gaps.append('missing request/download/provider observations')
    if not any(r['type'] == 'capital' for r in rows):
        gaps.append('missing refundable capital/reserve snapshots')
    seen = {r['category'] for r in lines}
    for category in sorted(CATEGORIES - seen):
        lines.append(dict(name=category, category=category, kind='unquoted', unit='USD',
                          count=1, network=network, source='not supplied', amount=None))
    # Balance reconciliation is whole-fleet, per currency; internal transfers cancel.
    flows = []
    for unit in ('MIST', 'FROST'):
        balances = {}
        for row in rows:
            if row['type'] == 'balance' and row['unit'] == unit:
                key = (row['account'], row['boundary'])
                if key in balances:
                    raise ValueError('duplicate balance boundary')
                balances[key] = int(row['amount'])
        if len(balances) != len(run['accounts']) * 2:
            gaps.append('missing consolidated balance boundaries: ' + unit)
            continue
        transfers = [r for r in rows if r['type'] == 'transfer' and r['unit'] == unit]
        for transfer in transfers:
            if transfer['digest'] not in txs or txs[transfer['digest']]['status'] != 'success':
                gaps.append('unsettled transfer: ' + transfer['id'])
        transfers = [r for r in transfers if r['digest'] in txs and txs[r['digest']]['status'] == 'success']
        flows.append(dict(name=unit, start=sum(balances[(a, 'start')] for a in run['accounts']),
                          end=sum(balances[(a, 'end')] for a in run['accounts']),
                          external_in=sum(int(r['amount']) for r in transfers
                                          if r['from'] not in run['accounts']),
                          external_out=sum(int(r['amount']) for r in transfers
                                           if r['to'] not in run['accounts']),
                          consumed=sum(effects_cost(r['effects']) if unit == 'MIST' else int(r['amount'])
                                       for r in lines if r['unit'] == unit)))
    model = dict(schema_version=1, workload={k: run['workload'][k] for k in ('devices', 'shared_releases')},
                 lines=lines, token_flows=flows)
    accounting = calculate(model)
    gaps.extend(accounting['missing_or_excluded'])
    if any(accounting['token_reconciliation_residuals'].values()):
        gaps.append('nonzero token reconciliation')
    gaps.append('final interval coverage, provider invoices and lifetime capital review require SF135 acceptance')
    return dict(schema_version=1, run=run, sources=sources, records=rows,
                accounting_model=model, accounting=accounting,
                complete_mainnet_bill=False, incomplete=gaps,
                capital_snapshots=[r for r in rows if r['type'] == 'capital'],
                subscription_input=None,
                subscription_limit='No automatic monthly scaling or USD pricing. Review complete Mainnet recurring consumption, FX and working capital before subscription_pricing.py.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--database', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = ingest(args.run, args.database)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(2, f'Collection refused: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
