#!/usr/bin/env python3
"""32-bit Linux x86 (i686) build, packaging and software-runner acceptance (SF-076).

    test_linux_x86.py --release-image IMAGE --runner-image IMAGE [--bundle DIR] [--evidence FILE]

On the host (needs Docker; the network is used only by the cross build to fetch
locked crates):
  1. cross-builds the debug daemon and its test examples for
     i686-unknown-linux-gnu in the pinned release builder (docker/release.Dockerfile);
  2. checks the result is a 32-bit Intel 80386 ELF with the i386 loader, and
     records the highest glibc symbol version it needs;
  3. runs the existing real-daemon simulator suites with that 32-bit daemon in
     the i386 Debian runner (docker/linux-x86.Dockerfile), with --network none:
     identity, OTA, catalog platform selection, components, dependencies,
     supervised lifecycle, configuration, daemon self-update and (as root, for
     distinct peer UIDs) component IPC;
  4. with --bundle, checks a 32-bit release bundle built by
     `scripts/release.py build --target i686-unknown-linux-gnu`: its packaged
     binary is 32-bit, runs identity commands in the runner, and verifies the
     bundle (signed here with a throwaway key) only for its own target.

The runner is a 32-bit userland on the host kernel's IA-32 compatibility layer.
It is not a 32-bit kernel, physical i686 hardware or a production OS image, and
an x86_64 run of the same suites says nothing about 32-bit behaviour.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TARGET = "i686-unknown-linux-gnu"
# (suite, extra arguments, needs root)
SUITES = [
    ("test_ota.py", [], False),
    ("test_catalog_ota.py", [], False),
    ("test_component_ota.py", [], False),
    ("test_dependency_ota.py", [], False),
    ("test_supervisor.py", [], False),
    ("test_configuration.py", [], False),
    ("test_daemon_update.py", [], False),
    ("test_ipc.py", [], True),
]
EM_386, EM_X86_64 = 3, 62


def elf(path):
    """(class bits, machine, interpreter) of an ELF file."""
    data = Path(path).read_bytes()
    assert data[:4] == b"\x7fELF", f"{path} is not ELF"
    bits = {1: 32, 2: 64}[data[4]]
    assert data[5] == 1, "expected little-endian"
    machine = struct.unpack_from("<H", data, 18)[0]
    if bits == 32:
        phoff, = struct.unpack_from("<I", data, 28)
        phentsize, phnum = struct.unpack_from("<HH", data, 42)
        headers = [struct.unpack_from("<IIIIII", data, phoff + i * phentsize) for i in range(phnum)]
        interp = [(offset, size) for kind, offset, _, _, size, _ in headers if kind == 3]
    else:
        phoff, = struct.unpack_from("<Q", data, 32)
        phentsize, phnum = struct.unpack_from("<HH", data, 54)
        headers = [struct.unpack_from("<IIQQQQ", data, phoff + i * phentsize) for i in range(phnum)]
        interp = [(offset, size) for kind, _, offset, _, _, size in headers if kind == 3]
    interpreter = data[interp[0][0]:interp[0][0] + interp[0][1]].rstrip(b"\0").decode() if interp else None
    return bits, machine, interpreter


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(command, **kwargs):
    print("+", " ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


# ── Inside the i386 runner ─────────────────────────────────────────────────────

def inside_environment():
    import platform as host
    machine = host.machine()
    facts = {"dpkg_architecture": subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip(),
             "python_pointer_bits": struct.calcsize("P") * 8, "uname_machine": machine,
             "x86_64_loader_present": Path("/lib64/ld-linux-x86-64.so.2").exists(),
             "glibc": host.libc_ver()[1]}
    assert facts["dpkg_architecture"] == "i386" and facts["python_pointer_bits"] == 32, facts
    assert machine == "i686" and not facts["x86_64_loader_present"], facts
    print(json.dumps(facts))


def inside_identity(binary):
    """Fresh, preserved, derived and corrupt identities with the 32-bit daemon."""
    def call(*arguments, check=True):
        return subprocess.run([binary, *map(str, arguments)], capture_output=True, text=True, timeout=60, check=check)
    with tempfile.TemporaryDirectory(prefix="sf076-identity-") as directory:
        root = Path(directory)
        keys = [root / "first.key", root / "second.key"]
        addresses = [call("--provision", key).stdout.strip() for key in keys]
        assert all(re.fullmatch("0x[0-9a-f]{64}", a) for a in addresses) and addresses[0] != addresses[1]
        assert oct(keys[0].stat().st_mode & 0o777) == "0o600"
        original = keys[0].read_bytes()
        assert call("--provision", keys[0]).stdout.strip() == addresses[0] and keys[0].read_bytes() == original
        assert call("--derive-address", keys[0]).stdout.strip() == addresses[0]
        # Fixed seed: the independent address vector shared with test_ota.py.
        sys.path.insert(0, str(ROOT / "scripts"))
        from test_ota import ADDRESS
        fixed = root / "fixed.key"
        fixed.write_text("07" * 32)
        fixed.chmod(0o600)
        derived = call("--derive-address", fixed).stdout.strip()
        assert derived == ADDRESS, derived
        corrupt = root / "corrupt.key"
        corrupt.write_text("invalid")
        assert call("--provision", corrupt, check=False).returncode != 0 and corrupt.read_text() == "invalid"
    print(json.dumps({"fresh_unique": True, "existing_preserved": True, "corrupt_refused": True,
                      "independent_address_vector": True}))


def inside_bundle(bundle, daemon, signer):
    """A 32-bit release bundle: packaged binary runs; verification is target-bound."""
    with tempfile.TemporaryDirectory(prefix="sf076-bundle-") as directory:
        root = Path(directory)
        copy = root / "bundle"
        shutil.copytree(bundle, copy)
        manifest = json.loads((copy / "release.json").read_text())
        assert manifest["target"] == TARGET, manifest["target"]
        tarball = next(copy / f["path"] for f in manifest["files"] if f["path"].endswith(".tar.gz"))
        packaged = root / "suifleet"
        with tarfile.open(tarball) as archive:
            member = archive.getmember("suifleet/bin/suifleet")
            assert member.isfile() and member.mode == 0o755, member
            packaged.write_bytes(archive.extractfile(member).read())
        packaged.chmod(0o755)
        packaged_sha256 = sha256(packaged)
        bits, machine, interpreter = elf(packaged)
        assert (bits, machine, interpreter) == (32, EM_386, "/lib/ld-linux.so.2"), (bits, machine, interpreter)
        key = root / "throwaway.key"
        public = subprocess.check_output([signer, "generate", key], text=True).strip()
        subprocess.run([signer, "sign", key, "sf076-throwaway", copy], check=True, capture_output=True)
        trust = root / "trust.toml"
        trust.write_text(f'[[keys]]\nid = "sf076-throwaway"\npublic_key = "{public}"\n')
        results = {}
        for name, binary in (("debug", daemon), ("packaged", packaged)):
            verify = lambda *target: subprocess.run([binary, "--verify-release", copy, trust, *target],
                                                    capture_output=True, text=True, timeout=60)
            assert verify().returncode == 0
            assert verify(TARGET).returncode == 0
            wrong = verify("x86_64-unknown-linux-gnu")
            assert wrong.returncode != 0 and "not the required target" in wrong.stderr, wrong.stderr
            results[name] = "verified; wrong target refused"
        address = subprocess.check_output([packaged, "--provision", root / "device.key"], text=True).strip()
        assert re.fullmatch("0x[0-9a-f]{64}", address)
        # Tampering with the signed manifest's target is a signature failure.
        (copy / "release.json").write_text((copy / "release.json").read_text().replace(TARGET, "x86_64-unknown-linux-gnu"))
        assert subprocess.run([daemon, "--verify-release", copy, trust], capture_output=True).returncode != 0
    print(json.dumps({"bundle_target": TARGET, "packaged_binary_sha256": packaged_sha256,
                      "packaged_elf": [bits, machine, interpreter], "verification": results,
                      "packaged_identity": "provisioned", "retargeted_manifest_refused": True}))


# ── Host side ─────────────────────────────────────────────────────────────────

def build(release_image, target_dir):
    uid, gid = os.getuid(), os.getgid()
    run(["docker", "run", "--rm", "--network", "host", "--user", f"{uid}:{gid}",
         "-v", f"{ROOT}:/src:ro", "-v", f"{target_dir}:/target",
         "-v", "sui_cargo-registry:/home/builder/.cargo/registry", "-v", "sui_cargo-git:/home/builder/.cargo/git",
         release_image, "cargo", "build", "--locked", "--target", TARGET, "--target-dir", "/target",
         "--manifest-path", "/src/suifleet-edge/Cargo.toml", "--bins", "--examples"])
    tools = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{target_dir}:/target:ro", release_image,
                            "sh", "-c", f"rustc -vV; i686-linux-gnu-gcc --version | head -1; "
                                        f"i686-linux-gnu-objdump -p /target/{TARGET}/debug/suifleet"],
                           check=True, capture_output=True, text=True).stdout
    glibc = sorted({tuple(map(int, v.split("."))) for v in re.findall(r"GLIBC_(\d+\.\d+(?:\.\d+)?)", tools)})
    return {"rustc": [l for l in tools.splitlines() if l.startswith(("rustc ", "host:", "release:"))],
            "linker": next(l for l in tools.splitlines() if "i686-linux-gnu-gcc" in l),
            "glibc_symbol_floor": ".".join(map(str, glibc[-1]))}


def runner(runner_image, target_dir, command, root=False, extra=()):
    user = "0:0" if root else f"{os.getuid()}:{os.getgid()}"
    return ["docker", "run", "--rm", "--network", "none", "--user", user, "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "HOME=/tmp", "-v", f"{ROOT}:/workspace:ro",
            "-v", f"{target_dir}/{TARGET}:/workspace/suifleet-edge/target:ro", *extra,
            runner_image, "setarch", "i686", *command]


def inside(command):
    """Run an in-runner step; show its output if it fails, then return its JSON result."""
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode:
        sys.stdout.write(result.stdout[-4000:])
        sys.stderr.write(result.stderr[-4000:])
        raise SystemExit(f"FAIL: {command[-1]} step on the 32-bit runner (exit {result.returncode})")
    return json.loads(result.stdout.strip().splitlines()[-1])


def image_id(image):
    return subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release-image", default="suifleet-release:local")
    parser.add_argument("--runner-image", default="suifleet-linux-x86:local")
    parser.add_argument("--bundle", type=Path, help="32-bit release bundle from scripts/release.py")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--target-dir", type=Path, help="reuse this cross-build target directory")
    parser.add_argument("--inside", choices=["environment", "identity", "bundle"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        binary = str(ROOT / "suifleet-edge/target/debug/suifleet")
        if args.inside == "environment":
            return inside_environment()
        if args.inside == "identity":
            return inside_identity(binary)
        return inside_bundle(Path("/bundle"), binary, str(ROOT / "suifleet-edge/target/debug/examples/sign_release"))

    # The runner mounts its 32-bit build over this (ignored) directory.
    (ROOT / "suifleet-edge/target").mkdir(exist_ok=True)
    evidence = {"ticket": "SF-076", "target": TARGET,
                "commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip(),
                "worktree_clean": not subprocess.check_output(["git", "-C", ROOT, "status", "--porcelain"], text=True).strip(),
                "host": {"kernel": platform.release(), "machine": platform.machine()},
                "release_image": image_id(args.release_image), "runner_image": image_id(args.runner_image)}
    temporary = None
    if args.target_dir:
        target_dir = args.target_dir.resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.mkdtemp(prefix="sf076-target-")
        target_dir = Path(temporary)
    try:
        evidence["toolchain"] = build(args.release_image, target_dir)
        binaries = {}
        for name in ("suifleet", "examples/grpc_sim_shim", "examples/sign_release"):
            path = target_dir / TARGET / "debug" / name
            bits, machine, interpreter = elf(path)
            assert (bits, machine, interpreter) == (32, EM_386, "/lib/ld-linux.so.2"), (name, bits, machine, interpreter)
            binaries[name] = {"sha256": sha256(path), "elf_class": bits, "machine": "EM_386", "interpreter": interpreter}
        evidence["binaries"] = binaries
        print(f"PASS: {TARGET} daemon and test examples are 32-bit i386 ELF (glibc symbols up to "
              f"{evidence['toolchain']['glibc_symbol_floor']})")

        script = ["python3", "scripts/test_linux_x86.py", "--inside"]
        for step in ("environment", "identity"):
            evidence[step] = inside(runner(args.runner_image, target_dir, [*script, step]))
            print(f"PASS: runner {step}: {evidence[step]}")

        evidence["suites"] = []
        for suite, extra, root in SUITES:
            started = time.monotonic()
            result = subprocess.run(runner(args.runner_image, target_dir, ["python3", f"scripts/{suite}", *extra], root=root),
                                    capture_output=True, text=True, timeout=1800)
            passes = [l for l in result.stdout.splitlines() if l.startswith("PASS")]
            evidence["suites"].append({"suite": suite, "root": root, "exit": result.returncode,
                                       "seconds": round(time.monotonic() - started), "pass_lines": passes})
            if result.returncode:
                sys.stdout.write(result.stdout[-4000:])
                sys.stderr.write(result.stderr[-4000:])
                raise SystemExit(f"FAIL: {suite} on the 32-bit runner (exit {result.returncode})")
            print(f"PASS: {suite} on the 32-bit runner ({len(passes)} checks)")

        if args.bundle:
            bundle = args.bundle.resolve()
            evidence["bundle"] = inside(runner(args.runner_image, target_dir, [*script, "bundle"],
                                               extra=("-v", f"{bundle}:/bundle:ro")))
            evidence["bundle"]["release_json_sha256"] = sha256(bundle / "release.json")
            print(f"PASS: 32-bit release bundle: {evidence['bundle']}")
        evidence["limitations"] = [
            "32-bit userland on the host kernel's IA-32 compatibility layer; no 32-bit kernel, physical i686 machine or production OS image",
            "systemd/cgroup-v2 service-manager evidence for the shipped units was not run on 32-bit",
            "public Sui network, real wallets and Walrus are outside this run (local simulators only)",
        ]
        evidence["result"] = "passed"
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)
        if args.evidence:
            args.evidence.parent.mkdir(parents=True, exist_ok=True)
            args.evidence.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"PASS: SF-076 32-bit Linux x86 build and software-runner suites ({len(evidence['suites'])} suites)")


if __name__ == "__main__":
    main()
