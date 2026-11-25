#!/usr/bin/env python3
"""Exercise real RPMs and disposable OpenPGP repository trust on Rocky Linux."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

from prepare_rpm_repo import prepare

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path("/etc/suifleet")
STATE = Path("/var/lib/suifleet")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(*args):
    return run(*args, stdout=subprocess.PIPE).stdout.strip()


def dnf(*args, accepted=True):
    result = subprocess.run(["dnf", "-y", "--disablerepo=*", "--enablerepo=sf-test", *args],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="")
    if accepted:
        result.check_returncode()
    else:
        assert result.returncode, "DNF accepted an invalid repository or package"
    return result.stdout


def snapshot(paths):
    return {p: (p.read_bytes(), p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode))
            for p in paths}


def lifecycle(first, second, work):
    keys = work / "gnupg"
    keys.mkdir(mode=0o700)
    os.environ["GNUPGHOME"] = str(keys)
    run("gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
        "--quick-gen-key", "Disposable RPM QA", "rsa3072", "sign", "1d")
    listing = output("gpg", "--batch", "--with-colons", "--list-keys")
    fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    repo = work / "repo"
    repo_config = Path("/etc/yum.repos.d/sf-test.repo")
    repo_config.write_text(f"""[sf-test]
name=SuiFleet QA
baseurl=file://{repo}
gpgkey=file://{repo}/RPM-GPG-KEY-suifleet
gpgcheck=1
repo_gpgcheck=1
metadata_expire=0
""")
    prepare(first, repo, fingerprint)
    db = work / "verify"
    db.mkdir(mode=0o700)
    run("gpg", "--homedir", str(db), "--batch", "--import", str(repo / "RPM-GPG-KEY-suifleet"))
    run("gpg", "--homedir", str(db), "--batch", "--verify", str(repo / "SHA256SUMS.asc"), str(repo / "SHA256SUMS"))
    run("sha256sum", "-c", "SHA256SUMS", cwd=repo)
    metadata = repo / "repodata/repomd.xml"
    intact = metadata.read_bytes()
    metadata.write_bytes(intact + b"\n")
    assert "signature" in dnf("install", "suifleet", accepted=False).lower()
    metadata.write_bytes(intact)
    # Re-sign the repository metadata over an unsigned RPM. This isolates
    # the package signature check from the independent repository check.
    bad = work / "unsigned"
    bad.mkdir()
    shutil.copyfile(first, bad / first.name)
    shutil.copyfile(repo / "RPM-GPG-KEY-suifleet", bad / "RPM-GPG-KEY-suifleet")
    run("createrepo_c", str(bad))
    run("gpg", "--batch", "--armor", "--detach-sign", str(bad / "repodata/repomd.xml"))
    saved_config = repo_config.read_text()
    repo_config.write_text(saved_config.replace(str(repo), str(bad)))
    dnf("clean", "all")
    assert "gpg" in dnf("install", "suifleet", accepted=False).lower()
    repo_config.write_text(saved_config)
    dnf("clean", "all")

    # Unsafe pre-existing account must fail before deploying any files.
    run("groupadd", "--system", "suifleet")
    run("useradd", "--system", "-g", "suifleet", "suifleet")
    daemon_uid = pwd.getpwnam("suifleet").pw_uid
    run("groupadd", "--system", "suifleet-signer")
    run("useradd", "--system", "-o", "-u", str(daemon_uid), "-g", "suifleet-signer", "suifleet-signer")
    assert "separate unprivileged" in dnf("install", "suifleet", accepted=False)
    assert not Path("/usr/bin/suifleet").exists()
    run("userdel", "suifleet-signer")

    dnf("install", "suifleet")
    run("rpm", "-V", "suifleet")
    assert output("rpm", "-q", "suifleet") + ".rpm" == first.name
    assert not (CONFIG / "device.key").exists()
    assert not (CONFIG / "config.toml").exists()
    daemon = pwd.getpwnam("suifleet")
    signer = pwd.getpwnam("suifleet-signer")
    assert CONFIG.stat().st_gid == daemon.pw_gid and stat.S_IMODE(CONFIG.stat().st_mode) == 0o750
    assert STATE.stat().st_uid == daemon.pw_uid and stat.S_IMODE(STATE.stat().st_mode) == 0o700
    for unit in ("suifleet.service", "suifleet-signer.socket", "suifleet-enrollment.path"):
        assert subprocess.run(["systemctl", "is-enabled", "--quiet", unit]).returncode
        assert subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode

    sys.path.insert(0, str(ROOT / "scripts"))
    from test_ota import CAP, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP
    manifest = CONFIG / "deployment.json"
    manifest.write_text(json.dumps(dict(schema_version=1, protocol_version=1, network="localnet",
        chain_identifier="12345678", rpc_url="http://127.0.0.1:1", call_package_id=PACKAGE,
        upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
        shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
        supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"},
                           "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
        walrus={"publisher_url": "http://127.0.0.1:1", "aggregator_url": "http://127.0.0.1:1"})))
    signer_config = CONFIG / "signer.toml"
    signer_config.write_text(f'manifest_path = "deployment.json"\ndevice_cap_id = "{CAP}"\n'
                             'keypair_path = "/etc/suifleet/device.key"\ncaller_unit = "suifleet.service"\n')
    os.chown(signer_config, 0, signer.pw_gid)
    signer_config.chmod(0o640)
    run("systemctl", "start", "suifleet-provision.service")
    key = CONFIG / "device.key"
    assert key.stat().st_uid == signer.pw_uid and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert subprocess.run(["runuser", "-u", "suifleet", "--", "test", "-r", str(key)]).returncode
    run("systemctl", "enable", "--now", "suifleet-signer.socket")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect("/run/suifleet-signer/sign.sock")
        client.sendall(b'{"op":"address"}\n')
        assert json.loads(client.makefile("rb").readline(65536))["address"].startswith("0x")
    run("systemctl", "is-active", "suifleet-signer.service")

    config = CONFIG / "config.toml"
    config.write_text("# operator config\n")
    journal = STATE / "state.json"
    journal.write_text('{"operator-sentinel":true}\n')
    os.chown(journal, daemon.pw_uid, daemon.pw_gid)
    journal.chmod(0o600)
    enrollment = CONFIG / "claim.key"
    enrollment.write_text("disposable-test-credential\n")
    enrollment.chmod(0o600)
    paths = (key, config, journal, enrollment, signer_config, manifest, CONFIG / "device.address")
    before = snapshot(paths)
    shutil.rmtree(repo)
    prepare(second, repo, fingerprint)
    dnf("clean", "all")
    dnf("upgrade", "suifleet")
    assert output("rpm", "-q", "suifleet") + ".rpm" == second.name
    assert snapshot(paths) == before
    for unit in ("suifleet-signer.socket", "suifleet-signer.service"):
        assert subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode
    dnf("remove", "suifleet")
    assert snapshot(paths) == before and not Path("/usr/bin/suifleet").exists()
    assert not (Path("/etc/systemd/system/sockets.target.wants") / "suifleet-signer.socket").is_symlink()
    dnf("install", "suifleet")
    assert snapshot(paths) == before
    assert pwd.getpwnam("suifleet").pw_uid == daemon.pw_uid
    assert pwd.getpwnam("suifleet-signer").pw_uid == signer.pw_uid
    print("PASS signed install, systemd socket activation, upgrade, erase, reinstall and state retention")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--image", default="suifleet-rpm:local")
    parser.add_argument("--inside", action="store_true")
    args = parser.parse_args()
    if args.inside:
        run("uname", "-r")
        run("rpm", "-q", "rocky-release", "systemd", "rpm-build", "dnf", "createrepo_c", "gnupg2")
        with tempfile.TemporaryDirectory(prefix="rpm-qa-") as work:
            lifecycle(args.first, args.second, Path(work))
        return
    first = f"/first/{args.first.name}"
    second = f"/second/{args.second.name}"
    # Systemd and the package manager need root inside the disposable QA container.
    container = output("docker", "run", "-d", "--user", "0:0", "--network", "none",
                       "--privileged", "--cgroupns=private",
                       "--tmpfs", "/run", "--tmpfs", "/run/lock",
                       "-v", f"{ROOT}:/src:ro", "-v", f"{args.first.resolve().parent}:/first:ro",
                       "-v", f"{args.second.resolve().parent}:/second:ro", args.image, "/sbin/init")
    try:
        for _ in range(45):
            state = subprocess.run(["docker", "exec", container, "systemctl", "is-system-running"],
                                   text=True, capture_output=True)
            if state.stdout.strip() in ("running", "degraded"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Rocky systemd container did not boot")
        run("docker", "exec", container, "python3", "-B", "/src/scripts/test_rpm.py",
            "--inside", first, second)
    except subprocess.CalledProcessError:
        subprocess.run(["docker", "exec", container, "journalctl", "--no-pager",
                        "-u", "suifleet-provision.service", "-u", "suifleet-signer.service",
                        "-u", "suifleet-signer.socket"], check=False)
        raise
    finally:
        run("docker", "rm", "-f", container)


if __name__ == "__main__":
    main()
