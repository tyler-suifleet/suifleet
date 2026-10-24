import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment_manifest as dm


def fixture():
    return dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier="1234abcd",
        rpc_url="http://127.0.0.1:9000", rpc_urls=["http://127.0.0.1:9000"], call_package_id=dm.object_id("0x3"), upgrade_cap_id=dm.object_id("0xc0"), package_version="3",
        type_origins={name: dm.object_id("0x2" if name.endswith("RecipeDigestKey") else "0x3" if name.endswith("DeviceAddressKey") else "0x1") for name in dm.REQUIRED_TYPES},
        shared_objects={"registry": {"id":dm.object_id("0xbe"), "initial_shared_version":"1"}},
        supported_clients={"dapp": {"min":"0.0.2", "max_exclusive":"0.2.0"}, "edge": {"min":"0.2.1", "max_exclusive":"0.3.0"}},
        walrus={"publisher_url":"https://publisher.example", "aggregator_url":"https://aggregator.example"})


def chain(manifest, cap_owner="0xa11ce"):
    """gRPC chain facts (the chain_facts tool's output) for a deployed manifest."""
    def read(url, *ids):
        objects = {
            manifest["call_package_id"]: {"kind": "package", "version": int(manifest["package_version"]), "type_origins": [
                {"module": name.split("::")[0], "datatype": name.split("::")[1], "package": origin}
                for name, origin in manifest["type_origins"].items()]},
            manifest["upgrade_cap_id"]: {"kind": "object", "type": dm.object_id("0x2") + "::package::UpgradeCap", "version": 9,
                "owner": {"address": dm.object_id(cap_owner)},
                "upgrade_cap": {"package": manifest["call_package_id"], "version": int(manifest["package_version"]), "policy": 0}},
            manifest["shared_objects"]["registry"]["id"]: {"kind": "object", "version": 5, "owner": {"shared": 1},
                "type": manifest["type_origins"]["device_registry::DeviceRegistry"] + "::device_registry::DeviceRegistry"},
        }
        return {"chain_identifier": manifest["chain_identifier"],
                "objects": {dm.object_id(i): objects.get(dm.object_id(i), {"kind": "absent"}) for i in ids}}
    return read


class ManifestTests(unittest.TestCase):
    def test_optional_ordered_aggregator_mirrors(self):
        manifest = fixture()
        mirrors = ["https://one.example", "https://two.example"]
        manifest["walrus"]["aggregator_urls"] = mirrors
        self.assertEqual(dm.validate(manifest)["walrus"]["aggregator_urls"], mirrors)
        for invalid in ["https://one.example", ["file:///tmp/blob"], [""]]:
            manifest["walrus"]["aggregator_urls"] = invalid
            with self.assertRaises(ValueError):
                dm.validate(manifest)

    def test_optional_endpoint_lists_are_bounded_and_legacy_is_valid(self):
        manifest = fixture()
        del manifest["rpc_urls"]
        dm.validate(manifest)
        for urls in [[], [manifest["rpc_url"], "https://fallback.example"]]:
            manifest["rpc_urls"] = urls
            dm.validate(manifest)
        for urls in ["https://bad.example", ["file:///etc/passwd"], [manifest["rpc_url"]]*9]:
            manifest["rpc_urls"] = urls
            with self.assertRaises(ValueError):
                dm.validate(manifest)

    def test_control_manifest_fences_pre_control_daemons(self):
        from unittest.mock import patch
        manifest = fixture()
        args = SimpleNamespace(chain_identifier=manifest["chain_identifier"], upgrade_cap=manifest["upgrade_cap_id"], rpc=manifest["rpc_url"], package=manifest["call_package_id"], registry=manifest["shared_objects"]["registry"]["id"],
            publisher=manifest["walrus"]["publisher_url"], aggregator=manifest["walrus"]["aggregator_url"], network="localnet")
        resolved = dm.resolve(args, chain(manifest))
        self.assertEqual(resolved["supported_clients"]["edge"]["min"], "0.2.1")
        with patch.dict(dm.CLIENT_VERSIONS, {"edge":"0.2.0"}):
            with self.assertRaisesRegex(ValueError, "unsupported edge version"):
                dm.validate(resolved)

    def test_optional_seal_policy_origin_group(self):
        manifest = fixture()
        manifest["type_origins"][dm.SEAL_POLICY_TYPES[0]] = dm.object_id("0x4")
        with self.assertRaisesRegex(ValueError, "partial seal policy"):
            dm.validate(manifest)
        manifest["type_origins"].update({n: dm.object_id("0x4") for n in dm.SEAL_POLICY_TYPES})
        dm.verify(manifest, chain(manifest))
        forged = copy.deepcopy(manifest)
        forged["type_origins"][dm.SEAL_POLICY_TYPES[-1]] = dm.object_id("0x3")
        with self.assertRaisesRegex(ValueError, "type origin mismatch"):
            dm.verify(forged, chain(manifest))

    def test_optional_fleet_budget_origin_group(self):
        manifest = fixture()
        manifest["type_origins"][dm.BUDGET_TYPES[0]] = dm.object_id("0x4")
        with self.assertRaisesRegex(ValueError, "partial fleet budget"):
            dm.validate(manifest)
        manifest["type_origins"].update({n: dm.object_id("0x4") for n in dm.BUDGET_TYPES})
        dm.verify(manifest, chain(manifest))
        forged = copy.deepcopy(manifest)
        forged["type_origins"][dm.BUDGET_TYPES[-1]] = dm.object_id("0x3")
        with self.assertRaisesRegex(ValueError, "type origin mismatch"):
            dm.verify(forged, chain(manifest))

    def test_optional_catalog_origin_group(self):
        manifest = fixture()
        manifest["type_origins"][dm.CATALOG_TYPES[0]] = dm.object_id("0x4")
        with self.assertRaisesRegex(ValueError, "partial catalog"):
            dm.validate(manifest)
        manifest["type_origins"].update({n:dm.object_id("0x4") for n in dm.CATALOG_TYPES})
        dm.verify(manifest, chain(manifest))
        forged = copy.deepcopy(manifest)
        forged["type_origins"][dm.CATALOG_TYPES[-1]] = dm.object_id("0x3")
        with self.assertRaisesRegex(ValueError, "type origin mismatch"):
            dm.verify(forged, chain(manifest))

    def test_resolves_mixed_origins(self):
        manifest = fixture()
        args = SimpleNamespace(chain_identifier=manifest["chain_identifier"], upgrade_cap=manifest["upgrade_cap_id"], rpc=manifest["rpc_url"], package=manifest["call_package_id"], registry=manifest["shared_objects"]["registry"]["id"],
            publisher=manifest["walrus"]["publisher_url"], aggregator=manifest["walrus"]["aggregator_url"], network="localnet")
        self.assertEqual(dm.resolve(args, chain(manifest)), manifest)

    def test_resolves_catalog_origins_with_grpc_facts(self):
        manifest = fixture()
        manifest["type_origins"].update({name: dm.object_id("0x4") for name in dm.CATALOG_TYPES})
        manifest["supported_clients"]["dapp"]["min"] = "0.1.0"
        manifest["supported_clients"]["edge"]["min"] = "0.2.1"
        args = SimpleNamespace(chain_identifier=manifest["chain_identifier"], upgrade_cap=manifest["upgrade_cap_id"],
            rpc=manifest["rpc_url"], package=manifest["call_package_id"],
            registry=manifest["shared_objects"]["registry"]["id"], network="localnet",
            publisher=manifest["walrus"]["publisher_url"], aggregator=manifest["walrus"]["aggregator_url"])
        self.assertEqual(dm.resolve(args, chain(manifest)), manifest)
        manifest["type_origins"].pop(dm.CATALOG_TYPES[-1])
        with self.assertRaisesRegex(ValueError, "partial catalog"):
            dm.resolve(args, chain(manifest))

    def test_stale_metadata_rejected(self):
        live = fixture()
        cases = [("chain_identifier", "deadbeef"), ("package_version", "2")]
        for key, value in cases:
            stale = copy.deepcopy(live); stale[key] = value
            with self.assertRaises(ValueError): dm.verify(stale, chain(live))
        stale = copy.deepcopy(live)
        stale["type_origins"]["deployment_manager::RecipeDigestKey"] = dm.object_id("0x1")
        with self.assertRaises(ValueError): dm.verify(stale, chain(live))
        stale = copy.deepcopy(live); stale["shared_objects"]["registry"]["initial_shared_version"] = "2"
        with self.assertRaises(ValueError): dm.verify(stale, chain(live))

    def test_invalid_and_unsupported_manifest(self):
        for key, value in [("network","testnet"), ("schema_version",2), ("schema_version",True), ("protocol_version",2), ("call_package_id","oops"), ("rpc_url","file:///tmp/x")]:
            manifest = fixture(); manifest[key] = value
            with self.assertRaises(ValueError): dm.validate(manifest)
        manifest = fixture(); manifest["type_origins"].pop("device_cap::DeviceAddressKey")
        with self.assertRaises(KeyError): dm.validate(manifest)
        manifest = fixture(); manifest["supported_clients"]["edge"]["max_exclusive"] = "0.1.0"
        with self.assertRaises(ValueError): dm.validate(manifest)

    def test_generated_pair_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); template = root / "device.toml"
            template.write_text('[device]\nkeypair_path="device.key"\ndevice_object_id="0xde"\ndevice_cap_id="0xca"\n[apply]\ncommand="/bin/true"\n')
            bundle = root / "release"
            dm.generate(fixture(), bundle, template)
            encoded = (bundle / "frontend.env").read_text().split("=",1)[1].strip().strip("'")
            self.assertEqual(json.loads(encoded), json.loads((bundle / "deployment.json").read_text()))
            self.assertEqual((bundle / "config.toml").read_text(), '[sui]\nmanifest_path = "deployment.json"\n\n' + template.read_text())
            with self.assertRaises(ValueError): dm.generate(fixture(), bundle, template)
            # Confidential delivery settings (SF-022) belong in the device template;
            # arrays of tables would bypass the section check and are refused.
            sealed = root / "sealed.toml"
            sealed.write_text(template.read_text() + '[seal]\nkey_server_package="0x5"\nkey_servers=[{object_id="0x6"}]\n')
            dm.generate(fixture(), root / "sealed", sealed)
            tables = root / "tables.toml"
            tables.write_text(template.read_text() + '[[seal.key_servers]]\nobject_id="0x6"\n')
            with self.assertRaises(ValueError): dm.generate(fixture(), root / "tables", tables)
            # Gas top-ups from the fleet budget (SF-083) are device settings too.
            funded = root / "funded.toml"
            funded.write_text(template.read_text() + '[budget]\nbudget_id="0xb0"\nlow_water_mist=20000000\n')
            dm.generate(fixture(), root / "funded", funded)
            other = root / "other.toml"
            other.write_text(template.read_text() + '[sui]\nmanifest_path="other.json"\n')
            with self.assertRaises(ValueError): dm.generate(fixture(), root / "other", other)

    def test_latest_package_checked_against_upgrade_cap(self):
        manifest = fixture(); ordinary = chain(manifest)
        def newer_cap(url, *ids):
            facts = ordinary(url, *ids)
            facts["objects"][manifest["upgrade_cap_id"]]["upgrade_cap"].update(package=dm.object_id("0x4"), version=4)
            return facts
        with self.assertRaisesRegex(ValueError, "UpgradeCap"):
            dm.verify(manifest, newer_cap)

    def test_missing_or_unshared_objects_rejected(self):
        manifest = fixture(); ordinary = chain(manifest)
        for object_key, change in [("upgrade_cap_id", {"kind": "absent"}),
                                   ("upgrade_cap_id", {"type": dm.object_id("0x2") + "::coin::Coin"}),
                                   ("call_package_id", {"kind": "object"}),
                                   ("registry", {"owner": {"address": dm.object_id("0x1")}})]:
            def altered(url, *ids):
                facts = ordinary(url, *ids)
                key = manifest["shared_objects"]["registry"]["id"] if object_key == "registry" else manifest[object_key]
                facts["objects"][key].update(change)
                return facts
            with self.assertRaises((ValueError, KeyError), msg=object_key): dm.verify(manifest, altered)

    def test_resolve_requires_every_type(self):
        manifest = fixture(); partial = copy.deepcopy(manifest)
        partial["type_origins"].pop("deployment_manager::RecipeDigestKey")
        args = SimpleNamespace(chain_identifier=manifest["chain_identifier"], upgrade_cap=manifest["upgrade_cap_id"], rpc=manifest["rpc_url"], package=manifest["call_package_id"], registry=manifest["shared_objects"]["registry"]["id"],
            publisher=manifest["walrus"]["publisher_url"], aggregator=manifest["walrus"]["aggregator_url"], network="localnet")
        with self.assertRaisesRegex(ValueError, "RecipeDigestKey"): dm.resolve(args, chain(partial))

    def test_declared_client_versions_match_packages(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(dm.CLIENT_VERSIONS["dapp"], json.loads((root / "dapp/package.json").read_text())["version"])
        import re
        actual = re.search(r'^version = "([^"]+)"', (root / "suifleet-edge/Cargo.toml").read_text(), re.MULTILINE)[1]
        self.assertEqual(dm.CLIENT_VERSIONS["edge"], actual)

    def test_duplicate_origin_rejected(self):
        package = chain(fixture())("", fixture()["call_package_id"])["objects"][fixture()["call_package_id"]]
        package["type_origins"].append(package["type_origins"][0])
        with self.assertRaises(ValueError): dm.origins(package)

if __name__ == "__main__": unittest.main()
