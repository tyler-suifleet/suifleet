#!/usr/bin/env python3
"""Install reviewed coordinator files locally; enabling is explicit --enable."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--enable',action='store_true')
parser.add_argument('--enable-dispatcher',action='store_true',help='Enable configured automatic implementation slots')
scope = parser.add_mutually_exclusive_group()
scope.add_argument('--dispatcher-only',action='store_true',help='Preserve the installed review coordinator and worker')
scope.add_argument('--coordinator-only',action='store_true',help='Replace only coordinator.py; preserve dispatcher, worker and service units')
scope.add_argument('--dashboard-only',action='store_true',help='Install only the fleet-status and agent-dashboard commands; touch no service, config or engine')
args = parser.parse_args()
source = Path(__file__).resolve().parent
home = Path.home()
def install_dashboard():
    (home/'.local/bin').mkdir(parents=True,exist_ok=True)
    shutil.copy2(source/'dashboard.py',home/'.local/lib/pr-coordinator/dashboard.py')
    for name in ('agent-dashboard', 'fleet-status'):
        command = home/'.local/bin'/name
        # Replace obsolete prototype symlinks rather than overwriting their target.
        if command.is_symlink():
            command.unlink()
        command.write_text(f'#!/bin/sh\nexec python3 {shlex.quote(str(home/".local/lib/pr-coordinator/dashboard.py"))} "$@"\n')
        command.chmod(0o755)
if args.dashboard_only:
    (home/'.local/lib/pr-coordinator').mkdir(parents=True,exist_ok=True)
    install_dashboard()
    print(f"Installed fleet-status and agent-dashboard in {home/'.local/bin'}; services and workers untouched.")
    raise SystemExit(0)
for directory in ['.local/lib/pr-coordinator','.config/pr-coordinator','.config/systemd/user']:
    (home/directory).mkdir(parents=True,exist_ok=True)
for name in (('coordinator.py',) if args.coordinator_only else ('dispatcher.py',) if args.dispatcher_only else ('coordinator.py','worker.py','dispatcher.py')):
    shutil.copy2(source/name,home/'.local/lib/pr-coordinator'/name)
if not args.coordinator_only and not args.dispatcher_only:
    install_dashboard()
config = home/'.config/pr-coordinator/config.json'
if not config.exists():
    import json
    settings = json.loads((source/'config.example.json').read_text())
    for command in settings['workers'].values():
        executable = shutil.which(command[0])
        if not executable: raise SystemExit(f'Missing worker executable: {command[0]}')
        command[0] = executable
    config.write_text(json.dumps(settings,indent=2)+'\n')
config.chmod(0o600)
service = (source/'pr-coordinator.service').read_text()
node = shutil.which('node')
if not node: raise SystemExit('Node is required by the installed Codex CLI')
service = service.replace('Environment=PATH=', f'Environment=PATH={Path(node).parent}:')
if not args.dispatcher_only and not args.coordinator_only:
    (home/'.config/systemd/user/pr-coordinator.service').write_text(service)
dispatch_service = (source/'ticket-dispatcher.service').read_text()
dispatch_service = dispatch_service.replace('Environment=PATH=', f'Environment=PATH={Path(node).parent}:')
if not args.coordinator_only:
    (home/'.config/systemd/user/ticket-dispatcher.service').write_text(dispatch_service)
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
if args.enable: subprocess.run(['systemctl','--user','enable','--now','pr-coordinator.service'],check=True)
if args.enable_dispatcher: subprocess.run(['systemctl','--user','enable','--now','ticket-dispatcher.service'],check=True)
print(f'Installed. Configuration: {config}')
