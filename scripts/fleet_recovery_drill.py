#!/usr/bin/env python3
"""SF-091 integrated recovery drill on isolated infrastructure.

Runs every offline recovery rehearsal and writes one receipt with measured
durations, observed loss and pass/fail per step. Nothing touches a public
network, wallet, provider or hardware. Requires the debug daemon/examples and
`dapp/node_modules` (make edge-build-style `cargo build --bins --examples`,
make dapp-install).

  restore/rollback      scripts/tests/test_fleet_recovery.py
  browser outages/loss  scripts/test_fleet_recovery.cjs
  device recovery       scripts/test_recovery_ota.py (real daemon)
  SF-092 payment drill  retention-worker purchase/executor/notifier/expiry tests
                        and the real-daemon expired-blob re-upload/reference repair
"""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RETENTION_CLASS = 'RetentionTests'
RETENTION_CASES = [
    ('failed funding purchase credits nothing; top-up resumes', 'test_purchase_decline_has_no_credit_and_topup_resumes'),
    ('unavailable executor: long submit outage resumes the original intent', 'test_long_submit_outage_recovers_original_intent_after_restart'),
    ('unavailable notifier: bounded retries and notifications', 'test_retries_and_notifications_are_bounded'),
    ('alert queue backpressure, export and acknowledgment', 'test_full_alert_queue_export_acknowledgment_and_restart'),
    ('expired blob never renews or installs', 'test_expired_blob_never_renews_or_installs'),
    ('expiry without effects is not success', 'test_expiry_without_successful_effects_is_not_success'),
]


def step(name, command, receipt=None, cwd=ROOT):
    started = time.monotonic()
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    entry = {'name': name, 'command': ' '.join(str(part) for part in command), 'passed': result.returncode == 0,
             'seconds': round(time.monotonic() - started, 3)}
    if receipt and receipt.exists():
        entry['observed'] = json.loads(receipt.read_text())
    if result.returncode:
        entry['output_tail'] = (result.stdout + result.stderr).splitlines()[-20:]
    print(('PASS' if entry['passed'] else 'FAIL') + f": {name} ({entry['seconds']}s)")
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='suifleet-drill-') as temporary:
        root = Path(temporary)
        steps = [
            step('clean-operator restore and frontend rollback', [sys.executable, '-m', 'unittest', 'discover', '-s', 'scripts/tests', '-p', 'test_fleet_recovery.py']),
            step('browser discovery, outages, loss and export', ['node', 'scripts/test_fleet_recovery.cjs', str(root / 'browser.json')], root / 'browser.json'),
            step('device recovery with the real daemon', [sys.executable, 'scripts/test_recovery_ota.py', '--receipt', str(root / 'device.json')], root / 'device.json'),
            step('SF-092 expired bytes, verified customer copy and new reference', [sys.executable, 'scripts/test_budget_ota.py']),
        ]
        for name, case in RETENTION_CASES:
            steps.append(step(f'SF-092 {name}', [sys.executable, '-m', 'unittest', f'test_retention_worker.{RETENTION_CLASS}.{case}'],
                              cwd=ROOT / 'scripts/tests'))
    receipt = {'schema': 1, 'ticket': 'SF-091', 'kind': 'isolated-recovery-drill',
               'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'host': {'python': platform.python_version(), 'system': platform.system()},
               'passed': all(entry['passed'] for entry in steps), 'steps': steps,
               'boundary': 'Isolated local simulators and fixtures only; no public network, wallet, production provider, Pi or Mainnet acceptance.'}
    args.receipt.write_text(json.dumps(receipt, indent=2) + '\n')
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
