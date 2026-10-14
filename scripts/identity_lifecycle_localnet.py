#!/usr/bin/env python3
"""Device identity lifecycle across a real package upgrade (SF-008).

Publishes the prior contracts, creates a device, a deployment and a genuine
report with them, upgrades to the identity-checked contracts, then attacks
through the prior package and the retired signatures. Every outcome is judged
from on-chain execution effects (and, for expected failures, the exact abort
function and code):

  - the prior package cannot write records created or migrated by the upgrade;
  - the retired report signature aborts in the upgraded package;
  - a device registered before identities exists is untrusted until its owner
    activates it; activation keeps its existing signer-bound cap;
  - revocation stops reports, only the recorded operator may revoke, and
    reactivation restores them;
  - key rotation distrusts the old key's caps and trusts a new cap for the new key;
  - deregistration through the upgraded package revokes; through the prior
    package it cannot, but the operator can still revoke by ID afterwards.

Run inside the dev image against a throwaway localnet and client config:

    git archive origin/main contracts | tar -x -C /tmp/prior     # on the host
    SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
    SUI_CONFIG_DIR=/tmp/sui-config sui client switch --env local
    SUI_CONFIG_DIR=/tmp/sui-config python3 scripts/identity_lifecycle_localnet.py \\
        --prior /tmp/prior/contracts --new contracts
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

import upgrade_security_localnet as u

CLOCK = u.CLOCK


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prior", required=True, help="contracts before identities (e.g. origin/main)")
    ap.add_argument("--new", required=True, help="identity-checked contracts")
    args = ap.parse_args()

    env, u.RPC_URL, chain = u.require_localnet()
    print(f"env={env} chain={chain} sui={subprocess.run(['sui', '--version'], capture_output=True, text=True).stdout.strip()}")
    op, dev, new_dev, atk = (u.address(a) for a in ("operator", "device", "rotated-device", "attacker"))
    for a in (op, dev, new_dev, atk):
        u.fund(a)
    time.sleep(2)

    work = tempfile.mkdtemp(prefix="sf008-")
    package = os.path.join(work, "package")
    pubfile = os.path.join(work, "Pub.localnet.toml")
    u.stage(args.prior, package)

    def deploy(pkg, label, device):
        tx = u.execute(f"{label}: create_deployment_verified",
                       "--move-call", f"{pkg}::deployment_manager::create_deployment_verified",
                       '"fw"', '"1.0.0"', '"recipe-blob"', '"artifact-sha"', f'"{u.RECIPE_SHA}"',
                       "vector[]", f"vector[@{device}]", CLOCK)
        return u.created(tx, "::deployment_manager::DeploymentRecord")[0]

    def register(pkg, name, device_addr):
        tx = u.execute(f"register {name}",
                       "--move-call", f"{pkg}::device_registry::register_device",
                       f"@{registry}", f'"{name}"', f"@{device_addr}", '"aarch64"', CLOCK,
                       "--assign", "device",
                       "--move-call", f"{pkg}::device_cap::issue_cap", "device",
                       "--transfer-objects", "[device]", f"@{op}")
        return u.created(tx, "::device_registry::Device")[0], u.created(tx, "::device_cap::DeviceCap")[0]

    def report(label, record, cap, *, signer=dev, ok=True, abort=None, old=None):
        u.use(signer)
        if old:  # The prior package's (or the retired) signature, without the registry.
            ptb = ["--move-call", f"{old}::deployment_manager::report_status", f"@{record}", f"@{cap}", "1u8"]
        else:
            ptb = ["--move-call", f"{pkg_new}::deployment_manager::report_verified_status",
                   f"@{record}", f"@{registry}", f"@{cap}", "1u8"]
        u.execute(label, *ptb, expect_success=ok, abort=abort)
        u.use(op)

    def registry_call(label, function, *arguments, sender=op, ok=True, abort=None):
        u.use(sender)
        u.execute(label, "--move-call", f"{pkg_new}::device_registry::{function}", f"@{registry}",
                  *arguments, expect_success=ok, abort=abort)
        u.use(op)

    # ── Prior package: a device without identity, a record and a genuine report ─
    u.use(op)
    tx = u.publish(package, pubfile)
    pkg_prior = u.published_package(tx)
    registry = u.created(tx, "::device_registry::DeviceRegistry")[0]
    upgrade_cap = u.created(tx, "::package::UpgradeCap")[0]
    legacy, legacy_cap = register(pkg_prior, "legacy", dev)
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
    u.execute("owner activates the legacy device",
              "--move-call", f"{pkg_new}::device_registry::activate_device", f"@{registry}", f"@{legacy}")
    report("activated legacy device reports with its existing cap", new_record, legacy_cap)

    report("prior package still writes an unmigrated old record (sweep required)", old_record, legacy_cap,
           old=pkg_prior)
    u.execute("anyone migrates the old record", "--move-call", f"{pkg_new}::deployment_manager::migrate", f"@{old_record}")
    report("prior package cannot write the migrated record", old_record, legacy_cap,
           old=pkg_prior, ok=False, abort=("receipts_mut", 5))

    # ── Revocation ────────────────────────────────────────────────────────────
    registry_call("attacker cannot revoke", "revoke_device", f"@{legacy}", sender=atk, ok=False,
                  abort=("revoke_device", 1))
    registry_call("operator revokes", "revoke_device", f"@{legacy}")
    report("revoked device cannot report", new_record, legacy_cap, ok=False, abort=("assert_active", 2))
    u.execute("owner reinstates the device",
              "--move-call", f"{pkg_new}::device_registry::activate_device", f"@{registry}", f"@{legacy}")
    report("reinstated device reports again", new_record, legacy_cap)

    # ── Rotation ──────────────────────────────────────────────────────────────
    rotating, old_key_cap = register(pkg_new, "rotating", dev)
    rotation_record = deploy(pkg_new, "rotation", rotating)
    tx = u.execute("owner rotates the key and issues a cap for it",
                   "--move-call", f"{pkg_new}::device_registry::rotate_device_key", f"@{registry}",
                   f"@{rotating}", f"@{new_dev}",
                   "--move-call", f"{pkg_new}::device_cap::issue_cap", f"@{rotating}")
    new_key_cap = u.created(tx, "::device_cap::DeviceCap")[0]
    report("rotated-out key cannot report", rotation_record, old_key_cap, ok=False, abort=("assert_active", 2))
    report("rotated-in key reports", rotation_record, new_key_cap, signer=new_dev)

    # ── Deregistration ────────────────────────────────────────────────────────
    gone, gone_cap = register(pkg_new, "deregistered", dev)
    gone_record = deploy(pkg_new, "deregistered", gone)
    u.execute("deregister through the upgraded package",
              "--move-call", f"{pkg_new}::device_registry::deregister_device", f"@{registry}", f"@{gone}")
    report("device deregistered through the upgrade cannot report", gone_record, gone_cap,
           ok=False, abort=("assert_active", 2))

    orphan, orphan_cap = register(pkg_new, "orphaned", dev)
    orphan_record = deploy(pkg_new, "orphaned", orphan)
    u.execute("deregister through the prior package (cannot touch identities)",
              "--move-call", f"{pkg_prior}::device_registry::deregister_device", f"@{registry}", f"@{orphan}")
    report("prior-package deregistration leaves the identity active", orphan_record, orphan_cap)
    registry_call("operator revokes the deleted device by ID", "revoke_device", f"@{orphan}")
    report("revoked after prior-package deregistration: cannot report", orphan_record, orphan_cap,
           ok=False, abort=("assert_active", 2))

    failed = [r for r in u.results if not r[0]]
    print(f"\n{len(u.results) - len(failed)}/{len(u.results)} checks passed")
    for _, label, detail, digest in failed:
        print(f"FAILED: {label}: {detail} {digest}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
