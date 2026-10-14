#!/usr/bin/env python3
"""Build the harmless SuiFleet test component (SF-030) deterministically.

    test_component.py build --version 1.0.0 --run-id RUN [--fail install|health] --out app-v1.sh
    test_component.py recipe --artifact app-v1.sh --blob-id BLOB --out recipe-v1.json

The same inputs always give the same bytes and SHA-256. The run ID is baked
into the artifact and echoed in the device receipt, so an acceptance run can
prove that exactly its artifact executed. A failure mode is part of the
artifact too, so injected failures are authenticated like any other update.
Recipes target the current one-shot apply path: no lifecycle commands and no
configuration, applied by `suifleet-test-updater` configured as apply.command.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "tests/components/suifleet-test-app"
TEMPLATE = COMPONENT_DIR / "app.sh.in"
UPDATER = COMPONENT_DIR / "suifleet-test-updater"
DEFAULT_COMPONENT = "suifleet-test-app"
FAILURES = ("none", "install", "health")
# Mirrors the updater's header check: values become paths and JSON strings.
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
HEADER = re.compile(r"^# (component|version|run_id|fail)=(.*)$", re.M)


def safe(value, what):
    if not SAFE.fullmatch(value):
        raise ValueError(f"{what} must match {SAFE.pattern}: {value!r}")
    return value


def build(version, run_id, fail="none", component=DEFAULT_COMPONENT):
    if fail not in FAILURES:
        raise ValueError(f"fail must be one of {FAILURES}")
    text = TEMPLATE.read_text()
    for name, value in {"COMPONENT": safe(component, "component"), "VERSION": safe(version, "version"),
                        "RUN_ID": safe(run_id, "run ID"), "FAIL": fail}.items():
        text = text.replace(f"@{name}@", value)
    return text.encode()


def header(artifact):
    fields = dict(HEADER.findall(artifact.decode()))
    if set(fields) != {"component", "version", "run_id", "fail"}:
        raise ValueError("not a SuiFleet test component artifact")
    return fields


def recipe(artifact, blob_id):
    fields = header(artifact)
    document = {
        "schemaVersion": "1.0",
        "componentName": fields["component"],
        "componentVersion": fields["version"],
        "description": f"Harmless SuiFleet OTA test component, run {fields['run_id']}"
                       + ("" if fields["fail"] == "none" else f", injected {fields['fail']} failure"),
        "artifactBlobId": blob_id,
        "artifactSha256": hashlib.sha256(artifact).hexdigest(),
        "lifecycle": {},
        "configuration": {},
    }
    return json.dumps(document, separators=(",", ":")).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build", help="write a versioned test artifact")
    builder.add_argument("--version", required=True)
    builder.add_argument("--run-id", required=True)
    builder.add_argument("--fail", choices=FAILURES, default="none")
    builder.add_argument("--component", default=DEFAULT_COMPONENT)
    builder.add_argument("--out", type=Path, required=True)
    recipes = commands.add_parser("recipe", help="write the recipe for an uploaded artifact")
    recipes.add_argument("--artifact", type=Path, required=True)
    recipes.add_argument("--blob-id", required=True)
    recipes.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "build":
            data = build(args.version, args.run_id, args.fail, args.component)
        else:
            data = recipe(args.artifact.read_bytes(), args.blob_id)
    except ValueError as error:
        sys.exit(f"test_component: {error}")
    args.out.write_bytes(data)
    print(json.dumps({"path": str(args.out), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}))


if __name__ == "__main__":
    main()
