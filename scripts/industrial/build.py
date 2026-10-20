"""Build the collector as a deterministic Python zip application (SF-070).

    python3 -m industrial.build --out suifleet-industrial-1.0.0.pyz
    python3 -I suifleet-industrial-1.0.0.pyz --config collector.json

The archive holds the collector package and the IPC SDK, in a fixed order with
fixed timestamps and permissions, so identical sources give identical bytes and
the recipe's artifact digest pins exactly this code. No third-party code is
included; Basic256Sha256 uses an operator-supplied `cryptography` package.
"""
import argparse
import hashlib
from pathlib import Path
import zipfile

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parents[2]
MODULES = ("__init__.py", "collector.py", "modbus.py", "opcua_client.py", "opcua_codec.py", "opcua_secure.py")
MAIN = b"import sys\nfrom industrial.collector import main\nsys.exit(main())\n"


def build(out):
    files = {"__main__.py": MAIN, "suifleet_ipc.py": (ROOT / "sdk/python/suifleet_ipc.py").read_bytes()}
    for name in MODULES:
        files[f"industrial/{name}"] = (ROOT / "scripts/industrial" / name).read_bytes()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    return hashlib.sha256(Path(out).read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    print(build(parser.parse_args().out))
