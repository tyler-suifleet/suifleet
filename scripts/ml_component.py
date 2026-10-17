#!/usr/bin/env python3
"""Build deterministic, self-contained native ML components; no remote publication.

download is the only network operation. build and recipe are offline.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "interfaces/compat/ml"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def document(name):
    return json.loads((ASSETS / name).read_text())


def selection(platform):
    lock = document("runtime-lock.json")
    if lock["format_version"] != 1 or platform not in lock["platforms"]:
        raise ValueError("unsupported platform")
    return lock, lock["platforms"][platform]


def check_wheel(wheel, raw):
    if sha(raw) != wheel["sha256"]:
        raise ValueError("wheel digest mismatch: " + wheel["filename"])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name, expected in wheel["notices"].items():
            if sha(archive.read(name)) != expected:
                raise ValueError("wheel license mismatch")


def download(platform, destination):
    _, entry = selection(platform)
    destination.mkdir(parents=True, exist_ok=True)
    for wheel in entry["wheels"]:
        path = destination / wheel["filename"]
        if path.exists():
            check_wheel(wheel, path.read_bytes())
            continue
        with urllib.request.urlopen(wheel["url"], timeout=60) as response:
            raw = response.read(64 * 1024 * 1024 + 1)
        check_wheel(wheel, raw)
        path.write_bytes(raw)


def build(task, platform, wheels):
    lock, entry = selection(platform)
    if task not in ("classifier", "detector"):
        raise ValueError("unsupported model")
    models = document("models.json")
    files = {}
    for name, expected in models["files"].items():
        if name.startswith("models/") and name != f"models/{task}-1.0.0.tflite":
            continue
        raw = (ASSETS / name).read_bytes()
        if sha(raw) != expected:
            raise ValueError("asset digest mismatch: " + name)
        files[name] = raw
    files["models.json"] = (ASSETS / "models.json").read_bytes()
    files["runtime-lock.json"] = (ASSETS / "runtime-lock.json").read_bytes()
    files["__main__.py"] = (ROOT / "scripts/ml_runner.py").read_bytes()
    names = []
    for wheel in entry["wheels"]:
        raw = (wheels / wheel["filename"]).read_bytes()
        check_wheel(wheel, raw)
        name = "wheels/" + wheel["filename"]
        files[name] = raw
        names.append(name)
    manifest = {"format_version": 1, "component_version": "1.0.0", "model_version": "1.0.0",
                "runtime_version": lock["runtime_version"], "numpy_version": lock["numpy_version"],
                "task": task, "platform": platform, "glibc_min": entry["glibc_min"], "wheels": names,
                "files": {name: sha(raw) for name, raw in sorted(files.items())}}
    files["manifest.json"] = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    output = io.BytesIO()
    # Stored entries avoid compressor/version differences; fixed mode/time/order.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    return output.getvalue()


def recipe(artifact, blob):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", blob):
        raise ValueError("invalid Walrus blob ID")
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    task = manifest["task"]
    if task not in ("classifier", "detector"):
        raise ValueError("unsupported model")
    command = "python3 -I -B {artifact}"
    # Workspace-local paths need no cross-component filesystem permissions.
    # Install self-tests both the runtime and its selected model before success.
    return {"schemaVersion": "1.0", "componentName": "suifleet-ml-" + task,
            "componentVersion": "1.0.0", "artifactBlobId": blob, "artifactSha256": sha(artifact),
            "description": "Native TF Lite fixture; " + manifest["platform"],
            "lifecycle": {"install": command + " install --state {artifacts}/ml-runtime && " +
                          command + " infer --state {artifacts}/ml-runtime --fixture marker"},
            "configuration": {}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("download", "build"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--platform", required=True)
        cmd.add_argument("--wheels", required=True, type=Path)
        if name == "build":
            cmd.add_argument("--task", required=True, choices=("classifier", "detector"))
            cmd.add_argument("--out", required=True, type=Path)
    cmd = commands.add_parser("recipe")
    cmd.add_argument("--artifact", required=True, type=Path)
    cmd.add_argument("--blob-id", required=True)
    cmd.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "download":
            download(args.platform, args.wheels)
            return
        raw = (build(args.task, args.platform, args.wheels) if args.command == "build" else
               (json.dumps(recipe(args.artifact.read_bytes(), args.blob_id), indent=2) + "\n").encode())
        args.out.write_bytes(raw)
        print(json.dumps({"path": str(args.out), "sha256": sha(raw), "bytes": len(raw)}))
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, f"ml-component: {error}\n")


if __name__ == "__main__":
    main()
