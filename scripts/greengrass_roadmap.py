"""SF-064 proposed functional reference contracts; never executes an adapter."""
import json
from pathlib import Path

SCOPES = dict(zip(('lambda', 'industrial', 'sitewise', 'video', 'ml', 'windows',
                   'macos', 'linux-x86', 'firehose', 'sns', 'console'),
                  (f'SF-{n:03}' for n in range(69, 80))))
PLATFORMS = {'windows': 'SF-074', 'darwin': 'SF-075', 'linux-x86': 'SF-076'}


def evaluate(scope, request):
    """Deterministic v1 decisions. Inputs are JSON, outputs are contract results."""
    if type(request.get('version')) is not int or request['version'] != 1:
        return {'status': 'unsupported-version'}
    if scope not in SCOPES:
        return {'status': 'unsupported-scope'}
    # Every privileged operation requires a current, scoped grant. Denial must
    # precede processing, buffering, process creation or network I/O.
    if request.get('authorized') is not True:
        return {'status': 'denied'}
    if scope == 'lambda':
        if request.get('mode') not in ('on-demand', 'long-lived'):
            return {'status': 'invalid-mode'}
        if request.get('elapsed_ms', 0) >= request['timeout_ms']:
            return {'status': 'timeout'}
        if request['active'] >= request['concurrency']:
            return {'status': 'busy'}
        routes = sorted(set(r['target'] for r in request['subscriptions']
                            if r['source'] == request['source'] and r['subject'] == request['subject']))
        return {'status': 'invoke', 'targets': routes, 'payload': request['payload']}
    if scope in ('industrial', 'sitewise'):
        if request['quality'] not in ('GOOD', 'BAD', 'UNCERTAIN'):
            return {'status': 'invalid-quality'}
        if type(request['value']) not in (int, float) or type(request['timestamp_ns']) is not int or request['timestamp_ns'] < 0:
            return {'status': 'invalid-measurement'}
        if scope == 'sitewise':
            if request['alias'] not in request['aliases']:
                return {'status': 'unknown-alias'}
            value = request['value'] * request['scale'] + request['offset']
        else:
            value = request['value']
        return {'status': 'measurement', 'value': value, 'timestamp_ns': request['timestamp_ns'], 'quality': request['quality']}
    if scope in ('video', 'firehose', 'sns'):
        # At least once: an ambiguous response retains data, so retries can
        # duplicate delivery. Only a contiguous acknowledged prefix advances.
        records = request['records']
        if sum(r['bytes'] for r in records) > request['max_bytes']:
            return {'status': 'backpressure', 'checkpoint': request['checkpoint']}
        checkpoint = request['checkpoint']
        for record in records:
            if record['sequence'] != checkpoint + 1 or record['result'] != 'ack':
                break
            checkpoint = record['sequence']
        return {'status': 'retained' if checkpoint != (records[-1]['sequence'] if records else checkpoint) else 'acknowledged', 'checkpoint': checkpoint}
    if scope == 'ml':
        if request['actual_digest'] != request['expected_digest']:
            return {'status': 'digest-mismatch'}
        if request['runtime'] != request['expected_runtime'] or request['shape'] != request['expected_shape']:
            return {'status': 'incompatible-input'}
        if len(request['actual']) != len(request['expected']):
            return {'status': 'output-mismatch'}
        return {'status': 'within-tolerance' if all(abs(a-b) <= request['tolerance'] for a,b in zip(request['actual'], request['expected'])) else 'output-mismatch'}
    if scope in ('windows', 'macos', 'linux-x86'):
        if request['artifact_platform'] != request['platform']:
            return {'status': 'incompatible-artifact'}
        if not request['contained'] or not request['private_identity']:
            return {'status': 'unsafe-runtime'}
        return {'status': 'recover-interrupted' if request['interrupted'] else 'eligible'}
    if scope == 'console':
        if request['origin'] != request['expected_origin']:
            return {'status': 'invalid-origin'}
        if request['operation'] not in request['grants']:
            return {'status': 'denied'}
        return {'status': 'ok', 'fields': {k: v for k, v in request['fields'].items() if k in ('component', 'version', 'health')}}
    raise AssertionError(scope)


def check(root: Path):
    """Validate published ownership, coverage and fixtures; return vector count."""
    roadmap = json.loads((root / 'interfaces/compat/remaining-roadmap.json').read_text())
    catalog = json.loads((root / 'interfaces/compat/managed-components.json').read_text())
    backlog = {i['key']: i for i in json.loads((root / 'docs/tracking/backlog.json').read_text())['issues']}
    published = json.loads((root / 'docs/tracking/github-state.json').read_text())['issues']
    assert roadmap['format_version'] == 1 and roadmap['parity_claim'] is False
    scopes = roadmap['scopes']
    assert len(scopes) == len(SCOPES) and {s['id'] for s in scopes} == set(SCOPES), 'missing/duplicate scope'
    covered = set()
    for scope in scopes:
        key = scope['implementation_ticket']
        assert key == SCOPES[scope['id']], 'wrong implementation owner'
        assert scope['contract_version'] == 1
        assert scope['runtime_execution'] == scope['live_execution'] == 'pending', 'roadmap is not runtime evidence'
        assert scope['depends_on'] == backlog[key]['depends_on'], 'dependency drift'
        assert scope['issue'] == published[key]['url'] and published[key]['project_item_id'], 'unpublished scope'
        assert scope['acceptance'] == backlog[key]['validation'], 'acceptance drift'
        for name in scope['components']:
            row = next(r for r in catalog['components'] if r['name'] == name)
            assert key in row['tickets'], f'{name}: missing implementation owner'
            covered.add(name)
    assert all(r['name'] in covered for r in catalog['components'] if r['disposition'] == 'roadmap'), 'unmapped catalog gap'
    for row in catalog['platform_phases']:
        if row['phase'] == 'roadmap':
            assert PLATFORMS[row['platform']] in row['tickets'], 'unmapped platform'
    matrix = json.loads((root / 'interfaces/compat/greengrass-compat.json').read_text())
    for row in matrix['platforms']:
        if row['id'] in PLATFORMS:
            assert PLATFORMS[row['id']] in row['tickets'], 'unmapped matrix platform'
    fixtures = json.loads((root / 'interfaces/compat/remaining-roadmap-cases.json').read_text())
    assert fixtures['format_version'] == 1
    assert {c['scope'] for c in fixtures['cases']} == set(SCOPES), 'missing fixture scope'
    ids = [c['id'] for c in fixtures['cases']]
    assert len(ids) == len(set(ids)), 'duplicate fixture ID'
    for scope in SCOPES:
        assert all(f'{scope}.{kind}' in ids for kind in ('baseline', 'version', 'denied')), 'missing baseline/denial fixture'
    for case in fixtures['cases']:
        assert evaluate(case['scope'], case['request']) == case['expected'], case['id']
    return len(fixtures['cases'])


if __name__ == '__main__':
    print(f'PASS: {check(Path(__file__).resolve().parents[1])} remaining-roadmap reference checks; runtime/live pending')
