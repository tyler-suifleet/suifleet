#!/usr/bin/env python3
"""Read-only expiry readiness, then one fenced owner dispatch. Never a signer."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def eligible(plan, minimum, fixture):
    """Require an actual expired anchor for the intended device/writer."""
    epoch = plan.get('epoch')
    if type(epoch) is not int or epoch < 0 or epoch > 0xffffffff:
        raise ValueError('probe returned an invalid epoch')
    anchors = plan.get('anchors')
    if not isinstance(anchors, list):
        raise ValueError('probe returned no anchor list')
    for anchor in anchors:
        if (not isinstance(anchor, dict)
                or anchor.get('writerId') != fixture['writerId']
                or anchor.get('deviceId') != fixture['deviceId']
                or type(anchor.get('endEpoch')) is not int
                or not 0 <= anchor['endEpoch'] <= epoch):
            raise ValueError('probe returned an unexpired or unrelated anchor')
    return epoch >= minimum and bool(anchors)


def probe(worktree, fixture):
    data = fixture['fleet_data']
    if data['chainIdentifier'] != '4c78adac':
        raise ValueError('fixture is not the authorized testnet chain')
    config = {key: data[key] for key in ('schema', 'chainIdentifier', 'registryId',
              'packageId', 'typeOrigin', 'fleetId', 'budgetId', 'storageId')}
    # This existing reviewed helper only reads Sui/Walrus over gRPC. No wallet,
    # transaction arguments, key material or signing command is passed.
    result = subprocess.run(['node', str(worktree/'scripts/reclaim_plan.cjs'),
        'https://fullnode.testnet.sui.io:443', json.dumps(config),
        fixture['packages']['storage']['package'], data['deviceId']],
        cwd=worktree, check=True, capture_output=True, text=True, timeout=60)
    return json.loads(result.stdout)


def tick(engine, project, journal, ticket, number, minimum, execute=False, read_probe=probe):
    if type(minimum) is not int or not 0 <= minimum <= 0xffffffff or type(number) is not int or number <= 0:
        raise ValueError('invalid PR or minimum epoch')
    old = json.loads(journal.read_text()) if journal.exists() else {}
    if old and any(old.get(key) != value for key, value in
                   (('ticket', ticket), ('pr', number), ('minimum_epoch', minimum))):
        raise ValueError('journal belongs to another readiness condition')
    if old.get('phase') in ('dispatched', 'complete', 'evidence_present', 'operator_required'):
        return old
    # An interrupted launch is never blindly repeated. The regular coordinator
    # collects its durable reserved/process receipts and owns any recovery.
    if old.get('phase') == 'launching':
        row = engine.store.db.execute('SELECT id,status FROM jobs WHERE project=? AND pr=? '
            'AND kind=? AND head=? AND feedback=?',
            (project['id'], number, 'fix', old['head'], old['feedback'])).fetchone()
        usable = row and row['status'] not in ('failed', 'orphaned', 'quarantined')
        result = {**old, 'phase': 'dispatched' if usable else 'operator_required',
                  'job': row['id'] if row else None, 'job_status': row['status'] if row else None}
        atomic(journal, result)
        return result
    owner = json.loads(engine.ticket(project, 'show', ticket))
    result = dict(ticket=ticket, pr=number, minimum_epoch=minimum, observed_at=time.time())
    if owner['status'] == 'done':
        result['phase'] = 'complete'
    elif (owner['status'] != 'review' or owner.get('publish_pending') or owner.get('expired')
          or owner.get('agent') not in ('Codex', 'Claude')):
        result['phase'] = 'waiting_owner'
    elif engine.store.db.execute('SELECT 1 FROM jobs WHERE project=? AND pr=? AND status IN '
            "('reserved','running','orphaned','quarantined','quota_wait','quota_launch')",
            (project['id'], number)).fetchone():
        result['phase'] = 'waiting_job'
    else:
        worktree = Path(owner['worktree'])
        dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=worktree,
            check=True, capture_output=True, text=True, timeout=30).stdout
        if dirty.strip():
            raise ValueError('owner worktree is dirty; no probe or dispatch')
        fixture = json.loads((worktree/'docs/validation/sf082-testnet-native.json').read_text())
        receipt = json.loads((worktree/'docs/validation/sf129-testnet.json').read_text())
        if receipt.get('reclaim'):
            # Supplied evidence still requires independent review; do not approve it.
            result['phase'] = 'evidence_present'
        else:
            plan = read_probe(worktree, fixture)
            ready = eligible(plan, minimum, fixture['fleet_data'])
            result.update(phase='ready' if ready else 'waiting_epoch',
                          epoch=plan['epoch'], eligible_anchors=len(plan['anchors']))
            if ready and execute:
                pr = engine.pr(project, number)
                if (pr['state'] != 'OPEN' or pr.get('isDraft') or pr.get('isCrossRepository')
                        or pr['headRefName'] != owner['branch']):
                    raise ValueError('PR ownership/state changed')
                marker = f'<!-- acceptance-expiry:{ticket}:{minimum} -->'
                note = (f'{marker}\nRead-only acceptance readiness: Walrus epoch {plan["epoch"]}, '
                    f'{len(plan["anchors"])} eligible expired fixture anchors. Resume the existing '
                    'owner to run scripts/live_fleet_reclaim.py --reclaim with chain/account/fixture '
                    'authority, current bytecode and type-origin verification. Hold/assert/renew '
                    'network:suifleet-testnet only during mutations, preserve strict non-replenishing '
                    'allowances and unknown transactions, and release promptly. Record actual '
                    'execution effects, digest, deleted objects and net rebate; return Review. '
                    'This observation is not successful reclaim evidence or merge approval. '
                    'No mainnet, production credential or hardware operation is authorized.')
                if not any(marker in comment['body'] for comment in pr['comments']):
                    engine.gh(project, ['pr', 'comment', str(number), '--repo', project['repository'], '--body', note])
                pr = engine.pr(project, number)
                # No separate orchestrator scan or merge loop: use the existing
                # transactional per-PR reservation/claim checks shared by workers.
                import coordinator
                result.update(phase='launching', head=pr['headRefOid'],
                    feedback=coordinator.feedback(pr),
                    claim_sha256=hashlib.sha256(owner['token'].encode()).hexdigest())
                atomic(journal, result)
                engine.launch(project, pr, owner, 'fix')
                row = engine.store.db.execute('SELECT id,status FROM jobs WHERE project=? AND pr=? '
                    'AND kind=? AND head=? AND feedback=?',
                    (project['id'], number, 'fix', result['head'], result['feedback'])).fetchone()
                if row:
                    result.update(phase='operator_required' if row['status'] in
                        ('failed', 'orphaned', 'quarantined') else 'dispatched',
                        job=row['id'], job_status=row['status'])
                else:
                    # A concurrent existing job won the per-PR reservation. It
                    # keeps ownership; a later fresh probe may dispatch after it.
                    result['phase'] = 'waiting_job'
    atomic(journal, result)
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--project', default='suifleet')
    parser.add_argument('--ticket', default='SF-129')
    parser.add_argument('--pr', type=int, default=279)
    parser.add_argument('--min-epoch', type=int, default=563)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if args.project != 'suifleet' or args.ticket != 'SF-129' or args.pr != 279:
        parser.error('this reviewed probe supports only the SF-129 Testnet fixture')
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = args.state/'sf129-expiry-watch.json'
    with (args.state/'sf129-expiry-watch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.path.insert(0, str(args.runtime))
        try:
            from coordinator import Coordinator
            config = json.loads(args.config.read_text())
            project = next(p for p in config['projects'] if p['id'] == args.project)
            engine = Coordinator(config, args.state)
            result = tick(engine, project, journal, args.ticket, args.pr, args.min_epoch, args.execute)
        except Exception as error:
            # CLI exceptions can contain argv with claim tokens. Never print
            # their message/traceback or erase a persisted launch-intent fence.
            result = json.loads(journal.read_text()) if journal.exists() else dict(
                ticket=args.ticket, pr=args.pr, minimum_epoch=args.min_epoch, phase='probe_error')
            result.update(last_error_type=type(error).__name__, error_at=time.time())
            atomic(journal, result)
            print(json.dumps(result, sort_keys=True))
            raise SystemExit(1)
        print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
