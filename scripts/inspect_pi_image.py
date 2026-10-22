#!/usr/bin/env python3
"""Read-only inspection of an SF-029/SF-066 SD image; never opens a block device."""
import argparse
import bz2
import hashlib
import json
import re
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile


def partitions(header):
    if len(header) != 512 or header[510:] != b'\x55\xaa':
        raise ValueError('missing MBR signature')
    result = []
    for i in range(4):
        entry = header[446+i*16:462+i*16]
        start, sectors = struct.unpack_from('<II', entry, 8)
        if sectors:
            result.append(dict(type=entry[4], start=start*512, size=sectors*512))
    if len(result) != 2 or result[0]['type'] not in (0xb, 0xc, 0xe, 0x6) or result[1]['type'] != 0x83:
        raise ValueError('expected FAT boot then Linux root partitions')
    if result[0]['start'] + result[0]['size'] > result[1]['start']:
        raise ValueError('overlapping partitions')
    return result


def sha(path):
    with open(path, 'rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
        return digest.hexdigest()


def recipe_pins(text):
    """The daemon source revision and Cargo.lock digest a recipe builds."""
    pins = {}
    for key, field in [('SRCREV', 'source_revision'), ('SUIFLEET_LOCK_SHA256', 'lock_sha256')]:
        match = re.search(r'^'+key+r' = "([0-9a-f]+)"$', text, re.M)
        if not match:
            raise ValueError('recipe does not pin '+key)
        pins[field] = match.group(1)
    return pins


def check_build_info(build_info, pins):
    """Require the image's build-info to name exactly the recipe's pins."""
    fields = dict(line.split('=', 1) for line in build_info.splitlines() if '=' in line)
    for field in ['source_revision', 'lock_sha256', 'crate_version']:
        if not fields.get(field):
            raise ValueError('build-info lacks '+field)
    for field, value in (pins or {}).items():
        if fields[field] != value:
            raise ValueError(f'image {field} {fields[field]} differs from the recipe pin {value}')
    return fields


def inspect(image, work, mtools_dir, expected_keys=None, debugfs="debugfs", e2fsck=None, pins=None):
    if not stat.S_ISREG(image.stat().st_mode):
        raise ValueError('input must be a regular image file, never a device')
    disk = work/'disk.wic'
    with bz2.open(image, 'rb') if image.suffix == '.bz2' else image.open('rb') as src, disk.open('wb') as dst:
        shutil.copyfileobj(src, dst)
    with disk.open('rb') as src:
        parts = partitions(src.read(512))
        if parts[-1]['start'] + parts[-1]['size'] > disk.stat().st_size:
            raise ValueError('truncated image')
        src.seek(parts[0]['start'])
        boot = src.read(512)
        assert boot[510:] == b'\x55\xaa', 'invalid FAT boot sector'
        src.seek(parts[1]['start'])
        root = work/'root.ext4'
        with root.open('wb') as dst:
            remaining = parts[1]['size']
            while remaining:
                chunk = src.read(min(remaining, 1024*1024))
                if not chunk:
                    raise ValueError('truncated root partition')
                dst.write(chunk)
                remaining -= len(chunk)
    boot_files = ['config.txt', 'cmdline.txt', 'kernel8.img', 'bcm2710-rpi-3-b-plus.dtb', 'start.elf', 'fixup.dat']
    fat_image = str(disk)+'@@'+str(parts[0]['start'])
    boot_hashes = {}
    for name in boot_files:
        subprocess.run([str(mtools_dir/'mdir'), '-i', fat_image, '::'+name], check=True, capture_output=True)
        target = work/('boot-'+name)
        subprocess.run([str(mtools_dir/'mcopy'), '-i', fat_image, '::'+name, str(target)], check=True, capture_output=True)
        boot_hashes[name] = sha(target)
    def boot_text(name):
        return subprocess.check_output([str(mtools_dir/'mtype'), '-i', fat_image, '::'+name], text=True)
    config = boot_text('config.txt')
    command_line = boot_text('cmdline.txt')
    assert re.search(r'^arm_64bit=1$', config, re.M), '64-bit boot not configured'
    assert 'root=' in command_line and 'rootwait' in command_line, 'missing SD root command line'
    fsck_checked = False
    if e2fsck is not None:
        subprocess.run([str(e2fsck), '-f', '-n', str(root)], check=True, capture_output=True)
        fsck_checked = True
    def debug(command):
        p = subprocess.run([str(debugfs), '-R', command, str(root)], capture_output=True)
        return p.stdout.decode(errors='replace'), p.stderr.decode(errors='replace')
    def read(path):
        out, err = debug('cat '+path)
        assert 'File not found' not in err, path
        return out
    def exists(path):
        out, err = debug('stat '+path)
        return 'Inode:' in out and 'File not found' not in err
    absent = ['/etc/suifleet/device.key','/etc/suifleet/device.address','/etc/suifleet/config.toml','/etc/suifleet/deployment.json','/etc/suifleet/firmware.toml','/etc/suifleet/signer.toml','/etc/suifleet/pkcs11.pin','/etc/suifleet/env','/run/suifleet-signer','/var/lib/suifleet','/home/operator/.ssh/id_ed25519','/root/.ssh/id_ed25519']
    for path in absent:
        assert not exists(path), 'runtime secret/state baked into image: '+path
    ssh_listing = debug('ls -p /etc/ssh')[0]
    assert 'ssh_host_' not in ssh_listing, 'baked SSH host identity'
    assert not read('/etc/machine-id').strip(), 'baked machine identity'
    assert read('/etc/hostname').strip() == 'suifleet-pi'
    assert not exists('/etc/network/interfaces'), 'legacy ifupdown configuration must not compete with networkd'
    network = read('/etc/systemd/network/20-wired.network')
    assert 'DHCP=yes' in network and 'Address=' not in network
    ssh = read('/etc/ssh/sshd_config.d/60-suifleet.conf')
    for setting in ['PasswordAuthentication no','KbdInteractiveAuthentication no','PermitRootLogin no','PubkeyAuthentication yes','AllowUsers operator']:
        assert setting in ssh, setting
    assert 'Include /etc/ssh/sshd_config.d/*.conf' in read('/etc/ssh/sshd_config')
    assert 'operator ALL=(ALL:ALL) NOPASSWD: ALL' in read('/etc/sudoers.d/operator')
    shadow = read('/etc/shadow')
    for user in ['root','operator']:
        password = next(line.split(':')[1] for line in shadow.splitlines() if line.startswith(user+':'))
        assert password and password[0] in '*!', 'password login enabled: '+user
    operator_uid = next(line.split(':')[2] for line in read('/etc/passwd').splitlines() if line.startswith('operator:'))
    for path, mode in [('/home/operator', '0750'), ('/home/operator/.ssh', '0700'), ('/home/operator/.ssh/authorized_keys', '0600')]:
        info, _ = debug('stat '+path)
        assert 'Mode:  '+mode in info or 'Mode:  '+mode.lstrip('0') in info, 'unsafe access mode: '+path
        assert re.search(r'User:\s+'+re.escape(operator_uid)+r'\s', info), 'operator ownership: '+path
    authorized_keys = read('/home/operator/.ssh/authorized_keys')
    if expected_keys is not None:
        assert authorized_keys == expected_keys.read_text(), 'image authorized keys differ from the private local overlay'
    keys = authorized_keys.splitlines()
    assert keys and all(k.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-')) for k in keys)
    enabled = ['/etc/systemd/system/multi-user.target.wants/systemd-networkd.service','/etc/systemd/system/multi-user.target.wants/systemd-resolved.service','/etc/systemd/system/sysinit.target.wants/systemd-timesyncd.service','/etc/systemd/system/multi-user.target.wants/suifleet-provision.service','/etc/systemd/system/multi-user.target.wants/suifleet-enrollment.path','/etc/systemd/system/multi-user.target.wants/suifleet.service','/etc/systemd/system/multi-user.target.wants/suifleet-signer.service','/etc/systemd/system/multi-user.target.wants/suifleet-firmware.path','/etc/systemd/system/sockets.target.wants/suifleet-signer.socket']
    for path in enabled:
        assert exists(path), 'service not enabled: '+path
        assert exists('/usr/lib/systemd/system/'+Path(path).name), 'missing enabled unit target: '+path
    assert any(exists(path) for path in ['/etc/systemd/system/sockets.target.wants/sshd.socket', '/etc/systemd/system/multi-user.target.wants/sshd.service']), 'SSH not enabled'
    assert 'ConditionPathExists=/etc/suifleet/config.toml' in read('/usr/lib/systemd/system/suifleet.service')
    assert 'User=suifleet' in read('/usr/lib/systemd/system/suifleet.service')
    # SF-066: the launcher and signer are modes of the same immutable binary.
    daemon_unit = read('/usr/lib/systemd/system/suifleet.service')
    for setting in ['ExecStart=/usr/bin/suifleet --launch',
                    'ExecStartPost=+/usr/bin/suifleet --record-main-process',
                    'NotifyAccess=main', 'KillMode=mixed']:
        assert setting in daemon_unit, setting
    signer_unit = read('/usr/lib/systemd/system/suifleet-signer.service')
    for setting in ['User=suifleet-signer', 'Group=suifleet-signer',
                    '--signer-service /etc/suifleet/signer.toml',
                    'ConditionPathExists=/etc/suifleet/signer.toml',
                    'IPAddressDeny=any', 'NoNewPrivileges=yes']:
        assert setting in signer_unit, setting
    passwd = read('/etc/passwd').splitlines()
    groups = read('/etc/group').splitlines()
    def account_id(lines, name):
        return next(line.split(':')[2] for line in lines if line.startswith(name+':'))
    assert account_id(passwd, 'suifleet') != account_id(passwd, 'suifleet-signer')
    signer_group = next(line.split(':')[3] for line in groups if line.startswith('suifleet-signer:'))
    assert 'suifleet' not in signer_group.split(','), 'daemon belongs to signer group'
    for path, mode, group in [
            ('/etc/suifleet', '0750', 'suifleet'),
            ('/etc/suifleet/config.toml.example', '0640', 'suifleet'),
            ('/etc/suifleet/signer.toml.example', '0640', 'suifleet-signer'),
            ('/usr/bin/suifleet', '0755', 'root'),
            ('/usr/libexec/suifleet-provision', '0755', 'root')]:
        info, _ = debug('stat '+path)
        assert 'Mode:  '+mode in info, 'unsafe mode: '+path
        assert re.search(r'User:\s+0\s', info), 'not root owned: '+path
        assert re.search(r'Group:\s+'+account_id(groups, group)+r'\s', info), 'wrong group: '+path
    assert 'kind = "service"' in read('/etc/suifleet/config.toml.example')
    assert exists('/etc/ssl/certs/ca-certificates.crt')
    build_info = read('/usr/share/suifleet/build-info')
    build_fields = check_build_info(build_info, pins)
    binaries = {}
    for name in ['suifleet','suifleet-test-updater']:
        dest = work/name
        _, err = debug(f'dump /usr/bin/{name} {dest}')
        assert dest.exists(), err
        binaries[name] = dict(sha256=sha(dest), size=dest.stat().st_size)
    with (work/'suifleet').open('rb') as stream:
        elf = stream.read(20)
    assert elf[:4] == b'\x7fELF' and elf[4:6] == b'\x02\x01' and struct.unpack_from('<H',elf,18)[0] == 183, 'daemon must be AArch64 ELF64'
    return dict(image=str(image.resolve()),sha256=sha(image),size=image.stat().st_size,uncompressed_size=disk.stat().st_size,uncompressed_sha256=sha(disk),partitions=parts,boot_files=boot_files,boot_file_sha256=boot_hashes,boot_config=config,boot_command_line=command_line,daemon_build_info=build_fields,build_info_matches_recipe=pins is not None,binaries=binaries,authorized_public_key_count=len(keys),authorized_keys_match_expected=True if expected_keys is not None else None,runtime_state_absent=absent,enabled_services=enabled,ext4_fsck_read_only_passed=fsck_checked,signer_service_separation_inspected=True,physical_boot_tested=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image',type=Path)
    parser.add_argument('--debugfs',default='debugfs')
    parser.add_argument('--e2fsck',type=Path)
    parser.add_argument('--expected-authorized-keys',type=Path)
    parser.add_argument('--mtools-dir',type=Path,default=Path('/usr/bin'))
    parser.add_argument('--recipe',type=Path,help='suifleet_1.0.bb whose SRCREV/lock digest the image must contain')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='sf029-inspect-') as directory:
        pins=recipe_pins(args.recipe.read_text()) if args.recipe else None
        result=inspect(args.image,Path(directory),args.mtools_dir,args.expected_authorized_keys,args.debugfs,args.e2fsck,pins)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))

if __name__ == '__main__':
    main()
