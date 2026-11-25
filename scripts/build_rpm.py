#!/usr/bin/env python3
"""Build a Rocky Linux 10 RPM from a trusted SuiFleet release build bundle."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = "x86_64-unknown-linux-gnu"
PACKAGING = ROOT / "suifleet-edge/packaging/rpm"
UNITS = ("suifleet.service", "suifleet-signer.service", "suifleet-signer.socket",
         "suifleet-provision.service", "suifleet-enrollment.path")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_artifact(bundle, manifest, name):
    data = (bundle / name).read_bytes()
    expected = [entry for entry in manifest["files"] if entry["path"] == name]
    if len(expected) != 1 or expected[0]["sha256"] != digest(data) or expected[0]["size"] != len(data):
        raise ValueError(f"release artifact mismatch: {name}")
    return data


def build(bundle, out, release_number):
    release_manifest = (bundle / "release.json").read_bytes()
    manifest = json.loads(release_manifest)
    version, commit = manifest["version"], manifest["commit"]
    if (manifest["schema"] != 1 or manifest["name"] != "suifleet" or manifest["target"] != TARGET
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        raise ValueError("expected a SuiFleet x86_64 GNU release manifest")
    provenance = json.loads(read_artifact(bundle, manifest, "provenance.json"))
    sbom = read_artifact(bundle, manifest, "sbom.spdx.json")
    if any(provenance[key] != manifest[key] for key in ("target", "version", "commit")):
        raise ValueError("release manifest and provenance disagree")
    epoch = int(provenance["source_date_epoch"])
    binary_tar = read_artifact(bundle, manifest, f"suifleet-{version}-{commit[:12]}-{TARGET}.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(binary_tar), mode="r:gz") as archive:
        member = archive.getmember("suifleet/bin/suifleet")
        if not member.isfile():
            raise ValueError("release binary is not a regular file")
        binary = archive.extractfile(member).read()
    if binary[:6] != b"\x7fELF\x02\x01" or binary[18:20] != b"\x3e\x00" or digest(binary) != provenance["binary_sha256"]:
        raise ValueError("release binary architecture or provenance mismatch")
    # An RPM release has a monotonically increasing numeric prefix for rebuilds.
    release = f"{release_number}.git{commit[:12]}.el10"
    rpm_name = f"suifleet-{version}-{release}.x86_64.rpm"
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("RPM output directory must be empty")
    shipped = ROOT / "suifleet-edge/meta-suifleet/recipes-edge/suifleet/files"
    with tempfile.TemporaryDirectory(prefix="suifleet-rpm-") as temp:
        top = Path(temp)
        (top / "SOURCES").mkdir()
        payload = {
            "usr/bin/suifleet": (binary, 0o755),
            "usr/libexec/suifleet-provision": ((shipped / "suifleet-provision").read_bytes(), 0o755),
            "usr/share/licenses/suifleet/LICENSE": ((ROOT / "LICENSE").read_bytes(), 0o644),
            "etc/suifleet/release-trust.toml": ((ROOT / "release/trust.toml").read_bytes(), 0o644),
        }
        for name in UNITS:
            payload[f"usr/lib/systemd/system/{name}"] = ((shipped / name).read_bytes(), 0o644)
        for name in ("signer.toml.example", "config.toml.example"):
            payload[f"usr/share/doc/suifleet/examples/{name}"] = ((shipped / name).read_bytes(), 0o644)
        for name in ("KEY_ISOLATION.md", "RPM_PACKAGING.md"):
            payload[f"usr/share/doc/suifleet/{name}"] = ((ROOT / "docs" / name).read_bytes(), 0o644)
        with tarfile.open(top / "SOURCES/payload.tar", "w") as archive:
            for path, (data, mode) in sorted(payload.items()):
                member = tarfile.TarInfo(path)
                member.size, member.mode, member.mtime = len(data), mode, epoch
                archive.addfile(member, io.BytesIO(data))
        subprocess.run(["rpmbuild", "-bb", "--target", "x86_64", "--define", f"_topdir {top}",
                        "--define", f"fleet_version {version}", "--define", f"fleet_release {release}",
                        str(PACKAGING / "suifleet.spec")], check=True,
                       env=dict(os.environ, SOURCE_DATE_EPOCH=str(epoch), TZ="UTC", LC_ALL="C"))
        shutil.copyfile(top / "RPMS/x86_64" / rpm_name, out / rpm_name)
    (out / "sbom.spdx.json").write_bytes(sbom)
    rpm_provenance = {
        "schema": 1, "release_manifest_sha256": digest(release_manifest),
        "upstream": provenance, "rpm": rpm_name,
        "rpm_sha256": digest((out / rpm_name).read_bytes()),
        "spec_sha256": digest((PACKAGING / "suifleet.spec").read_bytes()),
        "sbom_sha256": digest(sbom),
        "payload_sha256": {name: digest(data) for name, (data, _) in sorted(payload.items())},
        "rpmbuild": subprocess.check_output(["rpmbuild", "--version"], text=True).strip(),
    }
    (out / "rpm-provenance.json").write_text(json.dumps(rpm_provenance, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--release", type=int, default=1)
    args = parser.parse_args()
    if args.release < 1:
        parser.error("--release must be positive")
    build(args.bundle, args.out, args.release)
