#!/usr/bin/env python3
"""Publish or upgrade the SuiFleet contracts and generate both client configs (SF-006).

Spends gas and mutates the chain unless --dry-run is given. Before any
transaction it verifies, from the chain itself over gRPC:

- the active Sui CLI environment's chain identifier against Move.toml and
  this environment's own Published.toml section (never another environment's);
- the active account against --expect-address;
- for an upgrade, that the UpgradeCap is owned by that account and identifies
  the published package and version, and that the recorded deployment matches.

It stops on any failed command, failed execution effects or unparseable
metadata. Prior configuration is replaced only after a complete, verified
release exists: each release is recorded under RECORDS/ENV/releases/ with its
manifest (all IDs and type origins), frontend.env and device config.toml,
and RECORDS/ENV/current and the frontend's .env.local are then switched to it
together: both are prepared first and a durable journal names the release
before the two renames. An error from the second rename undoes the first;
an error after both (syncing them) keeps the journal. So does a killed
process: the next run of this tool (or `--reconcile`) finishes the switch
before anything else, and `make dapp-build` refuses to build until it has. If anything fails after the transaction executed, the
transaction is kept in RECORDS/ENV/pending-*.json and `--recover FILE`
finishes recording it without another transaction.

A reviewed release plan (scripts/release_plan.py, docs/RELEASE_PLANNING.md)
given with --plan must describe this exact transaction: network, chain,
account, deployment, contract sources and gas budget. Testnet publication
requires one; the plan is kept with the pending record and the release.
When the plan pins a bytecode digest (required on testnet), the transaction
is built unsigned, decoded and verified before signing: its own modules and
dependencies must have the planned package digest (and an upgrade must
authorize that digest), with the expected sender, gas budget, UpgradeCap and
command shape. Only those verified bytes are signed and executed, and this
tool then records [published.ENV] from the verified effects (the CLI writes
Published.toml only when it builds and signs in one step). The bytes are
signed with `sui keytool sign` and executed with `execute-signed-tx`;
`serialized-tx` may rebuild the gas data and is used only for the dry run.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deployment_manifest as dm
import release_plan
import sui_toolchain as toolchain

ROOT = Path(__file__).resolve().parents[1]
PINNED = {"35834a8a": "mainnet", "4c78adac": "testnet"}
WALRUS_DEFAULTS = {"testnet": ("https://publisher.walrus-testnet.walrus.space",
                               "https://aggregator.walrus-testnet.walrus.space")}
# Frontend variables this tool owns; every other line of .env.local is kept.
# The NEXT_PUBLIC_*_ID/NETWORK lines are the pre-manifest format, now unused.
OWNED_ENV = ("NEXT_PUBLIC_DEPLOYMENT_MANIFEST", "NEXT_PUBLIC_PACKAGE_ID", "NEXT_PUBLIC_ORIGINAL_PACKAGE_ID",
             "NEXT_PUBLIC_REGISTRY_ID", "NEXT_PUBLIC_NETWORK")


# Present only while (or because) an activation was interrupted; see activate().
JOURNAL = "activating.json"


class PublishError(Exception):
    pass


# ── Metadata ──────────────────────────────────────────────────────────────────

def parse_toml(text, name):
    """The TOML subset Move.toml and Published.toml use: [table] headers and
    key = "string" | integer | boolean | {inline table, kept raw}. Anything
    else is an error rather than a guess."""
    tables = {}
    current = tables
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = re.fullmatch(r"\[\s*([A-Za-z0-9_.-]+)\s*\](\s*#.*)?", line)
        if header:
            current = tables
            for part in header[1].split("."):
                current = current.setdefault(part, {})
                if not isinstance(current, dict):
                    raise PublishError(f"{name}:{number}: table redefines a value")
            continue
        pair = re.fullmatch(r'([A-Za-z0-9_-]+|"[^"]+")\s*=\s*(.+?)\s*(#[^"]*)?', line)
        if not pair:
            raise PublishError(f"{name}:{number}: unsupported TOML: {line}")
        key, value = pair[1].strip('"'), pair[2]
        if re.fullmatch(r'"[^"\\]*"', value):
            parsed = value[1:-1]
        elif re.fullmatch(r"-?[0-9]+", value):
            parsed = int(value)
        elif value in ("true", "false"):
            parsed = value == "true"
        elif re.fullmatch(r"\{[^{}]*\}", value):
            parsed = value
        else:
            raise PublishError(f"{name}:{number}: unsupported TOML value: {value}")
        if key in current:
            raise PublishError(f"{name}:{number}: duplicate key {key}")
        current[key] = parsed
    return tables


def publication(package, env):
    """This environment's [published.ENV] section, or None. Other
    environments' sections are never read."""
    path = package / "Published.toml"
    if not path.exists():
        return None
    section = parse_toml(path.read_text(), str(path)).get("published", {}).get(env)
    if section is None:
        return None
    try:
        return dict(chain=str(section["chain-id"]), package=dm.object_id(section["published-at"]),
                    original=dm.object_id(section["original-id"]), version=int(section["version"]),
                    cap=dm.object_id(section["upgrade-capability"]))
    except (KeyError, ValueError, TypeError) as error:
        raise PublishError(f"{path}: malformed [published.{env}] section: {error!r}")


def record_publication(package, env, chain, published_at, original, version, cap, toolchain_version):
    """Write this environment's [published.ENV] section, keeping every other
    section byte for byte. Used where the verified bytes were signed with
    `execute-signed-tx`, which (unlike `sui client publish`) records nothing."""
    path = package / "Published.toml"
    text = path.read_text() if path.exists() else (
        "# Generated by Move\n# This file contains metadata about published versions of this package in different "
        "environments\n# This file SHOULD be committed to source control\n")
    kept, skipping = [], False
    for line in text.splitlines():
        header = re.fullmatch(r"\[\s*([A-Za-z0-9_.-]+)\s*\](\s*#.*)?", line.strip())
        if header:
            skipping = header[1] == f"published.{env}"
        if not skipping:
            kept.append(line)
    section = (f'[published.{env}]\nchain-id = "{chain}"\npublished-at = "{dm.object_id(published_at)}"\n'
               f'original-id = "{dm.object_id(original)}"\nversion = {int(version)}\n'
               f'toolchain-version = "{toolchain_version}"\nbuild-config = {{ flavor = "sui", edition = "2024" }}\n'
               f'upgrade-capability = "{dm.object_id(cap)}"\n')
    write_atomic(path, "\n".join(kept).rstrip("\n") + "\n\n" + section)
    parse_toml(path.read_text(), str(path))


def declared_chain(package, env):
    environments = parse_toml((package / "Move.toml").read_text(), "Move.toml").get("environments", {})
    return environments.get(env)


# ── Sui CLI ───────────────────────────────────────────────────────────────────

class Cli:
    def __init__(self, client_config=None):
        self.config = client_config
        self.prefix = ["sui", "client"] + (["--client.config", str(client_config)] if client_config else [])

    def __call__(self, *args, check=True):
        result = subprocess.run(self.prefix + [str(a) for a in args], text=True, capture_output=True, timeout=900)
        if check and result.returncode:
            raise PublishError(f"`sui client {args[0]}` failed (exit {result.returncode}):\n{result.stderr.strip()[-4000:]}")
        return result

    def version(self):
        """The CLI's release, e.g. 1.81.1, as Published.toml records it."""
        output = subprocess.run([self.prefix[0], "--version"], text=True, capture_output=True, timeout=60).stdout
        found = re.search(r"\bsui (\d+\.\d+\.\d+)", output)
        if not found:
            raise PublishError(f"cannot read the Sui CLI version from {output.strip()!r}")
        return found[1]

    def sign(self, address, tx_bytes):
        """Sign exactly these transaction bytes with the client config's keystore."""
        keystore = []
        if self.config:
            found = re.search(r"(?m)^keystore:\s*\n\s+File:\s*(.+?)\s*$", Path(self.config).read_text())
            if not found:
                raise PublishError(f"{self.config} names no file keystore to sign with")
            keystore = ["--keystore-path", found[1].strip("\"'")]
        result = subprocess.run([self.prefix[0], "keytool", *keystore, "sign", "--address", address, "--data", tx_bytes,
                                 "--json"], text=True, capture_output=True, timeout=120)
        if result.returncode:
            raise PublishError(f"`sui keytool sign` failed (exit {result.returncode}):\n{result.stderr.strip()[-2000:]}")
        signature = single_json(result.stdout, "sui keytool sign").get("suiSignature")
        if not signature:
            raise PublishError("`sui keytool sign` returned no signature")
        return signature


def single_json(stdout, what):
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as error:
        raise PublishError(f"{what}: output is not JSON ({error})")


def environment(cli):
    envs, active = single_json(cli("envs", "--json").stdout, "sui client envs")
    if not active:
        raise PublishError("no active Sui environment; run: sui client switch --env ENV")
    rpc = [e["rpc"] for e in envs if e["alias"] == active]
    if len(rpc) != 1:
        raise PublishError(f"active environment {active!r} is not configured exactly once")
    address = cli("active-address").stdout.strip()
    return active, rpc[0], dm.object_id(address)


# ── Records ───────────────────────────────────────────────────────────────────

def stage(path, text):
    """Write text to a synced temporary file beside path; os.replace makes it live."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return Path(temporary)


def write_atomic(path, text):
    temporary = stage(path, text)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def current_manifest(records, env):
    path = records / env / "current" / "deployment.json"
    return json.loads(path.read_text()) if path.exists() else None


def install(manifest, records, env, template, frontend_env, plan=None, transaction=None):
    """Record the release with its transaction summary and admitting plan,
    then switch RECORDS/ENV/current and .env.local to it together (see
    activate). Rerunnable."""
    name = f"v{manifest['package_version']}-{manifest['call_package_id'][2:14]}"
    release = records / env / "releases" / name
    if release.exists():
        if json.loads((release / "deployment.json").read_text()) != manifest:
            raise PublishError(f"{release} exists with a different manifest")
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as device:
            device.write(device_template(template))
        try:
            dm.generate(manifest, release, Path(device.name))
        except ValueError as error:
            raise PublishError(f"cannot generate client configs: {error}")
        finally:
            os.unlink(device.name)
    for record, value in (("release-plan.json", plan), ("transaction.json", transaction)):
        if value is not None and not (release / record).exists():
            write_atomic(release / record, json.dumps(value, indent=2) + "\n")
    activate(records, env, name, frontend_env)
    return release


def sync_directory(path):
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def activate(records, env, name, frontend_env):
    """Switch RECORDS/ENV/current and .env.local to the complete release NAME.

    Everything that can fail is prepared first: the frontend file is fully
    written beside .env.local. A durable journal, RECORDS/ENV/activating.json,
    then names the release before either rename. If the second rename raises,
    the first is undone; if the process dies between them, the journal stays
    and reconcile() rolls forward to the named release before this tool does
    anything else (and `make dapp-build` refuses to build until it has).
    The journal is removed only when both clients name the same release, and
    a journal this run did not write (a roll forward by reconcile(), whose
    clients may already disagree) is kept on any failure."""
    frontend = None
    if frontend_env:
        kept = []
        if frontend_env.exists():
            kept = [line for line in frontend_env.read_text().splitlines()
                    if line.split("=", 1)[0].strip() not in OWNED_ENV]
        release_env = (records / env / "releases" / name / "frontend.env").read_text()
        frontend = stage(frontend_env, "\n".join(kept + [release_env.rstrip("\n")]) + "\n")
    journal = records / env / JOURNAL
    link = records / env / "current"
    previous = os.readlink(link) if link.is_symlink() else None
    target = str(Path("releases") / name)
    pointer = records / env / ".current.new"
    resumed = journal.exists()  # an earlier activation may have left the clients split
    consistent = True  # False while only a roll forward can make the clients agree durably
    try:
        write_atomic(journal, json.dumps({"release": name, "frontend_env": str(frontend_env.resolve()) if frontend_env
                                          else None, "staged": str(frontend) if frontend else None}, indent=2) + "\n")
        sync_directory(journal.parent)
        pointer.unlink(missing_ok=True)
        os.symlink(target, pointer)
        os.replace(pointer, link)
        consistent = False
        if frontend:
            try:
                os.replace(frontend, frontend_env)
            except BaseException:
                # Undo the pointer, unless it already named this release
                # (a roll forward): then only finishing can make it whole.
                # The undo is synced before the journal goes, so a crash
                # cannot keep the removal but lose the undo.
                if previous != target:
                    if previous is None:
                        link.unlink(missing_ok=True)
                    else:
                        pointer.unlink(missing_ok=True)
                        os.symlink(previous, pointer)
                        os.replace(pointer, link)
                    sync_directory(link.parent)
                    consistent = True
                raise
        # Both renames are done. If syncing them fails, keep the journal:
        # reconcile() repeats the (idempotent) switch and its syncs. Never
        # undo here, since .env.local already names the new release.
        if frontend:
            sync_directory(frontend_env.parent)
        sync_directory(link.parent)
        consistent = True
        journal.unlink()
    except BaseException:
        if consistent and not resumed:
            journal.unlink(missing_ok=True)
        raise
    finally:
        pointer.unlink(missing_ok=True)
        if frontend:
            frontend.unlink(missing_ok=True)


def interrupted(records):
    return sorted(records.glob(f"*/{JOURNAL}"))


def reconcile(records):
    """Finish every activation a killed run left half done: roll forward to the
    journaled release, which was complete and verified before its journal was
    written. Returns the environments repaired."""
    repaired = []
    for journal in interrupted(records):
        entry = json.loads(journal.read_text())
        env = journal.parent.name
        if entry.get("staged"):
            Path(entry["staged"]).unlink(missing_ok=True)
        if not (records / env / "releases" / entry["release"] / "deployment.json").is_file():
            raise PublishError(f"{journal} names missing release {entry['release']}; inspect it by hand")
        activate(records, env, entry["release"], Path(entry["frontend_env"]) if entry.get("frontend_env") else None)
        repaired.append(env)
    return repaired


def device_template(path):
    """The device sections of a daemon config; [sui] is generated."""
    text = path.read_text()
    return re.sub(r"(?ms)^\[sui\][^\n]*\n.*?(?=^\s*\[|\Z)", "", text) if re.search(r"(?m)^\[sui\]", text) else text


# ── Publish / upgrade ─────────────────────────────────────────────────────────

def plan(args, cli, read):
    env, rpc, address = environment(cli)
    if args.expect_address and dm.object_id(args.expect_address) != address:
        raise PublishError(f"active account {address} is not the expected {dm.object_id(args.expect_address)}")
    chain = read(rpc)["chain_identifier"]
    network = PINNED.get(chain) or args.network or (env if env in ("devnet", "localnet") else None)
    if network is None or (args.network and args.network != network):
        raise PublishError(f"cannot label chain {chain} (env {env}); pass --network devnet|localnet for a non-public chain"
                           if network is None else f"--network {args.network} does not match chain {chain} ({network})")
    if network == "mainnet":
        raise PublishError("mainnet publication is not supported by this development pipeline (docs/OPERATIONS.md)")
    declared = declared_chain(args.package, env)
    if declared is None and env not in ("testnet", "mainnet"):
        raise PublishError(f"Move.toml declares no environment {env!r}; add `{env} = \"{chain}\"` under [environments]")
    if declared is not None and declared != chain:
        raise PublishError(f"Move.toml declares {env} = {declared!r} but the chain is {chain}; the network may have reset")
    if env in ("testnet", "mainnet") and PINNED.get(chain) != env:
        raise PublishError(f"environment {env!r} points at chain {chain}, not {env}")
    previous = current_manifest(args.records, env)
    published = publication(args.package, env)
    step = SimpleNamespace(env=env, rpc=rpc, address=address, chain=chain, network=network, previous=previous,
                           published=published, registry=None)
    if published is None:
        if previous is not None:
            raise PublishError(f"{args.records / env / 'current'} records a deployment but Published.toml has no "
                               f"[published.{env}]; refusing a fresh publish that would orphan it")
        return step
    # Upgrade: everything must agree before spending gas.
    if published["chain"] != chain:
        raise PublishError(f"[published.{env}] is for chain {published['chain']}, not {chain}; the network may have reset")
    if previous is None:
        if not args.registry:
            raise PublishError(f"no recorded deployment for {env}; pass --registry ID to adopt the existing one")
        registry = dm.object_id(args.registry)
    else:
        if (previous["chain_identifier"], previous["call_package_id"], previous["upgrade_cap_id"]) != \
           (chain, published["package"], published["cap"]):
            raise PublishError(f"recorded deployment for {env} does not match [published.{env}]")
        registry = previous["shared_objects"]["registry"]["id"]
        if args.registry and dm.object_id(args.registry) != registry:
            raise PublishError("--registry differs from the recorded deployment")
    facts = read(rpc, published["package"], published["cap"], registry)["objects"]
    package = facts[published["package"]]
    if package.get("kind") != "package" or package["version"] != published["version"]:
        raise PublishError(f"published-at {published['package']} is not package version {published['version']} on chain")
    cap_package, cap_version, owner = dm.upgrade_cap(facts[published["cap"]])
    if (cap_package, cap_version) != (published["package"], str(published["version"])):
        raise PublishError(f"UpgradeCap {published['cap']} identifies {cap_package} v{cap_version}, "
                           f"not the published {published['package']} v{published['version']}")
    if owner is None or dm.object_id(owner) != address:
        raise PublishError(f"UpgradeCap {published['cap']} is not owned by the active account {address}")
    dm.shared_version(facts[registry], dm.origins(package)["device_registry::DeviceRegistry"])
    step.registry = registry
    return step


def transaction(args, cli, step, dry_run):
    command = ["upgrade", "--upgrade-capability", step.published["cap"]] if step.published else ["publish"]
    command += [args.package, "--json", "--gas-budget", str(args.gas_budget)]
    if dry_run:
        command.append("--dry-run")
    result = cli(*command)
    output = single_json(result.stdout, f"sui client {command[0]}")
    status = output.get("effects", {}).get("status", {})
    if status.get("status") != "success":
        raise PublishError(f"{command[0]} failed on chain: {status}")
    return output


def verify_planned(data, pinned, step, budget):
    """The unsigned transaction publishes exactly the planned bytecode. The
    digest is computed from the transaction's own modules and dependencies,
    so a different compiler, framework or build environment is refused."""
    package = next((c for c in data.get("commands") or [] if c.get("kind") in ("Publish", "Upgrade")), None)
    if package is None:
        raise toolchain.NotSubmitted("the transaction publishes no package")
    digest = toolchain.package_digest(package["modules"], package["dependencies"])
    if digest != pinned["digest"]:
        raise toolchain.NotSubmitted(f"transaction bytecode digest {digest} is not the planned {pinned['digest']}: "
                                     "the signing CLI's compiler, framework or build environment differs from the plan")
    built = {"modules": package["modules"], "dependencies": [toolchain.normal(d) for d in package["dependencies"]],
             "digest": list(bytes.fromhex(digest))}
    upgrade = {"cap": step.published["cap"], "package": step.published["package"]} if step.published else None
    checked = toolchain.verify_package_transaction(data, built, sender=step.address, budget=budget, upgrade=upgrade)
    return dict(checked, bytecode_digest=digest, unsigned_digest=data["digest"])


def planned_transaction(args, cli, step, pinned, dry_run, inspect):
    """Build unsigned, verify against the plan, then sign and run only those bytes."""
    command = ["upgrade", "--upgrade-capability", step.published["cap"]] if step.published else ["publish"]
    command += [args.package, "--gas-budget", str(args.gas_budget), "--serialize-unsigned-transaction"]
    serialized = cli(*command, check=False)
    lines = serialized.stdout.strip().splitlines()
    tx_bytes = lines[-1].strip() if lines else ""
    if serialized.returncode or not tx_bytes:
        raise PublishError(f"`sui client {command[0]}` could not build the transaction:\n{serialized.stderr.strip()[-3000:]}")
    try:
        verified = verify_planned(inspect(tx_bytes), pinned, step, args.gas_budget)
    except (toolchain.NotSubmitted, ValueError, KeyError, TypeError, IndexError) as error:
        raise PublishError(f"release plan refused the transaction before signing: {error}")
    if dry_run:  # Simulated only; nothing is signed.
        result = cli("serialized-tx", tx_bytes, "--dry-run", "--json")
    else:
        # `serialized-tx` may rebuild gas data, so sign and execute the verified bytes themselves.
        result = cli("execute-signed-tx", "--tx-bytes", tx_bytes, "--signatures", cli.sign(step.address, tx_bytes), "--json")
    output = single_json(result.stdout, "sui client " + ("serialized-tx" if dry_run else "execute-signed-tx"))
    status = output.get("effects", {}).get("status", {})
    if status.get("status") != "success":
        raise PublishError(f"{verified['kind']} failed on chain: {status}")
    return output, verified


def changes(output, step):
    """Package, UpgradeCap and registry from the executed transaction."""
    objects = output.get("objectChanges") or []
    published = [c for c in objects if c.get("type") == "published"]
    if len(published) != 1:
        raise PublishError(f"expected one published package, found {len(published)}")
    package, version = dm.object_id(published[0]["packageId"]), int(published[0]["version"])
    caps = [c for c in objects if c.get("type") in ("created", "mutated")
            and dm.same_type(c.get("objectType", "0x0::x::y"), "0x2::package::UpgradeCap")]
    if len(caps) != 1 or dm.object_id(caps[0]["owner"].get("AddressOwner", "0x0")) != step.address:
        raise PublishError("transaction did not leave exactly one UpgradeCap with the active account")
    cap = dm.object_id(caps[0]["objectId"])
    if step.published:
        if (cap, version) != (step.published["cap"], step.published["version"] + 1):
            raise PublishError(f"upgrade produced {cap} v{version}, expected {step.published['cap']} "
                               f"v{step.published['version'] + 1}")
        return package, version, cap, step.registry
    registries = [c for c in objects if c.get("type") == "created"
                  and dm.same_type(c.get("objectType", "0x0::x::y"), package + "::device_registry::DeviceRegistry")
                  and "Shared" in c.get("owner", {})]
    if len(registries) != 1:
        raise PublishError(f"expected one shared DeviceRegistry, found {len(registries)}")
    return package, version, cap, dm.object_id(registries[0]["objectId"])


def finish(pending, args, read):
    """Resolve the manifest from the chain, verify it and install it."""
    if pending.get("verified"):
        if pending["digest"] != pending["verified"]["unsigned_digest"]:
            raise PublishError(f"executed transaction {pending['digest']} is not the verified "
                               f"{pending['verified']['unsigned_digest']}; inspect it by hand")
        expected = dict(chain=pending["chain"], package=pending["package"], original=pending["original"],
                        version=pending["version"], cap=pending["cap"])
        if publication(Path(pending["package_path"]), pending["env"]) != expected:
            record_publication(Path(pending["package_path"]), pending["env"], pending["chain"], pending["package"],
                               pending["original"], pending["version"], pending["cap"], pending["toolchain_version"])
    published = publication(Path(pending["package_path"]), pending["env"])
    if published is None or (published["package"], published["version"], published["cap"], published["chain"]) != \
       (pending["package"], pending["version"], pending["cap"], pending["chain"]):
        raise PublishError(f"Published.toml [published.{pending['env']}] does not record transaction "
                           f"{pending['digest']} ({published})")
    resolve = SimpleNamespace(rpc=pending["rpc"], chain_identifier=pending["chain"], package=pending["package"],
                              upgrade_cap=pending["cap"], registry=pending["registry"], network=pending["network"],
                              publisher=pending["walrus"][0], aggregator=pending["walrus"][1],
                              aggregator_mirror=pending.get("aggregator_urls", []), publisher_mirror=pending.get("publisher_mirrors", []), publisher_urls=pending.get("publisher_urls"),
                              treasury_payee=pending.get("treasury_payee"))
    for attempt in range(10):
        try:
            manifest = dm.resolve(resolve, read)
            break
        except (ValueError, KeyError) as error:
            # A fullnode may serve the new objects a moment after execution.
            if attempt == 9:
                raise PublishError(f"cannot verify the new deployment on chain: {error!r}")
            time.sleep(2)
    if manifest["package_version"] != str(pending["version"]):
        raise PublishError(f"chain reports package version {manifest['package_version']}, expected {pending['version']}")
    transaction = {"digest": pending["digest"], "effects": pending.get("effects")}
    if pending.get("verified"):
        transaction["verified_before_signing"] = pending["verified"]
    return install(manifest, args.records, pending["env"], args.device_template, args.frontend_env, pending.get("plan"),
                   transaction)


def admit_plan(args, step):
    """The reviewed plan for this transaction, or None where none is required."""
    if args.plan is None:
        if step.network == "testnet" and not args.dry_run:
            raise PublishError("testnet publication needs a reviewed release plan: run scripts/release_plan.py plan, "
                               "review it, then pass --plan PLAN.json (docs/RELEASE_PLANNING.md)")
        return None
    try:
        return release_plan.admit(json.loads(args.plan.read_text()), network=step.network, env=step.env,
                                  chain=step.chain, address=step.address, published=step.published,
                                  registry=step.registry, package=args.package, gas_budget=args.gas_budget)
    except (release_plan.PlanError, ValueError, KeyError, TypeError) as error:
        raise PublishError(f"release plan refused: {error}")


def active_configuration(args, env):
    """What the clients currently use: the current pointer and .env.local."""
    link = args.records / env / "current"
    frontend = args.frontend_env
    return (os.readlink(link) if link.is_symlink() else None,
            frontend.read_bytes() if frontend and frontend.exists() else None)


def run(args, cli=None, read=dm.chain_facts, inspect=toolchain.decode):
    cli = cli or Cli(args.client_config)
    if args.dry_run and interrupted(args.records):
        raise PublishError(f"activation was interrupted ({', '.join(map(str, interrupted(args.records)))}); "
                           f"run --reconcile to finish it first")
    for env in reconcile(args.records):
        print(f"finished an interrupted activation of {args.records / env / 'current'} and the frontend env")
    if args.reconcile:
        return None
    if args.recover:
        pending = json.loads(args.recover.read_text())
        release = finish(pending, args, read)
        args.recover.unlink()
        return release
    step = plan(args, cli, read)
    walrus = (args.publisher, args.aggregator)
    if not all(walrus):
        defaults = (step.previous or {}).get("walrus")
        defaults = (defaults["publisher_url"], defaults["aggregator_url"]) if defaults else WALRUS_DEFAULTS.get(step.network)
        if defaults is None:
            raise PublishError(f"pass --publisher and --aggregator Walrus URLs for {step.network}")
        walrus = tuple(given or default for given, default in zip(walrus, defaults))
    for url in walrus:
        dm.endpoint(url)
    treasury = getattr(args, "treasury_payee", None)
    if treasury is None:
        treasury = (step.previous or {}).get("treasury_payee")
    if treasury is not None:
        dm.treasury_payee(treasury)
    mirrors = getattr(args, "aggregator_mirror", None)
    if mirrors is None:
        mirrors = (step.previous or {}).get("walrus", {}).get("aggregator_urls", [])
    for url in mirrors:
        dm.endpoint(url)
    publisher_mirrors = getattr(args, "publisher_mirror", None)
    if publisher_mirrors is None:
        publisher_mirrors = [url for url in (step.previous or {}).get("walrus", {}).get("publisher_urls", []) if url != walrus[0]]
    publisher_urls = [walrus[0], *publisher_mirrors]
    if args.publisher is None and getattr(args, "publisher_mirror", None) is None:
        publisher_urls = (step.previous or {}).get("walrus", {}).get("publisher_urls", publisher_urls)
    for url in publisher_urls:
        dm.endpoint(url)
    action = f"upgrade {step.published['package']} v{step.published['version']}" if step.published else "fresh publish"
    admitted = admit_plan(args, step)
    print(f"{action} on {step.env} ({step.network}, chain {step.chain}) as {step.address}"
          + (f" under plan {admitted['plan_digest']}" if admitted else ""))
    pinned = admitted and admitted["revision"].get("bytecode")
    if args.dry_run:
        output = planned_transaction(args, cli, step, pinned, True, inspect)[0] if pinned else \
            transaction(args, cli, step, dry_run=True)
        gas = output["effects"].get("gasUsed", {})
        print(f"dry run succeeded; nothing was executed or written. Gas: {json.dumps(gas)}")
        return None
    before = (args.package / "Published.toml").read_bytes() if (args.package / "Published.toml").exists() else None
    verified = None
    if pinned:
        toolchain_version = cli.version()
        output, verified = planned_transaction(args, cli, step, pinned, False, inspect)
    else:
        output = transaction(args, cli, step, dry_run=False)
    # The chain has changed. Keep the transaction before interpreting it further.
    digest = output.get("digest") or output.get("effects", {}).get("transactionDigest", "unknown")
    pending_path = args.records / step.env / f"pending-{digest}.json"
    pending = dict(env=step.env, network=step.network, chain=step.chain, rpc=step.rpc, address=step.address,
                   digest=digest, package_path=str(args.package.resolve()), walrus=list(walrus),
                   object_changes=output.get("objectChanges"), aggregator_urls=mirrors, publisher_mirrors=publisher_mirrors, publisher_urls=publisher_urls if len(publisher_urls) > 1 or "publisher_urls" in (step.previous or {}).get("walrus", {}) else None, treasury_payee=treasury, plan=admitted,
                   effects={k: output["effects"].get(k) for k in ("status", "gasUsed")}, verified=verified)
    if verified:
        pending.update(original=step.published["original"] if step.published else None,
                       toolchain_version=toolchain_version)
    write_atomic(pending_path, json.dumps(pending, indent=2) + "\n")
    configuration = active_configuration(args, step.env)
    try:
        package, version, cap, registry = changes(output, step)
        pending.update(package=package, version=version, cap=cap, registry=registry)
        if verified and not step.published:
            pending["original"] = package
        write_atomic(pending_path, json.dumps(pending, indent=2) + "\n")
        if not verified and before is not None and (args.package / "Published.toml").read_bytes() == before:
            raise PublishError("the Sui CLI did not update Published.toml")
        release = finish(pending, args, read)
    except Exception as error:
        # Report what is actually live, not what should be.
        state = ("previous configuration is unchanged" if active_configuration(args, step.env) == configuration
                 else "WARNING: deployments/current or .env.local changed; check both before rebuilding")
        raise PublishError(f"{error}\nTransaction {digest} EXECUTED; {state}. "
                           f"Fix the cause, then run: {Path(sys.argv[0]).name} --recover {pending_path}") from error
    pending_path.unlink()
    return release


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--expect-address", help="account that must be active (required unless --recover)")
    p.add_argument("--network", choices=("devnet", "localnet"), help="label for a chain that is neither testnet nor mainnet")
    p.add_argument("--dry-run", action="store_true", help="simulate the transaction; write nothing")
    p.add_argument("--publisher-mirror", action="append", help="additional publisher in failover order; default preserves previous release")
    p.add_argument("--treasury-payee", help="reviewed canonical treasury multisig address (default: preserve previous release)")
    p.add_argument("--publisher", help="Walrus publisher URL (default: previous release, or testnet's public one)")
    p.add_argument("--aggregator-mirror", action="append", help="ordered device mirrors; repeat for each URL (default: preserve previous release)")
    p.add_argument("--aggregator", help="Walrus aggregator URL (default: previous release, or testnet's public one)")
    p.add_argument("--registry", help="adopt this existing DeviceRegistry when no deployment is recorded")
    p.add_argument("--gas-budget", type=int, default=500_000_000, help="MIST (default 0.5 SUI)")
    p.add_argument("--package", type=Path, default=ROOT / "contracts")
    p.add_argument("--records", type=Path, default=ROOT / "deployments")
    p.add_argument("--device-template", type=Path, default=ROOT / "suifleet-edge/config.example.toml")
    p.add_argument("--frontend-env", type=Path, default=ROOT / "dapp/.env.local",
                   help="frontend env file to update; pass '' to skip")
    p.add_argument("--client-config", type=Path, help="Sui client.yaml (default: the CLI's)")
    p.add_argument("--plan", type=Path, help="reviewed release plan (scripts/release_plan.py); required on testnet")
    p.add_argument("--recover", type=Path, metavar="PENDING_JSON", help="finish recording an executed transaction")
    p.add_argument("--reconcile", action="store_true",
                   help="only finish an interrupted activation (RECORDS/ENV/activating.json); no transaction")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.frontend_env == Path(""):
        args.frontend_env = None
    if not args.expect_address and not args.recover and not args.reconcile:
        parser().error("--expect-address is required: the account that must sign")
    try:
        release = run(args)
    except (PublishError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if release:
        manifest = json.loads((release / "deployment.json").read_text())
        transaction = json.loads((release / "transaction.json").read_text()) if (release / "transaction.json").exists() else {}
        print(f"Recorded {release}\n  transaction {transaction.get('digest', 'unknown')}\n  package {manifest['call_package_id']} (v{manifest['package_version']})\n"
              f"  UpgradeCap {manifest['upgrade_cap_id']}\n  registry {manifest['shared_objects']['registry']['id']}")
        if args.frontend_env:
            print(f"Updated {args.frontend_env}; rebuild the frontend. Device config template: {release / 'config.toml'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
