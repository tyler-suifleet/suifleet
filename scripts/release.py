#!/usr/bin/env python3
"""Reproducible SuiFleet daemon release bundles (SF-039).

    release.py build --out dist [--ref HEAD] [--image suifleet-release:local] [--reproduce]

On the host: exports exactly the committed tree of REF with `git archive` (so
uncommitted files can never leak into a release), then builds inside the
pinned release image (docker/release.Dockerfile). The bundle contains:

  suifleet-<version>-<commit12>-<target>.tar.gz   the daemon (normalized tar/gzip)
  sbom.spdx.json                                   SPDX 2.3, locked target dependency graph
  provenance.json                                  commit, toolchain, lockfile, builder image
  release.json                                     names and SHA-256s of the three files above

Sign release.json with `cargo run --example sign_release` and verify on a
device with `suifleet --verify-release BUNDLE TRUST_FILE` (docs/RELEASES.md).
`--reproduce` builds twice from scratch and fails unless every file is
byte-identical. Nothing here publishes anything or touches signing keys.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

TARGET = "aarch64-unknown-linux-gnu"
NAMESPACE = "https://github.com/tyler-suifleet/suifleet/releases"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(*command, cwd=None, env=None, capture=False):
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE if capture else None, check=True)
    return result.stdout.strip() if capture else None


# ── Host side ────────────────────────────────────────────────────────────────

def build(args):
    root = Path(run("git", "rev-parse", "--show-toplevel", capture=True))
    commit = run("git", "-C", str(root), "rev-parse", "--verify", args.ref + "^{commit}", capture=True)
    epoch = run("git", "-C", str(root), "show", "-s", "--format=%ct", commit, capture=True)
    image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", args.image, capture=True)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        sys.exit(f"output directory {out} is not empty")
    rounds = 2 if args.reproduce else 1
    bundles = []
    with tempfile.TemporaryDirectory(prefix="suifleet-release-") as work:
        work = Path(work)
        for number in range(rounds):
            source = work / f"source-{number}"
            source.mkdir()
            archive = subprocess.Popen(["git", "-C", str(root), "archive", commit], stdout=subprocess.PIPE)
            subprocess.run(["tar", "-x", "-C", str(source)], stdin=archive.stdout, check=True)
            archive.wait()
            if archive.returncode:
                sys.exit("git archive failed")
            result = out if number == 0 else work / f"out-{number}"
            result.mkdir(exist_ok=True)
            # The same in-container path each round, so remapped paths match.
            run("docker", "run", "--rm", "--network", "host",
                "-v", f"{source}:/src:ro", "-v", f"{result}:/out",
                "-v", "sui_cargo-registry:/home/builder/.cargo/registry",
                "-v", "sui_cargo-git:/home/builder/.cargo/git",
                "-e", f"SOURCE_DATE_EPOCH={epoch}", "-e", f"RELEASE_COMMIT={commit}",
                "-e", f"RELEASE_BUILDER_IMAGE={image_id}",
                args.image, "python3", "/src/scripts/release.py", "container-build", "--src", "/src", "--out", "/out")
            bundles.append({p.name: sha256(p) for p in result.iterdir()})
    if args.reproduce:
        first, second = bundles
        if first != second:
            for name in sorted(set(first) | set(second)):
                if first.get(name) != second.get(name):
                    print(f"DIFFERS: {name} {first.get(name)} {second.get(name)}", file=sys.stderr)
            sys.exit("release is not reproducible")
        print(f"REPRODUCIBLE: {len(first)} files byte-identical across two clean builds")
    print(json.dumps({p.name: sha256(p) for p in sorted(out.iterdir())}, indent=2))


# ── Inside the release image ────────────────────────────────────────────────

def container_build(args):
    src, out = Path(args.src), Path(args.out)
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    commit = os.environ["RELEASE_COMMIT"]
    edge = src / "suifleet-edge"
    target_dir = Path(tempfile.mkdtemp(prefix="target-"))
    cargo_home = os.environ.get("CARGO_HOME", str(Path.home() / ".cargo"))
    env = dict(os.environ)
    # Embedded paths (panic locations) must not depend on where it was built.
    env["RUSTFLAGS"] = f"--remap-path-prefix={cargo_home}=/cargo --remap-path-prefix={src}=/build"
    run("cargo", "build", "--locked", "--release", "--target", TARGET,
        "--target-dir", str(target_dir), "--manifest-path", str(edge / "Cargo.toml"), env=env)
    version = package_version(edge / "Cargo.toml")
    binary = target_dir / TARGET / "release" / "suifleet"

    tarball = out / f"suifleet-{version}-{commit[:12]}-{TARGET}.tar.gz"
    write_tarball(tarball, epoch, [
        ("suifleet/bin/suifleet", binary.read_bytes(), 0o755),
        ("suifleet/LICENSE", (src / "LICENSE").read_bytes(), 0o644),
        ("suifleet/config.example.toml", (edge / "config.example.toml").read_bytes(), 0o644),
    ])

    # The exact build graph: normal dependencies for TARGET with the features
    # a release build resolves (cargo metadata would also unify dev features).
    tree = run("cargo", "tree", "--locked", "-e", "normal", "--target", TARGET, "--prefix", "depth",
               "--format", "{p}", "--manifest-path", str(edge / "Cargo.toml"), capture=True, env=env)
    metadata = json.loads(run("cargo", "metadata", "--locked", "--format-version", "1",
                              "--filter-platform", TARGET, "--manifest-path", str(edge / "Cargo.toml"),
                              capture=True, env=env))
    lock = lock_checksums(edge / "Cargo.lock")
    sbom = out / "sbom.spdx.json"
    write_json(sbom, spdx(build_graph(tree), metadata, lock, version, commit, epoch, binary))

    provenance = out / "provenance.json"
    write_json(provenance, {
        "schema": 1,
        "name": "suifleet",
        "version": version,
        "commit": commit,
        "source_date_epoch": epoch,
        "target": TARGET,
        "rustc": run("rustc", "-vV", capture=True).splitlines(),
        "cargo": run("cargo", "-V", capture=True),
        "linker": run("aarch64-linux-gnu-gcc", "--version", capture=True).splitlines()[0],
        "cargo_lock_sha256": sha256(edge / "Cargo.lock"),
        "builder_image": os.environ.get("RELEASE_BUILDER_IMAGE", "unknown"),
        "binary_sha256": sha256(binary),
        "source": "git archive of the commit; no working-tree files",
    })
    write_json(out / "release.json", {
        "schema": 1,
        "name": "suifleet",
        "version": version,
        "commit": commit,
        "target": TARGET,
        "files": [{"path": p.name, "sha256": sha256(p), "size": p.stat().st_size}
                  for p in (tarball, sbom, provenance)],
    })


def package_version(manifest):
    """[package] version from Cargo.toml (Python 3.10 has no tomllib)."""
    section = None
    for line in Path(manifest).read_text().splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[package]" and line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    raise ValueError("no [package] version")


def lock_checksums(lockfile):
    """(name, version) -> registry SHA-256 from Cargo.lock [[package]] entries."""
    checksums = {}
    for block in Path(lockfile).read_text().split("[[package]]")[1:]:
        fields = dict(re.findall(r'^(name|version|checksum) = "([^"]*)"$', block, re.M))
        checksums[(fields["name"], fields["version"])] = fields.get("checksum")
    return checksums


def write_tarball(path, epoch, entries):
    """Sorted entries, fixed mtime, root:root, no gzip name or timestamp."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data, mode in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, epoch
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(data))
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as out:
        out.write(buffer.getvalue())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def spdx_id(name, version):
    return "SPDXRef-Package-" + re.sub(r"[^A-Za-z0-9.-]", "-", f"{name}-{version}")


def license_expression(value):
    if not value:
        return "NOASSERTION"
    # Old crates use "A/B" for a choice of licenses.
    return " OR ".join(part.strip() for part in value.split("/")) if "/" in value else value


def build_graph(tree):
    """(root, packages, edges) from `cargo tree --prefix depth --format {p}`.
    Lines look like `3serde v1.0.228` or `0suifleet v0.1.0 (/build/...)`;
    a repeated subtree is marked `(*)` and its children appear once."""
    root, stack, packages, edges = None, [], set(), set()
    for line in tree.splitlines():
        match = re.match(r"^(\d+)(\S+) v(\S+)", line)
        if not match:
            continue
        depth, key = int(match.group(1)), (match.group(2), match.group(3))
        packages.add(key)
        del stack[depth:]
        if stack:
            edges.add((stack[-1], key))
        else:
            root = key
        stack.append(key)
    return root, packages, edges


def spdx(graph, metadata, lock, version, commit, epoch, binary):
    """SPDX 2.3 for the packages built into the target binary: its normal
    dependency graph for TARGET. Build-script-only dependencies are not
    linked into the binary and are not listed."""
    root, keys, edges = graph
    by_key = {(p["name"], p["version"]): p for p in metadata["packages"]}
    packages = {key: by_key[key] for key in keys}
    checksums = lock
    reachable = keys
    import datetime
    created = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = []
    for package_id in sorted(reachable):
        package = packages[package_id]
        entry = {
            "SPDXID": spdx_id(package["name"], package["version"]),
            "name": package["name"],
            "versionInfo": package["version"],
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": license_expression(package.get("license")),
            "copyrightText": "NOASSERTION",
            "filesAnalyzed": False,
            "supplier": "NOASSERTION",
        }
        if package_id == root:
            entry["downloadLocation"] = f"git+https://github.com/tyler-suifleet/suifleet@{commit}"
            entry["checksums"] = [{"algorithm": "SHA256", "checksumValue": sha256(binary)}]
            entry["primaryPackagePurpose"] = "APPLICATION"
        else:
            checksum = checksums.get((package["name"], package["version"]))
            if package.get("source", "").startswith("registry+"):
                entry["downloadLocation"] = f"https://crates.io/api/v1/crates/{package['name']}/{package['version']}/download"
                entry["externalRefs"] = [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
                                          "referenceLocator": f"pkg:cargo/{package['name']}@{package['version']}"}]
            else:
                entry["downloadLocation"] = "NOASSERTION"
            if checksum:
                entry["checksums"] = [{"algorithm": "SHA256", "checksumValue": checksum}]
        entries.append(entry)
    relationships = [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES",
                      "relatedSpdxElement": spdx_id(packages[root]["name"], packages[root]["version"])}]
    for parent, child in sorted(edges):
        relationships.append({"spdxElementId": spdx_id(packages[parent]["name"], packages[parent]["version"]),
                              "relationshipType": "DEPENDS_ON",
                              "relatedSpdxElement": spdx_id(packages[child]["name"], packages[child]["version"])})
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"suifleet-{version}-{TARGET}",
        "documentNamespace": f"{NAMESPACE}/{commit}/{TARGET}/sbom",
        "creationInfo": {"created": created, "creators": ["Tool: suifleet-release.py"]},
        "packages": entries,
        "relationships": relationships,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--out", required=True)
    builder.add_argument("--ref", default="HEAD")
    builder.add_argument("--image", default="suifleet-release:local")
    builder.add_argument("--reproduce", action="store_true")
    inside = commands.add_parser("container-build")
    inside.add_argument("--src", required=True)
    inside.add_argument("--out", required=True)
    args = parser.parse_args()
    build(args) if args.command == "build" else container_build(args)


if __name__ == "__main__":
    main()
