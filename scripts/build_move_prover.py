#!/usr/bin/env python3
"""Build the pinned release-framework proof toolchain inside the dev image.

No wallet, transaction or production credential is used. The tools directory is
private build/cache state; do not point this at an unrelated existing checkout.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / "contracts/security-specs/toolchain.json").read_text())
OVERLAY = ROOT / "contracts/security-specs/compatibility"


def run(*args, cwd=None, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def checkout(directory, repository, commit, sparse=None):
    if not directory.exists():
        run("git", "init", str(directory))
        run("git", "fetch", "--depth=1", "--filter=blob:none", repository, commit, cwd=directory)
        if sparse:
            run("git", "sparse-checkout", "set", sparse, cwd=directory)
        run("git", "checkout", "--detach", commit, cwd=directory)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=directory, text=True).strip()
    if actual != commit:
        raise RuntimeError("Unexpected tool source revision: " + str(directory))


def archive(tools, name, url, expected, algorithm, target, zipped=False):
    path = tools / name
    if not path.exists():
        urllib.request.urlretrieve(url, path)
    if hashlib.new(algorithm, path.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Tool archive checksum mismatch: " + name)
    if not target.exists():
        target.mkdir()
        if zipped:
            with zipfile.ZipFile(path) as source:
                source.extractall(target)
        else:
            with tarfile.open(path) as source:
                source.extractall(target, filter="data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    args = parser.parse_args()
    tools = args.tools.resolve()
    tools.mkdir(parents=True, exist_ok=True)
    for name, expected in PIN["compatibility"].items():
        if hashlib.sha256((OVERLAY / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Overlay checksum mismatch: " + name)
    source = tools / "sui-prover"
    checkout(source, PIN["prover"]["repository"], PIN["prover"]["commit"])
    patch = OVERLAY / "release-model.patch"
    diff_args = ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--full-index",
                 "HEAD"]
    diff = subprocess.check_output(diff_args, cwd=source)
    if not diff:
        run("git", "apply", str(patch), cwd=source)
    elif diff != patch.read_bytes():
        raise RuntimeError("Unexpected prover modifications; refusing to overwrite")
    if subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=source):
        raise RuntimeError("Untracked prover source/model files")
    if hashlib.sha256((source / "Cargo.lock").read_bytes()).hexdigest() != PIN["prover"]["cargo_lock_sha256"]:
        raise RuntimeError("Unexpected prover Cargo.lock")
    rust = subprocess.check_output(["rustc", "--version"], text=True).split()[1]
    if rust != PIN["rust"]:
        raise RuntimeError("Requires pinned Rust " + PIN["rust"])
    run("cargo", "build", "--locked", "--release", "-p", "sui-prover", cwd=source,
        env=dict(os.environ, CARGO_BUILD_JOBS="2"))
    release = tools / "sui-release"
    checkout(release, PIN["release_framework"]["repository"], PIN["release_framework"]["commit"],
             "crates/sui-framework/packages")
    if not (tools / "framework").exists():
        shutil.copytree(release / "crates/sui-framework/packages", tools / "framework")
    dotnet, z3 = PIN["dotnet_sdk"], PIN["z3"]
    archive(tools, "dotnet.tar.gz", dotnet["url"], dotnet["hash"], "sha512", tools / "dotnet")
    archive(tools, "z3.zip", z3["url"], z3["sha256"], "sha256", tools / "z3", zipped=True)
    (tools / "z3/z3-4.13.4-x64-glibc-2.35/bin/z3").chmod(0o755)
    boogie = tools / "boogie"
    checkout(boogie, PIN["boogie"]["repository"], PIN["boogie"]["commit"])
    if subprocess.check_output(["git", "diff", "HEAD"], cwd=boogie):
        raise RuntimeError("Unexpected tracked Boogie modifications")
    digest = hashlib.sha256()
    locks = OVERLAY / "boogie-locks"
    for path in sorted(locks.rglob("packages.lock.json")):
        digest.update(str(path.relative_to(locks)).encode() + b"\0" + path.read_bytes() + b"\0")
    if digest.hexdigest() != PIN["boogie"]["nuget_locks_sha256"]:
        raise RuntimeError("Boogie NuGet lock checksum mismatch")
    allowed = {str(Path("Source") / p.relative_to(locks)) for p in locks.rglob("packages.lock.json")}
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=boogie, text=True).splitlines()
    if set(untracked) - allowed:
        raise RuntimeError("Untracked Boogie source/configuration files")
    for path in locks.rglob("packages.lock.json"):
        shutil.copyfile(path, boogie / "Source" / path.relative_to(locks))
    env = dict(os.environ, DOTNET_ROOT=str(tools / "dotnet"),
               DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1", NUGET_PACKAGES=str(tools / "nuget"))
    run(str(tools / "dotnet/dotnet"), "restore", "Source/BoogieDriver/BoogieDriver.csproj", "--locked-mode", cwd=boogie, env=env)
    run(str(tools / "dotnet/dotnet"), "build", "Source/BoogieDriver/BoogieDriver.csproj", "-c", "Release", "--no-restore", "-m:2", cwd=boogie, env=env)


if __name__ == "__main__":
    main()
