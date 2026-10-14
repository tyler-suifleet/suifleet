#!/usr/bin/env python3
"""Install the committed standalone engine locally; no pip/network required."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--prefix', type=Path, default=Path.home() / '.local', help='Installation prefix; defaults to ~/.local')
args = parser.parse_args()
source = Path(__file__).with_name('atomic_ticketing.py')
modules = [source, *sorted(source.parent.glob('ticket_*.py'))]
digest = hashlib.sha256()
for module in modules:
    digest.update(module.name.encode())
    digest.update(module.read_bytes())
version = '0.2.0-' + digest.hexdigest()[:12]
installation = args.prefix / 'share/atomic-ticketing'
version_dir = installation / version
version_dir.mkdir(parents=True, exist_ok=True)
target = version_dir / 'atomic_ticketing.py'
for module in modules:
    destination = version_dir / module.name
    shutil.copyfile(module, destination)
    os.chmod(destination, 0o755 if module == source else 0o644)
bin_dir = args.prefix / 'bin'
bin_dir.mkdir(parents=True, exist_ok=True)
launcher = bin_dir / 'ticket'
if launcher.exists() or launcher.is_symlink():
    if not launcher.is_symlink() or installation.resolve() not in launcher.resolve().parents:
        raise SystemExit('Refusing to replace an unrelated ~/.local/bin/ticket command')
with tempfile.TemporaryDirectory(prefix='.ticket-install-', dir=bin_dir) as temporary:
    link = Path(temporary) / 'ticket'
    link.symlink_to(target)
    os.replace(link, launcher)
print('Installed', launcher, 'from committed engine', version)
print('Database: ${XDG_STATE_HOME:-~/.local/state}/atomic-ticketing/coordination.sqlite3')
