"""Reconcile uncertain testnet setup mutations against executed effects (SF-146).

A checkpointed setup mutation records its intent before anything is built and,
once the unsigned transaction is verified, its exact bytes and digest, both
durably before signing. A later run therefore knows the only transaction the
mutation can ever have executed as, and decides its outcome from chain facts:

* found by digest: adopt the executed effects (or clear a failed one);
* not found and provably dead (its ValidDuring/Epoch window has passed, or a
  gas coin it spends has moved): it never executed and never can;
* not found and still executable: submit the identical verified bytes again.
  A transaction executes at most once, so this cannot duplicate anything.

Historical markers written before identities were retained (a bare label) have
an explicit, authenticated path for publication steps only: scan the wallet's
unrecorded UpgradeCaps for executed effects with the exact verified bytecode,
and conclude "never executed" only after every transaction the marker could
stand for has expired. The marker is never deleted to force progress.

Fullnodes prune transaction history (about two weeks on public testnet), so an
automatic "not executed" conclusion is only drawn for recent operations.
"""
import json
from pathlib import Path
import re
import subprocess

import sui_toolchain as toolchain

ROOT = Path(__file__).resolve().parents[1]
RETENTION_SECONDS = 3 * 24 * 3600
LEGACY_LABELS = ('publish base', 'upgrade to head')
OBJECT_ID = re.compile(r'0x[0-9a-fA-F]{64}')


class Uncertain(RuntimeError):
    """The outcome cannot be decided yet; the durable marker is retained."""


class Unexpired(Uncertain):
    """A historical transaction without identity shows no executed effects but may still execute."""


def normal_pending(value):
    if isinstance(value, str) and value:
        return {'label': value, 'legacy': True}
    if isinstance(value, dict) and isinstance(value.get('label'), str) and value['label']:
        return value
    raise Uncertain('malformed pending operation marker; explicit reconciliation required')


def known_objects(state):
    """Every object ID the private state records anywhere (fixtures, publications, suites)."""
    return {toolchain.normal(match) for match in OBJECT_ID.findall(json.dumps(state))}


def cli_result(facts):
    """Executed gRPC facts in the `sui client --json` shape callers already parse."""
    changes = []
    for created in facts['created']:
        if created['type'] == 'package':
            changes.append({'type': 'published', 'packageId': created['objectId']})
        else:
            changes.append({'type': 'created', 'objectType': created['type'], 'objectId': created['objectId']})
    return {'digest': facts['digest'], 'objectChanges': changes, 'reconciled': True, 'gas_mist': net_gas(facts),
            'effects': {'status': {'status': 'success' if facts['success'] else 'failure', 'error': facts['error']},
                        'gasUsed': facts['gas_used']}}


def net_gas(facts):
    used = facts['gas_used']
    return max(0, int(used['computationCost']) + int(used['storageCost']) - int(used['storageRebate']))


class Chain:
    """Read-only gRPC facts through scripts/testnet_reconcile.cjs."""

    def __init__(self, rpc, run=subprocess.run):
        self.rpc, self.run = rpc, run

    def read(self, *args):
        result = self.run(['node', str(ROOT / 'scripts/testnet_reconcile.cjs'), self.rpc, *map(str, args)],
                          capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise Uncertain(f'chain facts unavailable ({args[0]}): {result.stderr.strip()[-300:]}')
        return json.loads(result.stdout)

    def transaction(self, digest):
        facts = self.read('transaction', digest)
        return facts if facts.get('found') else None

    def epoch(self):
        return int(self.read('epoch')['epoch'])

    def caps(self, owner):
        return self.read('caps', owner)

    def coins(self, owner):
        return {toolchain.normal(c['objectId']): c['version'] for c in self.read('coins', owner)}

    def versions(self, ids):
        if not ids:
            return {}
        return {toolchain.normal(o['objectId']): (o['version'] if o['exists'] else None)
                for o in self.read('objects', *ids)}


def settle(facts, operator):
    if (toolchain.normal(facts['sender']) != operator or toolchain.normal(facts['gas_owner']) != operator):
        raise Uncertain(f'transaction {facts["digest"]} was not sent and paid by the verified wallet')
    return ('executed' if facts['success'] else 'failed'), cli_result(facts)


def dead(tx, chain):
    """Why the retained transaction can never execute, or None."""
    expiry = (tx.get('expiration') or {}).get('max_epoch')
    epoch = chain.epoch()
    if expiry is not None and epoch > int(expiry):
        return f'expired: current epoch {epoch} is past its last valid epoch {expiry}'
    coins = [p for p in tx.get('gas_payment') or [] if str(p['version']) != '0']  # 0: address-balance reservation
    current = chain.versions([p['objectId'] for p in coins])
    for payment in coins:
        version = current.get(toolchain.normal(payment['objectId']))
        if version is None or int(version) > int(payment['version']):
            return f'gas coin {payment["objectId"]} moved past version {payment["version"]}'
    return None


def reconcile(pending, *, chain, operator, inspect, verify, resubmit, now, legacy=None):
    """Decide one pending setup mutation.

    Returns ('executed', result), ('failed', result) or ('absent', reason), where
    a result has the `sui client --json` shape.
    Raises Uncertain when the outcome cannot be decided; the caller keeps the
    marker. `verify(data, tx)` re-checks the retained bytes against the step's
    intent (raising toolchain.NotSubmitted); `resubmit(tx)` sends the identical
    verified bytes through the spending ledger.
    """
    operator = toolchain.normal(operator)
    pending = normal_pending(pending)
    tx = pending.get('tx')
    if pending.get('legacy'):
        if legacy is None:
            raise Uncertain(f'historical pending {pending["label"]!r} has no retained transaction; explicit reconciliation required')
        return legacy(pending)
    if not tx:
        # Bytes are persisted before signing, so without them nothing was ever signed.
        return 'absent', 'interrupted before any transaction was verified or signed'
    data = inspect(tx['tx_bytes'])
    if (data.get('digest') != tx.get('digest') or toolchain.normal(data['sender']) != operator
            or toolchain.normal(tx.get('sender', '0x0')) != operator):
        raise Uncertain('retained transaction identity does not match its bytes or wallet (tampered provenance)')
    try:
        verify(data, tx)
    except toolchain.NotSubmitted as error:
        raise Uncertain(f'retained transaction no longer matches its verified intent: {error}') from error
    # The death proof and the ledger reservation use only fields re-derived from
    # the verified bytes; any disagreement with the retained copy is tampering.
    try:
        derived = toolchain.identity(tx['tx_bytes'], data, data['gas_budget'])
    except (KeyError, TypeError, ValueError) as error:
        raise Uncertain(f'retained transaction bytes lack a decodable identity: {error}') from error
    mismatched = sorted(key for key in derived if tx.get(key) != derived[key])
    if mismatched:
        raise Uncertain(f'retained transaction {mismatched} differ from its bytes (tampered provenance)')
    tx = derived
    facts = chain.transaction(tx['digest'])
    if facts:
        return settle(facts, operator)
    reason = dead(tx, chain)
    if reason:
        if now - float(pending.get('created') or 0) > RETENTION_SECONDS:
            raise Uncertain('pending transaction is older than the automatic history window; explicit reconciliation required')
        facts = chain.transaction(tx['digest'])  # Executed while we were deciding?
        if facts:
            return settle(facts, operator)
        return 'absent', f'{tx["digest"]} never executed and never can ({reason})'
    return 'executed', resubmit(tx)


def publish_matches(facts, built, operator):
    kinds = [c['kind'] for c in facts['commands']]
    if not facts['success'] or kinds != ['Publish', 'TransferObjects']:
        return False
    if toolchain.normal(facts['sender']) != operator or toolchain.normal(facts['gas_owner']) != operator:
        return False
    publish = facts['commands'][0]
    return (sorted(publish['modules']) == sorted(built['modules'])
            and sorted(toolchain.normal(d) for d in publish['dependencies']) == sorted(built['dependencies'])
            and any(c['type'].endswith('::device_registry::DeviceRegistry') for c in facts['created']))


def upgrade_matches(facts, built, operator, base, cap):
    kinds = [c['kind'] for c in facts['commands']]
    if not facts['success'] or kinds != ['MoveCall', 'Upgrade', 'MoveCall']:
        return False
    if toolchain.normal(facts['sender']) != operator or toolchain.normal(facts['gas_owner']) != operator:
        return False
    upgrade = facts['commands'][1]
    return (toolchain.normal(upgrade['package']) == toolchain.normal(base)
            and sorted(upgrade['modules']) == sorted(built['modules'])
            and sorted(toolchain.normal(d) for d in upgrade['dependencies']) == sorted(built['dependencies'])
            and toolchain.normal(cap['package']) in {toolchain.normal(c['objectId']) for c in facts['created']
                                                      if c['type'] == 'package'})


def legacy_publication(pending, *, chain, operator, built, known, now, upgrade=None):
    """Authenticated reconciliation for a historical marker without bytes.

    Publication only: 'publish base' looks for an unrecorded, never-upgraded
    UpgradeCap whose creating transaction published exactly `built` from the
    wallet; 'upgrade to head' inspects the publication's own UpgradeCap. Absence
    is concluded only after two epoch boundaries past the first observation
    (every transaction the pinned CLI built by then carries a ValidDuring window
    ending at most one epoch after it was built) and once every gas coin seen at
    observation has moved. Mutates `pending` to record the observation.
    """
    operator = toolchain.normal(operator)
    label = pending['label']
    if label not in LEGACY_LABELS or (label == 'upgrade to head') != (upgrade is not None):
        raise Uncertain(f'historical pending {label!r} has no retained transaction; explicit reconciliation required')
    epoch = chain.epoch()
    observed = pending.get('observed')
    if not observed:
        observed = pending['observed'] = {'epoch': epoch, 'at': now, 'coins': chain.coins(operator)}

    def executed():
        caps = chain.caps(operator)
        if upgrade is not None:
            cap = next((c for c in caps if toolchain.normal(c['objectId']) == toolchain.normal(upgrade['cap'])), None)
            if cap is None:
                raise Uncertain('the publication UpgradeCap is no longer owned by the wallet')
            if toolchain.normal(cap['package']) == toolchain.normal(upgrade['package']):
                return None
            facts = chain.transaction(cap['previous_transaction'])
            if facts and upgrade_matches(facts, built, operator, upgrade['package'], cap):
                return facts
            raise Uncertain('the publication UpgradeCap moved through an unverified transaction')
        matches, unauthenticated = [], []
        for cap in caps:
            if toolchain.normal(cap['objectId']) in known or str(cap.get('cap_version')) != '1':
                continue  # Recorded elsewhere, or upgraded by a run that knew it.
            facts = chain.transaction(cap['previous_transaction'])
            if facts is None:
                unauthenticated.append(cap['objectId'])
            elif publish_matches(facts, built, operator):
                matches.append(facts)
        if len(matches) > 1:
            raise Uncertain(f'{len(matches)} unrecorded publications match; explicit reconciliation required')
        if not matches and unauthenticated:
            raise Uncertain(f'unrecorded UpgradeCaps without retrievable creation history: {unauthenticated[:5]}')
        return matches[0] if matches else None

    facts = executed()
    if facts:
        return 'executed', cli_result(facts)
    if epoch < int(observed['epoch']) + 2:
        raise Unexpired(f'historical {label!r} without transaction identity has no executed effects yet; '
                        f'it could still execute through epoch {int(observed["epoch"]) + 1} (now {epoch})')
    current = chain.versions(list(observed['coins']))
    unmoved = [c for c, v in observed['coins'].items() if current.get(c) is not None and int(current[c]) <= int(v)]
    if unmoved:
        raise Unexpired(f'gas coins observed with the historical marker are unchanged: {unmoved[:5]}')
    facts = executed()  # Final scan once every candidate transaction is dead.
    if facts:
        return 'executed', cli_result(facts)
    return 'absent', (f'historical {label!r}: no executed effects, and every transaction it could stand for '
                      f'expired (epoch {epoch} > {int(observed["epoch"]) + 1}) with observed gas coins moved')
