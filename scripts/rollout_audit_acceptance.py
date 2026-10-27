"""SF164 automatic protected Testnet audit, using the caller's strict gas ledger."""
import json
import subprocess
from pathlib import Path


def run(transaction, client, manifest, device, evidence, root):
    if 'deployment_manager::DecisionKey' not in manifest.get('type_origins', {}):
        return
    package = manifest['call_package_id']
    record = None
    try:
        made = transaction('create durable decision audit rollout',
            '--move-call', f'{package}::deployment_manager::rollout_policy',
            '1', '1', '1', '0', '1', '10000', '3600000', '--assign', 'policy',
            '--move-call', f'{package}::deployment_manager::new_rollout_verified',
            '"audit-only"', '"1"', '"not-executed"', json.dumps('a' * 64), json.dumps('b' * 64),
            'vector[]', f'vector[@{device}]', 'vector[0u64]', 'policy', '@0x6', '--assign', 'record',
            '--move-call', f'{package}::deployment_manager::share_deployment', 'record', budget=20_000_000)
        record = next(c['objectId'] for c in made['objectChanges'] if c['type'] == 'created' and c['objectType'].endswith('::deployment_manager::DeploymentRecord'))
        advanced = transaction('admit durable audit cohort', '--move-call',
            f'{package}::deployment_manager::advance_rollout_onchain', f'@{record}',
            f"@{manifest['shared_objects']['registry']['id']}", f'vector[@{device}]', '@0x6', budget=20_000_000)
        execution = client('tx-block', advanced['digest'])
        checkpoint = execution.get('checkpoint')
        assert checkpoint is not None, 'decision checkpoint unavailable'
        input_path = Path(root) / 'rollout-audit-input.json'
        input_path.write_text(json.dumps(dict(manifest=manifest, record=record, revision=2, checkpoint=checkpoint, device=device)))
        output = subprocess.run(['node', 'scripts/read_rollout_audit_live.cjs', str(input_path)], capture_output=True, text=True, timeout=330)
        assert output.returncode == 0, output.stderr[-2000:]
        evidence['rollout_audit'] = dict(record=record, checkpoint=str(checkpoint), **json.loads(output.stdout))
        evidence['checks'].append('SF164 full current-state audit read after actual fullnode retention floor passed the decision checkpoint; event reads forbidden')
    finally:
        if record:
            transaction('delete durable decision audit rollout', '--move-call', f'{package}::deployment_manager::delete_rollout', f'@{record}', budget=20_000_000)
