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
parser.add_argument('--activation-project', help='Bootstrap the reviewed main runtime and enable safe local activation for this configured project')
args = parser.parse_args()
if args.activation_project and (args.coordinator_only or args.dispatcher_only or args.dashboard_only):
    parser.error('activation bootstrap requires the full compatible runtime')
source = Path(__file__).resolve().parent
home = Path.home()
os.umask(0o077)

def bootstrap_activation():
    import json
    from activation import FILES, Activator, atomic, switch
    config = home/'.config/pr-coordinator/config.json'
    settings = json.loads(config.read_text())
    project = next(p for p in settings['projects'] if p['id'] == args.activation_project)
    library = home/'.local/lib/pr-coordinator'
    state = home/'.local/state/pr-coordinator'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    activation_settings = dict(project=project['id'], root=project['root'],
                               repository=project['repository'], github_account=project['github_account'])
    engine = Activator(activation_settings, library, state)
    # Bootstrap is explicit and must use committed main source, never this PR's
    # worktree, dirty files, or an origin ref that happens to have the right name.
    target = engine.tip()
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip() != target:
        raise SystemExit('Bootstrap must run from the fetched main revision')
    for name in (*FILES, 'activation.py', 'install.py', 'pr-coordinator.service', 'ticket-dispatcher.service',
                 'runtime-activation.service', 'runtime-activation.timer'):
        expected = subprocess.check_output(['git', 'show', target+':tools/pr-coordinator/'+name], cwd=source)
        if (source/name).read_bytes() != expected:
            raise SystemExit('Bootstrap source differs from committed main')
    with (state/'activation.lock').open('a') as lock:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if engine.link.exists() or engine.link.is_symlink():
            raise SystemExit('Activation already bootstrapped; use the activation service')
        engine.verify_bootstrap(target)
        generation = engine.stage(target)
        engine.validate(target)
        if engine.tip() != target:
            raise SystemExit('Main moved during bootstrap; retry')
        units = engine.services.active()
        unit_dir = home/'.config/systemd/user'
        originals = {unit: (unit_dir/unit).read_bytes() for unit in ('pr-coordinator.service', 'ticket-dispatcher.service')}
        supervisor = library/'activation.py'
        previous_supervisor = supervisor.read_bytes() if supervisor.exists() else None
        node = shutil.which('node')
        if not node:
            raise SystemExit('Node is required by the installed coordinator validation suite')
        try:
            engine.services.stop(units)
            switch(engine.link, generation)
            # Preserve all existing unit protections/environment, changing only the runtime path.
            for unit, content in originals.items():
                (unit_dir/unit).write_bytes(content.replace(b'/pr-coordinator/coordinator.py', b'/pr-coordinator/current/coordinator.py')
                                          .replace(b'/pr-coordinator/dispatcher.py', b'/pr-coordinator/current/dispatcher.py'))
            shutil.copy2(source/'activation.py', supervisor)
            atomic(home/'.config/pr-coordinator/activation.json', activation_settings)
            for name in ('runtime-activation.service', 'runtime-activation.timer'):
                content = (source/name).read_text()
                if name.endswith('.service'):
                    content = content.replace('Environment=PATH=', f'Environment=PATH={Path(node).parent}:')
                (unit_dir/name).write_text(content)
            subprocess.run(['systemctl','--user','daemon-reload'], check=True)
            engine.services.start(units, generation)
            subprocess.run(['systemctl','--user','enable','--now','runtime-activation.timer'], check=True)
        except BaseException:
            engine.services.stop(units)
            for unit, content in originals.items():
                (unit_dir/unit).write_bytes(content)
            engine.link.unlink(missing_ok=True)
            if previous_supervisor is None:
                supervisor.unlink(missing_ok=True)
            else:
                supervisor.write_bytes(previous_supervisor)
            subprocess.run(['systemctl','--user','daemon-reload'], check=True)
            if units:
                subprocess.run(['systemctl','--user','start',*units], check=True)
            raise
    print('Local runtime activation enabled; private configuration and model permissions preserved.')

if args.activation_project:
    bootstrap_activation()
    raise SystemExit(0)

if (home/'.local/lib/pr-coordinator/current').is_symlink() and not args.dashboard_only:
    raise SystemExit('Runtime activation is enabled; use its serialized service instead of copying engine files')

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
for name in (('coordinator.py',) if args.coordinator_only else ('dispatcher.py',) if args.dispatcher_only else ('coordinator.py','worker.py','dispatcher.py','activation.py')):
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
