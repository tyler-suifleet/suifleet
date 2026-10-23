#!/usr/bin/env python3
"""Gate the supported fresh two-slot DOF fixture; arbitrary heaps remain pending.

This never treats an unsupported projection rejection as a successful proof.
The separate arbitrary-input add/custody obligations must pass before SF050 Review.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


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
    phases = {f"object_fields::object_roundtrip_spec_{p}" for p in ("Check", "Assume", "SpecNoAbortCheck")}
    cases = [(None, None), ("object-omit-outer", "0x8C"),
             ("object-omit-child", "0x8D"), ("object-skip-clear", "0x90"),
             ("object-wrong-owner", "0x8D"), ("object-wrong-id", "0x8D")]
    for control, abort in cases:
        extra = ["--include-object-fixtures", "--functions", "object_roundtrip_spec"]
        if control:
            extra += ["--native-model-control", control]
        result = subprocess.run(command + extra, env=env, text=True, capture_output=True)
        output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout + result.stderr)
        passed = set(re.findall(r"✅ (\w+::\w+)", output))
        failed = re.findall(r"❌ (\w+::\w+)", output)
        if control:
            valid = (result.returncode == 1 and failed == ["object_fields::object_roundtrip_spec_Check"]
                     and phases - set(failed) <= passed
                     and "error: code should not abort" in output
                     and "abort happened here with code " + abort in output
                     and "exiting with verification errors" in output)
        else:
            valid = result.returncode == 0 and not failed and phases <= passed and "Verification successful" in output
        if not valid:
            raise RuntimeError(f"Unexpected DOF proof result ({control}):\n{output}")
        print("PASS: " + (control or "fresh DOF roundtrip: 3 phase obligations"), flush=True)
    result = subprocess.run(command + ["--include-overlap-fixtures", "--functions",
                                      "overlap_same_spec", "overlap_different_spec"],
                            env=env, text=True, capture_output=True)
    output = result.stdout + result.stderr
    if (result.returncode != 1 or "native storage projection: raw DF and DOF with the same Name type require separate physical slots" not in output
            or "exiting with bytecode transformation errors" not in output
            or "Verification successful" in output or "panicked" in output or "🔄" in output):
        raise RuntimeError("Overlapping namespaces must fail before code generation:\n" + output)
    print("PASS: same-Name overlap explicitly unsupported (not proved)", flush=True)


if __name__ == "__main__":
    main()
