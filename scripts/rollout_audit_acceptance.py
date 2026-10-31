"""Retained post-pruning audit fixture, using the caller's strict gas ledger.

The authenticated private package cache owns this state. Retention timeouts
preserve the original decision: recreating it resets the clock forever.
"""
import json
import subprocess
import time
from pathlib import Path

import sui_toolchain as toolchain
import testnet_reconcile as reconciler


class RetentionPending(RuntimeError):
    """The authenticated fixture is intact; its decision has not aged enough."""


def checkpointed_step(state_path, state, fixture, label, ptb, calls, *, client,
                      sign, mutate, ledger, operator, chain, checkpoint=None,
                      record_other=None):
    """Reconcile retained verified bytes before any further submission."""
    from live_package_upgrade import checkpoint_setup, other_execution, save_state
    # The executable caller may be __main__; preserve its exception identities.
    checkpoint = checkpoint or checkpoint_setup
    record_other = record_other or other_execution
    if fixture.get('pending_operation'):
        marker = reconciler.normal_pending(fixture['pending_operation'])
        if marker['label'] != label:
            raise reconciler.Uncertain('pending audit operation differs from the next stage')
        fixture['pending_operation'] = marker
        try:
            # A missing indexed result does not authorize signing another tx.
            def no_replay(_):
                raise reconciler.Uncertain('audit outcome not indexed; retain exact transaction for reconciliation')
            outcome, result = reconciler.reconcile(marker, chain=chain, operator=operator,
                inspect=toolchain.decode,
                verify=lambda data, tx: toolchain.verify_ptb_transaction(
                    data, sender=operator, budget=tx['gas_budget'], calls=calls),
                resubmit=no_replay, now=time.time())
        except Exception as error:
            record_other(marker, error)
            save_state(state_path, state)
            raise
        fixture.pop('pending_operation')
        if outcome == 'executed':
            return result  # Caller saves phase + cleared marker atomically.
        save_state(state_path, state)  # Failed/provably unsubmitted: fresh is safe.
    return checkpoint(state_path, state, fixture, label,
        lambda persist: mutate(label, toolchain.ptb_submitter(client, ptb,
            sender=operator, calls=calls, sign=sign, persist=persist),
            gas=20_000_000, require_full=True), ledger)


def run_scheduled(*args, **kwargs):
    """Record temporal acceptance separately from ordinary testnet regressions.

    Only the reader's explicit floor-pending exit is nonfatal. Invalid proofs,
    provenance, effects and all other reader failures still fail the run.
    """
    try:
        return run(*args, **kwargs)
    except RetentionPending:
        # run() persisted the exact original fixture and pending evidence.
        # Do not add a passed check or reuse any earlier successful proof.
        return None


def run(step, client, manifest, device, evidence, root, *, fixture=None, save=None,
        operator=None, fixture_key=None, retire=False):
    if 'deployment_manager::DecisionKey' not in manifest.get('type_origins', {}):
        return
    if fixture is None or save is None or not operator or not fixture_key:
        raise RuntimeError('post-pruning audit requires authenticated durable fixture state')
    package = manifest['call_package_id']
    binding = dict(chain=manifest['chain_identifier'], operator=operator,
        fixture_key=fixture_key, package=package, origins=manifest['type_origins'],
        registry=manifest['shared_objects']['registry']['id'], device=device)
    if fixture:
        if fixture.get('schema') != 1 or fixture.get('binding') != binding:
            raise RuntimeError('audit fixture provenance changed; reconcile without recreating it')
    else:
        fixture.update(schema=1, binding=json.loads(json.dumps(binding)))
        save()
    if fixture.get('retired'):
        raise RuntimeError('audit fixture already retired; no automatic recreation')

    def perform(name, ptb, calls):
        result = step(name, ptb, calls)
        if result.get('effects', {}).get('status', {}).get('status') != 'success' or not result.get('digest'):
            raise RuntimeError('audit mutation has no successful verified execution effects')
        return result

    if not fixture.get('record'):
        calls = [f'{package}::deployment_manager::{name}' for name in
                 ['rollout_policy', 'new_rollout_verified', 'share_deployment']]
        made = perform('create durable decision audit rollout', [
            '--move-call', calls[0], '1', '1', '1', '0', '1', '10000', '3600000', '--assign', 'policy',
            '--move-call', calls[1], '"audit-only"', '"1"', '"not-executed"',
            json.dumps('a' * 64), json.dumps('b' * 64), 'vector[]', f'vector[@{device}]',
            'vector[0u64]', 'policy', '@0x6', '--assign', 'record',
            '--move-call', calls[2], 'record'], calls)
        records = [c['objectId'] for c in made['objectChanges'] if c['type'] == 'created'
                   and c['objectType'] == f"{manifest['type_origins']['deployment_manager::DeploymentRecord']}::deployment_manager::DeploymentRecord"]
        if len(records) != 1:
            raise RuntimeError('audit creation did not return exactly one correctly originated record')
        fixture.update(record=records[0], creation_digest=made['digest'])
        save()
    record = fixture['record']
    if not fixture.get('admission_digest'):
        from live_common import plain
        observed = client('object', record)
        content = observed.get('content') or {}
        fields = plain(content)
        expected_type = f"{manifest['type_origins']['deployment_manager::DeploymentRecord']}::deployment_manager::DeploymentRecord"
        # The pinned gRPC CLI returns objType beside a flat content map;
        # wrapped Move JSON carries type inside content instead. Validate every
        # supplied type so conflicting metadata cannot bypass the binding.
        types = [value for value in (observed.get('objType'), content.get('type'))
                 if value is not None]
        if (not types or any(value != expected_type for value in types)
                or 'Shared' not in (observed.get('owner') or {})
                or fields.get('creator') != operator or fields.get('component_name') != 'audit-only'
                or fields.get('version') != '1' or str(fields.get('schema_version')) != '3'):
            raise RuntimeError('retained audit target identity changed; no admission submitted')
        call = f'{package}::deployment_manager::advance_rollout_onchain'
        advanced = perform('admit durable audit cohort', ['--move-call', call,
            f'@{record}', f"@{binding['registry']}", f'vector[@{device}]', '@0x6'], [call])
        fixture['admission_digest'] = advanced['digest']
        save()
    if 'checkpoint' not in fixture:
        execution = client('tx-block', fixture['admission_digest'])
        if (execution.get('digest') != fixture['admission_digest']
                or execution.get('effects', {}).get('status', {}).get('status') != 'success'
                or execution.get('checkpoint') is None):
            raise RuntimeError('verified admission checkpoint unavailable; fixture retained')
        fixture['checkpoint'] = str(execution['checkpoint'])
        save()

    # An interrupted retirement is reconciled before reading its possibly
    # deleted object. Only a previously completed real proof permits cleanup.
    pending = fixture.get('pending_operation') or {}
    retiring = pending.get('label') == 'delete durable decision audit rollout'
    if retiring and not fixture.get('proof'):
        raise RuntimeError('audit cleanup has no preceding post-pruning proof')
    if not retiring:
        input_path = Path(root) / 'rollout-audit-input.json'
        input_path.write_text(json.dumps(dict(manifest=manifest, record=record,
            revision=2, checkpoint=fixture['checkpoint'], device=device, operator=operator, timeoutMs=0)))
        output = subprocess.run(['node', 'scripts/read_rollout_audit_live.cjs', str(input_path)],
            capture_output=True, text=True, timeout=330)
        if output.returncode:
            evidence['rollout_audit_pending'] = dict(status='pending', acceptance_complete=False,
                record=record, checkpoint=fixture['checkpoint'],
                admission_digest=fixture['admission_digest'], retained=True)
            error = RetentionPending if output.returncode == 3 else RuntimeError
            raise error(output.stderr[-2000:] or 'post-pruning read failed; fixture retained')
        proof = json.loads(output.stdout)
        if (int(proof['lowest']) <= int(fixture['checkpoint'])
                or proof.get('audit', {}).get('complete') is not True):
            raise RuntimeError('reader did not prove post-pruning complete audit; fixture retained')
        fixture['proof'] = proof
        save()
    evidence['rollout_audit'] = dict(record=record, checkpoint=fixture['checkpoint'],
        admission_digest=fixture['admission_digest'], **fixture['proof'])
    evidence['checks'].append('SF164 full current-state audit read after actual fullnode retention floor passed the decision checkpoint; event reads forbidden')
    # One bounded aged record per bytecode fixture permits repeated free reads.
    # Explicit retirement is allowed only after genuine post-pruning proof.
    if retire or retiring:
        call = f'{package}::deployment_manager::delete_rollout'
        deleted = perform('delete durable decision audit rollout', ['--move-call', call, f'@{record}'], [call])
        fixture.update(retired=True, cleanup_digest=deleted['digest'])
        save()
