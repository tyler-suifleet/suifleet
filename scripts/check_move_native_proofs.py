#!/usr/bin/env python3
"""Gate raw-field model conformance and distinguish unsupported heap operations."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ["raw_add_spec", "raw_remove_spec", "raw_roundtrip_spec",
           "raw_foreign_parent_spec", "raw_unknown_spec", "raw_name_only_spec", "raw_borrow_spec", "raw_remove_opt_spec"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    args = parser.parse_args()
    tools = args.tools.resolve()
    env = dict(os.environ, DOTNET_ROOT=str(tools / "dotnet"),
               DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1",
               BOOGIE_EXE=str(tools / "boogie/Source/BoogieDriver/bin/Release/net8.0/BoogieDriver"),
               Z3_EXE=str(tools / "z3/z3-4.13.4-x64-glibc-2.35/bin/z3"))
    command = [sys.executable, str(ROOT / "scripts/evaluate_move_prover.py"),
               "--prover-source", str(tools / "sui-prover"),
               "--framework-packages", str(tools / "framework"),
               "--prover-binary", str(tools / "sui-prover/target/release/sui-prover")]
    cases = [
        ("positive", ["--include-raw-fixtures", "--functions", *TARGETS], None, None),
        ("missing-tag-clear", ["--include-raw-fixtures", "--native-model-control", "missing-tag-clear",
                               "--functions", "raw_remove_spec"], "raw_remove_spec_Check", "ensures(!df::exists(parent, key));"),
        ("wrong-type-alias", ["--include-raw-fixtures", "--native-model-control", "wrong-type-alias",
                              "--functions", "raw_add_spec"], "raw_add_spec_Check", "ensures(!df::exists_with_type<u64, bool>(parent, key));"),
        ("unknown-occupied-is-possible", ["--include-raw-fixtures", "--unknown-occupancy-control",
                                          "--functions", "raw_unknown_spec"], "raw_unknown_spec_Check", "ensures(!result);"),
    ]
    for name, extra, failure, statement in cases:
        result = subprocess.run(command + extra, env=env, text=True, capture_output=True)
        output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout + result.stderr)
        failed = re.findall(r"❌ (\w+::\w+)", output)
        passed = set(re.findall(r"✅ (\w+::\w+)", output))
        if failure:
            function = failure.removesuffix("_Check")
            expected_other = {f"raw_field_specs::{function}_{p}" for p in ("Assume", "SpecNoAbortCheck")}
            valid = (result.returncode == 1 and failed == ["raw_field_specs::" + failure]
                     and expected_other <= passed and statement in output
                     and "error: prover::ensures does not hold" in output
                     and "exiting with verification errors" in output)
        else:
            expected = {f"raw_field_specs::{f}_{p}" for f in TARGETS
                        for p in ("Check", "Assume", "SpecNoAbortCheck")}
            valid = (result.returncode == 0 and not failed and expected <= passed
                     and "Verification successful" in output)
        if not valid:
            raise RuntimeError(f"Unexpected raw native proof result ({name}):\n{output}")
        print("PASS: " + name, flush=True)
    # This is a compatibility rejection, NOT a successful property proof or a
    # solver-negative control. The real release VM accepts the rewrap fixture.
    result = subprocess.run(command + ["--include-carrier-fixtures", "--functions", "raw_rewrap_spec"],
                            env=env, text=True, capture_output=True)
    output = result.stdout + result.stderr
    if (result.returncode != 1 or "packing an existing or unproven-fresh UID would discard its child store" not in output
            or "exiting with condition generation errors" not in output or "Verification successful" in output):
        raise RuntimeError("Unsupported carrier reconstruction must fail closed:\n" + output)
    print("PASS: existing-UID reconstruction rejected as unsupported (not proved)", flush=True)


if __name__ == "__main__":
    main()
