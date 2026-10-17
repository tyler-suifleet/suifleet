#!/usr/bin/env python3
"""Old-package bypass acceptance test on a local Sui network (SF-001).

Publishes a prior package, creates objects with it, upgrades to the current
contracts and then calls the *old* package's entry points directly. Passes only
if those calls cannot change authoritative status receipts, judged from on-chain
execution effects and object state rather than returned digests.

Run inside the dev image against a throwaway localnet and client config:

    git archive <v1-ref> contracts | tar -x -C /tmp/v1     # on the host
    SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
    SUI_CONFIG_DIR=/tmp/sui-config sui client switch --env local
    SUI_CONFIG_DIR=/tmp/sui-config python3 scripts/upgrade_security_localnet.py \
        --v1 /tmp/v1/contracts --v2 contracts

It refuses to run unless the active environment's RPC is on localhost.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import urllib.request
from urllib.parse import urlparse

CLOCK = "@0x6"
APPLIED, FAILED = 1, 2
RECIPE_SHA = "ab" * 32
RPC_URL = ""
results = []


def sui(*args, check=True):
    proc = subprocess.run(["sui", "client", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.exit(f"sui client {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc


def sui_json(*args):
    out = sui(*args, "--json").stdout
    return json.loads(out[out.index("{") if out.lstrip().startswith("{") else out.index("["):])


def require_localnet():
    envs, active = sui_json("envs")
    url = next(e["rpc"] for e in envs if e["alias"] == active)
    if urlparse(url).hostname not in {"127.0.0.1", "localhost"}:
        sys.exit(f"Refusing to run: active env {active} uses {url}, not a localnet")
    output = sui("chain-identifier").stdout.strip()
    # Sui 1.81.1 prints both Base58 and Hex; the manifest compares the
    # canonical eight-digit hex identifier, not the formatted CLI display.
    match = re.fullmatch(r"[0-9a-fA-F]{8}", output) or re.search(r"(?m)^Hex: ([0-9a-fA-F]{8})$", output)
    if not match:
        sys.exit("Unrecognized local chain identifier output")
    chain = (match.group(1) if match.lastindex else match.group()).lower()
    return active, url, chain


def rpc(method, params, url=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url or RPC_URL, body, {"Content-Type": "application/json"})
    reply = json.load(urllib.request.urlopen(req))
    if "error" in reply:
        sys.exit(f"{method}: {reply['error']}")
    return reply["result"]


def address(alias):
    known = {a[0]: a[1] for a in sui_json("addresses")["addresses"]}
    if alias not in known:
        known[alias] = sui_json("new-address", "ed25519", alias)["address"]
    return known[alias]


def fund(addr):
    sui("faucet", "--address", addr)


def use(addr):
    sui("switch", "--address", addr)


def execute(label, *ptb, expect_success=True, abort=None, error_text=None):
    """Submit for real (explicit budget skips the CLI's dry-run gate) and judge
    the transaction's on-chain effects, fetched from the node by digest."""
    proc = sui("ptb", *ptb, "--gas-budget", "100000000", "--json", check=False)
    output = proc.stdout + proc.stderr
    try:
        digest = json.loads(proc.stdout)["digest"]
    except (ValueError, KeyError):
        # The CLI reports executed-but-aborted transactions as text.
        found = re.search(r"transaction '([1-9A-HJ-NP-Za-km-z]{32,44})'", output)
        digest = found.group(1) if found else ""
    if digest:
        tx = rpc("sui_getTransactionBlock", [digest, {"showEffects": True, "showObjectChanges": True}])
        status = tx["effects"]["status"]
        ok, error = status["status"] == "success", status.get("error", "")
    else:
        # Rejected before execution (e.g. function absent from that package version).
        ok, error, tx = False, "not executed: " + output.strip()[-300:], None
    passed = ok == expect_success
    if abort and not ok:
        # Only the intended check counts; e.g. a type mismatch must not pass.
        function, code = abort
        passed = passed and f'function_name: Some("{function}") }}, {code})' in error
    if error_text and not ok:
        passed = passed and error_text in error
    results.append((passed, label, "success" if ok else f"failure: {error}", digest))
    print(f"[{'PASS' if passed else 'FAIL'}] {label}: {'success' if ok else 'aborted'} {digest}")
    if not ok:
        print(f"       {error}")
    return tx


def created(tx, suffix):
    return [c["objectId"] for c in tx["objectChanges"]
            if c["type"] == "created" and c["objectType"].endswith(suffix)]


def published_package(tx):
    return next(c["packageId"] for c in tx["objectChanges"] if c["type"] == "published")


def fields_of(object_id):
    """Map dynamic-field key type name -> (full key type, value)."""
    out = {}
    for entry in rpc("suix_getDynamicFields", [object_id, None, 50])["data"]:
        key_type = entry["name"]["type"]
        obj = rpc("sui_getObject", [entry["objectId"], {"showContent": True}])
        out[key_type.rsplit("::", 1)[-1]] = (key_type, obj["data"]["content"]["fields"]["value"])
    return out


def owned(addr, type_suffix):
    page = rpc("suix_getOwnedObjects", [addr, {"options": {"showType": True, "showContent": True}}, None, 50])
    return [o["data"] for o in page["data"] if o["data"]["type"].endswith(type_suffix)]


def vecmap(value):
    return {e["fields"]["key"]: int(e["fields"]["value"]) for e in value["fields"]["contents"]}


def check(label, condition, detail=""):
    results.append((bool(condition), label, detail, ""))
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}")


def stage(src, dst):
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("build", "Published.toml"))


def publish(path, pubfile, upgrade_cap=None):
    args = ["test-upgrade", path, "--upgrade-capability", upgrade_cap] if upgrade_cap else ["test-publish", path]
    proc = sui(*args, "--build-env", "testnet", "--pubfile-path", pubfile,
               "--gas-budget", "500000000", "--json", "--silence-warnings")
    out = proc.stdout
    tx = json.loads(out[out.index("{"):])
    if tx["effects"]["status"]["status"] != "success":
        sys.exit(f"{args[0]} failed: {tx['effects']['status']}")
    return tx


def register(pkg, registry, name, device_addr, operator):
    tx = execute(f"register {name} via {pkg[:10]}",
                 "--move-call", f"{pkg}::device_registry::register_device",
                 f"@{registry}", f'"{name}"', f"@{device_addr}", '"aarch64"', CLOCK,
                 "--assign", "device",
                 "--move-call", f"{pkg}::device_cap::issue_cap", "device",
                 "--transfer-objects", "[device]", f"@{operator}")
    return created(tx, "::device_registry::Device")[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--v1", required=True, help="prior contracts package directory")
    ap.add_argument("--v2", required=True, help="upgraded contracts package directory")
    args = ap.parse_args()

    global RPC_URL
    env, RPC_URL, chain = require_localnet()
    print(f"env={env} chain={chain} sui={subprocess.run(['sui', '--version'], capture_output=True, text=True).stdout.strip()}")
    op, dev, atk = address("operator"), address("device"), address("attacker")
    for a in (op, dev, atk):
        fund(a)
    time.sleep(2)
    print(f"operator={op}\ndevice={dev}\nattacker={atk}")

    # The ephemeral pubfile keys publications by source path, so v2 replaces
    # v1's sources in the same directory before upgrading.
    work = tempfile.mkdtemp(prefix="sf001-")
    package = os.path.join(work, "package")
    pubfile = os.path.join(work, "Pub.localnet.toml")
    stage(args.v1, package)

    # ── v1: publish and create legacy state ──────────────────────────────────
    use(op)
    tx = publish(package, pubfile)
    pkg1 = published_package(tx)
    registry = created(tx, "::device_registry::DeviceRegistry")[0]
    upgrade_cap = created(tx, "::package::UpgradeCap")[0]
    print(f"v1 package={pkg1} registry={registry}")

    d_legacy = register(pkg1, registry, "legacy-device", dev, op)
    tx = execute("v1 create_deployment R1 -> legacy-device",
                 "--move-call", f"{pkg1}::deployment_manager::create_deployment",
                 '"fw"', '"1.0.0"', '"recipe-blob"', '"artifact-sha"',
                 "vector[]", f"vector[@{d_legacy}]", CLOCK)
    r1 = created(tx, "::deployment_manager::DeploymentRecord")[0]
    tx = execute("v1 create_deployment R3 -> legacy-device (never genuinely reported)",
                 "--move-call", f"{pkg1}::deployment_manager::create_deployment",
                 '"fw"', '"1.0.1"', '"recipe-blob"', '"artifact-sha"',
                 "vector[]", f"vector[@{d_legacy}]", CLOCK)
    r3 = created(tx, "::deployment_manager::DeploymentRecord")[0]

    # ── Upgrade to v2 ─────────────────────────────────────────────────────────
    stage(args.v2, package)
    tx = publish(package, pubfile, upgrade_cap)
    pkg2 = published_package(tx)
    print(f"v2 package={pkg2}")

    d_new = register(pkg2, registry, "new-device", dev, op)
    # Reissue a signer-bound cap for the legacy device through v2.
    execute("v2 issue_cap (bound) for legacy-device",
            "--move-call", f"{pkg2}::device_cap::issue_cap", f"@{d_legacy}")
    tx = execute("v2 create_deployment_verified R2 -> new-device",
                 "--move-call", f"{pkg2}::deployment_manager::create_deployment_verified",
                 '"fw"', '"2.0.0"', '"recipe-blob"', '"artifact-sha"', f'"{RECIPE_SHA}"',
                 "vector[]", f"vector[@{d_new}]", CLOCK)
    r2 = created(tx, "::deployment_manager::DeploymentRecord")[0]

    # Operator keeps unbound (v1) and bound (v2) caps for devices it does not run.
    tx = execute("operator mints unbound cap for new-device via v1 issue",
                 "--move-call", f"{pkg1}::device_cap::issue", f"@{d_new}",
                 "--assign", "cap", "--transfer-objects", "[cap]", f"@{op}")
    op_cap_v1_new = created(tx, "::device_cap::DeviceCap")[0]
    tx = execute("operator mints unbound cap for legacy-device via v1 issue",
                 "--move-call", f"{pkg1}::device_cap::issue", f"@{d_legacy}",
                 "--assign", "cap", "--transfer-objects", "[cap]", f"@{op}")
    op_cap_v1_legacy = created(tx, "::device_cap::DeviceCap")[0]
    tx = execute("operator mints bound cap for new-device via v2 issue",
                 "--move-call", f"{pkg2}::device_cap::issue", f"@{d_new}",
                 "--assign", "cap", "--transfer-objects", "[cap]", f"@{op}")
    op_cap_v2_new = created(tx, "::device_cap::DeviceCap")[0]

    # ── Attacks ──────────────────────────────────────────────────────────────
    execute("ATTACK operator -> v1 report_status on new record R2",
            "--move-call", f"{pkg1}::deployment_manager::report_status",
            f"@{r2}", f"@{op_cap_v1_new}", f"{APPLIED}u8", expect_success=False, abort=("borrow_child_object_mut", 1))
    execute("ATTACK operator -> v2 report_status with unbound v1 cap on R2",
            "--move-call", f"{pkg2}::deployment_manager::report_status",
            f"@{r2}", f"@{op_cap_v1_new}", f"{APPLIED}u8", expect_success=False, abort=("assert_signer", 1))
    execute("ATTACK operator -> v2 report_status with retained bound cap on R2",
            "--move-call", f"{pkg2}::deployment_manager::report_status",
            f"@{r2}", f"@{op_cap_v2_new}", f"{APPLIED}u8", expect_success=False, abort=("assert_signer", 0))

    use(atk)
    d_atk = register(pkg1, registry, "attacker-device", atk, atk)
    atk_cap = next(c["objectId"] for c in owned(atk, f"{pkg1}::device_cap::DeviceCap")
                   if c["content"]["fields"]["device_id"] == d_atk)
    execute("ATTACK outsider -> v1 report_status on new record R2",
            "--move-call", f"{pkg1}::deployment_manager::report_status",
            f"@{r2}", f"@{atk_cap}", f"{APPLIED}u8", expect_success=False, abort=("borrow_child_object_mut", 1))
    execute("legacy write: outsider -> v1 report_status on legacy record R1",
            "--move-call", f"{pkg1}::deployment_manager::report_status",
            f"@{r1}", f"@{atk_cap}", f"{APPLIED}u8")
    use(op)
    execute("legacy write: operator forges legacy-device via v1 on R3",
            "--move-call", f"{pkg1}::deployment_manager::report_status",
            f"@{r3}", f"@{op_cap_v1_legacy}", f"{APPLIED}u8")
    execute("legacy write: operator forges legacy-device via v1 on R1",
            "--move-call", f"{pkg1}::deployment_manager::report_status",
            f"@{r1}", f"@{op_cap_v1_legacy}", f"{APPLIED}u8")

    # ── Genuine device reports through v2 ────────────────────────────────────
    use(dev)
    bound = {cap["content"]["fields"]["device_id"]: cap["objectId"]
             for cap in owned(dev, f"{pkg1}::device_cap::DeviceCap")
             if "DeviceAddressKey" in fields_of(cap["objectId"])}
    execute("device reports APPLIED on R2 via v2",
            "--move-call", f"{pkg2}::deployment_manager::report_status",
            f"@{r2}", f"@{bound[d_new]}", f"{APPLIED}u8")
    execute("device reports FAILED on legacy R1 via v2",
            "--move-call", f"{pkg2}::deployment_manager::report_status",
            f"@{r1}", f"@{bound[d_legacy]}", f"{FAILED}u8")
    use(op)
    execute("legacy write after receipts: operator forges legacy-device via v1 on R1 again",
            "--move-call", f"{pkg1}::deployment_manager::report_status",
            f"@{r1}", f"@{op_cap_v1_legacy}", f"{APPLIED}u8")
    execute("v1 has no migrate entry point",
            "--move-call", f"{pkg1}::deployment_manager::migrate", f"@{r1}", expect_success=False)

    # ── v3: a later authority upgrade locks out v2 entry points ──────────────
    stage(args.v2, package)
    manager = os.path.join(package, "sources", "deployment_manager.move")
    with open(manager) as fh:
        source = fh.read()
    bumped = source.replace("const RECEIPTS_VERSION: u64 = 1;", "const RECEIPTS_VERSION: u64 = 2;")
    if bumped == source:
        sys.exit("RECEIPTS_VERSION constant not found in v2 sources")
    with open(manager, "w") as fh:
        fh.write(bumped)
    tx = publish(package, pubfile, upgrade_cap)
    pkg3 = published_package(tx)
    print(f"v3 package={pkg3} (RECEIPTS_VERSION=2)")
    execute("anyone -> v3 migrate R2", "--move-call", f"{pkg3}::deployment_manager::migrate", f"@{r2}")
    execute("v2 migrate cannot downgrade R2",
            "--move-call", f"{pkg2}::deployment_manager::migrate", f"@{r2}", expect_success=False, abort=("receipts_mut", 5))
    use(dev)
    execute("LOCKOUT device -> v2 report_status on migrated R2",
            "--move-call", f"{pkg2}::deployment_manager::report_status",
            f"@{r2}", f"@{bound[d_new]}", f"{FAILED}u8", expect_success=False, abort=("receipts_mut", 5))
    execute("device -> v3 report_status on migrated R2",
            "--move-call", f"{pkg3}::deployment_manager::report_status",
            f"@{r2}", f"@{bound[d_new]}", f"{FAILED}u8")
    use(op)

    # ── Authoritative state ──────────────────────────────────────────────────
    f1, f2 = fields_of(r1), fields_of(r2)
    check("R2 has no legacy StatusesKey", "StatusesKey" not in f2, str(sorted(f2)))
    receipts_type, receipts2 = f2["ReceiptsKey"]
    check("ReceiptsKey type originates in the upgrade package", receipts_type.startswith(pkg2),
          receipts_type)
    check("R2 receipts == {new-device: FAILED} at version 2",
          vecmap(receipts2["fields"]["statuses"]) == {d_new: FAILED} and receipts2["fields"]["version"] == "2",
          json.dumps(receipts2["fields"]))
    legacy = vecmap(f1["StatusesKey"][1])
    check("R3 has forged legacy statuses and no receipts",
          "ReceiptsKey" not in fields_of(r3) and vecmap(fields_of(r3)["StatusesKey"][1]) == {d_legacy: APPLIED})
    check("R1 legacy history keeps forged entries", legacy == {d_atk: APPLIED, d_legacy: APPLIED},
          json.dumps(legacy))
    receipts1 = vecmap(f1["ReceiptsKey"][1]["fields"]["statuses"])
    check("R1 receipts == {legacy-device: FAILED} only", receipts1 == {d_legacy: FAILED}, json.dumps(receipts1))

    events = rpc("suix_queryEvents",
                 [{"MoveEventType": f"{pkg2}::deployment_manager::DeviceStatusReported"}, None, 50, False])
    reporters = [e["parsedJson"]["reporter"] for e in events["data"]]
    check("DeviceStatusReported emitted only for the device signer",
          len(reporters) == 3 and set(reporters) == {dev}, json.dumps(reporters))
    legacy_events = rpc("suix_queryEvents",
                        [{"MoveEventType": f"{pkg1}::deployment_manager::DeviceStatusUpdated"}, None, 50, False])
    check("legacy DeviceStatusUpdated still forgeable (4 events, none from device)",
          len(legacy_events["data"]) == 4 and all(e["sender"] != dev for e in legacy_events["data"]),
          str(len(legacy_events["data"])))

    # ── Frontend reader on these records, via per-type manifest origins ─────
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import deployment_manifest as dm
    manifest = dm.resolve(SimpleNamespace(chain_identifier=chain, rpc=RPC_URL, package=pkg3, upgrade_cap=upgrade_cap,
                                          registry=registry, network="localnet", publisher="http://127.0.0.1:31415",
                                          aggregator="http://127.0.0.1:31416"))
    origin = manifest["type_origins"]["deployment_manager::ReceiptsKey"]
    check("manifest resolves ReceiptsKey to the upgrade that introduced it",
          int(origin, 16) == int(pkg2, 16) and int(origin, 16) not in (int(pkg1, 16), int(pkg3, 16)), origin)
    expected = {
        r2: {"verified": True, "statuses": {d_new: FAILED}},
        r1: {"verified": True, "statuses": {d_legacy: FAILED}},  # forged legacy entries ignored
        r3: {"verified": False, "statuses": {}},  # only forged legacy statuses: unverified
    }
    manifest_file, expected_file = os.path.join(work, "manifest.json"), os.path.join(work, "expected.json")
    with open(manifest_file, "w") as fh:
        json.dump(manifest, fh)
    with open(expected_file, "w") as fh:
        json.dump(expected, fh)
    reader = subprocess.run(["node", os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_deployment_receipts.cjs"),
                             "--live", manifest_file, expected_file, op], capture_output=True, text=True)
    check("frontend fetchDeploymentEvents: new verified, legacy receipts only, forged-only legacy unverified",
          reader.returncode == 0, (reader.stdout + reader.stderr).strip()[-600:])

    print(f"\npackages: v1={pkg1} v2={pkg2} v3={pkg3}\nrecords: R1={r1} R2={r2} R3={r3}")
    failed = [r for r in results if not r[0]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
