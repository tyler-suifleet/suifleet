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


# Mutations affect only a disposable copy of the actual release implementation.
MUTATIONS = {
    "close-authority": ("close", "assert_operator(policy, ctx);", ""),
    "close-state": ("close", "policy.open = false;", ""),
    "limits-authority": ("set_limits", "assert_operator(policy, ctx);", ""),
    "limits-quota": ("set_limits", "policy.remaining = quota;", "policy.remaining = 0;"),
}


def mutate_enrollment(package, mutation):
    function, old, new = MUTATIONS[mutation]
    path = package / "sources/enrollment.move"
    text = path.read_text()
    start = text.index("    public fun " + function + "(")
    end = text.index("\n    }", start)
    body = text[start:end]
    if body.count(old) != 1:
        raise SystemExit("Mutation no longer matches exactly one implementation statement")
    path.write_text(text[:start] + body.replace(old, new) + text[end:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prover-source", type=Path, required=True)
    parser.add_argument("--framework-packages", type=Path, required=True)
    parser.add_argument("--prover-binary", type=Path, required=True)
    parser.add_argument("--functions", nargs="+", help="Exact prover target filter; does not change implementation sources")
    parser.add_argument("--mutation", choices=MUTATIONS, help="Negative control in temporary implementation copy; expect solver failure")
    args = parser.parse_args()
    source = args.prover_source.resolve()
    framework = args.framework_packages.resolve()
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != PIN["prover"]["commit"]:
        raise SystemExit("Prover source revision does not match toolchain.json")
    overlay = ROOT / "contracts/security-specs/compatibility"
    for name, expected in PIN["compatibility"].items():
        if hashlib.sha256((overlay / name).read_bytes()).hexdigest() != expected:
            raise SystemExit("Compatibility overlay checksum mismatch: " + name)
    # Require exactly the reviewed patch, including the compiled Rust native map.
    actual = subprocess.check_output(["git", "-C", str(source), "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--full-index", "HEAD"])
    if actual != (overlay / "release-model.patch").read_bytes():
        raise SystemExit("Prover source must contain exactly release-model.patch")
    untracked = subprocess.check_output(["git", "-C", str(source), "ls-files", "--others", "--exclude-standard"])
    if untracked:
        raise SystemExit("Untracked prover source/model files are forbidden")
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
        # One proof-only native datatype, no bodies/axioms. Check the untouched
        # release checksum before copying; never edit the executable framework.
        shutil.copyfile(overlay / "integer.move", libraries / "move-stdlib/sources/integer.move")
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
        if args.mutation:
            mutate_enrollment(package, args.mutation)
        manifest = package / "Move.toml"
        manifest.write_text(manifest.read_text() + '\n[addresses]\nsui_edge = "0x0"\n')
        env = dict(os.environ, SUI_PROVER_FRAMEWORK_PATH=str(libraries))
        result = subprocess.run([str(args.prover_binary.resolve()), "--path", str(package / "security-specs"),
                                 "--timeout", "30", "--force-timeout",
                                 *[part for function in (args.functions or []) for part in ("--functions", function)]], env=env, cwd=temporary)
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
