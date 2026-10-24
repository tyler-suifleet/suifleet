#!/usr/bin/env python3
"""Resolve and verify Sui deployment metadata over gRPC; never publishes or spends gas."""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlparse

_PROTOCOL = json.loads((Path(__file__).resolve().parents[1] / "interfaces/fleet-v1.json").read_text())
REQUIRED_TYPES = tuple(_PROTOCOL["required_type_origins"])
CATALOG_TYPES = tuple(_PROTOCOL["optional_type_origins"]["component_catalog"])
SEAL_POLICY_TYPES = tuple(_PROTOCOL["optional_type_origins"]["seal_policy"])
HANDOVER_TYPES = tuple(_PROTOCOL["optional_type_origins"]["fleet_handover"])
BUDGET_TYPES = tuple(_PROTOCOL["optional_type_origins"]["fleet_budget"])
ROLLOUT_HEALTH_TYPES = tuple(_PROTOCOL["optional_type_origins"]["rollout_health"])
OPTIONAL_GROUPS = {"catalog": CATALOG_TYPES, "seal policy": SEAL_POLICY_TYPES, "fleet budget": BUDGET_TYPES, "rollout health": ROLLOUT_HEALTH_TYPES}

def manifest_types(manifest):
    types = REQUIRED_TYPES
    for label, group in OPTIONAL_GROUPS.items():
        present = [name for name in group if name in manifest["type_origins"]]
        if present and len(present) != len(group):
            raise ValueError(f"partial {label} type origins")
        types += tuple(present)
    return types + tuple(n for n in HANDOVER_TYPES if n in manifest["type_origins"])

CLIENT_VERSIONS = {"dapp": "0.1.0", "edge": "0.2.1"}


def object_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value):
        raise ValueError("invalid Sui ID")
    return "0x" + value[2:].lower().zfill(64)


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError("client version must be major.minor.patch")
    return tuple(map(int, value.split(".")))


def endpoint(value):
    if not isinstance(value, str):
        raise ValueError("endpoint must be a URL")
    url = urlparse(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("invalid public endpoint")
    return value


def validate(manifest):
    if type(manifest["schema_version"]) is not int or type(manifest["protocol_version"]) is not int or manifest["schema_version"] != _PROTOCOL["manifest_schema_version"] or manifest["protocol_version"] != _PROTOCOL["protocol_version"]:
        raise ValueError("unsupported manifest schema/protocol")
    if manifest["network"] not in ("localnet", "devnet", "testnet", "mainnet"):
        raise ValueError("unsupported network")
    pinned = {"mainnet": "35834a8a", "testnet": "4c78adac"}
    if manifest["network"] in pinned and manifest["chain_identifier"] != pinned[manifest["network"]]:
        raise ValueError("network label/chain mismatch")
    if not re.fullmatch(r"[0-9a-f]{8}", manifest["chain_identifier"]):
        raise ValueError("invalid chain identifier")
    if not re.fullmatch(r"[1-9][0-9]*", manifest["package_version"]):
        raise ValueError("invalid package version")
    object_id(manifest["call_package_id"])
    object_id(manifest["upgrade_cap_id"])
    endpoint(manifest["rpc_url"])
    urls = manifest.get("rpc_urls", [])
    if not isinstance(urls, list) or len(urls) > 8:
        raise ValueError("rpc_urls must be a list of at most 8 endpoints")
    for url in urls:
        endpoint(url)
    for name in manifest_types(manifest):
        object_id(manifest["type_origins"][name])
    registry = manifest["shared_objects"]["registry"]
    object_id(registry["id"])
    if not re.fullmatch(r"[1-9][0-9]*", registry["initial_shared_version"]):
        raise ValueError("invalid registry shared version")
    for client, current in CLIENT_VERSIONS.items():
        bounds = manifest["supported_clients"][client]
        if not version(bounds["min"]) <= version(current) < version(bounds["max_exclusive"]):
            raise ValueError("unsupported " + client + " version")
    for name in ("publisher_url", "aggregator_url"):
        endpoint(manifest["walrus"][name])
    mirrors = manifest["walrus"].get("aggregator_urls", [])
    if not isinstance(mirrors, list):
        raise ValueError("aggregator_urls must be a list")
    for url in mirrors:
        endpoint(url)
    return manifest


def rpc(url, method, params):
    """Legacy JSON-RPC. Only localnet tests use it, to inspect chain state
    independently of the gRPC clients; resolution and verification use gRPC."""
    request = Request(url, json.dumps(dict(jsonrpc="2.0", id=1, method=method, params=params)).encode(),
                      {"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if "error" in result:
        raise ValueError(f"{method}: {result['error']}")
    return result["result"]


FACTS_TOOL = Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/examples/chain_facts"


def chain_facts(url, *ids):
    """Chain identifier and BCS-decoded objects, read over Sui gRPC v2 by the
    chain_facts example (build: cargo build --example chain_facts)."""
    tool = os.environ.get("SUIFLEET_CHAIN_FACTS", str(FACTS_TOOL))
    if not Path(tool).exists():
        raise ValueError(f"{tool} not built; run: cargo build --locked --example chain_facts (in suifleet-edge)")
    result = subprocess.run([tool, url, *ids], text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError("chain read failed: " + result.stderr.strip()[-2000:])
    return json.loads(result.stdout)


def same_type(actual, expected):
    address, _, rest = actual.partition("::")
    want_address, _, want_rest = expected.partition("::")
    return object_id(address) == object_id(want_address) and rest == want_rest


def origins(fact):
    if fact.get("kind") != "package":
        raise ValueError("call ID is not a package")
    table = {}
    for entry in fact["type_origins"]:
        name = entry["module"] + "::" + entry["datatype"]
        if name in table:
            raise ValueError("duplicate type origin: " + name)
        table[name] = object_id(entry["package"])
    return table


def upgrade_cap(fact):
    """(package, version, owner address) of an UpgradeCap fact."""
    if fact.get("kind") != "object" or not same_type(fact["type"], "0x2::package::UpgradeCap"):
        raise ValueError("UpgradeCap object not found")
    cap = fact["upgrade_cap"]
    return object_id(cap["package"]), str(cap["version"]), fact["owner"].get("address")


def shared_version(fact, origin):
    if fact.get("kind") != "object" or not same_type(fact["type"], origin + "::device_registry::DeviceRegistry"):
        raise ValueError("registry type/shared version mismatch")
    if "shared" not in fact["owner"]:
        raise ValueError("registry is not shared")
    return str(fact["owner"]["shared"])


def verify(manifest, read=chain_facts):
    validate(manifest)
    registry = manifest["shared_objects"]["registry"]
    facts = read(manifest["rpc_url"], manifest["call_package_id"], manifest["upgrade_cap_id"], registry["id"])
    objects = facts["objects"]
    if facts["chain_identifier"] != manifest["chain_identifier"]:
        raise ValueError("chain identifier mismatch; network may have reset")
    package = objects[object_id(manifest["call_package_id"])]
    table = origins(package)
    if str(package["version"]) != manifest["package_version"]:
        raise ValueError("package version mismatch")
    for name in manifest_types(manifest):
        if table.get(name) != object_id(manifest["type_origins"][name]):
            raise ValueError("type origin mismatch: " + name)
    if upgrade_cap(objects[object_id(manifest["upgrade_cap_id"])])[:2] != (object_id(manifest["call_package_id"]), manifest["package_version"]):
        raise ValueError("UpgradeCap does not identify the latest call package/version")
    origin = object_id(manifest["type_origins"]["device_registry::DeviceRegistry"])
    if shared_version(objects[object_id(registry["id"])], origin) != registry["initial_shared_version"]:
        raise ValueError("registry type/shared version mismatch")
    return manifest


def resolve(args, read=chain_facts):
    package_id = object_id(args.package)
    facts = read(args.rpc, package_id, object_id(args.upgrade_cap), object_id(args.registry))
    if facts["chain_identifier"] != args.chain_identifier:
        raise ValueError("expected chain identifier mismatch")
    package = facts["objects"][package_id]
    table = origins(package)
    missing = [name for name in REQUIRED_TYPES if name not in table]
    if missing:
        raise ValueError("package lacks required types: " + ", ".join(missing))
    registry = facts["objects"][object_id(args.registry)]
    manifest = dict(schema_version=_PROTOCOL["manifest_schema_version"], protocol_version=_PROTOCOL["protocol_version"], network=args.network,
        chain_identifier=args.chain_identifier, rpc_url=args.rpc, rpc_urls=[args.rpc],
        call_package_id=package_id, upgrade_cap_id=object_id(args.upgrade_cap), package_version=str(package["version"]),
        type_origins={name: table[name] for name in REQUIRED_TYPES + tuple(n for n in CATALOG_TYPES + SEAL_POLICY_TYPES + HANDOVER_TYPES + BUDGET_TYPES + ROLLOUT_HEALTH_TYPES if n in table)},
        shared_objects={"registry": {"id": object_id(args.registry),
            "initial_shared_version": shared_version(registry, table["device_registry::DeviceRegistry"])}},
        supported_clients={"dapp": {"min": "0.1.0" if all(n in table for n in CATALOG_TYPES) else "0.0.2", "max_exclusive": "0.2.0"}, "edge": {"min": "0.2.1", "max_exclusive": "0.3.0"}},
        walrus={"publisher_url": args.publisher, "aggregator_url": args.aggregator})
    if getattr(args, "aggregator_mirror", None):
        manifest["walrus"]["aggregator_urls"] = args.aggregator_mirror
    return verify(manifest, read)


def generate(manifest, output, device_template):
    validate(manifest)
    # Refuse overwrite: a complete release bundle is prepared before it is installed.
    if output.exists():
        raise ValueError("output bundle already exists")
    template = device_template.read_text()
    # Device templates deliberately permit simple top-level sections only. Their
    # values are parsed by the daemon's TOML parser, never merged with network IDs.
    sections = re.findall(r"^\s*\[([^\]\n]+)\]\s*(?:#.*)?$", template, re.MULTILINE)
    if re.search(r"^\s*\[\[", template, re.MULTILINE):
        raise ValueError("device template permits no arrays of tables; use inline arrays")
    if any(section not in ("device", "device.signer", "apply", "runtime", "seal", "budget") for section in sections):
        raise ValueError("device template permits only device, device.signer, apply, runtime, seal and budget sections")
    if len(set(sections)) != len(sections) or not {"device", "apply"} <= set(sections):
        raise ValueError("missing or duplicate device template sections")
    encoded = json.dumps(manifest, separators=(",", ":"))
    # dotenv quoting must remain unambiguous, including its variable interpolation.
    if any(char in encoded for char in ("'", "$", "\n", "\r")):
        raise ValueError("manifest cannot be represented safely in dotenv")
    import tempfile
    import shutil
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the destination exclusively, then replace that empty directory with
    # a fully written sibling. An interrupted generation exposes no partial files.
    output.mkdir()
    staging = Path(tempfile.mkdtemp(prefix=".manifest-bundle-", dir=output.parent))
    try:
        _write_bundle(manifest, encoded, template, staging)
        staging.replace(output)
    except BaseException:
        shutil.rmtree(staging)
        output.rmdir()
        raise


def _write_bundle(manifest, encoded, template, output):
    (output / "deployment.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "frontend.env").write_text("NEXT_PUBLIC_DEPLOYMENT_MANIFEST='" + encoded + "'\n")
    (output / "config.toml").write_text('[sui]\nmanifest_path = "deployment.json"\n\n' + template)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    resolve_parser = sub.add_parser("resolve")
    for name in ("rpc", "chain-identifier", "package", "upgrade-cap", "registry", "publisher", "aggregator", "network"):
        resolve_parser.add_argument("--" + name, required=True)
    resolve_parser.add_argument("--aggregator-mirror", action="append", help="ordered device mirror, repeat for each URL; replaces legacy device endpoint")
    resolve_parser.add_argument("--output", type=Path, required=True)
    for name in ("verify", "generate"):
        command = sub.add_parser(name)
        command.add_argument("manifest", type=Path)
        if name == "generate":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--device-template", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "resolve":
        manifest = resolve(args)
        with args.output.open("x") as output:
            output.write(json.dumps(manifest, indent=2) + "\n")
    else:
        manifest = verify(json.loads(args.manifest.read_text()))
        if args.command == "generate":
            generate(manifest, args.output, args.device_template)
    print("Verified deployment manifest")


if __name__ == "__main__":
    main()
