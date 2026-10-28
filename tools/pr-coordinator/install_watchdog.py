#!/usr/bin/env python3
"""Install only the recovery watchdog; preserve coordinator/dispatcher generations."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from recovery_watchdog import atomic, settings


def install(home, projects, enable=False):
    state = home/'.local/state/pr-coordinator'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state/'activation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return install_locked(home, projects, enable)


def install_locked(home, projects, enable):
    source = Path(__file__).resolve().parent
    config_path = home/'.config/pr-coordinator/config.json'
    original = config_path.read_text()
    config = json.loads(original)
    config['recovery_watchdog'] = {**config.get('recovery_watchdog', {}),
                                   'enabled':enable, 'projects':projects}
    settings(config)  # Validate everything before changing the installed files.
    node = shutil.which('node')
    if not node:
        raise ValueError('Node must be available for the configured Codex CLI')
    root = home/'.local/lib/pr-coordinator/recovery-watchdog'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    names = ('recovery_watchdog.py', 'recovery_worker.py')
    digest = hashlib.sha256(b''.join((source/n).read_bytes() for n in names)).hexdigest()
    generation = root/digest
    if generation.exists():
        if any((generation/n).read_bytes() != (source/n).read_bytes() for n in names):
            raise ValueError('existing generation differs from its digest')
    else:
        stage = Path(tempfile.mkdtemp(prefix='.generation-', dir=root))
        try:
            for name in names:
                shutil.copyfile(source/name, stage/name)
                (stage/name).chmod(0o600)
            stage.rename(generation)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    if config_path.read_text() != original:
        raise ValueError('configuration changed during installation; retry')
    link = root/'next'
    link.unlink(missing_ok=True)
    link.symlink_to(generation)
    link.replace(root/'current')
    # Preserve the complete private configuration and add only the new section.
    backup = config_path.with_name('config.before-watchdog-'+str(time.time_ns())+'.json')
    shutil.copyfile(config_path, backup)
    backup.chmod(0o600)
    atomic(config_path, config)
    units = home/'.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    for name in ('recovery-watchdog.service', 'recovery-watchdog.timer'):
        text = (source/name).read_text().replace('@NODE_DIRECTORY@', str(Path(node).parent))
        (units/name).write_text(text)
    if enable:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'recovery-watchdog.timer'], check=True)
    return generation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', action='append', required=True)
    parser.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    print('Installed watchdog:', install(Path.home(), args.project, args.enable))


if __name__ == '__main__':
    main()
