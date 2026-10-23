#!/usr/bin/env python3
"""Gate native allocation, arbitrary DOF inputs and ownership preservation.

Every negative executes the real solver; unsupported projection rejections are
separate coverage limits, never successful property proofs.
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
    targets = ["object_roundtrip_spec", "object_add_slots_spec", "object_frame_spec",
               "allocation_model_spec", "allocation_distinct_spec",
               "object_local_ownership_spec", "object_return_ownership_spec"]
    result = subprocess.run(command + ["--include-object-fixtures", "--functions", *targets],
                            env=env, text=True, capture_output=True)
    output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout + result.stderr)
    expected = {f"object_fields::{f}_{p}" for f in targets for p in ("Check", "Assume", "SpecNoAbortCheck")}
    if (result.returncode != 0 or "❌" in output or "Verification successful" not in output
            or not expected <= set(re.findall(r"✅ (\w+::\w+)", output))):
        raise RuntimeError("Native positive obligations failed:\n" + output)
    print(f"PASS: {len(expected)} native allocation/DOF/ownership phase obligations", flush=True)
    cases = [("object-omit-outer", "object_roundtrip_spec", "0x8C"),
             ("object-omit-child", "object_roundtrip_spec", "0x8D"),
             ("object-skip-clear", "object_roundtrip_spec", "0x90"),
             ("object-wrong-owner", "object_roundtrip_spec", "0x8D"),
             ("object-wrong-id", "object_roundtrip_spec", "0x8D"),
             ("allocator-freeze-counter", "allocation_distinct_spec", "0xB0"),
             ("allocator-reuse-result", "allocation_distinct_spec", "0xB0"),
             ("allocator-last-increments", "allocation_model_spec", None),
             ("object-retain-owner", "object_return_ownership_spec", None)]
    for control, target, abort in cases:
        result = subprocess.run(command + ["--include-object-fixtures", "--functions", target,
                                          "--native-model-control", control],
                                env=env, text=True, capture_output=True)
        output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout + result.stderr)
        failed = set(re.findall(r"❌ (\w+::\w+)", output))
        expected_failed = {f"object_fields::{target}_Check"}
        if control == "object-retain-owner":
            expected_failed.add(f"object_fields::{target}_Assume")
            diagnostic = "error: VM local-child ownership separation"
        elif control == "allocator-last-increments":
            diagnostic = "error: code should not abort"
        else:
            diagnostic = "abort happened here with code " + abort
        expected_passed = {f"object_fields::{target}_{p}" for p in ("Check", "Assume", "SpecNoAbortCheck")} - expected_failed
        if (result.returncode != 1 or failed != expected_failed
                or not expected_passed <= set(re.findall(r"✅ (\w+::\w+)", output))
                or diagnostic not in output or "exiting with verification errors" not in output
                or "timed out" in output or "hard timeout" in output or "compilation errors" in output):
            raise RuntimeError(f"Unexpected native negative result ({control}):\n{output}")
        print("PASS: " + control, flush=True)
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
