#!/usr/bin/env python3
"""Pinned compiler/linter and adversarial regression gate; not a formal proof.

Run in the pinned dev image. All compiler inputs/state are temporary; no wallet
is mounted or generated and no chain connection is needed.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "sui 1.81.1-e04cf9aa915c"


def diagnostics(package, output):
    found = []
    for block in output.split("error[")[1:]:
        code = block.split("]", 1)[0]
        location = re.search(r"[./]*sources/([^:]+):(\d+):", block)
        if not location:
            raise RuntimeError("Unrecognized diagnostic location: " + block)
        filename, line = location[1], int(location[2])
        text = (package / "sources" / filename).read_text().splitlines()[:line]
        symbols = re.findall(r"\b(?:fun|struct)\s+(\w+)", "\n".join(text))
        if not symbols:
            raise RuntimeError("Unrecognized diagnostic symbol: " + block)
        found.append(f"{filename}::{symbols[-1]}:{code}")
    return sorted(found)


def check_unsuppressed(build, package, env, expected):
    result = subprocess.run(build, env=env, text=True, capture_output=True)
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)
    actual = diagnostics(package, output)
    if result.returncode != 1 or actual != expected:
        raise RuntimeError(f"Unreviewed unsuppressed diagnostics: {actual!r}\n{output}")


def run(args, env, *, failure=None):
    result = subprocess.run(args, env=env, text=True, capture_output=True)
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)
    if failure is None:
        if result.returncode:
            raise RuntimeError(f"{args!r} failed:\n{output}")
    elif result.returncode == 0 or failure not in output:
        raise RuntimeError(f"Expected rejection {failure!r}: {args!r}\n{output}")
    return output


def main():
    if subprocess.check_output(["sui", "--version"], text=True).strip() != VERSION:
        raise SystemExit(f"Requires exactly {VERSION}; use the pinned Dockerfile")
    with tempfile.TemporaryDirectory(prefix="move-security-") as directory:
        temporary = Path(directory)
        config = temporary / "config"
        config.mkdir(mode=0o700)
        (config / "sui.keystore").write_text("[]")
        (config / "client.yaml").write_text(
            f"keystore:\n  File: {config / 'sui.keystore'}\n"
            "envs:\n  - alias: testnet\n    rpc: https://fullnode.testnet.sui.io:443\n"
            "active_env: testnet\nactive_address: ~\n"
        )
        env = dict(os.environ, SUI_CONFIG_DIR=str(config))
        package = temporary / "contracts"
        shutil.copytree(ROOT / "contracts", package,
                        ignore=shutil.ignore_patterns("build", ".move"))
        build = ["sui", "move", "build", "--path", str(package),
                 "--default-move-flavor", "sui", "--lint", "--warnings-are-errors"]
        run(build, env)
        print("PASS: pinned release compiler and extra Sui linters (warnings are errors)")
        # Independently inspect diagnostics with ALL allow attributes removed.
        # This prevents attribute scope/tool behavior from hiding new warnings.
        unsuppressed = temporary / "unsuppressed"
        shutil.copytree(package, unsuppressed, ignore=shutil.ignore_patterns("build"))
        for path in (unsuppressed / "sources").glob("*.move"):
            path.write_text(re.sub(r"^[ \t]*#\[allow\([^\n]+\)\]\n", "", path.read_text(), flags=re.M))
        unbuild = [arg if arg != str(package) else str(unsuppressed) for arg in build]
        expected = json.loads((ROOT / "contracts/security-lint-baseline.json").read_text())["diagnostics"]
        check_unsuppressed(unbuild, unsuppressed, env, expected)
        print("PASS: unsuppressed diagnostics match the reviewed compatibility baseline")
        path = unsuppressed / "sources" / "roles.move"
        path.write_text(path.read_text().rstrip()[:-1] + "public entry fun redundant_entry() {}\n}\n")
        try:
            check_unsuppressed(unbuild, unsuppressed, env, expected)
        except RuntimeError as error:
            if "roles.move::redundant_entry:ESL01011" not in str(error):
                raise
        else:
            raise RuntimeError("Unsuppressed gate accepted a new public-entry warning")
        print("PASS: unsuppressed gate rejects a new lint inside the actual package")
        negative = temporary / "negative"
        (negative / "sources").mkdir(parents=True)
        # Use the exact release manifest/lock: a fresh manifest alone resolves
        # today's implicit framework instead of the reviewed release revision.
        for name in ("Move.toml", "Move.lock"):
            shutil.copyfile(ROOT / "contracts" / name, negative / name)
        fixture = negative / "sources" / "negative.move"
        fixture.write_text("module sui_edge::negative {\n"
                           "public entry fun redundant_entry(_ctx: &mut TxContext) {}\n}\n")
        negative_build = [arg if arg != str(package) else str(negative) for arg in build]
        run(negative_build, env, failure="error[ESL01011]")
        print("PASS: extra linter rejects public-entry negative fixture")
        fixture.write_text("module sui_edge::negative {\n"
                           "public struct Linear {}\n"
                           "public fun discard(value: Linear) {}\n}\n")
        run(negative_build, env, failure="does not have the 'drop' ability")
        print("PASS: compiler rejects destruction of a linear capability")

        output = run(["sui", "move", "test", "--path", str(package)], env)
        print("PASS: current Move authorization/upgrade regression tests")
        print(next((line for line in output.splitlines() if "Test result:" in line), output[-300:]))

        # The Walrus-dependent package is a separate compilation/security unit.
        storage = package / "fleet_storage"
        storage_build = [arg if arg != str(package) else str(storage) for arg in build]
        run(storage_build, env)
        print("PASS: separate fleet_storage pinned compiler and strict Sui linters")
        storage_unsuppressed = unsuppressed / "fleet_storage"
        for source in (storage_unsuppressed / "sources").glob("*.move"):
            source.write_text(re.sub(r"^[ \t]*#\[allow\([^\n]+\)\]\n", "", source.read_text(), flags=re.M))
        check_unsuppressed([arg if arg != str(storage) else str(storage_unsuppressed) for arg in storage_build],
                           storage_unsuppressed, env, ["fleet_storage.move::execute_renewal:ESL06002"])
        print("PASS: fleet_storage unsuppressed baseline (authenticated executor reward)")
        output = run(["sui", "move", "test", "--path", str(storage)], env)
        print("PASS: separate fleet_storage authorization/quota/retention tests")
        print(next((line for line in output.splitlines() if "Test result:" in line), output[-300:]))

        # SF-082 is a third, independently compiled package, not coverage
        # inherited merely because root/storage are its dependencies.
        data = package / "fleet_data"
        data_build = [arg if arg != str(package) else str(data) for arg in build]
        run(data_build, env)
        print("PASS: separate fleet_data pinned compiler and strict Sui linters")
        data_unsuppressed = unsuppressed / "fleet_data"
        for source in (data_unsuppressed / "sources").glob("*.move"):
            source.write_text(re.sub(r"^[ \t]*#\[allow\([^\n]+\)\]\n", "", source.read_text(), flags=re.M))
        data_expected = json.loads((ROOT / "contracts/fleet_data/security-lint-baseline.json").read_text())["diagnostics"]
        check_unsuppressed([arg if arg != str(data) else str(data_unsuppressed) for arg in data_build],
                           data_unsuppressed, env, data_expected)
        print("PASS: fleet_data unsuppressed public-signature compatibility baseline")
        output = run(["sui", "move", "test", "--path", str(data)], env)
        print("PASS: separate fleet_data signer, sequence, cadence, blob and Seal access tests")
        print(next((line for line in output.splitlines() if "Test result:" in line), output[-300:]))
        data_source = data / "sources/fleet_data.move"
        for before, after, test in [
            ("fleet_budget::assert_may_draw(budget, account, registry, cap, ctx);",
             "let _ = ctx;", "revoked_health"),
            ("assert!(identity_matches(fleet, &id) && fleet.support.contains(&ctx.sender()), EAccess);",
             "assert!(identity_matches(fleet, &id), EAccess);", "device_cannot_read"),
        ]:
            original = data_source.read_text()
            if original.count(before) != 1:
                raise RuntimeError("fleet_data authority mutation anchor drift")
            data_source.write_text(original.replace(before, after))
            try:
                output = run(["sui", "move", "test", "--path", str(data), test], env,
                             failure="Test did not error as expected")
                if test not in output or "FAIL" not in output:
                    raise RuntimeError("Missing actual fleet_data authority counterexample")
                print(f"PASS: fleet_data rejects authority bypass: {test}")
            finally:
                data_source.write_text(original)

        storage_source = storage / "sources/fleet_storage.move"
        original = storage_source.read_text()
        start = original.index("    fun assert_owner(")
        end = original.index("\n    }", start)
        checked = original[start:end]
        statement = "fleet_budget::assert_owner(budget, cap);"
        if checked.count(statement) != 1:
            raise RuntimeError("Storage owner mutation anchor drift")
        storage_source.write_text(original[:start] + checked.replace(statement, "") + original[end:])
        try:
            run(["sui", "move", "test", "--path", str(storage), "a_device_cannot_release_fleet_blobs"],
                env, failure="Test did not error as expected")
            print("PASS: fleet_storage rejects owner-authority removal by genuine failing test")
        finally:
            storage_source.write_text(original)

        # These mutate actual implementation bodies and run the existing tests.
        # A compiler failure is not accepted as a detected authority violation.
        mutations = [
            ("roles.move", "assert!(is_active(registry, role), ERoleRevoked);",
             "let _ = is_active(registry, role);", "revoked_role_cannot_deploy"),
            ("device_cap.move", "assert!(device_registry::is_active_signer(registry, cap.device_id, ctx.sender()), EInactiveIdentity);",
             "let _ = device_registry::is_active_signer(registry, cap.device_id, ctx.sender());",
             "test_revoked_device_cannot_report"),
            ("fleet_inbox.move", "assert!(device_group::admin(group) == creator, ENotAdmin);",
             "let _ = device_group::admin(group);", "a_foreign_creator_cannot_announce_to_a_group"),
            ("fleet_inbox.move", "assert!(deployment_manager::target_devices(deployment).contains(&device_id), ENotTargeted);",
             "let _ = deployment_manager::target_devices(deployment);", "an_untargeted_device_cannot_be_announced"),
            ("device_group.move", "if (df::exists(&group.id, MemberKey { device_id })) return true;",
             "if (df::exists(&group.id, IndexKey {})) return true;", "a_non_member_cannot_follow_a_group"),
        ]
        for filename, before, after, test in mutations:
            path = package / "sources" / filename
            original = path.read_text()
            if original.count(before) != 1:
                raise RuntimeError(f"Mutation anchor drift: {filename}")
            path.write_text(original.replace(before, after))
            try:
                output = run(["sui", "move", "test", "--path", str(package),
                              test], env, failure="Test did not error as expected")
                if test not in output or "FAIL" not in output:
                    raise RuntimeError("Missing actual failing security test")
                print(f"PASS: regression rejects bypass mutation: {test}")
            finally:
                path.write_text(original)


if __name__ == "__main__":
    main()
