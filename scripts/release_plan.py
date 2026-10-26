#!/usr/bin/env python3
"""Reviewable release plans for the SuiFleet contracts (SF-154).

Read-only: it never signs, submits, transfers a capability or reads a key. It
reads the chain over gRPC (chain_facts), this checkout's Move sources and
deployment records, and writes a plan (JSON, plus Markdown for review) that
pins, for one network:

- the revision: commit, contracts source digest and (optionally) the release
  compiler's bytecode digest, and any cached publication of identical bytecode;
- the chain identifier, the custody account and the UpgradeCap it must own;
- the recorded manifest, verified against the chain, and the type origins;
- static compatibility: types and modules on chain that the sources lost
  (blocking), and new types and optional protocol groups that need migration
  review;
- client generation from the recorded manifest, the cost/exposure limits of
  manifests/release-policy.json, the rollout stages, and abort boundaries.

A plan is ready only without blocking findings. scripts/publish.py --plan
admits a transaction only for a ready, unexpired, untampered plan matching the
live chain, account, deployment, sources and gas budget (required on testnet).
Mainnet plans are planning only: the policy forbids their execution and
publish.py keeps refusing mainnet; actual production execution is SF-087/#159.

  release_plan.py plan --network NET --rpc URL --custody ADDRESS --out PLAN.json [--markdown PLAN.md] ...
  release_plan.py check PLAN.json     # integrity, readiness and expiry; prints the summary
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deployment_manifest as dm

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "manifests/release-policy.json"
PINNED = {"mainnet": "35834a8a", "testnet": "4c78adac"}
NETWORKS = ("localnet", "devnet", "testnet", "mainnet")
EXECUTION = ("forbidden", "rehearsal", "disposable")
SCHEMA = 1
# Files that make up the published package's source. Published.toml is the
# CLI's output record, and build/ is derived; neither changes the bytecode.
SOURCE_GLOBS = ("Move.toml", "Move.lock", "sources/**/*.move")

ABORT_BOUNDARIES = [
    {"phase": "before the transaction",
     "boundary": "publish.py checks chain, account, UpgradeCap, records and this plan; any refusal changes nothing and spends no gas"},
    {"phase": "transaction executed with failed effects",
     "boundary": "gas is charged; package, records, Published.toml and client configs are unchanged"},
    {"phase": "transaction executed, recording failed",
     "boundary": "the chain has changed and cannot be rolled back; deployments/ENV/pending-DIGEST.json is kept and "
                 "`redeploy.sh --recover` finishes recording without another transaction; prior configs stay active"},
    {"phase": "activation interrupted",
     "boundary": "the journal rolls forward to the verified release (`redeploy.sh --reconcile`); make dapp-build refuses meanwhile"},
    {"phase": "after activation",
     "boundary": "packages are immutable and older versions stay callable; there is no on-chain rollback. The superseded "
                 "release is kept under releases/ but every client rejects it as stale, so recovery is a reviewed "
                 "forward-fix upgrade under a new plan"},
    {"phase": "rollout",
     "boundary": "halt the current stage on any halt condition; devices keep their journals and keys; never force replay by deleting state"},
]
REHEARSAL_CHECKS = [
    "offline: make test-publish (publisher, release planner and admission gate)",
    "isolated chain: make test-release-rehearsal-localnet (plan, publish, compatible upgrade, refusals, both clients)",
    "old-package entry points: scripts/identity_lifecycle_localnet.py into the rehearsed head (part of the "
    "rehearsal), the historical receipt/identity suites (scripts/test_move_security_localnet.py) and the automatic "
    "testnet upgrade-security gate (docs/TESTNET.md) for the exact head",
    "package upgrade on testnet: the automatic package-upgrade gate, reusing cached identical bytecode",
]
OUTSTANDING = [
    "Actual mainnet publication/upgrade, UpgradeCap custody transfer and mainnet end-to-end acceptance: SF-087/#159, "
    "only under explicit user authorization for that run",
    "Production release signing environment, trust roots and device trust integration: SF-042/#62",
    "Physical Pi/target rollout, reboot and power-loss acceptance: SF-031/#32",
]


class PlanError(Exception):
    pass


class BuildFailed(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest_of(plan):
    body = {k: v for k, v in plan.items() if k != "plan_digest"}
    return "sha256:" + hashlib.sha256(canonical(body).encode()).hexdigest()


# ── Inputs ────────────────────────────────────────────────────────────────────

def load_policy(path):
    policy = json.loads(Path(path).read_text())
    if policy.get("schema_version") != 1 or set(policy.get("networks", {})) != set(NETWORKS):
        raise PlanError(f"{path}: release policy must be schema 1 and cover {', '.join(NETWORKS)}")
    for name, net in policy["networks"].items():
        if net.get("execution") not in EXECUTION:
            raise PlanError(f"{path}: {name}.execution must be one of {EXECUTION}")
        limits = [net.get(k) for k in ("max_gas_budget_mist", "max_run_spend_mist", "plan_validity_seconds")]
        if any(type(v) is not int or v <= 0 for v in limits) or limits[0] > limits[1]:
            raise PlanError(f"{path}: {name} needs positive limits with max_gas_budget_mist <= max_run_spend_mist")
        if type(net.get("require_bytecode_digest")) is not bool:
            raise PlanError(f"{path}: {name}.require_bytecode_digest must be a boolean")
        net["forbidden_custody"] = [dm.object_id(a) for a in net.get("forbidden_custody", [])]
    if policy["networks"]["mainnet"]["execution"] != "forbidden":
        raise PlanError(f"{path}: mainnet execution must stay forbidden in this pipeline (SF-087/#159)")
    estimates = policy.get("estimates_mist", {})
    if any(type(estimates.get(k)) is not int or estimates[k] <= 0 for k in ("publish", "upgrade")):
        raise PlanError(f"{path}: estimates_mist needs positive publish and upgrade")
    stages = policy.get("rollout", {}).get("stages", [])
    fractions = [s.get("max_fraction") for s in stages if "max_fraction" in s]
    if (not stages or any(not s.get("name") or type(s.get("min_soak_minutes")) is not int for s in stages)
            or any(not isinstance(f, (int, float)) or not 0 < f <= 1 for f in fractions)
            or fractions != sorted(fractions) or stages[-1].get("max_fraction") != 1):
        raise PlanError(f"{path}: rollout stages must be named, soaked, increasing and end at the whole fleet")
    return policy


def git(*args, cwd=ROOT):
    result = subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True)
    if result.returncode:
        raise PlanError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def source_files(package):
    files = set()
    for pattern in SOURCE_GLOBS:
        files.update(p for p in Path(package).glob(pattern) if p.is_file() and "build" not in p.relative_to(package).parts)
    return sorted(files)


def source_digest(package):
    """SHA-256 over the package's source files (path and bytes), so a plan is
    bound to exactly the sources it reviewed."""
    package = Path(package)
    h = hashlib.sha256()
    for path in source_files(package):
        name = path.relative_to(package).as_posix().encode()
        data = path.read_bytes()
        h.update(len(name).to_bytes(4, "big") + name + len(data).to_bytes(8, "big") + data)
    return "sha256:" + h.hexdigest()


def source_types(package):
    """module::Type for every struct/enum the sources declare."""
    found = set()
    for path in sorted(Path(package).glob("sources/**/*.move")):
        module = None
        for line in path.read_text().splitlines():
            line = line.split("//", 1)[0]
            declared = re.match(r"\s*module\s+\w+::(\w+)", line)
            if declared:
                module = declared[1]
                continue
            datatype = re.match(r"\s*(?:public\s+)?(?:struct|enum)\s+([A-Za-z_]\w*)", line)
            if datatype and module:
                found.add(f"{module}::{datatype[1]}")
    return found


def revision(package, allow_dirty):
    package = Path(package).resolve()
    commit = git("rev-parse", "HEAD", cwd=package)
    dirty = git("status", "--porcelain", "--", "Move.toml", "Move.lock", "sources", cwd=package)
    if dirty and not allow_dirty:
        raise PlanError(f"uncommitted contract sources; commit them or pass --allow-dirty for a scratch plan:\n{dirty}")
    return {"commit": commit, "contracts_source_digest": source_digest(package), "uncommitted_sources": bool(dirty)}


def bytecode(args):
    """The release compiler's package digest, given or computed."""
    if args.bytecode_digest:
        value = args.bytecode_digest.removeprefix("bytecode-v1:").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise PlanError("--bytecode-digest must be 64 hex digits")
        return {"digest": value, "compiler": args.compiler or "operator-supplied"}
    if args.build_tool:
        import sui_toolchain as toolchain
        # A scratch copy of exactly the planned sources: the checkout is never
        # written, and a build that would change Move.lock is refused.
        with tempfile.TemporaryDirectory(prefix="release-plan-build-") as scratch:
            copy = Path(scratch) / "package"
            for path in source_files(args.package):
                target = copy / path.relative_to(args.package)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
            try:
                built = toolchain.compile_package(args.build_tool, copy, environment=args.build_env or args.env or args.network)
            except RuntimeError as error:
                raise BuildFailed(str(error))
        version = subprocess.run([str(args.build_tool), "--version"], text=True, capture_output=True).stdout.strip()
        return {"digest": bytes(built["digest"]).hex(), "compiler": version, "modules": len(built["modules"]),
                "dependencies": built["dependencies"]}
    return None


def cached_publication(state_path, digest, chain):
    """A fixture whose verified bytecode equals this digest: identical code is
    already published on this chain and must be reused, never republished."""
    if not state_path or not digest:
        return None
    state = json.loads(Path(state_path).read_text())
    if state.get("chain") != chain:
        return None
    for key, fixture in sorted(state.get("fixtures", {}).items()):
        verified = (fixture.get("provenance") or {}).get("verified_transactions") or {}
        for role, package in (("upgrade", "head_package"), ("base", "base_package"), ("publish", "base_package")):
            if (verified.get(role) or {}).get("digest") == digest and fixture.get(package):
                return {"fixture": key, "role": role, "package": dm.object_id(fixture[package]),
                        "upgrade_cap": dm.object_id(fixture["upgrade_cap"]), "registry": dm.object_id(fixture["registry"])}
    return None


def declared_chain(package, env):
    import publish
    return publish.declared_chain(Path(package), env)


# ── Planning ──────────────────────────────────────────────────────────────────

def plan(args, read=None, now=time.time):
    import publish
    read = read or dm.chain_facts
    policy = load_policy(args.policy)
    limits = policy["networks"][args.network]
    env = args.env or args.network
    findings = []

    def finding(severity, code, detail):
        findings.append({"severity": severity, "code": code, "detail": detail})

    custody = dm.object_id(args.custody)
    chain = read(args.rpc)["chain_identifier"]
    if args.network in PINNED and chain != PINNED[args.network]:
        raise PlanError(f"{args.rpc} serves chain {chain}, not {args.network} ({PINNED[args.network]})")
    if args.network not in PINNED and chain in PINNED.values():
        raise PlanError(f"{args.rpc} serves public chain {chain}; label it {args.network!r} is refused")
    declared = declared_chain(args.package, env)
    if env not in PINNED and declared is None:
        finding("blocking", "environment-undeclared", f"Move.toml declares no environment {env!r} for chain {chain}")
    elif declared is not None and declared != chain:
        finding("blocking", "environment-chain", f"Move.toml declares {env} = {declared!r}, but the chain is {chain}")
    if custody == dm.object_id("0x0"):
        finding("blocking", "custody-unset", "no custody account: name the account that owns the UpgradeCap and signs")
    elif custody in limits["forbidden_custody"]:
        finding("blocking", "custody-forbidden", f"{custody} is a development/test wallet; it may not hold {args.network} release custody")

    source = revision(args.package, args.allow_dirty)
    if source["uncommitted_sources"]:
        finding("blocking" if args.network in PINNED else "warning", "uncommitted-sources",
                "the plan reviews uncommitted contract sources")
    try:
        built, build_failed = bytecode(args), False
    except BuildFailed as error:
        built, build_failed = None, True
        lock = "Move.lock lacks dependency pins for this environment; " if "changed Move.lock" in str(error) else ""
        finding("blocking", "bytecode-build", f"{lock}the release compiler could not build the planned sources: "
                                              f"{str(error).strip().splitlines()[-1][:300]}")
    if built is None and not build_failed and limits["require_bytecode_digest"]:
        finding("blocking", "bytecode-unpinned", f"{args.network} plans must pin the release compiler's bytecode digest")
    cached = cached_publication(args.fixture_state, built and built["digest"], chain)

    records, package_dir = Path(args.records), Path(args.package)
    recorded = publish.current_manifest(records, env)
    published = publish.publication(package_dir, env)
    head_types = source_types(package_dir)
    deployment, compatibility = None, {"removed_types": [], "removed_modules": [], "new_types": sorted(head_types),
                                       "new_optional_groups": [], "migrations": []}
    target = None
    if args.package_id:
        if published or recorded:
            raise PlanError("--package-id adopts an unrecorded deployment; this environment already records one")
        if not (args.upgrade_cap and args.registry):
            raise PlanError("--package-id needs --upgrade-cap and --registry")
        target = dict(package=dm.object_id(args.package_id), cap=dm.object_id(args.upgrade_cap),
                      registry=dm.object_id(args.registry), version=None)
    elif published:
        if published["chain"] != chain:
            raise PlanError(f"[published.{env}] is for chain {published['chain']}, not {chain}; the network may have reset")
        registry = recorded["shared_objects"]["registry"]["id"] if recorded else args.registry
        if registry is None:
            raise PlanError(f"no recorded deployment for {env}; pass --registry ID")
        target = dict(package=published["package"], cap=published["cap"], registry=dm.object_id(registry),
                      version=published["version"])
        if recorded and (recorded["call_package_id"], recorded["upgrade_cap_id"]) != (published["package"], published["cap"]):
            finding("blocking", "records-disagree", f"deployments/{env}/current does not match [published.{env}]")
    elif recorded:
        finding("blocking", "orphan", f"deployments/{env}/current records a deployment but Published.toml has no "
                                      f"[published.{env}]; a fresh publish would orphan its fleet")

    if target:
        action = "upgrade"
        objects = read(args.rpc, target["package"], target["cap"], target["registry"])["objects"]
        package = objects[target["package"]]
        if package.get("kind") != "package":
            raise PlanError(f"{target['package']} is not a package on chain {chain}")
        if target["version"] is not None and int(package["version"]) != target["version"]:
            finding("blocking", "package-version", f"chain has {target['package']} at v{package['version']}, "
                                                   f"Published.toml records v{target['version']}")
        version = int(package["version"])
        try:
            cap_package, cap_version, owner = dm.upgrade_cap(objects[target["cap"]])
        except (ValueError, KeyError) as error:
            raise PlanError(f"UpgradeCap {target['cap']}: {error}")
        if (cap_package, cap_version) != (target["package"], str(version)):
            finding("blocking", "cap-stale", f"UpgradeCap identifies {cap_package} v{cap_version}, not the latest "
                                             f"{target['package']} v{version}; another upgrade was chained elsewhere")
        if owner is None:
            finding("blocking", "cap-custody", f"UpgradeCap {target['cap']} is not owned by an address "
                                               f"({objects[target['cap']].get('owner')})")
        elif dm.object_id(owner) != custody:
            finding("blocking", "custody-mismatch", f"UpgradeCap is owned by {dm.object_id(owner)}, not custody {custody}")
        origins = dm.origins(package)
        try:
            registry_version = dm.shared_version(objects[target["registry"]], origins["device_registry::DeviceRegistry"])
        except (ValueError, KeyError) as error:
            raise PlanError(f"registry {target['registry']}: {error}")
        if recorded:
            try:
                dm.verify(recorded, read)
            except (ValueError, KeyError) as error:
                finding("blocking", "manifest-stale", f"recorded manifest fails verification: {error}")
        chain_types = set(origins)
        chain_modules = {name.split("::")[0] for name in chain_types}
        compatibility["removed_types"] = sorted(chain_types - head_types)
        compatibility["removed_modules"] = sorted(chain_modules - {n.split("::")[0] for n in head_types})
        compatibility["new_types"] = sorted(head_types - chain_types)
        for name in compatibility["removed_types"]:
            finding("blocking", "type-removed", f"{name} is on chain but not in the sources; a compatible upgrade keeps every type")
        for label, group in dm.OPTIONAL_GROUPS.items():
            present = [n for n in group if n in chain_types]
            if present and len(present) != len(group):
                missing = sorted(set(group) - set(present))
                if set(missing) <= head_types:
                    compatibility["new_optional_groups"].append(label)
                else:
                    finding("blocking", "partial-group", f"{label} types are partly on chain and partly missing: {missing}")
            elif not present and set(group) <= head_types:
                compatibility["new_optional_groups"].append(label)
        for name in compatibility["new_types"]:
            compatibility["migrations"].append(
                {"type": name, "origin": "the upgrade package",
                 "review": "existing objects gain no fields or dynamic fields automatically; record whether this type "
                           "needs an explicit migration call or is created on demand"})
        if compatibility["new_types"]:
            finding("warning", "migration-review", f"{len(compatibility['new_types'])} new type(s) originate in the upgrade; "
                                                   "review compatibility.migrations")
        deployment = {"call_package_id": target["package"], "package_version": version, "upgrade_cap_id": target["cap"],
                      "upgrade_cap_owner": owner and dm.object_id(owner), "registry_id": target["registry"],
                      "registry_initial_shared_version": registry_version, "type_origins": dict(sorted(origins.items())),
                      "recorded_manifest": recorded is not None}
        if not recorded:
            finding("warning", "adopt", "no recorded manifest for this deployment; executing adopts it with --registry")
    else:
        action = "publish"
    if cached:
        finding("blocking" if args.network in PINNED else "warning", "identical-bytecode",
                f"identical bytecode is already published at {cached['package']} ({cached['fixture']}); reuse it, never republish")

    previous_plan = recorded and records / env / "current" / "release-plan.json"
    if previous_plan and previous_plan.exists() and built:
        if (json.loads(previous_plan.read_text()).get("revision", {}).get("bytecode") or {}).get("digest") == built["digest"]:
            finding("blocking" if args.network in PINNED else "warning", "identical-bytecode",
                    "the current release already published this exact bytecode; never republish identical code")

    clients = {"generated": False}
    if recorded:
        with tempfile.TemporaryDirectory(prefix="release-plan-") as scratch:
            try:
                template = Path(scratch) / "device.toml"
                template.write_text(publish.device_template(Path(args.device_template)))
                dm.generate(recorded, Path(scratch) / "bundle", template)
                clients = {"generated": True, "files": sorted(p.name for p in (Path(scratch) / "bundle").iterdir())}
            except (ValueError, KeyError, OSError) as error:
                finding("blocking", "client-config", f"client configs cannot be generated from the recorded manifest: {error}")
    clients["client_versions"] = dict(dm.CLIENT_VERSIONS)
    clients["after_execution"] = ("publish.py resolves the new manifest from the chain, writes deployment.json, "
                                  "frontend.env and config.toml under deployments/ENV/releases/, then activates both")

    budget = args.gas_budget or limits["max_gas_budget_mist"]
    estimate = policy["estimates_mist"][action]
    if budget > limits["max_gas_budget_mist"]:
        finding("blocking", "budget-exceeds-policy", f"gas budget {budget} exceeds the {args.network} limit {limits['max_gas_budget_mist']}")
    if max(budget, estimate) > limits["max_run_spend_mist"]:
        finding("blocking", "spend-exceeds-policy", f"worst case {max(budget, estimate)} exceeds the run limit {limits['max_run_spend_mist']}")

    if limits["execution"] == "forbidden":
        executable, reason = False, ("mainnet execution is outside this pipeline: publish.py refuses mainnet and actual "
                                     "production execution requires explicit user authorization under SF-087/#159")
    elif args.package_id:
        executable, reason = False, (f"planning only: this checkout has no [published.{env}] record of the adopted "
                                     "package, so publish.py would not upgrade it")
    else:
        executable, reason = True, f"{limits['execution']} execution through scripts/redeploy.sh with this plan"
    common = (f"--expect-address {custody} --plan PLAN.json --gas-budget {budget}"
              + (f" --network {args.network}" if args.network not in PINNED else "")
              + (f" --registry {target['registry']}" if target and not recorded else ""))
    created = int(now())
    result = {
        "schema_version": SCHEMA,
        "kind": "suifleet-release-plan",
        "created_at": created,
        "expires_at": created + limits["plan_validity_seconds"],
        "network": args.network, "environment": env, "chain_identifier": chain, "rpc_url": args.rpc,
        "action": action, "custody": {"address": custody, "upgrade_cap": target and target["cap"],
                                      "transfer": "never performed by this pipeline"},
        "revision": dict(source, bytecode=built, cached_publication=cached),
        "deployment": deployment,
        "compatibility": compatibility,
        "clients": clients,
        "cost": {"gas_budget_mist": budget, "estimate_mist": estimate,
                 "max_gas_budget_mist": limits["max_gas_budget_mist"], "max_run_spend_mist": limits["max_run_spend_mist"],
                 "storage": "package storage is never refunded; failed transactions are still charged",
                 "exposure": "one package publication/upgrade signed by the custody account; no capability transfer, "
                             "no device or production credential operations"},
        "execution": {"policy": limits["execution"], "allowed": executable,
                      "reason": reason,
                      "commands": [f"scripts/redeploy.sh {common} --dry-run", f"scripts/redeploy.sh {common}"]
                      if executable else []},
        "rollout": policy["rollout"],
        "abort_boundaries": ABORT_BOUNDARIES,
        "rehearsal_checks": REHEARSAL_CHECKS,
        "outstanding_production_checks": OUTSTANDING,
        "findings": findings,
        "ready": not any(f["severity"] == "blocking" for f in findings),
    }
    result["plan_digest"] = digest_of(result)
    return result


# ── Checking and admission ────────────────────────────────────────────────────

def check(plan_value, now=None):
    """Integrity and readiness of a plan someone hands back."""
    if plan_value.get("kind") != "suifleet-release-plan" or plan_value.get("schema_version") != SCHEMA:
        raise PlanError("not a schema-1 SuiFleet release plan")
    if plan_value.get("plan_digest") != digest_of(plan_value):
        raise PlanError("release plan digest does not match its contents; it was edited after planning")
    if not plan_value["ready"]:
        codes = ", ".join(f["code"] for f in plan_value["findings"] if f["severity"] == "blocking")
        raise PlanError(f"release plan is not ready (blocking: {codes})")
    if (now or time.time)() >= plan_value["expires_at"]:
        raise PlanError("release plan has expired; plan again against the current chain")
    return plan_value


def admit(plan_value, *, network, env, chain, address, published, registry, package, gas_budget, now=None):
    """Refuse a transaction the reviewed plan does not describe exactly."""
    check(plan_value, now)
    if not plan_value["execution"]["allowed"]:
        raise PlanError(f"plan forbids execution: {plan_value['execution']['reason']}")
    expected = (network, env, chain, dm.object_id(address))
    planned = (plan_value["network"], plan_value["environment"], plan_value["chain_identifier"],
               plan_value["custody"]["address"])
    if planned != expected:
        raise PlanError(f"plan is for {planned}, not {expected}")
    if plan_value["action"] != ("upgrade" if published else "publish"):
        raise PlanError(f"plan is a {plan_value['action']}, but this run would "
                        f"{'upgrade' if published else 'publish'}; plan again")
    if published:
        current = plan_value["deployment"]
        if (current["call_package_id"], current["package_version"], current["upgrade_cap_id"], current["registry_id"]) != \
           (published["package"], published["version"], published["cap"], registry):
            raise PlanError("the deployment changed since planning; plan again")
    if source_digest(package) != plan_value["revision"]["contracts_source_digest"]:
        raise PlanError("contract sources differ from the planned revision; plan again")
    if gas_budget > plan_value["cost"]["gas_budget_mist"]:
        raise PlanError(f"gas budget {gas_budget} exceeds the planned {plan_value['cost']['gas_budget_mist']}")
    return plan_value


# ── Output ────────────────────────────────────────────────────────────────────

def markdown(p):
    lines = [f"# SuiFleet release plan: {p['action']} on {p['network']}", "",
             f"- Plan digest: `{p['plan_digest']}`",
             f"- Ready: **{'yes' if p['ready'] else 'NO'}**; execution: {p['execution']['policy']} "
             f"({'allowed' if p['execution']['allowed'] else 'not allowed'})",
             f"- Valid: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(p['created_at']))} to "
             f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(p['expires_at']))}",
             f"- Chain: `{p['chain_identifier']}` via {p['rpc_url']} (environment `{p['environment']}`)",
             f"- Custody: `{p['custody']['address']}`; UpgradeCap `{p['custody']['upgrade_cap']}`; transfer: {p['custody']['transfer']}",
             f"- Revision: `{p['revision']['commit']}`; sources `{p['revision']['contracts_source_digest']}`",
             f"- Bytecode: {('`' + p['revision']['bytecode']['digest'] + '` (' + p['revision']['bytecode']['compiler'] + ')') if p['revision']['bytecode'] else 'not pinned'}",
             ""]
    if p["revision"]["cached_publication"]:
        lines += [f"Identical bytecode already published: `{json.dumps(p['revision']['cached_publication'])}`", ""]
    lines += ["## Findings", ""] + ([f"- **{f['severity']}** `{f['code']}`: {f['detail']}" for f in p["findings"]] or ["- none"])
    if p["deployment"]:
        d = p["deployment"]
        lines += ["", "## Current deployment", "",
                  f"- Package `{d['call_package_id']}` v{d['package_version']}; UpgradeCap `{d['upgrade_cap_id']}` "
                  f"owned by `{d['upgrade_cap_owner']}`",
                  f"- Registry `{d['registry_id']}` (initial shared version {d['registry_initial_shared_version']})",
                  f"- Type origins: {len(d['type_origins'])} on chain; recorded manifest: {d['recorded_manifest']}"]
    c = p["compatibility"]
    lines += ["", "## Compatibility and migration", "",
              f"- Removed types: {c['removed_types'] or 'none'}; removed modules: {c['removed_modules'] or 'none'}",
              f"- New types ({len(c['new_types'])}): {', '.join(c['new_types']) or 'none'}",
              f"- New optional protocol groups: {', '.join(c['new_optional_groups']) or 'none'}",
              "- Static checks only: the chain's upgrade verifier (dry run, rehearsal) remains authoritative for layouts and signatures.",
              "", "## Cost and exposure", "",
              f"- Gas budget {p['cost']['gas_budget_mist']} MIST (policy max {p['cost']['max_gas_budget_mist']}); "
              f"estimate {p['cost']['estimate_mist']}; run limit {p['cost']['max_run_spend_mist']}",
              f"- {p['cost']['storage']}", f"- {p['cost']['exposure']}",
              "", "## Execution", "", f"- {p['execution']['reason']}"]
    lines += [f"- `{command}`" for command in p["execution"]["commands"]]
    lines += ["", "## Clients", "", f"- From the recorded manifest: {'generated ' + ', '.join(p['clients'].get('files', [])) if p['clients']['generated'] else 'none recorded'}",
              f"- {p['clients']['after_execution']}", "", "## Rollout", ""]
    lines += [f"- {s['name']}: " + (f"{s['max_devices']} device(s)" if "max_devices" in s else f"up to {s['max_fraction']:.0%} of the fleet")
              + f", soak {s['min_soak_minutes']} min" for s in p["rollout"]["stages"]]
    lines += [f"- Halt on: {h}" for h in p["rollout"]["halt_on"]]
    lines += ["", "## Abort and rollback boundaries", ""] + [f"- {b['phase']}: {b['boundary']}" for b in p["abort_boundaries"]]
    lines += ["", "## Rehearsal checks", ""] + [f"- {r}" for r in p["rehearsal_checks"]]
    lines += ["", "## Outstanding production checks (not run by this plan)", ""] + [f"- {o}" for o in p["outstanding_production_checks"]]
    return "\n".join(lines) + "\n"


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    make = sub.add_parser("plan", help="write a release plan (read-only)")
    make.add_argument("--network", choices=NETWORKS, required=True)
    make.add_argument("--rpc", required=True, help="gRPC fullnode URL for read-only chain facts")
    make.add_argument("--custody", required=True, help="account that must own the UpgradeCap and sign")
    make.add_argument("--env", help="Sui CLI environment alias (default: the network)")
    make.add_argument("--package", type=Path, default=ROOT / "contracts")
    make.add_argument("--records", type=Path, default=ROOT / "deployments")
    make.add_argument("--policy", type=Path, default=POLICY)
    make.add_argument("--device-template", type=Path, default=ROOT / "suifleet-edge/config.example.toml")
    make.add_argument("--package-id", help="adopt an unrecorded deployment: its latest package")
    make.add_argument("--upgrade-cap", help="adopt: its UpgradeCap")
    make.add_argument("--registry", help="adopt: its shared DeviceRegistry")
    make.add_argument("--gas-budget", type=int, help="MIST (default: the policy maximum)")
    make.add_argument("--bytecode-digest", help="release compiler package digest (hex)")
    make.add_argument("--compiler", help="compiler that produced --bytecode-digest")
    make.add_argument("--build-tool", type=Path, help="compile with this pinned release `sui` instead")
    make.add_argument("--build-env", help="Move build environment (default: --env)")
    make.add_argument("--fixture-state", type=Path, help="testnet fixture cache to find identical published bytecode")
    make.add_argument("--allow-dirty", action="store_true", help="plan uncommitted sources (blocking on public networks)")
    make.add_argument("--out", type=Path, required=True)
    make.add_argument("--markdown", type=Path)
    verify = sub.add_parser("check", help="check a plan's integrity, readiness and expiry")
    verify.add_argument("plan", type=Path)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = plan(args)
            with args.out.open("x") as out:
                out.write(json.dumps(result, indent=2) + "\n")
            if args.markdown:
                with args.markdown.open("x") as out:
                    out.write(markdown(result))
        else:
            result = check(json.loads(args.plan.read_text()))
    except (PlanError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    blocking = [f["code"] for f in result["findings"] if f["severity"] == "blocking"]
    print(f"{result['action']} plan for {result['network']} ({result['chain_identifier']}): "
          f"{'ready' if result['ready'] else 'NOT READY: ' + ', '.join(blocking)}; "
          f"execution {'allowed' if result['execution']['allowed'] else 'forbidden'}; {result['plan_digest']}")
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    sys.exit(main())
