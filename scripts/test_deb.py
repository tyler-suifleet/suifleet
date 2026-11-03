#!/usr/bin/env python3
"""Real dpkg lifecycle tests in disposable Debian/Ubuntu containers (Docker/QEMU)."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
BASES = {
    "debian12": "debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587",
    "ubuntu2404": "ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852",
}
DOCKERFILE = '''ARG BASE
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv ca-certificates adduser python3 procps lintian && rm -rf /var/lib/apt/lists/* && systemctl mask systemd-logind.service getty.target console-getty.service systemd-udevd.service
# Privileged systemd containers must not reset the host binfmt registrations.
RUN systemctl mask systemd-binfmt.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
'''


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def unsafe_accounts(package):
    run("adduser", "--system", "--group", "--no-create-home", "suifleet")
    run("adduser", "--system", "--group", "--no-create-home", "suifleet-signer")
    import pwd
    run("usermod", "--non-unique", "--uid", str(pwd.getpwnam("suifleet").pw_uid), "suifleet-signer")
    result = subprocess.run(["apt-get", "install", "-y", str(package)], capture_output=True, text=True)
    assert result.returncode != 0 and "must have separate users and groups" in result.stdout + result.stderr
    assert not Path("/etc/suifleet/device.key").exists()
    print("PASS package rejects preexisting daemon/signer UID collision")


def inside(package):
    import os
    import pwd
    import shutil
    import stat
    import socket
    import sys
    package = Path(package)
    run("apt-get", "install", "-y", str(package))
    assert not Path("/etc/suifleet/device.key").exists()
    assert not Path("/etc/suifleet/config.toml").exists()
    for unit in ("suifleet.service", "suifleet-enrollment.path", "suifleet-signer.socket"):
        result = subprocess.run(["systemctl", "is-enabled", unit], capture_output=True, text=True)
        assert result.returncode != 0, result.stdout
    assert stat.S_IMODE(Path("/etc/suifleet").stat().st_mode) == 0o750
    assert Path("/var/lib/suifleet").stat().st_uid == pwd.getpwnam("suifleet").pw_uid
    run("/usr/libexec/suifleet-provision")
    key = Path("/etc/suifleet/device.key")
    digest = hashlib.sha256(key.read_bytes()).hexdigest()
    run("runuser", "-u", "suifleet", "--", "suifleet", "--derive-address", str(key))
    # Start the actual installed signer as its own UID, also on emulated ARM.
    # Native systemd/cgroup caller isolation is exercised by --systemd.
    sys.path.insert(0, str(ROOT / "scripts"))
    from test_ota import CAP, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP
    manifest = Path("/etc/suifleet/deployment.json")
    manifest.write_text(json.dumps(dict(schema_version=1, protocol_version=1, network="localnet",
        chain_identifier="12345678", rpc_url="http://127.0.0.1:1", call_package_id=PACKAGE,
        upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
        shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
        supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"},
                           "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
        walrus={"publisher_url": "http://127.0.0.1:1", "aggregator_url": "http://127.0.0.1:1"})))
    manifest.chmod(0o644)
    signer = Path("/etc/suifleet/signer.toml")
    signer.write_text('manifest_path = "deployment.json"\n' + f'device_cap_id = "{CAP}"\n'
        'keypair_path = "/etc/suifleet/device.key"\ncaller_unit = "suifleet.service"\n'
        'socket = "/run/suifleet-deb-smoke/sign.sock"\n')
    os.chown(signer, 0, pwd.getpwnam("suifleet-signer").pw_gid)
    signer.chmod(0o640)
    run("/usr/libexec/suifleet-provision")
    assert key.stat().st_uid == pwd.getpwnam("suifleet-signer").pw_uid
    assert subprocess.run(["runuser", "-u", "suifleet", "--", "test", "-r", str(key)]).returncode != 0
    socket_dir = Path("/run/suifleet-deb-smoke")
    socket_dir.mkdir(mode=0o750)
    os.chown(socket_dir, pwd.getpwnam("suifleet-signer").pw_uid, pwd.getpwnam("suifleet").pw_gid)
    process = subprocess.Popen(["runuser", "-u", "suifleet-signer", "-g", "suifleet-signer", "-G", "suifleet", "--",
                                "suifleet", "--signer-service", str(signer)], start_new_session=True)
    sock = socket_dir / "sign.sock"
    try:
        for _ in range(100):
            if sock.exists():
                break
            assert process.poll() is None, "signer exited"
            time.sleep(0.1)
        else:
            raise AssertionError("signer socket missing")
        os.chown(sock, pwd.getpwnam("suifleet-signer").pw_uid, pwd.getpwnam("suifleet").pw_gid)
        sock.chmod(0o660)
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(sock))
            client.sendall(b'{"op":"address"}\n')
            assert json.loads(client.makefile("rb").readline(65536))["address"].startswith("0x")
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(sock))
            client.sendall(b'{"op":"export"}\n')
            assert "error" in json.loads(client.makefile("rb").readline(65536))
    finally:
        import signal
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    print("PASS installed unprivileged signer address/refused export and daemon key-read isolation")
    config = Path("/etc/suifleet/config.toml")
    config.write_text("# operator configuration: preserve byte for byte\n")
    config.chmod(0o640)
    journal = Path("/var/lib/suifleet/state.json")
    journal.write_text('{"operator-sentinel":true}\n')
    os.chown(journal, pwd.getpwnam("suifleet").pw_uid, pwd.getpwnam("suifleet").pw_gid)
    enrollment = Path("/etc/suifleet/claim.key")
    enrollment.write_text("disposable-test-credential\n")
    enrollment.chmod(0o600)
    trust = Path("/etc/suifleet/release-trust.toml")
    trust.write_text(trust.read_text() + "\n# operator trust change\n")
    paths = [key, config, journal, enrollment, signer, manifest, trust]
    original = {str(p): (p.read_bytes(), p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode)) for p in paths}

    def preserved(include_trust=True):
        for p in paths if include_trust else paths[:-1]:
            assert (p.read_bytes(), p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode)) == original[str(p)], str(p)
        assert hashlib.sha256(key.read_bytes()).hexdigest() == digest

    run("systemctl", "--no-reload", "enable", "suifleet-enrollment.path", "suifleet-signer.socket")
    # Real package upgrade of the same payload with a higher Debian revision.
    stage = Path("/tmp/deb-upgrade")
    run("dpkg-deb", "-R", str(package), str(stage))
    control = stage / "DEBIAN/control"
    text = control.read_text()
    version = next(line[9:] for line in text.splitlines() if line.startswith("Version: "))
    control.write_text(text.replace("Version: " + version, "Version: " + version + ".1"))
    upgrade = Path("/tmp/upgrade.deb")
    run("dpkg-deb", "--root-owner-group", "--build", str(stage), str(upgrade))
    run("apt-get", "install", "-y", str(upgrade))
    preserved()
    run("apt-get", "remove", "-y", "suifleet")
    preserved()
    assert not Path("/etc/systemd/system/multi-user.target.wants/suifleet-enrollment.path").is_symlink()
    assert not Path("/etc/systemd/system/sockets.target.wants/suifleet-signer.socket").is_symlink()
    assert not Path("/usr/bin/suifleet").exists()
    run("apt-get", "install", "-y", str(package))
    preserved()
    run("apt-get", "purge", "-y", "suifleet")
    preserved(False)
    assert not trust.exists()
    assert pwd.getpwnam("suifleet") and pwd.getpwnam("suifleet-signer")
    shutil.rmtree(stage)
    print("PASS real install/upgrade/remove/reinstall/purge, offline identity CLI, permissions and data preservation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path)
    parser.add_argument("--inside", type=Path)
    parser.add_argument("--unsafe-account-test", type=Path)
    parser.add_argument("--arch", choices=("amd64", "arm64"), action="append")
    parser.add_argument("--distro", choices=BASES, action="append")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--systemd", action="store_true", help="test installed package with real signer/daemon; requires --shim")
    parser.add_argument("--shim", type=Path)
    args = parser.parse_args()
    if args.unsafe_account_test:
        unsafe_accounts(args.unsafe_account_test)
        return
    if args.inside:
        inside(args.inside)
        return
    evidence = []
    for arch in args.arch or ("amd64", "arm64"):
        packages = sorted(args.packages.rglob(f"*_{arch}.deb"))
        assert len(packages) == 1, packages
        for distro in args.distro or BASES:
            image = f"suifleet-deb-test:{distro}-{arch}"
            run("docker", "build", "--platform", "linux/" + arch, "--build-arg", "BASE=" + BASES[distro],
                "-t", image, "-", input=DOCKERFILE)
            run("docker", "run", "--rm", "--network", "none", "--platform", "linux/" + arch,
                "-v", f"{ROOT}:/workspace:ro", "-v", f"{packages[0].resolve()}:/package.deb:ro", image,
                "python3", "/workspace/scripts/test_deb.py", "--unsafe-account-test", "/package.deb")
            run("docker", "run", "--rm", "--network", "none", "--platform", "linux/" + arch,
                "-v", f"{ROOT}:/workspace:ro", "-v", f"{packages[0].resolve()}:/package.deb:ro", image,
                "python3", "/workspace/scripts/test_deb.py", "--inside", "/package.deb")
            if args.systemd:
                assert args.shim and args.shim.is_file(), "--shim is required"
                name = f"suifleet-deb-systemd-{distro}-{arch}"
                run("docker", "run", "-d", "--name", name, "--network", "none", "--privileged", "--cgroupns=private",
                    "--platform", "linux/" + arch, "--tmpfs", "/run", "--tmpfs", "/run/lock",
                    "-v", f"{ROOT}:/workspace:ro", "-v", f"{packages[0].resolve()}:/package.deb:ro",
                    "-v", f"{args.shim.resolve()}:/grpc_sim_shim:ro", image)
                try:
                    for _ in range(30):
                        state = subprocess.run(["docker", "exec", name, "systemctl", "is-system-running"], capture_output=True, text=True)
                        if state.stdout.strip() in ("running", "degraded"):
                            break
                        time.sleep(1)
                    else:
                        raise AssertionError("systemd did not boot")
                    run("docker", "exec", "-e", "SUIFLEET_TEST_DEB=/package.deb", "-e", "SUIFLEET_TEST_SHIM=/grpc_sim_shim",
                        "-e", "PYTHONDONTWRITEBYTECODE=1", name, "python3", "/workspace/scripts/test_key_isolation_systemd.py",
                        "--inside", "--head", "debian-package-smoke")
                finally:
                    run("docker", "rm", "-f", name)
            # Lint separately, preserve diagnostics for review; fail on errors.
            run("docker", "run", "--rm", "--network", "none", "--platform", "linux/" + arch,
                "-v", f"{packages[0].resolve()}:/package.deb:ro", image, "lintian", "--allow-root", "/package.deb")
            evidence.append(dict(distro=distro, arch=arch, package=packages[0].name,
                                 sha256=hashlib.sha256(packages[0].read_bytes()).hexdigest(), result="pass"))
    if args.evidence:
        args.evidence.write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
