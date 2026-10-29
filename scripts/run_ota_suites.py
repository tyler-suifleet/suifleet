#!/usr/bin/env python3
"""Run isolated OTA simulators with bounded concurrency after a single build."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
# Each suite owns its TemporaryDirectory, dynamically allocated listeners and
# daemon/shim processes. Shared binaries and node_modules are read-only here.
SUITES = [
    ('ota', 'test_ota.py'),
    ('poll-reads', 'test_ota.py', '--poll-reads'),
    ('platform-installer', 'test_ota.py', '--platform-installer'),
    ('catalog', 'test_catalog_ota.py'),
    ('dependencies', 'test_dependency_ota.py'),
    ('firmware-helper', 'test_ota.py', '--firmware-helper'),
    ('firmware-restart', 'test_firmware_restart.py'),
    ('supervisor', 'test_supervisor.py'),
    ('configuration', 'test_configuration.py'),
    ('greengrass-runtime', 'test_greengrass_runtime.py'),
    ('daemon-update', 'test_daemon_update.py'),
    ('desired-state', 'test_desired_state.py'),
    ('inbox', 'test_inbox_ota.py'),
    ('handover', 'test_handover_ota.py'),
    ('deployment-control', 'test_deployment_control.py'),
    ('telemetry', 'test_telemetry.py'),
    ('logs', 'test_logs.py'),
    ('cloudwatch-logs', 'test_cloudwatch_logs.py'),
    ('console', 'test_console.py'),
    ('rollout', 'test_rollout.py'),
    ('cadence', 'test_cadence_ota.py'),
    ('budget', 'test_budget_ota.py'),
    ('fleet-settings', 'test_fleet_settings_ota.py'),
    ('recovery', 'test_recovery_ota.py'),
]
for name, flags in [
    ('fleet-data', []), ('fleet-data-idle', ['--idle-reads']),
    ('fleet-data-compact', ['--compact-history']),
    ('fleet-data-quilt', ['--compact-history', '--quilt']),
    ('fleet-data-retention', ['--compact-history', '--quilt', '--stream-retention']),
]:
    SUITES.append((name, 'test_fleet_data_ota.py', '--binary',
                   'suifleet-edge/target/debug/suifleet', '--shim',
                   'suifleet-edge/target/debug/examples/grpc_sim_shim', *flags))


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def stop_all(active):
    # Let Python suites execute finally blocks (daemon, shim, server cleanup).
    for process, *_ in active:
        signal_group(process, signal.SIGINT)
    deadline = time.monotonic() + 5
    while any(p.poll() is None for p, *_ in active) and time.monotonic() < deadline:
        time.sleep(.05)
    for process, *_ in active:
        signal_group(process, signal.SIGKILL)
        process.wait()


def run_suites(commands, jobs, log_root, *, cwd=ROOT, timeout=1200):
    if not 1 <= jobs <= 8 or timeout <= 0:
        raise ValueError('jobs must be 1..8 and timeout must be positive')
    names = [name for name, _ in commands]
    if len(set(names)) != len(names) or any(not n or not all(c.isalnum() or c in '-_' for c in n) for n in names):
        raise ValueError('suite names must be unique safe filenames')
    log_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='run-', dir=log_root))
    print(f'OTA: {len(commands)} suites, concurrency {jobs}; logs: {directory}', flush=True)
    pending = iter(commands)
    active, results = [], []
    started = time.monotonic()
    last_notice = started
    def save():
        payload = dict(jobs=jobs, seconds=round(time.monotonic()-started, 3),
                       complete=len(results) == len(commands) and not any(r.get('interrupted') for r in results), results=results)
        temp = directory / 'summary.tmp'
        temp.write_text(json.dumps(payload, indent=2) + '\n')
        temp.replace(directory / 'summary.json')
    try:
        exhausted = False
        while active or not exhausted:
            while len(active) < jobs and not exhausted:
                try:
                    name, command = next(pending)
                except StopIteration:
                    exhausted = True
                    break
                log = (directory / f'{name}.log').open('wb')
                try:
                    process = subprocess.Popen(command, cwd=cwd, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True,
                                               env={**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
                except BaseException:
                    log.close()
                    raise
                active.append((process, name, log, time.monotonic()))
                print(f'START {name}', flush=True)
            for item in active[:]:
                process, name, log, when = item
                expired = process.poll() is None and time.monotonic()-when > timeout
                if expired:
                    stop_all([item])
                code = process.poll()
                if code is None:
                    continue
                log.close()
                active.remove(item)
                result = dict(name=name, returncode=code, timed_out=expired,
                              seconds=round(time.monotonic()-when, 3), log=f'{name}.log')
                results.append(result)
                print(f'{"PASS" if code == 0 and not expired else "FAIL"} {name}: {result["seconds"]}s', flush=True)
                if code != 0 or expired:
                    print((directory / f'{name}.log').read_text(errors='replace')[-8000:], flush=True)
                save()
            if active:
                if time.monotonic() - last_notice >= 30:
                    print("RUNNING " + ", ".join(name for _, name, *_ in active), flush=True)
                    last_notice = time.monotonic()
                time.sleep(.05)
    finally:
        stop_all(active)
        for process, name, log, when in active:
            log.close()
            results.append(dict(name=name, returncode=process.returncode,
                                interrupted=True, seconds=round(time.monotonic()-when, 3), log=f'{name}.log'))
        save()
    failed = sum(r['returncode'] != 0 or r.get('timed_out', False) for r in results)
    print(f'OTA: {len(results)}/{len(commands)} completed, {failed} failed, {time.monotonic()-started:.1f}s; {directory}/summary.json', flush=True)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=3)
    parser.add_argument('--logs', type=Path, default=ROOT / 'ci-logs' / 'ota')
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return run_suites([(s[0], [sys.executable, str(ROOT/'scripts'/s[1]), *s[2:]]) for s in SUITES], args.jobs, args.logs)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError) as error:
        parser.exit(2, f'{error}\n')


if __name__ == '__main__':
    sys.exit(main())
