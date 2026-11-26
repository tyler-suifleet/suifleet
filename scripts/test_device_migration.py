#!/usr/bin/env python3
"""Exercise the package migration hooks on the lab's 0.2.1 filesystem layout.

Run as root in a disposable Docker container with --network none and a
current daemon executable. Only the layout is recorded, never production
state, secrets or enrollment credentials.
"""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess

CONFIG = Path("/etc/suifleet")
STATE = Path("/var/lib/suifleet")
KEY = CONFIG / "device.key"
MIGRATE = "/usr/sbin/suifleet-migrate"
PENDING = CONFIG / "migration.pending"


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def require_disposable():
    assert Path("/.dockerenv").exists() and os.geteuid() == 0
    assert {p.name for p in Path("/sys/class/net").iterdir()} == {"lo"}


def create_legacy_key(daemon):
    require_disposable()
    assert not KEY.exists() and not (CONFIG / "config.toml").exists()
    gid = grp.getgrnam("suifleet").gr_gid
    CONFIG.mkdir(mode=0o750, exist_ok=True)
    os.chown(CONFIG, 0, gid)
    address = run(daemon, "--provision", KEY)
    os.chown(KEY, pwd.getpwnam("suifleet").pw_uid, gid)
    assert stat.S_IMODE(KEY.stat().st_mode) == 0o600
    return address


def legacy_files(manifest, device, cap, address):
    # The lab used the standard locations, a direct keypair_path config and
    # a bound journal. The old device/cap and retired manifest are archived.
    files = {
        CONFIG / "config.toml": (
            '[sui]\nmanifest_path = "deployment.json"\n[device]\n'
            f'keypair_path = "{KEY}"\ndevice_object_id = "{device}"\ndevice_cap_id = "{cap}"\n'
            f'[apply]\ncommand = "/usr/bin/false"\n[runtime]\nstate_dir = "{STATE}"\n'
        ).encode(),
        CONFIG / "deployment.json": json.dumps(manifest).encode(),
        STATE / "state.json": json.dumps({
            "binding": f'{manifest["chain_identifier"]}/{manifest["type_origins"]["device_registry::DeviceRegistry"]}/{device}/{address}',
            "cursor": None, "deployments": {}, "initialized": True,
        }).encode(),
    }
    uid = pwd.getpwnam("suifleet").pw_uid
    gid = grp.getgrnam("suifleet").gr_gid
    STATE.mkdir(mode=0o700, exist_ok=True)
    os.chown(STATE, uid, gid)
    for path, contents in files.items():
        path.write_bytes(contents)
        os.chown(path, uid if path.parent == STATE else 0, gid)
        path.chmod(0o600 if path.parent == STATE else 0o640)
    return files


def check_parked(files, address, key_bytes):
    assert PENDING.is_dir() and stat.S_IMODE(PENDING.stat().st_mode) == 0o700
    assert (KEY.stat().st_uid, KEY.stat().st_gid, stat.S_IMODE(KEY.stat().st_mode)) == (
        pwd.getpwnam("suifleet-signer").pw_uid, grp.getgrnam("suifleet-signer").gr_gid, 0o600)
    assert KEY.read_bytes() == key_bytes
    assert run("/usr/bin/suifleet", "--derive-address", KEY) == address
    assert subprocess.run(["runuser", "-u", "suifleet", "--", "test", "-r", KEY]).returncode != 0
    for path, contents in files.items():
        assert path.read_bytes() == contents, path
    assert subprocess.run(["/usr/libexec/suifleet-provision"], capture_output=True).returncode != 0
    assert KEY.read_bytes() == key_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Migration-capable .deb or .rpm")
    parser.add_argument("--daemon", type=Path, required=True, help="Fixture identity generator")
    args = parser.parse_args()
    address = create_legacy_key(args.daemon)
    manifest = {
        "chain_identifier": "4c78adac",
        "call_package_id": "0x99ade699",
        "type_origins": {"device_registry::DeviceRegistry": "0x99ade699"},
        "shared_objects": {"registry": {"id": "0x4431790c"}},
    }
    files = legacy_files(manifest, "0xd1", "0xd2", address)
    key_bytes = KEY.read_bytes()
    assert not any(entry.pw_name == "suifleet-signer" for entry in pwd.getpwall())
    # The recorded lab image had a package-managed unit in the vendor directory.
    systemd = Path("/run/systemd/system").is_dir()
    if systemd:
        units = Path("/usr/lib/systemd/system")
        (units / "suifleet.service").write_text(
            "[Unit]\nDescription=Legacy SuiFleet layout fixture\n"
            "[Service]\nUser=suifleet\nExecStart=/bin/sleep infinity\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        (units / "suifleet-enrollment.path").write_text(
            "[Path]\nPathExists=/etc/suifleet/config.toml\nUnit=suifleet.service\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", "suifleet-enrollment.path", "suifleet.service")
        run("systemctl", "is-active", "suifleet.service")

    install = ["dpkg", "-i"] if args.package.suffix == ".deb" else ["rpm", "-U"]
    print(run(*install, args.package))
    check_parked(files, address, key_bytes)
    if systemd:
        run("systemctl", "start", "suifleet-enrollment.path", "suifleet.service")
        for unit in ("suifleet-enrollment.path", "suifleet.service", "suifleet-signer.socket"):
            assert subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode != 0

    link = CONFIG / "fixture-link.key"
    os.link(KEY, link)
    refused = subprocess.run([MIGRATE, "prepare"], capture_output=True, text=True)
    assert refused.returncode != 0 and "unlinked regular device.key" in refused.stderr
    assert KEY.read_bytes() == key_bytes and (STATE / "state.json").is_file()
    link.unlink()
    run(MIGRATE, "prepare")
    run(MIGRATE, "prepare")
    assert not (CONFIG / "config.toml").exists() and not (STATE / "state.json").exists()
    archive = Path((PENDING / "archive").read_text().strip())
    assert stat.S_IMODE(archive.stat().st_mode) == 0o700
    assert (archive / "state/state.json").read_bytes() == files[STATE / "state.json"]
    assert (PENDING / "config.toml").read_bytes() == files[CONFIG / "config.toml"]
    assert KEY.read_bytes() == key_bytes
    print(f"PASS {args.package.suffix} migrated the 0.2.1 layout, preserving key and journal")


if __name__ == "__main__":
    main()
