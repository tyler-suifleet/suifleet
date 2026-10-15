#!/usr/bin/env python3
"""Resolve and verify Sui deployment metadata; never publishes or spends gas."""
import argparse
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlparse

REQUIRED_TYPES = (
    "device_registry::DeviceRegistry", "device_registry::Device",
    "device_group::DeviceGroup", "device_group::GroupCreated",
    "device_cap::DeviceCap", "device_cap::DeviceAddressKey",
    "deployment_manager::DeploymentRecord", "deployment_manager::DeploymentCreated",
    "deployment_manager::DeviceStatusUpdated", "deployment_manager::TargetGroupsKey",
    "deployment_manager::TargetDevicesKey", "deployment_manager::StatusesKey",
    "deployment_manager::RecipeDigestKey", "deployment_manager::ReceiptsKey",
    "deployment_manager::Receipts", "deployment_manager::DeviceStatusReported",
    "device_registry::IdentityKey", "device_registry::Identity", "device_registry::DeviceIdentityChanged",
    "device_registry::CustodyKey", "device_registry::DeviceOwnerCap",
)
CLIENT_VERSIONS = {"dapp": "0.0.2", "edge": "0.1.0"}


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
    if type(manifest["schema_version"]) is not int or type(manifest["protocol_version"]) is not int or manifest["schema_version"] != 1 or manifest["protocol_version"] != 1:
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
    for name in REQUIRED_TYPES:
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
    return manifest


def rpc(url, method, params):
    request = Request(url, json.dumps(dict(jsonrpc="2.0", id=1, method=method, params=params)).encode(),
                      {"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if "error" in result:
        raise ValueError(f"{method}: {result['error']}")
    return result["result"]


def origins(package):
    raw = package["data"]["bcs"]
    if raw["dataType"] != "package":
        raise ValueError("call ID is not a package")
    table = {}
    for entry in raw["typeOriginTable"]:
        name = entry["module_name"] + "::" + entry["datatype_name"]
        if name in table:
            raise ValueError("duplicate type origin: " + name)
        table[name] = object_id(entry["package"])
    return table


def verify(manifest, call=rpc):
    validate(manifest)
    url = manifest["rpc_url"]
    if call(url, "sui_getChainIdentifier", []) != manifest["chain_identifier"]:
        raise ValueError("chain identifier mismatch; network may have reset")
    package = call(url, "sui_getObject", [manifest["call_package_id"], {"showBcs": True}])
    table = origins(package)
    if str(package["data"]["version"]) != manifest["package_version"]:
        raise ValueError("package version mismatch")
    for name in REQUIRED_TYPES:
        if table.get(name) != object_id(manifest["type_origins"][name]):
            raise ValueError("type origin mismatch: " + name)
    cap = call(url, "sui_getObject", [manifest["upgrade_cap_id"], {"showContent": True}])["data"]["content"]
    if cap["type"].split("::")[1:] != ["package", "UpgradeCap"] or object_id(cap["type"].split("::")[0]) != object_id("0x2") or object_id(cap["fields"]["package"]) != object_id(manifest["call_package_id"]) or str(cap["fields"]["version"]) != manifest["package_version"]:
        raise ValueError("UpgradeCap does not identify the latest call package/version")
    registry = manifest["shared_objects"]["registry"]
    obj = call(url, "sui_getObject", [registry["id"], {"showContent": True, "showOwner": True}])["data"]
    expected = object_id(manifest["type_origins"]["device_registry::DeviceRegistry"]) + "::device_registry::DeviceRegistry"
    parts = obj["content"]["type"].split("::")
    actual = object_id(parts[0]) + "::" + "::".join(parts[1:])
    if actual != expected or str(obj["owner"].get("Shared", {}).get("initial_shared_version")) != registry["initial_shared_version"]:
        raise ValueError("registry type/shared version mismatch")
    return manifest


def resolve(args, call=rpc):
    if call(args.rpc, "sui_getChainIdentifier", []) != args.chain_identifier:
        raise ValueError("expected chain identifier mismatch")
    package_id = object_id(args.package)
    package = call(args.rpc, "sui_getObject", [package_id, {"showBcs": True}])
    table = origins(package)
    registry = call(args.rpc, "sui_getObject", [object_id(args.registry), {"showContent": True, "showOwner": True}])["data"]
    manifest = dict(schema_version=1, protocol_version=1, network=args.network,
        chain_identifier=args.chain_identifier, rpc_url=args.rpc,
        call_package_id=package_id, upgrade_cap_id=object_id(args.upgrade_cap), package_version=str(package["data"]["version"]),
        type_origins={name: table[name] for name in REQUIRED_TYPES},
        shared_objects={"registry": {"id": object_id(args.registry), "initial_shared_version": str(registry["owner"]["Shared"]["initial_shared_version"])}},
        supported_clients={"dapp": {"min": "0.0.2", "max_exclusive": "0.0.3"}, "edge": {"min": "0.1.0", "max_exclusive": "0.1.1"}},
        walrus={"publisher_url": args.publisher, "aggregator_url": args.aggregator})
    return verify(manifest, call)


def generate(manifest, output, device_template):
    validate(manifest)
    # Refuse overwrite: a complete release bundle is prepared before it is installed.
    if output.exists():
        raise ValueError("output bundle already exists")
    template = device_template.read_text()
    # Device templates deliberately permit simple top-level sections only. Their
    # values are parsed by the daemon's TOML parser, never merged with network IDs.
    sections = re.findall(r"^\s*\[([^\]\n]+)\]\s*(?:#.*)?$", template, re.MULTILINE)
    if any(section not in ("device", "apply", "runtime") for section in sections):
        raise ValueError("device template permits only device/apply/runtime sections")
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
