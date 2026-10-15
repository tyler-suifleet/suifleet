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
        rpc_url="http://127.0.0.1:9000", call_package_id=dm.object_id("0x3"), upgrade_cap_id=dm.object_id("0xc0"), package_version="3",
        type_origins={name: dm.object_id("0x2" if name.endswith("RecipeDigestKey") else "0x3" if name.endswith("DeviceAddressKey") else "0x1") for name in dm.REQUIRED_TYPES},
        shared_objects={"registry": {"id":dm.object_id("0xbe"), "initial_shared_version":"1"}},
        supported_clients={"dapp": {"min":"0.0.2", "max_exclusive":"0.2.0"}, "edge": {"min":"0.1.0", "max_exclusive":"0.3.0"}},
        walrus={"publisher_url":"https://publisher.example", "aggregator_url":"https://aggregator.example"})


def chain(manifest):
    def call(url, method, params):
        if method == "sui_getChainIdentifier": return manifest["chain_identifier"]
        if params[0] == manifest["call_package_id"]:
            return {"data": {"version": manifest["package_version"], "bcs": {"dataType":"package", "typeOriginTable":[
                {"module_name":name.split("::")[0], "datatype_name":name.split("::")[1], "package":origin}
                for name, origin in manifest["type_origins"].items()]}}}
        if params[0] == manifest["upgrade_cap_id"]:
            return {"data":{"content":{"type":"0x2::package::UpgradeCap","fields":{"package":manifest["call_package_id"],"version":manifest["package_version"]}}}}
        return {"data": {"owner":{"Shared":{"initial_shared_version":"1"}}, "content":{
            "type":manifest["type_origins"]["device_registry::DeviceRegistry"] + "::device_registry::DeviceRegistry"}}}
    return call


class ManifestTests(unittest.TestCase):
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

    def test_latest_package_checked_against_upgrade_cap(self):
        manifest = fixture(); ordinary = chain(manifest)
        def newer_cap(url, method, params):
            result = ordinary(url, method, params)
            if method == "sui_getObject" and params[0] == manifest["upgrade_cap_id"]:
                result["data"]["content"]["fields"] = {"package":"0x4", "version":"4"}
            return result
        with self.assertRaisesRegex(ValueError, "UpgradeCap"):
            dm.verify(manifest, newer_cap)

    def test_declared_client_versions_match_packages(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(dm.CLIENT_VERSIONS["dapp"], json.loads((root / "dapp/package.json").read_text())["version"])
        import re
        actual = re.search(r'^version = "([^"]+)"', (root / "suifleet-edge/Cargo.toml").read_text(), re.MULTILINE)[1]
        self.assertEqual(dm.CLIENT_VERSIONS["edge"], actual)

    def test_duplicate_origin_rejected(self):
        raw = chain(fixture())("", "sui_getObject", [fixture()["call_package_id"]])
        raw["data"]["bcs"]["typeOriginTable"].append(raw["data"]["bcs"]["typeOriginTable"][0])
        with self.assertRaises(ValueError): dm.origins(raw)

if __name__ == "__main__": unittest.main()
