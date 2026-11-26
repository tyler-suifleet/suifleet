#!/usr/bin/env python3
"""Inspect target package staging and optionally execute the bundled codec offline."""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

NAMES = ("suifleet", "suifleet-node", "suifleet-walrus-codec")
PRIVATE = {
    "device.key", "device.address", "config.toml", "firmware.toml", "signer.toml",
    "enrollment.toml", "deployment.json", "journal.json",
}


def one(paths):
    paths = list(paths)
    if len(paths) != 1:
        raise ValueError(f"Expected exactly one package location, got {paths}")
    return paths[0]


def payload_files(ipk):
    members = subprocess.check_output(["ar", "t", str(ipk)], text=True).splitlines()
    name = one(name for name in members if name.startswith("data.tar."))
    data = subprocess.check_output(["ar", "p", str(ipk), name])
    if name.endswith(".zst"):
        data = subprocess.check_output(["zstd", "-dc"], input=data)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as payload:
        return [Path(member.name) for member in payload]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    parser.add_argument("--runtime", action="store_true")
    args = parser.parse_args()
    tmp = args.build.resolve() / "tmp"
    packages = {}
    for name in NAMES:
        root = one(tmp.glob(f"work/*/{name}/*/packages-split/{name}"))
        ipk = one(tmp.glob(f"deploy/ipk/*/{name}_*.ipk"))
        files = payload_files(ipk)
        if not files:
            raise ValueError(f"Empty package payload in {ipk.name}")
        for file in files:
            if file.name in PRIVATE or file.parts[:3] == ("var", "lib", "suifleet"):
                raise ValueError(f"Runtime identity or state in {name}: {file}")
        packages[name] = root
        print(f"{ipk.name}: package payload contains no device identity or state")

    if args.runtime:
        daemon_work = packages["suifleet"].parents[1]
        sysroot = daemon_work / "recipe-sysroot"
        loader = one(sysroot.glob("usr/lib/ld-linux-aarch64.so.1"))
        node = packages["suifleet-node"] / "usr/lib/suifleet/node/bin/node"
        codec = packages["suifleet-walrus-codec"] / "usr/lib/suifleet/walrus-codec/walrus-device-codec.cjs"
        qemu = shutil.which("qemu-aarch64")
        if not qemu:
            raise ValueError("Install qemu-user to run the package smoke")
        command = [qemu, str(loader), "--library-path", str(loader.parent), str(node)]
        version = subprocess.check_output(command + ["--version"], text=True, timeout=60).strip()
        manifest = json.loads(codec.with_name("manifest.json").read_text())
        if version != "v" + manifest["node"]:
            raise ValueError(f"Unexpected packaged Node: {version}")
        result = subprocess.run(
            command + [str(codec)], text=True, input='{"chain":"00000000"}',
            capture_output=True, timeout=60, env={"PATH": os.defpath},
        )
        if result.returncode != 1 or "Walrus codec operation failed" not in result.stderr:
            raise ValueError("Packaged codec did not reject the foreign chain")
        print(f"{version}: AArch64 user-mode codec rejected a foreign chain")


if __name__ == "__main__":
    main()
