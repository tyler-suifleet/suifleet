#!/usr/bin/env python3
"""Gate the explicitly verified subset using real positive and negative solver runs.

Reachable custody construction and full-package security are separate obligations;
see docs/MOVE_SECURITY.md. Model errors/timeouts never count as negative proofs.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
POSITIVES = {
    "enrollment": ["close_spec", "set_limits_spec", "revoke_claim_security_spec"],
    "device_group": ["handover_security_spec"],
    "identity": ["is_active_signer_spec"],
    "device_registry": ["set_identity_security_spec", "revoke_device_security_spec",
                        "reinstate_device_security_spec", "rotate_device_key_security_spec", "borrow_device_security_spec"],
    "device_cap": ["assert_signer_security_spec"],
    "roles": ["is_active_security_spec", "revoke_security_spec"],
    "deployment_manager": ["new_receipts_security_spec", "receipts_mut_security_spec",
                           "assert_recipe_hash_security_spec", "new_deployment_verified_security_spec",
                           "deployment_handover_security_spec", "make_deployment_security_spec", "report_status_security_spec",
                           "report_status_for_group_security_spec"],
}
# Each failure must identify its exact intended specification statement.
NEGATIVES = [
    ("legacy-report", "deployment_manager", "report_status_security_spec", "Assume", "prover::prover::asserts(false);"),
    ("legacy-group-report", "deployment_manager", "report_status_for_group_security_spec", "Assume", "prover::prover::asserts(false);"),
    ("group-authority", "device_group", "handover_security_spec", "Assume", "prover::prover::asserts(ctx.sender() == group.admin);"),
    ("deployment-authority", "deployment_manager", "deployment_handover_security_spec", "Assume", "prover::prover::asserts(ctx.sender() == d.creator);"),
    ("claim-authority", "enrollment", "revoke_claim_security_spec", "Assume", "prover::prover::asserts(ctx.sender() == policy.operator);"),
    ("claim-remove", "enrollment", "revoke_claim_security_spec", "Check", "prover::prover::ensures(!policy.claims.contains(claim));"),
    ("binding-artifact", "deployment_manager", "make_deployment_security_spec", "Check", "prover::prover::ensures(record.artifact_sha256 == artifact);"),
    ("binding-recipe", "deployment_manager", "new_deployment_verified_security_spec", "Check", "prover::prover::ensures(typed);"),
    ("binding-recipe-value", "deployment_manager", "new_deployment_verified_security_spec", "Check", "prover::prover::ensures(*df::borrow<RecipeDigestKey, String>(&record.id, RecipeDigestKey {}) == recipe);"),
    ("recipe-length", "deployment_manager", "assert_recipe_hash_security_spec", "Assume", "prover::prover::asserts(bytes.length() == 64);"),
    ("close-authority", "enrollment", "close_spec", "Assume", "asserts(ctx.sender() == enrollment::operator(policy));"),
    ("close-state", "enrollment", "close_spec", "Check", "ensures(!enrollment::is_open(policy));"),
    ("limits-authority", "enrollment", "set_limits_spec", "Assume", "asserts(ctx.sender() == enrollment::operator(policy));"),
    ("limits-quota", "enrollment", "set_limits_spec", "Check", "ensures(enrollment::remaining(policy) == quota);"),
    ("identity-custody", "identity", "is_active_signer_spec", "Check", "ensures(!result);"),
    ("identity-revoked", "identity", "is_active_signer_spec", "Check", "ensures(result == (!device_registry::identity_revoked(&identity)"),
    ("identity-signer", "identity", "is_active_signer_spec", "Check", "ensures(result == (!device_registry::identity_revoked(&identity)"),
    ("cap-authority", "device_cap", "assert_signer_security_spec", "Assume", "prover::prover::asserts(ctx.sender() == signer);"),
    ("receipts-future", "deployment_manager", "receipts_mut_security_spec", "Assume", "prover::prover::asserts(old_version <= RECEIPTS_VERSION);"),
    ("receipts-clear", "deployment_manager", "receipts_mut_security_spec", "Check", "prover::prover::ensures(result.statuses.is_empty());"),
    ("role-authority", "roles", "revoke_security_spec", "Assume", "prover::prover::asserts(ctx.sender() == operator);"),
    ("role-remove", "roles", "revoke_security_spec", "Check", "prover::prover::ensures(!df::exists_with_type<RoleKey, address>"),
    ("device-revoke", "device_registry", "revoke_device_security_spec", "Check", "prover::prover::ensures(identity_of(registry, device_id).revoked);"),
    ("rotation-revoked", "device_registry", "rotate_device_key_security_spec", "Check", "prover::prover::ensures(identity_of(registry, device_id).revoked == revoked);"),
]


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
    positives = [f for functions in POSITIVES.values() for f in functions]
    expected = {f"{module}::{f}_{phase}" for module, functions in POSITIVES.items()
                for f in functions for phase in ("Check", "Assume", "SpecNoAbortCheck")}
    for case in [None, *NEGATIVES]:
        extra = (["--mutation", case[0], "--functions", case[2]] if case else
                 ["--functions", *positives])
        result = subprocess.run(command + extra, env=env, text=True, capture_output=True)
        output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout + result.stderr)
        failed = re.findall(r"❌ (\w+::\w+)", output)
        if case:
            mutation, module, function, phase, obligation = case
            valid = (result.returncode == 1 and failed == [f"{module}::{function}_{phase}"]
                     and "error: prover::ensures does not hold" in output
                     and obligation in output and "exiting with verification errors" in output)
        else:
            passed = set(re.findall(r"✅ (\w+::\w+)", output))
            valid = (result.returncode == 0 and not failed and expected <= passed
                     and "Verification successful" in output)
        if not valid:
            raise RuntimeError(f"Unexpected proof result ({case[0] if case else 'positive'}):\n{output}")
        print(f"PASS: {case[0] if case else str(len(expected)) + ' positive obligations'}", flush=True)


if __name__ == "__main__":
    main()
