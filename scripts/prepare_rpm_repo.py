#!/usr/bin/env python3
"""Stage a signed RPM repository from a trusted, reproducible RPM build."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(package, out, fingerprint):
    if not re.fullmatch(r"[0-9A-F]{40}", fingerprint):
        raise ValueError("provide the full uppercase OpenPGP primary-key fingerprint")
    if out.exists() or out.is_symlink():
        raise ValueError("repository output must not exist")
    provenance = package.parent / "rpm-provenance.json"
    sbom = package.parent / "sbom.spdx.json"
    record = json.loads(provenance.read_bytes())
    if (record["schema"] != 1 or record["rpm"] != package.name
            or record["rpm_sha256"] != sha256(package) or record["sbom_sha256"] != sha256(sbom)):
        raise ValueError("RPM or SBOM does not match the build provenance")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rpm-staging-", dir=out.parent) as temp:
        root = Path(temp)
        repo = root / "repo"
        repo.mkdir()
        signed = repo / package.name
        shutil.copyfile(package, signed)
        public = repo / "RPM-GPG-KEY-suifleet"
        run("gpg", "--batch", "--armor", "--output", str(public), "--export", fingerprint)
        if not public.stat().st_size:
            raise ValueError("signing public key not found")
        run("rpmsign", "--define", f"_gpg_name {fingerprint}",
            "--resign", str(signed))
        db = root / "rpmdb"
        db.mkdir()
        run("rpmkeys", "--dbpath", str(db), "--import", str(public))
        run("rpmkeys", "--dbpath", str(db), "--checksig", str(signed))
        shutil.copyfile(provenance, repo / provenance.name)
        shutil.copyfile(sbom, repo / sbom.name)
        run("createrepo_c", str(repo))
        metadata = repo / "repodata/repomd.xml"
        checksums = repo / "SHA256SUMS"
        files = (signed, metadata, public, repo / provenance.name, repo / sbom.name)
        checksums.write_text("".join(f"{sha256(path)}  {path.relative_to(repo)}\n" for path in files))
        for path in (metadata, checksums):
            run("gpg", "--batch", "--armor", "--local-user", fingerprint, "--detach-sign", str(path))
            run("gpg", "--batch", "--verify", str(path) + ".asc", str(path))
        repo.rename(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fingerprint", required=True)
    args = parser.parse_args()
    prepare(args.package, args.out.absolute(), args.fingerprint)
