#!/usr/bin/env python3
"""Package an already built GNU/Linux release binary; no identity or network access."""
import argparse
import hashlib
import gzip
from email.utils import formatdate
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ARCHES = {"amd64": 62, "arm64": 183}


def build(binary, out, version, arch, epoch):
    subprocess.run(["dpkg", "--validate-version", version], check=True)
    header = binary.read_bytes()[:20]
    if (header[:6] != b"\x7fELF\x02\x01" or len(header) < 20
            or struct.unpack("<H", header[18:20])[0] != ARCHES[arch]):
        raise ValueError("binary must be a little-endian ELF64 for " + arch)
    out.mkdir(parents=True, exist_ok=True)
    package = out / f"suifleet_{version}_{arch}.deb"
    with tempfile.TemporaryDirectory(prefix="suifleet-deb-") as temporary:
        stage = Path(temporary)

        def copy(source, destination, mode=0o644):
            dest = stage / destination
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            dest.chmod(mode)

        copy(binary, "usr/bin/suifleet", 0o755)
        files = ROOT / "suifleet-edge/meta-suifleet/recipes-edge/suifleet/files"
        # Firmware helpers are deliberately opt-in source integrations, not part
        # of this application package. No privileged updater is installed.
        for name in ("suifleet.service", "suifleet-signer.service", "suifleet-signer.socket",
                     "suifleet-provision.service", "suifleet-enrollment.path"):
            copy(files / name, "lib/systemd/system/" + name)
        copy(files / "suifleet-provision", "usr/libexec/suifleet-provision", 0o755)
        for name in ("signer.toml.example", "config.toml.example"):
            copy(files / name, "usr/share/doc/suifleet/examples/" + name)
        for name in ("KEY_ISOLATION.md", "INSTALL_DEVICE_CLIENT.md", "DEBIAN_PACKAGING.md"):
            copy(ROOT / "docs" / name, "usr/share/doc/suifleet/" + name)
        copy(ROOT / "LICENSE", "usr/share/doc/suifleet/copyright")
        changelog = (f"suifleet ({version}) unstable; urgency=medium\n\n"
                     "  * Package the daemon and isolated signer; retain runtime state.\n\n"
                     f" -- Tyler Bush <tyler.jonathan.bush@gmail.com>  {formatdate(epoch, localtime=False)}\n")
        (stage / "usr/share/doc/suifleet/changelog.Debian.gz").write_bytes(
            gzip.compress(changelog.encode(), mtime=0))
        (stage / "usr/share/doc/suifleet/changelog.Debian.gz").chmod(0o644)
        copy(ROOT / "release/trust.toml", "etc/suifleet/release-trust.toml")
        for name in ("postinst", "prerm", "postrm"):
            copy(ROOT / "suifleet-edge/packaging/debian" / name, "DEBIAN/" + name, 0o755)
        (stage / "DEBIAN/conffiles").write_text("/etc/suifleet/release-trust.toml\n")
        size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) // 1024 + 1
        (stage / "DEBIAN/control").write_text(f"""Package: suifleet
Version: {version}
Architecture: {arch}
Maintainer: SuiFleet <tyler.jonathan.bush@gmail.com>
Section: admin
Priority: optional
Installed-Size: {size}
Depends: libc6 (>= 2.35), libgcc-s1, ca-certificates, adduser, systemd (>= 249), init-system-helpers (>= 1.62)
Homepage: https://github.com/tyler-suifleet/suifleet
Description: edge deployment client and isolated device signer
 Authenticated component delivery with unprivileged execution and a separate
 device signing service. Services require explicit operator configuration.
""")
        sums = []
        for path in sorted(stage.rglob("*")):
            if path.is_file() and "DEBIAN" not in path.parts:
                sums.append(hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest() + "  " + str(path.relative_to(stage)))
        (stage / "DEBIAN/md5sums").write_text("\n".join(sums) + "\n")
        stage.chmod(0o755)
        for path in stage.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            os.utime(path, (epoch, epoch))
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch))
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(stage), str(package)],
                       check=True, env=env)
    return package


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", choices=ARCHES, required=True)
    parser.add_argument("--epoch", type=int, required=True)
    args = parser.parse_args()
    package = build(args.binary, args.out, args.version, args.arch, args.epoch)
    (args.out / (package.name + ".sha256")).write_text(
        hashlib.sha256(package.read_bytes()).hexdigest() + "  " + package.name + "\n")
