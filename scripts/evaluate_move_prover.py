#!/usr/bin/env python3
"""Reproduce SF-050's release-framework prover evaluation, failing closed.

Tool installation and exact verified/pending scopes are in docs/MOVE_SECURITY.md.
This stages pinned spec-only overlays without replacing implementation bodies.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / "contracts/security-specs/toolchain.json").read_text())


# Mutations affect only a disposable copy of the actual release implementation.
MUTATIONS = {
    "close-authority": ("enrollment", "close", "assert_operator(policy, ctx);", ""),
    "close-state": ("enrollment", "close", "policy.open = false;", ""),
    "limits-authority": ("enrollment", "set_limits", "assert_operator(policy, ctx);", ""),
    "limits-quota": ("enrollment", "set_limits", "policy.remaining = quota;", "policy.remaining = 0;"),
    "legacy-report": ("deployment_manager", "report_status", "abort EUseVerifiedReport", ""),
    "legacy-group-report": ("deployment_manager", "report_status_for_group", "abort EUseVerifiedReport", ""),
    "group-authority": ("device_group", "handover", "assert!(ctx.sender() == group.admin, ENotAdmin);", ""),
    "deployment-authority": ("deployment_manager", "handover", "assert!(ctx.sender() == d.creator, ENotCreator);", ""),
    "claim-authority": ("enrollment", "revoke_claim", "assert_operator(policy, ctx);", ""),
    "claim-remove": ("enrollment", "revoke_claim", "policy.claims.remove(claim);", ""),
    "binding-artifact": ("deployment_manager", "make_deployment", "            artifact_sha256,\n            created_at:", "            artifact_sha256: recipe_blob_id,\n            created_at:"),
    "binding-recipe-value": ("deployment_manager", "new_deployment_verified", "df::add(&mut record.id, RecipeDigestKey {}, recipe_sha256);", "df::add(&mut record.id, RecipeDigestKey {}, artifact_sha256);"),
    "binding-recipe": ("deployment_manager", "new_deployment_verified", "df::add(&mut record.id, RecipeDigestKey {}, recipe_sha256);", ""),
    "recipe-length": ("deployment_manager", "assert_recipe_hash", "assert!(bytes.length() == 64, EInvalidRecipeHash);", ""),
    "cap-authority": ("device_cap", "assert_signer", "assert!(ctx.sender() == *signer, EWrongSigner);", ""),
    "receipts-future": ("deployment_manager", "receipts_mut", "assert!(receipts.version <= RECEIPTS_VERSION, EPackageOutdated);", ""),
    "receipts-clear": ("deployment_manager", "receipts_mut", "receipts.statuses = vec_map::empty();", ""),
    "role-authority": ("roles", "revoke", "assert!(ctx.sender() == operator, ENotOperator);", ""),
    "role-remove": ("roles", "revoke", "let _: address = df::remove(device_registry::uid_mut(registry), key);", "let _: address = *df::borrow(device_registry::uid(registry), key);"),
    "device-revoke": ("device_registry", "revoke_device", "revoked: true", "revoked: false"),
    "rotation-revoked": ("device_registry", "rotate_device_key", "signer: new_address, revoked,", "signer: new_address, revoked: false,"),
    "activation-identity": ("device_registry", "activate_device", "set_identity(registry, device_id, Identity { signer: device.device_address, revoked: false, owner_cap: object::id(&cap) });", ""),
    "identity-custody": ("device_registry", "is_active_signer", "if (!dof::exists(&registry.id, CustodyKey { device_id })) return false;", ""),
    "identity-revoked": ("device_registry", "is_active_signer", "!identity.revoked && identity.signer == signer", "identity.signer == signer"),
    "identity-signer": ("device_registry", "is_active_signer", "!identity.revoked && identity.signer == signer", "!identity.revoked"),
}


def mutate_implementation(package, mutation):
    module, function, old, new = MUTATIONS[mutation]
    path = package / f"sources/{module}.move"
    text = path.read_text()
    start = re.search(r"    (?:public(?:\(package\))? )?fun " + function + r"\(", text).start()
    end = text.index("\n    }", start)
    body = text[start:end]
    if body.count(old) != 1:
        raise SystemExit("Mutation no longer matches exactly one implementation statement")
    path.write_text(text[:start] + body.replace(old, new) + text[end:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prover-source", type=Path, required=True)
    parser.add_argument("--framework-packages", type=Path, required=True)
    parser.add_argument("--prover-binary", type=Path, required=True)
    parser.add_argument("--include-pending-enrollment", action="store_true", help="Run the unverified full enrollment obligation")
    parser.add_argument("--include-name-absence", action="store_true", help="Reproduce stronger untyped name-absence obligations (not verified)")
    parser.add_argument("--timeout", type=int, default=30, help="Per-obligation hard solver timeout in seconds")
    parser.add_argument("--include-pending-custody", action="store_true", help="Reproduce unverified custody construction/return obligations")
    parser.add_argument("--functions", nargs="+", help="Exact prover target filter; does not change implementation sources")
    parser.add_argument("--mutation", choices=MUTATIONS, help="Negative control in temporary implementation copy; expect solver failure")
    args = parser.parse_args()
    source = args.prover_source.resolve()
    framework = args.framework_packages.resolve()
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != PIN["prover"]["commit"]:
        raise SystemExit("Prover source revision does not match toolchain.json")
    overlay = ROOT / "contracts/security-specs/compatibility"
    for name, expected in PIN["compatibility"].items():
        if hashlib.sha256((overlay / name).read_bytes()).hexdigest() != expected:
            raise SystemExit("Compatibility overlay checksum mismatch: " + name)
    # Require exactly the reviewed patch, including the compiled Rust native map.
    actual = subprocess.check_output(["git", "-C", str(source), "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--full-index", "HEAD"])
    if actual != (overlay / "release-model.patch").read_bytes():
        raise SystemExit("Prover source must contain exactly release-model.patch")
    untracked = subprocess.check_output(["git", "-C", str(source), "ls-files", "--others", "--exclude-standard"])
    if untracked:
        raise SystemExit("Untracked prover source/model files are forbidden")
    if hashlib.sha256((source / "Cargo.lock").read_bytes()).hexdigest() != PIN["prover"]["cargo_lock_sha256"]:
        raise SystemExit("Prover lockfile checksum mismatch")
    digest = hashlib.sha256()
    for name in ("move-stdlib", "sui-framework"):
        for path in sorted([framework / name / "Move.toml", *(framework / name / "sources").rglob("*.move")]):
            digest.update(str(path.relative_to(framework)).encode() + b"\0" + path.read_bytes() + b"\0")
    if digest.hexdigest() != PIN["release_framework"]["source_sha256"]:
        raise SystemExit("Release framework source checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="fleet-prover-") as directory:
        temporary = Path(directory)
        libraries = temporary / "framework"
        for name in ("move-stdlib", "sui-framework"):
            shutil.copytree(framework / name, libraries / name)
        # One proof-only native datatype, no bodies/axioms. Check the untouched
        # release checksum before copying; never edit the executable framework.
        shutil.copyfile(overlay / "integer.move", libraries / "move-stdlib/sources/integer.move")
        dof = libraries / "sui-framework/sources/dynamic_object_field.move"
        dof.write_text(dof.read_text() + (overlay / "dof-spec-helpers.move").read_text())
        for name in ("prover", "sui-specs", "sui-prover"):
            shutil.copytree(source / "packages" / name, libraries / name)
            manifest = libraries / name / "Move.toml"
            lines = manifest.read_text().splitlines()
            for index, line in enumerate(lines):
                for dependency, local in (("Sui", "sui-framework"), ("MoveStdlib", "move-stdlib")):
                    if line.startswith(dependency + " = { git"):
                        lines[index] = f'{dependency} = {{ local = "../{local}", override = true }}'
            manifest.write_text("\n".join(lines) + "\n")
        package = temporary / "contracts"
        # The prover uses the legacy package resolver, not the release CLI's
        # version-4 environment lock. Dependencies above are explicit and pinned.
        shutil.copytree(ROOT / "contracts", package,
                        ignore=shutil.ignore_patterns("build", "Move.lock", "Published.toml"))
        for module, helper in (("device_registry", "registry"), ("roles", "roles"),
                               ("deployment_manager", "deployment"), ("device_cap", "cap"),
                               ("enrollment", "enrollment"), ("device_group", "group")):
            implementation = package / f"sources/{module}.move"
            body = implementation.read_text()
            end = body.rindex("}")
            additions = (overlay / f"{helper}-spec-helpers.move").read_text()
            if module == "device_registry" and args.include_pending_custody:
                additions += (overlay / "registry-custody-pending.move").read_text()
            if module == "enrollment" and args.include_pending_enrollment:
                additions += (overlay / "enrollment-pending.move").read_text()
            if args.include_name_absence and module == "roles":
                additions = additions.replace(
                    'prover::prover::ensures(!df::exists_with_type<RoleKey, address>(device_registry::uid(registry), key));',
                    'prover::prover::ensures(!df::exists(device_registry::uid(registry), key));')
            implementation.write_text(body[:end] + additions + body[end:])
        if args.mutation:
            mutate_implementation(package, args.mutation)
        manifest = package / "Move.toml"
        manifest.write_text(manifest.read_text() + '\n[addresses]\nsui_edge = "0x0"\n')
        # Analyze implementation modules as root targets too: the prover skips
        # prove-annotations in dependencies. Sources remain identical apart
        # from the checksum-pinned spec-only observation/obligation overlay.
        specs = package / "security-specs"
        for implementation in (package / "sources").glob("*.move"):
            shutil.copyfile(implementation, specs / "sources" / ("impl_" + implementation.name))
        spec_manifest = specs / "Move.toml"
        spec_manifest.write_text(spec_manifest.read_text().replace(
            'sui_edge = { local = ".." }',
            '')
            + '\nsui_edge = "0x0"\n')
        env = dict(os.environ, SUI_PROVER_FRAMEWORK_PATH=str(libraries))
        result = subprocess.run([str(args.prover_binary.resolve()), "--path", str(package / "security-specs"),
                                 "--timeout", str(args.timeout), "--force-timeout",
                                 *[part for function in (args.functions or []) for part in ("--functions", function)]], env=env, cwd=temporary, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout, end="", flush=True)
        if "Nothing to verify" in result.stdout:
            raise SystemExit("No matching proof targets: refusing an empty verification run")
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
