#!/usr/bin/env python3
"""Fail when client code references Move functions or types the contracts no longer provide.

Compiles `contracts` with `sui move summary` and checks every `module::name`
reference to this package in the dApp, daemon and deployment-manifest sources.
A reference belongs to this package when it follows a package placeholder
(`${PACKAGE_ID}::`, `{}::`, `"...::`) or is an address-free string such as
`"device_cap::DeviceAddressKey"` (manifest type-origin keys, `moveType(...)`).
Address-free strings naming a Sui/Move framework module are skipped; any other
unknown module is drift, never silently ignored.

- modules must exist in the contracts;
- functions must exist and be callable from a transaction (public or entry);
- dApp `tx.moveCall` argument counts must match the parameters, excluding the
  trailing `&TxContext`/`&mut TxContext` the runtime supplies;
- daemon calls built as `module` + `function = "name"` must exist;
- capitalized names must be structs/events in that module.

This catches name/arity drift only. Argument types, event fields and recipe
schemas still need the shared generated interfaces planned in SF-005.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLIENT_SOURCES = [
    *sorted((ROOT / "dapp/src").rglob("*.ts")),
    *sorted((ROOT / "dapp/src").rglob("*.tsx")),
    *sorted((ROOT / "suifleet-edge/src").rglob("*.rs")),
    *sorted((ROOT / "manifests").glob("*.json")),
    *[path for path in [ROOT / "scripts/deployment_manifest.py"] if path.exists()],
]
MODULE_NAME = r"([a-z_][a-z0-9_]*)::([A-Za-z_][A-Za-z0-9_]*)"
# `${PACKAGE_ID}::m::n`, `{}::m::n`, `${origin(t)}::m::n`, `ID + "::m::n"`.
PACKAGE_REFERENCE = re.compile(r"(?:\}|[\"'`])::" + MODULE_NAME)
# `"m::N"` or `"m::N<...>"`: a type named without its (per-type origin) address.
BARE_REFERENCE = re.compile(r"[\"'`]" + MODULE_NAME + r"(?=[\"'`<])")
MOVE_CALL = re.compile(r"moveCall\(\{\s*target:\s*`[^`]*::([a-z_0-9]+)::([a-z_0-9]+)`\s*,\s*arguments:\s*\[")
# Daemon calls: `function = "name"` or `function: "name"`, in the module named
# by `"module", function,` (JSON-RPC era) or `const ..._MODULE: &str = "module";`.
RUST_FUNCTION = re.compile(r'\bfunction\s*[:=]\s*"([a-z_0-9]+)"')
RUST_MODULE = re.compile(r'"([a-z_0-9]+)",\s*function\s*,|\bconst\s+\w*MODULE\w*\s*:\s*&str\s*=\s*"([a-z_0-9]+)"')


def summarize(package, summary_dir):
    """Return this package's module summaries and the dependency module names."""
    subprocess.run(["sui", "move", "summary", "--path", str(package), "--output-directory", str(summary_dir)],
                   check=True, capture_output=True, text=True)
    addresses = json.loads((summary_dir / "address_mapping.json").read_text())
    package_name = next(name for name, address in addresses.items() if int(address, 16) == 0)
    modules = {path.stem: json.loads(path.read_text()) for path in (summary_dir / package_name).glob("*.json")}
    framework = {path.stem for name in addresses if name != package_name
                 for path in (summary_dir / name).glob("*.json")}
    return modules, framework


def is_tx_context(param):
    ref = param["type_"].get("Reference") if isinstance(param["type_"], dict) else None
    inner = ref[1] if ref else None
    return bool(inner and inner.get("Datatype", {}).get("name") == "TxContext")


def top_level_items(text, start):
    """Count comma-separated items of the array whose `[` precedes `start`."""
    depth, count, saw_item, i = 1, 0, False, start
    while depth:
        char = text[i]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 1:
            count, saw_item = count + 1, False
            i += 1
            continue
        if depth and not char.isspace():
            saw_item = True
        i += 1
    return count + (1 if saw_item else 0)


def check(modules, framework=frozenset(), sources=None):
    errors, checked = [], 0
    for path in CLIENT_SOURCES if sources is None else sources:
        text = path.read_text()
        rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path

        def line(pos):
            return text.count("\n", 0, pos) + 1

        references = [(match, False) for match in PACKAGE_REFERENCE.finditer(text)]
        references += [(match, True) for match in BARE_REFERENCE.finditer(text)]
        for match, bare in references:
            module, name = match.groups()
            if bare and module not in modules and module in framework:
                continue  # e.g. "package::UpgradeCap" from the Sui framework.
            checked += 1
            if module not in modules:
                errors.append(f"{rel}:{line(match.start())}: {module}::{name}: no module {module} in the contracts")
                continue
            summary = modules[module]
            if name[0].isupper():
                if name not in summary["structs"]:
                    errors.append(f"{rel}:{line(match.start())}: {module}::{name} is not a struct in the contracts")
            else:
                function = summary["functions"].get(name)
                if function is None:
                    errors.append(f"{rel}:{line(match.start())}: {module}::{name} is not a function in the contracts")
                elif function["visibility"] != "Public" and not function["entry"]:
                    errors.append(f"{rel}:{line(match.start())}: {module}::{name} is not callable from a transaction")

        for match in MOVE_CALL.finditer(text):
            module, name = match.groups()
            function = modules.get(module, {}).get("functions", {}).get(name)
            if not function:
                continue  # Missing module/function reported above.
            expected = sum(1 for p in function["parameters"] if not is_tx_context(p))
            actual = top_level_items(text, match.end())
            if actual != expected:
                errors.append(f"{rel}:{line(match.start())}: {module}::{name} takes {expected} arguments; "
                              f"client passes {actual}")

        rust_modules = [old or new for old, new in RUST_MODULE.findall(text)]
        for match in RUST_FUNCTION.finditer(text):
            checked += 1
            name = match.group(1)
            functions = [modules.get(module, {}).get("functions", {}).get(name) for module in rust_modules]
            functions = [function for function in functions if function is not None]
            if not functions:
                errors.append(f"{rel}:{line(match.start())}: {'/'.join(rust_modules) or '?'}::{name} "
                              "is not a function in the contracts")
            elif not any(function["visibility"] == "Public" or function["entry"] for function in functions):
                errors.append(f"{rel}:{line(match.start())}: {'/'.join(rust_modules)}::{name} "
                              "is not callable from a transaction")
    return errors, checked


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--contracts", type=Path, default=ROOT / "contracts")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="move-summary-") as summary_dir:
        try:
            modules, framework = summarize(args.contracts, Path(summary_dir))
        except subprocess.CalledProcessError as error:
            sys.exit(f"sui move summary failed:\n{error.stdout}\n{error.stderr}")
    errors, checked = check(modules, framework)
    if not checked:
        errors.append("no client references found; the scanner no longer matches the client sources")
    for error in errors:
        print("DRIFT:", error)
    print(f"{checked} client references checked against {len(modules)} compiled modules; {len(errors)} drift errors")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
