#!/usr/bin/env python3
"""Build an optional Linux localproxy artifact; never installs/enables a service.
The daemon binary is supplied by the normal SuiFleet build/release.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import tarfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'suifleet-edge/packaging/tunneling'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='suifleet-tunnel-package-') as tmp:
        path = Path(tmp)
        image = 'suifleet-tunnel-package:8a71618acf53'
        subprocess.run(['docker', 'build', '-f', str(SOURCE / 'localproxy.Dockerfile'), '--target', 'build', '-t', image, str(ROOT)], check=True)
        container = subprocess.check_output(['docker', 'create', image], text=True).strip()
        try:
            subprocess.run(['docker', 'cp', f'{container}:/package/.', str(path)], check=True)
        finally:
            subprocess.run(['docker', 'rm', container], check=True, stdout=subprocess.DEVNULL)
        for name in ('suifleet-tunnel.service', 'config.example.json', 'proxy-settings.json'):
            (path / name).write_bytes((SOURCE / name).read_bytes())
            # These public installer files must be readable by the service UID,
            # regardless of the caller's umask or checkout file permissions.
            (path / name).chmod(0o644)
        (path / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'localproxy_version': '3.3.1',
            'source_revision': '8a71618acf536923e882387e8d9eca9dbc7fdbe2',
            'localproxy_sha256': hashlib.sha256((path / 'localproxy').read_bytes()).hexdigest(),
            'architecture': subprocess.check_output(['docker', 'image', 'inspect', image, '--format', '{{.Architecture}}'], text=True).strip(),
            'enabled_by_default': False,
        }, indent=2) + '\n')
        (path / 'manifest.json').chmod(0o644)
        with tarfile.open(args.output, 'w:gz') as archive:
            for item in sorted(path.iterdir()):
                archive.add(item, arcname=item.name)
    print(args.output)


if __name__ == '__main__':
    main()
