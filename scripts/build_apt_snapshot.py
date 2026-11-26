#!/usr/bin/env python3
"""Build a signed, static APT snapshot from verified SuiFleet release packages."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def build(output, packages, fingerprint):
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", fingerprint):
        raise ValueError("use a full OpenPGP signing fingerprint")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(output)

    architectures = set()
    versions = set()
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        stage = Path(temporary) / "snapshot"
        pool = stage / "pool"
        pool.mkdir(parents=True)
        for package in packages:
            def field(name):
                return subprocess.check_output(
                    ["dpkg-deb", "--field", package, name], text=True).strip()

            arch = field("Architecture")
            version = field("Version")
            if field("Package") != "suifleet" or arch not in ("amd64", "arm64"):
                raise ValueError(f"unexpected package or architecture: {package}")
            if arch in architectures or not re.fullmatch(r"[0-9][0-9A-Za-z.+~-]*", version):
                raise ValueError(f"duplicate architecture or unsupported version: {package}")
            versions.add(version)
            architectures.add(arch)
            shutil.copyfile(package, pool / f"suifleet_{version}_{arch}.deb")
        if len(versions) != 1:
            raise ValueError("each snapshot must contain exactly one package version")

        with (stage / "Packages").open("wb") as index:
            subprocess.run(["apt-ftparchive", "packages", "pool"], cwd=stage, stdout=index, check=True)
        (stage / "Release").write_bytes(subprocess.check_output([
            "apt-ftparchive", "-o", "APT::FTPArchive::Release::Origin=SuiFleet",
            "-o", "APT::FTPArchive::Release::Label=SuiFleet",
            "-o", f"APT::FTPArchive::Release::Architectures={' '.join(sorted(architectures))}",
            "release", ".",
        ], cwd=stage))
        gpg = ["gpg", "--batch", "--local-user", fingerprint + "!", "--digest-algo", "SHA256"]
        subprocess.run([*gpg, "--output", "InRelease", "--clearsign", "Release"], cwd=stage, check=True)
        subprocess.run([*gpg, "--armor", "--output", "Release.gpg", "--detach-sign", "Release"],
                       cwd=stage, check=True)
        stage.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("packages", nargs="+", type=Path)
    args = parser.parse_args()
    build(args.output, args.packages, args.fingerprint)


if __name__ == "__main__":
    main()
