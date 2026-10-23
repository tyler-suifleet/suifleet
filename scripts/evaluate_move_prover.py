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
    "binding-legacy-status": ("deployment_manager", "make_deployment", "df::add(&mut record.id, ReceiptsKey {},      new_receipts());", "df::add(&mut record.id, ReceiptsKey {},      new_receipts());\n        df::add(&mut record.id, StatusesKey {}, vec_map::empty<ID, u8>());"),
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
    parser.add_argument("--unknown-occupancy-control", action="store_true", help="Deliberately assert unmodeled occupied slots are impossible; must fail")
    parser.add_argument("--native-model-control", choices=("missing-tag-clear", "wrong-type-alias", "object-omit-outer", "object-omit-child", "object-skip-clear", "object-wrong-owner", "object-wrong-id"), help="Deliberately mutate an emitted native transition; solver rejection required")
    parser.add_argument("--include-overlap-fixtures", action="store_true", help="Reproduce unsupported same-Name raw/object overlap; not a proof")
    parser.add_argument("--include-object-fixtures", action="store_true", help="Reproduce real outer Wrapper/ID and UID-bearing child obligations")
    parser.add_argument("--include-carrier-fixtures", action="store_true", help="Reproduce existing-UID carrier reconstruction boundary")
    parser.add_argument("--include-raw-fixtures", action="store_true", help="Run executable raw native conformance fixtures and actual-function specifications")
    parser.add_argument("--artifacts", type=Path, help="Copy proof-only generated solver artifacts to a new private directory")
    parser.add_argument("--include-pending-enrollment", action="store_true", help="Run the unverified full enrollment obligation")
    parser.add_argument("--include-name-absence", action="store_true", help="Legacy flag: name absence is now required by the default role specification")
    parser.add_argument("--timeout", type=int, default=30, help="Per-obligation hard solver timeout in seconds")
    parser.add_argument("--include-pending-custody", action="store_true", help="Reproduce unverified custody construction/return obligations")
    parser.add_argument("--functions", nargs="+", help="Exact prover target filter; does not change implementation sources")
    parser.add_argument("--mutation", choices=MUTATIONS, help="Negative control in temporary implementation copy; expect solver failure")
    args = parser.parse_args()
    if args.unknown_occupancy_control and not args.include_raw_fixtures:
        parser.error("Unknown occupancy control requires --include-raw-fixtures")
    if args.artifacts and args.artifacts.exists():
        raise SystemExit("Artifact destination must not already exist")
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
    # Git accepts unprefixed empty context lines. Normalize only diff context,
    # preserving every source byte while avoiding whitespace-only patch lines.
    if actual.replace(b"\n \n", b"\n\n") != (overlay / "release-model.patch").read_bytes():
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
        if args.include_raw_fixtures:
            shutil.copyfile(ROOT / "contracts/security-native-fixtures/sources/raw_fields.move", specs / "sources/raw_fields.move")
            raw_spec = (overlay / "raw-field-spec-helpers.move").read_text()
            if args.unknown_occupancy_control:
                old = "    ensures(result == (df::exists(parent, key)\n        && !df::exists_with_type<u64, u64>(parent, key)\n        && !df::exists_with_type<u64, bool>(parent, key)));"
                if raw_spec.count(old) != 1:
                    raise SystemExit("Unknown occupancy control must match exactly one obligation")
                raw_spec = raw_spec.replace(old, "    ensures(!result);")
            (specs / "sources/raw_field_specs.move").write_text(raw_spec)
        if args.include_carrier_fixtures:
            shutil.copyfile(ROOT / "contracts/security-native-fixtures/sources/carrier_fields.move", specs / "sources/carrier_fields.move")
            shutil.copyfile(overlay / "carrier-field-spec-helpers.move", specs / "sources/carrier_field_specs.move")
        if args.include_overlap_fixtures:
            overlap = (ROOT / "contracts/security-native-fixtures/sources/overlap_fields.move").read_text()
            overlap += "\n#[mode(spec), ext(spec(prove, target = same_name_noninterference))]\nfun overlap_same_spec(ctx: &mut TxContext) { same_name_noninterference(ctx); }\n"
            overlap += "\n#[mode(spec), ext(spec(prove, target = raw_value_noninterference))]\nfun overlap_different_spec(ctx: &mut TxContext) { raw_value_noninterference(ctx); }\n"
            (specs / "sources/overlap_fields.move").write_text(overlap)
        if args.include_object_fixtures:
            body = (ROOT / "contracts/security-native-fixtures/sources/object_fields.move").read_text()
            # Semicolon module syntax extends to EOF; append spec-only obligations.
            body += (overlay / "object-field-spec-helpers.move").read_text()
            (specs / "sources/object_fields.move").write_text(body)
        spec_manifest = specs / "Move.toml"
        spec_manifest.write_text(spec_manifest.read_text().replace(
            'sui_edge = { local = ".." }',
            '')
            + '\nsui_edge = "0x0"\nsf050_native = "0x51"\n')
        env = dict(os.environ, SUI_PROVER_FRAMEWORK_PATH=str(libraries))
        if args.native_model_control:
            if not (args.include_raw_fixtures or args.include_object_fixtures):
                raise SystemExit("Native model controls require the matching native fixtures")
            wrapper = temporary / "boogie-control"
            shutil.copyfile(ROOT / "scripts/move_native_model_control.py", wrapper)
            wrapper.chmod(0o700)
            env.update(SF050_REAL_BOOGIE=env["BOOGIE_EXE"], BOOGIE_EXE=str(wrapper),
                       SF050_NATIVE_CONTROL=args.native_model_control)
        result = subprocess.run([str(args.prover_binary.resolve()), "--path", str(package / "security-specs"),
                                 "--timeout", str(args.timeout), "--force-timeout",
                                 *(["--keep-temp"] if args.artifacts else []),
                                 *[part for function in (args.functions or []) for part in ("--functions", function)]], env=env, cwd=temporary, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if args.artifacts:
            args.artifacts.mkdir(mode=0o700, parents=True)
            for artifact in temporary.rglob("*.bpl"):
                destination = args.artifacts / artifact.relative_to(temporary)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(artifact, destination)
        print(result.stdout, end="", flush=True)
        if "Nothing to verify" in result.stdout:
            raise SystemExit("No matching proof targets: refusing an empty verification run")
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
