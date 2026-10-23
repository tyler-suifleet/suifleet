#!/usr/bin/env python3
"""Gate the verified enrollment subset with real solver-positive/negative runs.

This does not verify the complete identity/custody/receipt security boundary.
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
    cases = [(None, None, None, None),
             ("close-authority", "close_spec", "Assume", "asserts(ctx.sender() == enrollment::operator(policy));"),
             ("close-state", "close_spec", "Check", "ensures(!enrollment::is_open(policy));"),
             ("limits-authority", "set_limits_spec", "Assume", "asserts(ctx.sender() == enrollment::operator(policy));"),
             ("limits-quota", "set_limits_spec", "Check", "ensures(enrollment::remaining(policy) == quota);")]
    for mutation, function, phase, obligation in cases:
        extra = (["--mutation", mutation, "--functions", function] if mutation else
                 ["--functions", "close_spec", "set_limits_spec"])
        result = subprocess.run(command + extra, env=env, text=True, capture_output=True)
        output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout + result.stderr)
        failed = re.findall(r"❌ (enrollment::\w+)", output)
        if mutation:
            valid = (result.returncode == 1 and failed == [f"enrollment::{function}_{phase}"]
                     and "error: prover::ensures does not hold" in output
                     and obligation in output and "exiting with verification errors" in output)
        else:
            expected = {f"enrollment::{f}_{p}" for f in ("close_spec", "set_limits_spec")
                        for p in ("Check", "Assume", "SpecNoAbortCheck")}
            passed = set(re.findall(r"✅ (enrollment::\w+)", output))
            valid = (result.returncode == 0 and not failed and expected <= passed
                     and "Verification successful" in output)
        if not valid:
            raise RuntimeError(f"Unexpected proof result ({mutation or 'positive'}):\n{output}")
        print(f"PASS: {mutation or 'unmodified close/set_limits, all six obligations'}", flush=True)


if __name__ == "__main__":
    main()
