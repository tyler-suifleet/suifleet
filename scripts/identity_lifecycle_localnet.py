#!/usr/bin/env python3
"""Device identity lifecycle across a real package upgrade (SF-008).

Publishes the prior contracts, creates a device, a deployment and a genuine
report with them, upgrades to the identity-checked contracts, then attacks
through the prior package and the retired signatures. Every outcome is judged
from on-chain execution effects (for expected failures, the exact abort site or
execution error) and from receipt state as the dApp reads it:

  - the prior package cannot write records created or migrated by the upgrade;
  - the retired report signature aborts in the upgraded package;
  - a device registered before identities exists is untrusted until its owner
    activates it (taking it into registry custody); it then reports with its
    existing signer-bound cap;
  - an activated Device cannot reach the prior package's deregister_device,
    directly or borrowed, so it cannot be deleted without its identity;
  - revocation stops reports, reinstatement restores them, rotation distrusts
    the old key's caps and trusts a new cap; ownership moves with the owner cap;
  - deregistration through the upgrade revokes;
  - a revoked key still writes version-1 receipts through the prior package
    (on unmigrated records and on records the prior package creates after the
    sweep), but they are never trusted: the dApp reader reports them
    unverified and migration or the next current report discards them.

Run inside the dev image against a throwaway localnet and client config:

    git archive origin/main contracts | tar -x -C /tmp/prior     # on the host
    SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
    SUI_CONFIG_DIR=/tmp/sui-config sui client switch --env local
    SUI_CONFIG_DIR=/tmp/sui-config python3 scripts/identity_lifecycle_localnet.py \\
        --prior /tmp/prior/contracts --new contracts
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import upgrade_security_localnet as u

CLOCK = u.CLOCK
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prior", required=True, help="contracts before identities (e.g. origin/main)")
    ap.add_argument("--new", required=True, help="identity-checked contracts")
    args = ap.parse_args()

    env, u.RPC_URL, chain = u.require_localnet()
    print(f"env={env} chain={chain} sui={subprocess.run(['sui', '--version'], capture_output=True, text=True).stdout.strip()}")
    op, dev, new_dev, new_op, other_dev = (u.address(a) for a in ("operator", "device", "rotated-device", "new-owner", "other-device"))
    for a in (op, dev, new_dev, new_op, other_dev):
        u.fund(a)
    time.sleep(2)

    work = tempfile.mkdtemp(prefix="sf008-")
    package = os.path.join(work, "package")
    pubfile = os.path.join(work, "Pub.localnet.toml")
    u.stage(args.prior, package)

    def deploy(pkg, label, *devices):
        tx = u.execute(f"{label}: create_deployment_verified via {'prior' if pkg == pkg_prior else 'upgrade'}",
                       "--move-call", f"{pkg}::deployment_manager::create_deployment_verified",
                       '"fw"', '"1.0.0"', '"recipe-blob"', '"artifact-sha"', f'"{u.RECIPE_SHA}"',
                       "vector[]", "vector[" + ", ".join(f"@{d}" for d in devices) + "]", CLOCK)
        return u.created(tx, "::deployment_manager::DeploymentRecord")[0]

    def register(pkg, name, device_addr, activate=True):
        """Register and issue a cap; through the upgrade, activate in the same PTB."""
        ptb = ["--move-call", f"{pkg}::device_registry::register_device",
               f"@{registry}", f'"{name}"', f"@{device_addr}", '"aarch64"', CLOCK,
               "--assign", "device",
               "--move-call", f"{pkg}::device_cap::issue_cap", "device"]
        if activate:
            ptb += ["--move-call", f"{pkg}::device_registry::activate_device", f"@{registry}", "device",
                    "--assign", "owner", "--transfer-objects", "[owner]", f"@{op}"]
        else:
            ptb += ["--transfer-objects", "[device]", f"@{op}"]
        tx = u.execute(f"register {name}", *ptb)
        owner = u.created(tx, "::device_registry::DeviceOwnerCap") if activate else [None]
        return u.created(tx, "::device_registry::Device")[0], u.created(tx, "::device_cap::DeviceCap")[0], owner[0]

    def report(label, record, cap, *, signer=dev, ok=True, abort=None, old=None):
        u.use(signer)
        if old:  # The prior package's (or the retired) signature, without the registry.
            ptb = ["--move-call", f"{old}::deployment_manager::report_status", f"@{record}", f"@{cap}", "1u8"]
        else:
            ptb = ["--move-call", f"{pkg_new}::deployment_manager::report_verified_status",
                   f"@{record}", f"@{registry}", f"@{cap}", "1u8"]
        u.execute(label, *ptb, expect_success=ok, abort=abort)
        u.use(op)

    def lifecycle(label, function, owner_cap, *arguments, sender=op):
        u.use(sender)
        u.execute(label, "--move-call", f"{pkg_new}::device_registry::{function}", f"@{registry}",
                  f"@{owner_cap}", *arguments)
        u.use(op)

    def receipts(record):
        """(version, {device: status}) of the record's receipts, as stored."""
        _, value = u.fields_of(record)["ReceiptsKey"]
        return int(value["fields"]["version"]), u.vecmap(value["fields"]["statuses"])

    # ── Prior package: a device without identity, a record and a genuine report ─
    u.use(op)
    tx = u.publish(package, pubfile)
    pkg_prior = u.published_package(tx)
    registry = u.created(tx, "::device_registry::DeviceRegistry")[0]
    upgrade_cap = u.created(tx, "::package::UpgradeCap")[0]
    legacy, legacy_cap, _ = register(pkg_prior, "legacy", dev, activate=False)
    old_record = deploy(pkg_prior, "prior", legacy)
    report("prior package: genuine report on its own record", old_record, legacy_cap, old=pkg_prior)

    # ── Upgrade ───────────────────────────────────────────────────────────────
    u.stage(args.new, package)
    tx = u.publish(package, pubfile, upgrade_cap)
    pkg_new = u.published_package(tx)
    u.check("upgrade accepted by the on-chain compatibility check", bool(pkg_new), pkg_new)
    new_record = deploy(pkg_new, "upgraded", legacy)

    report("prior package cannot write a record created by the upgrade", new_record, legacy_cap,
           old=pkg_prior, ok=False, abort=("receipts_mut", 5))
    report("retired report signature aborts in the upgraded package", new_record, legacy_cap,
           old=pkg_new, ok=False, abort=("report_status", 7))
    report("legacy device without identity is untrusted", new_record, legacy_cap,
           ok=False, abort=("assert_active", 2))
    tx = u.execute("owner activates the legacy device (registry custody)",
                   "--move-call", f"{pkg_new}::device_registry::activate_device", f"@{registry}", f"@{legacy}",
                   "--assign", "owner", "--transfer-objects", "[owner]", f"@{op}")
    legacy_owner = u.created(tx, "::device_registry::DeviceOwnerCap")[0]
    u.check("activated Device is no longer owned by the operator",
            not any(o["objectId"] == legacy for o in u.owned(op, "::device_registry::Device")))
    report("activated legacy device reports with its existing cap", new_record, legacy_cap)

    # ── The prior package's deregister_device cannot reach an activated Device ─
    u.execute("prior-package deregister cannot take the custodied Device",
              "--move-call", f"{pkg_prior}::device_registry::deregister_device", f"@{registry}", f"@{legacy}",
              expect_success=False)
    u.execute("a transaction cannot link the prior package's deregister with a borrow",
              "--move-call", f"{pkg_new}::device_registry::borrow_device", f"@{registry}", f"@{legacy_owner}",
              "--assign", "loaned",
              "--move-call", f"{pkg_prior}::device_registry::deregister_device", f"@{registry}", "loaned.0",
              expect_success=False, error_text="InvalidLinkage")
    # The loan also stops any by-value consumer of a borrowed Device (here the
    # upgrade's own unchecked deregister_device, linkable in one transaction).
    u.execute("deregistering a borrowed Device leaves the loan unpaid",
              "--move-call", f"{pkg_new}::device_registry::borrow_device", f"@{registry}", f"@{legacy_owner}",
              "--assign", "loaned",
              "--move-call", f"{pkg_new}::device_registry::deregister_device", f"@{registry}", "loaned.0",
              expect_success=False, error_text="UnusedValueWithoutDrop")
    report("device still reports after both deletion attempts", new_record, legacy_cap)

    # ── Revocation, reinstatement, rotation, ownership transfer ───────────────
    lifecycle("owner revokes", "revoke_device", legacy_owner)
    report("revoked device cannot report", new_record, legacy_cap, ok=False, abort=("assert_active", 2))
    lifecycle("owner reinstates", "reinstate_device", legacy_owner)
    report("reinstated device reports again", new_record, legacy_cap)

    rotating, old_key_cap, rotating_owner = register(pkg_new, "rotating", dev)
    rotation_record = deploy(pkg_new, "rotation", rotating)
    tx = u.execute("owner rotates the key and issues a cap for it",
                   "--move-call", f"{pkg_new}::device_registry::rotate_device_key", f"@{registry}",
                   f"@{rotating_owner}", f"@{new_dev}",
                   "--move-call", f"{pkg_new}::device_registry::borrow_device", f"@{registry}", f"@{rotating_owner}",
                   "--assign", "loaned",
                   "--move-call", f"{pkg_new}::device_cap::issue_cap", "loaned.0",
                   "--move-call", f"{pkg_new}::device_registry::return_device", f"@{registry}", "loaned.0", "loaned.1")
    new_key_cap = u.created(tx, "::device_cap::DeviceCap")[0]
    report("rotated-out key cannot report", rotation_record, old_key_cap, ok=False, abort=("assert_active", 2))
    report("rotated-in key reports", rotation_record, new_key_cap, signer=new_dev)

    u.execute("owner transfers the owner cap", "--transfer-objects", f"[@{rotating_owner}]", f"@{new_op}")
    u.use(op)
    u.execute("previous owner can no longer revoke", "--move-call", f"{pkg_new}::device_registry::revoke_device",
              f"@{registry}", f"@{rotating_owner}", expect_success=False)
    lifecycle("new owner revokes", "revoke_device", rotating_owner, sender=new_op)
    report("revoked by the new owner: cannot report", rotation_record, new_key_cap, signer=new_dev,
           ok=False, abort=("assert_active", 2))

    gone, gone_cap, gone_owner = register(pkg_new, "deregistered", dev)
    gone_record = deploy(pkg_new, "deregistered", gone)
    lifecycle("deregister through the upgraded package", "deregister_activated_device", gone_owner)
    report("deregistered device cannot report", gone_record, gone_cap, ok=False, abort=("assert_active", 2))

    # ── Version-1 receipts from a revoked key are never trusted ───────────────
    lifecycle("owner revokes the legacy device again", "revoke_device", legacy_owner)
    report("revoked key: prior package still writes the unmigrated old record", old_record, legacy_cap,
           old=pkg_prior)
    version, statuses = receipts(old_record)
    u.check("that write stays version 1, below the trusted authority version", version == 1, f"version={version}")
    u.execute("anyone migrates the old record", "--move-call", f"{pkg_new}::deployment_manager::migrate", f"@{old_record}")
    version, statuses = receipts(old_record)
    u.check("migration discards version-1 statuses instead of promoting them", (version, statuses) == (2, {}),
            f"version={version} statuses={statuses}")
    report("prior package cannot write the migrated record", old_record, legacy_cap,
           old=pkg_prior, ok=False, abort=("receipts_mut", 5))

    other, other_cap, _ = register(pkg_new, "current", other_dev)
    late_record = deploy(pkg_prior, "after the sweep", legacy, other)
    report("revoked key: prior package writes a record it created after the sweep", late_record, legacy_cap,
           old=pkg_prior)
    version, statuses = receipts(late_record)
    u.check("post-sweep prior-package record stays version 1", version == 1 and legacy in statuses,
            f"version={version} statuses={statuses}")
    report("an active device reports through the upgrade on that record", late_record, other_cap, signer=other_dev)
    version, statuses = receipts(late_record)
    u.check("the current report replaces the revoked key's status", (version, statuses) == (2, {other: 1}),
            f"version={version} statuses={statuses}")

    # The dApp's receipt reader, against this chain: version-1 receipts are
    # unverified and show no statuses.
    stale_record = deploy(pkg_prior, "unswept", legacy)
    report("revoked key: prior package writes another post-sweep record", stale_record, legacy_cap, old=pkg_prior)
    expected = {stale_record: {"verified": False, "statuses": {}}, late_record: {"verified": True, "statuses": {other: 1}},
                old_record: {"verified": True, "statuses": {}}}
    # Receipts types already exist in the prior package, so that is their origin.
    reader = subprocess.run(["node", "-e", READER, os.path.join(ROOT, "scripts"), u.RPC_URL, pkg_new, pkg_prior,
                             json.dumps(expected)],
                            capture_output=True, text=True, cwd=ROOT)
    u.check("dApp readReceipts treats only current-version receipts as verified", reader.returncode == 0,
            (reader.stdout + reader.stderr).strip()[-600:])

    failed = [r for r in u.results if not r[0]]
    print(f"\n{len(u.results) - len(failed)}/{len(u.results)} checks passed")
    for _, label, detail, digest in failed:
        print(f"FAILED: {label}: {detail} {digest}")
    sys.exit(1 if failed else 0)


# Loads dapp/src/lib/sui.ts with a manifest giving the Receipts types their
# live origin (the only fields readReceipts uses) and reads live records.
READER = r"""
const [scripts, rpc, pkg, origin, expected] = process.argv.slice(1);
const assert = require("node:assert/strict");
const { loader, appRequire } = require(scripts + "/frontend_test_utils.cjs");
const fixture = require(scripts + "/../manifests/fixture.localnet.json");
for (const name of ["deployment_manager::ReceiptsKey", "deployment_manager::Receipts"]) fixture.type_origins[name] = origin;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify({ ...fixture, rpc_url: rpc, call_package_id: pkg });
const sui = loader()("lib/sui.ts");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc");
const { normalizeSuiAddress } = appRequire("@mysten/sui/utils");
const client = new SuiGrpcClient({ network: "localnet", baseUrl: rpc });
(async () => {
  for (const [id, want] of Object.entries(JSON.parse(expected))) {
    // Every record has receipts: "unverified" must come from their version.
    await client.getDynamicField({ parentId: id, name: { type: origin + "::deployment_manager::ReceiptsKey", bcs: new Uint8Array([0]) } });
    const got = await sui.readReceipts(client, id);
    const statuses = Object.fromEntries(Object.entries(want.statuses).map(([k, v]) => [normalizeSuiAddress(k), v]));
    assert.deepEqual(got, { verified: want.verified, statuses }, id);
  }
  console.log("readReceipts matched " + Object.keys(JSON.parse(expected)).length + " live records");
})().catch((error) => { console.error(error); process.exit(1); });
"""


if __name__ == "__main__":
    main()
