#!/usr/bin/env python3
"""Reproduce the SF121 planning arithmetic from committed executed effects."""
import json
from pathlib import Path
root = Path(__file__).resolve().parents[1] / 'docs/validation'
load = lambda name: json.loads((root / name).read_text())
r = load('sf121-operating-envelope.json')
q = load('sf121-quilt-runtime.json')
c = load('sf121-compact-effects.json')
h = load('sf082-testnet-native.json')
measured = c['compact_measured']
first = sum(measured[f'{kind}_0']['net_gas_mist'] for kind in ['register', 'certify', 'anchor'])
second = sum(measured[f'{kind}_1']['net_gas_mist'] for kind in ['register', 'certify', 'anchor'])
health = next(t['net_gas_mist'] for t in h['native_transactions'] if t['label'] == 'health-5')
for profile in r['profiles']:
    devices, archives, health_count = profile['devices'], profile['archives_per_device'], profile['health']
    count = devices * archives
    assert all(profile[key] == count for key in ['registration','certification','data_anchor'])
    assert profile['total_transactions'] == count * 3 + health_count
    assert profile['reference_archive_cash_mist'] == devices * (first + (archives-1) * second)
    assert profile['reference_health_cash_mist'] == health_count * health
    assert profile['reference_subtotal_mist'] == profile['reference_archive_cash_mist'] + profile['reference_health_cash_mist']
    assert profile['sample_ciphertext_bytes'] == count * q['ciphertext_bytes']
    assert profile['sample_quilt_blob_bytes'] == count * q['encoding']['size']
    assert profile['sample_walrus_encoded_bytes'] == count * int(q['upload']['encoded_size'])
    assert profile['current_sample_write_fee_frost'] == count * int(q['upload']['write_fee'])
assert q['allowance_used_mist'] <= q['original_allowance_mist'] == 500_000_000
assert all(t['independently_verified'] for t in q['transactions'])
print('PASS effect-derived 1000-device transaction, byte, fee and allowance arithmetic; scenario assumptions remain explicit')
