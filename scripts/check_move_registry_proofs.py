#!/usr/bin/env python3
"""Gate the closed-writer registry invariant and actual enrollment implementation.

The ordinary arbitrary-state application gate remains independent. Initializer
renaming changes no body; no private custody/freshness precondition is added.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "device_registry": ["init_security_spec", "register_device_security_spec",
                        "deregister_device_security_spec", "deregister_activated_security_spec",
                        "activate_device_security_spec", "return_device_security_spec",
                        "set_identity_security_spec", "revoke_device_security_spec",
                        "reinstate_device_security_spec", "rotate_device_key_security_spec",
                        "borrow_device_security_spec"],
    "enrollment": ["enroll_security_spec"],
    "roles": ["role_add_frame_spec", "revoke_security_spec"],
}
CASES = [
    ("init-untyped-identity", "device_registry", "init_security_spec", "DeviceRegistry_inv"),
    ("activation-identity", "device_registry", "activate_device_security_spec", "spec_typed_identity"),
    ("return-omit-custody", "device_registry", "return_device_security_spec", "is_custodied"),
    ("enrollment-claim", "enrollment", "enroll_security_spec", "ensures(!policy.claims.contains(claim));"),
    ("enrollment-quota", "enrollment", "enroll_security_spec", "ensures(policy.remaining == remaining - 1);"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    tools = parser.parse_args().tools.resolve()
    env = dict(os.environ, DOTNET_ROOT=str(tools / "dotnet"),
               DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1",
               BOOGIE_EXE=str(tools / "boogie/Source/BoogieDriver/bin/Release/net8.0/BoogieDriver"),
               Z3_EXE=str(tools / "z3/z3-4.13.4-x64-glibc-2.35/bin/z3"))
    command = [sys.executable, str(ROOT / "scripts/evaluate_move_prover.py"),
               "--prover-source", str(tools / "sui-prover"),
               "--framework-packages", str(tools / "framework"),
               "--prover-binary", str(tools / "sui-prover/target/release/sui-prover"),
               "--include-registry-induction", "--include-pending-custody",
               "--include-pending-enrollment", "--timeout", "120"]
    phases = ("Check", "Assume", "SpecNoAbortCheck")
    for case in [None, *CASES]:
        extra = (["--mutation", case[0], "--functions", case[2]] if case else
                 ["--functions", *[f for fs in TARGETS.values() for f in fs]])
        result = subprocess.run(command + extra, env=env, text=True, capture_output=True)
        output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout + result.stderr)
        failed = re.findall(r"❌ (\w+::\w+)", output)
        passed = set(re.findall(r"✅ (\w+::\w+)", output))
        if case:
            mutation, module, function, diagnostic = case
            valid = (result.returncode == 1 and failed == [f"{module}::{function}_Check"]
                     and {f"{module}::{function}_{p}" for p in phases[1:]} <= passed
                     and "error: prover::ensures does not hold" in output and diagnostic in output
                     and "exiting with verification errors" in output)
        else:
            expected = {f"{m}::{f}_{p}" for m, fs in TARGETS.items() for f in fs for p in phases}
            valid = result.returncode == 0 and not failed and expected <= passed and "Verification successful" in output
        if not valid:
            raise RuntimeError(f"Unexpected registry proof result ({case}):\n{output}")
        print("PASS: " + (case[0] if case else "42 closed-writer/enrollment phase obligations"), flush=True)


if __name__ == "__main__":
    main()
