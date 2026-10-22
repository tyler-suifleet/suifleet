#!/usr/bin/env python3
"""Exercise the image's AArch64 identity commands in disposable user-mode QEMU.

This is not a Pi boot, systemd service or physical persistence test. Generated
keys remain in a private temporary directory and are never printed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--rootfs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    binary = args.rootfs/'usr/bin/suifleet'
    before = hashlib.sha256(binary.read_bytes()).hexdigest()
    def run(*arguments, check=True):
        return subprocess.run([str(args.qemu), '-L', str(args.rootfs), str(binary), *map(str, arguments)], capture_output=True, text=True, timeout=60, check=check)
    with tempfile.TemporaryDirectory(prefix='sf029-identity-') as directory:
        root = Path(directory)
        keys = [root/'first.key', root/'second.key']
        addresses = [run('--provision', key).stdout.strip() for key in keys]
        assert all(re.fullmatch('0x[0-9a-f]{64}', a) for a in addresses)
        assert addresses[0] != addresses[1], 'fresh identities must differ'
        original = keys[0].read_bytes()
        assert stat.S_IMODE(keys[0].stat().st_mode) == 0o600
        assert run('--provision', keys[0]).stdout.strip() == addresses[0]
        assert keys[0].read_bytes() == original, 'existing identity changed'
        assert run('--derive-address', keys[0]).stdout.strip() == addresses[0]
        # Exercise the packaged binary, not a host build, against the reviewed
        # mixed-origin fixture. All checks are offline in this temporary tree.
        manifest = json.loads((Path(__file__).parents[1]/'manifests/fixture.localnet.json').read_text())
        manifest_path = root/'deployment.json'
        manifest_path.write_text(json.dumps(manifest))
        config = root/'config.toml'
        legacy = (f'[sui]\nmanifest_path = "{manifest_path}"\n'
                  f'[device]\nkeypair_path = "{keys[0]}"\n'
                  'device_object_id = "0x1"\ndevice_cap_id = "0x2"\n'
                  '[apply]\ncommand = "/usr/bin/false"\n'
                  f'[runtime]\nstate_dir = "{root / "state"}"\n')
        config.write_text(legacy)
        assert '0.2.1 accepts' in run('--self-check', config).stdout
        assert run('--key-holder', config).stdout.strip() == 'suifleet'
        service = legacy.replace(f'keypair_path = "{keys[0]}"\n', '')
        service += '\n[device.signer]\nkind = "service"\nsocket = "/run/suifleet-signer/sign.sock"\n'
        config.write_text(service)
        assert run('--key-holder', config).stdout.strip() == 'suifleet-signer'
        config.write_text(service+'\n[daemon_update]\nenabled = true\n')
        refused = run('--self-check', config, check=False)
        assert refused.returncode != 0 and 'daemon_update cannot be enabled with the service signer' in refused.stderr
        config.write_text(legacy)
        for field, replacement, error in [
                ('protocol_version', 999, 'unsupported manifest schema/protocol'),
                ('supported_clients', {'edge': {'min': '99.0.0', 'max_exclusive': '100.0.0'}}, 'unsupported edge client version')]:
            bad = dict(manifest)
            bad[field] = replacement
            manifest_path.write_text(json.dumps(bad))
            refused = run('--self-check', config, check=False)
            assert refused.returncode != 0 and error in refused.stderr, refused.stderr
        corrupt = root/'corrupt.key'
        corrupt.write_text('invalid')
        result = run('--provision', corrupt, check=False)
        assert result.returncode != 0, 'corrupt key was accepted'
        assert corrupt.read_text() == 'invalid', 'corrupt key was silently replaced'
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == before
    result = dict(binary_sha256=before, fresh_identities_unique=True,
                  existing_identity_preserved=True, derived_address_matches=True,
                  corrupt_identity_refused=True, private_keys_discarded=True,
                  offline_manifest_client_compatibility=True, service_signer_key_holder=True,
                  service_signer_self_update_refused=True, physical_boot_tested=False)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
