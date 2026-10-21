"""Fetch and unpack the hash-pinned optional `cryptography` runtime (SF-070).

    python3 -m industrial.deps --dest .ci-cache/industrial-runtime

Only this step uses the network. Each wheel is checked against
interfaces/compat/industrial/crypto-lock.json before it is saved or unpacked;
an existing corrupt cache entry fails rather than being replaced. Add
DEST/site to PYTHONPATH to use the unpacked packages.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "interfaces/compat/industrial/crypto-lock.json"


def selection():
    lock = json.loads(LOCK.read_text())
    key = f"linux-{platform.machine()}-cp{sys.version_info.major}{sys.version_info.minor}"
    if sys.platform != "linux" or key not in lock["platforms"]:
        raise SystemExit(f"no pinned cryptography runtime for {key}")
    return lock["platforms"][key]


def fetch(dest):
    wheels, site = dest / "wheels", dest / "site"
    wheels.mkdir(parents=True, exist_ok=True)
    for wheel in selection():
        path = wheels / wheel["filename"]
        if path.exists():
            raw = path.read_bytes()
        else:
            with urllib.request.urlopen(wheel["url"], timeout=60) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
        if hashlib.sha256(raw).hexdigest() != wheel["sha256"]:
            raise SystemExit(f"{wheel['filename']}: SHA-256 mismatch")
        if not path.exists():
            path.write_bytes(raw)
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                name = PurePosixPath(member.filename)
                if name.is_absolute() or ".." in name.parts:
                    raise SystemExit(f"{wheel['filename']}: unsafe member {member.filename}")
            archive.extractall(site)
    return site


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", required=True, type=Path)
    print(fetch(parser.parse_args().dest))
