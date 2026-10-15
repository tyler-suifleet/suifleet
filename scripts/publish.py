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
RECORDS/ENV/current is switched to it with one atomic rename, and only then is
the frontend's .env.local rewritten (also atomically). If anything fails after
the transaction executed, the transaction is kept in RECORDS/ENV/pending-*.json
and `--recover FILE` finishes recording it without another transaction.
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

ROOT = Path(__file__).resolve().parents[1]
PINNED = {"35834a8a": "mainnet", "4c78adac": "testnet"}
WALRUS_DEFAULTS = {"testnet": ("https://publisher.walrus-testnet.walrus.space",
                               "https://aggregator.walrus-testnet.walrus.space")}
# Frontend variables this tool owns; every other line of .env.local is kept.
# The NEXT_PUBLIC_*_ID/NETWORK lines are the pre-manifest format, now unused.
OWNED_ENV = ("NEXT_PUBLIC_DEPLOYMENT_MANIFEST", "NEXT_PUBLIC_PACKAGE_ID", "NEXT_PUBLIC_ORIGINAL_PACKAGE_ID",
             "NEXT_PUBLIC_REGISTRY_ID", "NEXT_PUBLIC_NETWORK")


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


def declared_chain(package, env):
    environments = parse_toml((package / "Move.toml").read_text(), "Move.toml").get("environments", {})
    return environments.get(env)


# ── Sui CLI ───────────────────────────────────────────────────────────────────

class Cli:
    def __init__(self, client_config=None):
        self.prefix = ["sui", "client"] + (["--client.config", str(client_config)] if client_config else [])

    def __call__(self, *args, check=True):
        result = subprocess.run(self.prefix + [str(a) for a in args], text=True, capture_output=True, timeout=900)
        if check and result.returncode:
            raise PublishError(f"`sui client {args[0]}` failed (exit {result.returncode}):\n{result.stderr.strip()[-4000:]}")
        return result


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

def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def current_manifest(records, env):
    path = records / env / "current" / "deployment.json"
    return json.loads(path.read_text()) if path.exists() else None


def install(manifest, records, env, template, frontend_env):
    """Record the release, switch RECORDS/ENV/current to it atomically, then
    rewrite .env.local atomically. Rerunnable after a partial failure."""
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
    link = records / env / "current"
    staging = records / env / ".current.new"
    staging.unlink(missing_ok=True)
    os.symlink(Path("releases") / name, staging)
    os.replace(staging, link)
    frontend = (release / "frontend.env").read_text()
    if frontend_env:
        kept = []
        if frontend_env.exists():
            kept = [line for line in frontend_env.read_text().splitlines()
                    if line.split("=", 1)[0].strip() not in OWNED_ENV]
        write_atomic(frontend_env, "\n".join(kept + [frontend.rstrip("\n")]) + "\n")
    return release


def device_template(path):
    """The device sections of a daemon config; [sui] is generated."""
    text = path.read_text()
    return re.sub(r"(?ms)^\[sui\][^\[]*", "", text) if re.search(r"(?m)^\[sui\]", text) else text


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
    published = publication(Path(pending["package_path"]), pending["env"])
    if published is None or (published["package"], published["version"], published["cap"], published["chain"]) != \
       (pending["package"], pending["version"], pending["cap"], pending["chain"]):
        raise PublishError(f"Published.toml [published.{pending['env']}] does not record transaction "
                           f"{pending['digest']} ({published})")
    resolve = SimpleNamespace(rpc=pending["rpc"], chain_identifier=pending["chain"], package=pending["package"],
                              upgrade_cap=pending["cap"], registry=pending["registry"], network=pending["network"],
                              publisher=pending["walrus"][0], aggregator=pending["walrus"][1])
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
    return install(manifest, args.records, pending["env"], args.device_template, args.frontend_env)


def run(args, cli=None, read=dm.chain_facts):
    cli = cli or Cli(args.client_config)
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
    action = f"upgrade {step.published['package']} v{step.published['version']}" if step.published else "fresh publish"
    print(f"{action} on {step.env} ({step.network}, chain {step.chain}) as {step.address}")
    if args.dry_run:
        output = transaction(args, cli, step, dry_run=True)
        gas = output["effects"].get("gasUsed", {})
        print(f"dry run succeeded; nothing was executed or written. Gas: {json.dumps(gas)}")
        return None
    before = (args.package / "Published.toml").read_bytes() if (args.package / "Published.toml").exists() else None
    output = transaction(args, cli, step, dry_run=False)
    # The chain has changed. Keep the transaction before interpreting it further.
    digest = output.get("digest") or output.get("effects", {}).get("transactionDigest", "unknown")
    pending_path = args.records / step.env / f"pending-{digest}.json"
    pending = dict(env=step.env, network=step.network, chain=step.chain, rpc=step.rpc, address=step.address,
                   digest=digest, package_path=str(args.package.resolve()), walrus=list(walrus),
                   object_changes=output.get("objectChanges"))
    write_atomic(pending_path, json.dumps(pending, indent=2) + "\n")
    try:
        package, version, cap, registry = changes(output, step)
        pending.update(package=package, version=version, cap=cap, registry=registry)
        write_atomic(pending_path, json.dumps(pending, indent=2) + "\n")
        if before is not None and (args.package / "Published.toml").read_bytes() == before:
            raise PublishError("the Sui CLI did not update Published.toml")
        release = finish(pending, args, read)
    except Exception as error:
        raise PublishError(f"{error}\nTransaction {digest} EXECUTED; previous configuration is unchanged. "
                           f"Fix the cause, then run: {Path(sys.argv[0]).name} --recover {pending_path}") from error
    pending_path.unlink()
    return release


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--expect-address", help="account that must be active (required unless --recover)")
    p.add_argument("--network", choices=("devnet", "localnet"), help="label for a chain that is neither testnet nor mainnet")
    p.add_argument("--dry-run", action="store_true", help="simulate the transaction; write nothing")
    p.add_argument("--publisher", help="Walrus publisher URL (default: previous release, or testnet's public one)")
    p.add_argument("--aggregator", help="Walrus aggregator URL (default: previous release, or testnet's public one)")
    p.add_argument("--registry", help="adopt this existing DeviceRegistry when no deployment is recorded")
    p.add_argument("--gas-budget", type=int, default=500_000_000, help="MIST (default 0.5 SUI)")
    p.add_argument("--package", type=Path, default=ROOT / "contracts")
    p.add_argument("--records", type=Path, default=ROOT / "deployments")
    p.add_argument("--device-template", type=Path, default=ROOT / "suifleet-edge/config.example.toml")
    p.add_argument("--frontend-env", type=Path, default=ROOT / "dapp/.env.local",
                   help="frontend env file to update; pass '' to skip")
    p.add_argument("--client-config", type=Path, help="Sui client.yaml (default: the CLI's)")
    p.add_argument("--recover", type=Path, metavar="PENDING_JSON", help="finish recording an executed transaction")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.frontend_env == Path(""):
        args.frontend_env = None
    if not args.expect_address and not args.recover:
        parser().error("--expect-address is required: the account that must sign")
    try:
        release = run(args)
    except (PublishError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if release:
        manifest = json.loads((release / "deployment.json").read_text())
        print(f"Recorded {release}\n  package {manifest['call_package_id']} (v{manifest['package_version']})\n"
              f"  UpgradeCap {manifest['upgrade_cap_id']}\n  registry {manifest['shared_objects']['registry']['id']}")
        if args.frontend_env:
            print(f"Updated {args.frontend_env}; rebuild the frontend. Device config template: {release / 'config.toml'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
