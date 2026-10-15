#!/usr/bin/env python3
"""Reproduce SF-050's release-framework prover evaluation, failing closed.

Tool installation is documented in docs/MOVE_SECURITY.md. This is not part of
the successful compiler gate: its current failure is an unmet acceptance item.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / "contracts/security-specs/toolchain.json").read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prover-source", type=Path, required=True)
    parser.add_argument("--framework-packages", type=Path, required=True)
    parser.add_argument("--prover-binary", type=Path, required=True)
    args = parser.parse_args()
    source = args.prover_source.resolve()
    framework = args.framework_packages.resolve()
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != PIN["prover"]["commit"]:
        raise SystemExit("Prover source revision does not match toolchain.json")
    if subprocess.check_output(["git", "-C", str(source), "diff", "HEAD", "--", "crates", "packages", "Cargo.toml", "Cargo.lock"]):
        raise SystemExit("Prover sources/models must be unmodified")
    if hashlib.sha256((source / "Cargo.lock").read_bytes()).hexdigest() != PIN["prover"]["cargo_lock_sha256"]:
        raise SystemExit("Prover lockfile checksum mismatch")
    digest = hashlib.sha256()
    for name in ("move-stdlib", "sui-framework"):
        for path in sorted([framework / name / "Move.toml", *(framework / name / "sources").rglob("*.move")]):
            digest.update(str(path.relative_to(framework)).encode() + b"\0" + path.read_bytes() + b"\0")
    if digest.hexdigest() != PIN["release_framework"]["source_sha256"]:
        raise SystemExit("Release framework source checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="fleet-prover-") as directory:
        temporary = Path(directory)
        libraries = temporary / "framework"
        for name in ("move-stdlib", "sui-framework"):
            shutil.copytree(framework / name, libraries / name)
        for name in ("prover", "sui-specs", "sui-prover"):
            shutil.copytree(source / "packages" / name, libraries / name)
            manifest = libraries / name / "Move.toml"
            lines = manifest.read_text().splitlines()
            for index, line in enumerate(lines):
                for dependency, local in (("Sui", "sui-framework"), ("MoveStdlib", "move-stdlib")):
                    if line.startswith(dependency + " = { git"):
                        lines[index] = f'{dependency} = {{ local = "../{local}", override = true }}'
            manifest.write_text("\n".join(lines) + "\n")
        package = temporary / "contracts"
        # The prover uses the legacy package resolver, not the release CLI's
        # version-4 environment lock. Dependencies above are explicit and pinned.
        shutil.copytree(ROOT / "contracts", package,
                        ignore=shutil.ignore_patterns("build", "Move.lock", "Published.toml"))
        manifest = package / "Move.toml"
        manifest.write_text(manifest.read_text() + '\n[addresses]\nsui_edge = "0x0"\n')
        env = dict(os.environ, SUI_PROVER_FRAMEWORK_PATH=str(libraries))
        result = subprocess.run([str(args.prover_binary.resolve()), "--path", str(package / "security-specs"),
                                 "--timeout", "30", "--force-timeout"], env=env, cwd=temporary)
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
