"""Offline tests for the contract drift gate, using synthetic Move summaries."""
from pathlib import Path
import sys
import json
import shutil
from copy import deepcopy
from unittest.mock import patch
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_contract_interfaces as drift  # noqa: E402


def function(*parameters, visibility="Public", entry=False):
    return {"visibility": visibility, "entry": entry,
            "parameters": [{"type_": parameter} for parameter in parameters]}


TX_CONTEXT = {"Reference": [True, {"Datatype": {"name": "TxContext"}}]}
MODULES = {
    "device_registry": {
        "functions": {"register_device": function("u64", "u64", TX_CONTEXT)},
        "structs": {"Device": {}, "DeviceRegistry": {}},
    },
    "device_cap": {"functions": {}, "structs": {"DeviceCap": {}, "DeviceAddressKey": {}}},
    "deployment_manager": {"functions": {}, "structs": {"ReceiptsKey": {}, "RecipeDigestKey": {}}},
}
FRAMEWORK = {"package", "coin", "object", "string"}


class DriftCheckTest(unittest.TestCase):
    def run_check(self, source, suffix=".ts", modules=MODULES):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"client{suffix}"
            path.write_text(source)
            return drift.check(modules, FRAMEWORK, [path])

    def assert_drift(self, source, fragment, suffix=".ts", modules=MODULES):
        errors, checked = self.run_check(source, suffix, modules)
        self.assertTrue(checked, "the reference must be recognized")
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_separate_package_compilation_and_drift(self):
        """Exercise collection of distinct compiler outputs, not an exemption."""
        data = {"functions": {"seal_approve_": function("vector", "object", TX_CONTEXT)},
                "structs": {"AnchorKey": {}, "Anchor": {}}}
        compiled = {
            "core": ("sui_edge", MODULES),
            "fleet_storage": ("fleet_storage", {"fleet_storage": {"functions": {}, "structs": {}}}),
            "fleet_data": ("fleet_data", {"fleet_data": data}),
        }
        calls = []

        def compiler(argv, **kwargs):
            package = Path(argv[argv.index("--path") + 1])
            output = Path(argv[argv.index("--output-directory") + 1])
            key = package.name if package.name in drift.EXTRA_PACKAGES else "core"
            calls.append(key)
            name, modules = compiled[key]
            (output / name).mkdir(parents=True)
            (output / "address_mapping.json").write_text(json.dumps({name: "0x0"}))
            for module, summary in modules.items():
                (output / name / f"{module}.json").write_text(json.dumps(summary))

        with tempfile.TemporaryDirectory() as directory, patch.object(drift.subprocess, "run", compiler):
            modules, framework = drift.summarize_packages(Path(directory) / "contracts", Path(directory) / "summaries")
        self.assertEqual(calls, ["core", "fleet_storage", "fleet_data"])
        self.assertIn("fleet_data", modules)
        source = ('"fleet_data::AnchorKey"; "fleet_data::Anchor"; '
                  'tx.moveCall({ target: `${DATA_PACKAGE}::fleet_data::seal_approve_`, arguments: [a, b] });')
        self.assertEqual(self.run_check(source, modules=modules), ([], 3))
        for old, new, message in (
            ("AnchorKey", "MissingKey", "MissingKey is not a struct"),
            ("Anchor\"", "MissingAnchor\"", "MissingAnchor is not a struct"),
            ("seal_approve_", "missing_call", "missing_call is not a function"),
            ("[a, b]", "[a]", "takes 2 arguments; client passes 1"),
        ):
            with self.subTest(message=message):
                self.assert_drift(source.replace(old, new), message, modules=modules)
        self.assert_drift(source, "no module fleet_data", modules=MODULES)
        missing_type = {**modules, "fleet_data": {**data, "structs": {"AnchorKey": {}}}}
        self.assert_drift(source, "Anchor is not a struct", modules=missing_type)

    @unittest.skipUnless(shutil.which("sui"), "requires the pinned Sui compiler")
    def test_real_fleet_data_summary_rejects_missing_types_and_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            modules, _ = drift.summarize_packages(drift.ROOT / "contracts", Path(directory))
        source = ('"fleet_data::AnchorKey"; "fleet_data::Anchor"; '
                  'tx.moveCall({ target: `${DATA_PACKAGE}::fleet_data::seal_approve_support`, arguments: [a, b] });')
        self.assertEqual(self.run_check(source, modules=modules), ([], 3))
        for kind, name in (("structs", "AnchorKey"), ("structs", "Anchor"),
                           ("functions", "seal_approve_support")):
            with self.subTest(kind=kind, name=name):
                changed = deepcopy(modules)
                del changed["fleet_data"][kind][name]
                self.assert_drift(source, f"{name} is not a", modules=changed)
        self.assert_drift(source.replace("[a, b]", "[a]"), "takes 2 arguments", modules=modules)
        self.assert_drift(source.replace("AnchorKey", "AnchorKeys"), "AnchorKeys is not a struct", modules=modules)

    def test_valid_call_passes(self):
        errors, checked = self.run_check(
            "tx.moveCall({ target: `${PACKAGE_ID}::device_registry::register_device`, arguments: [a, b] });")
        self.assertEqual(errors, [])
        self.assertEqual(checked, 1)

    def test_reviewed_external_types_only(self):
        # Seal's key server type is another package's, verified at runtime.
        errors, _ = self.run_check('same_type(&t, &format!("{package}::key_server::KeyServer"));\n"key_server::KeyServer";', ".rs")
        self.assertEqual(errors, [])
        self.assert_drift('format!("{package}::key_server::KeyServerV9")', "no module key_server", ".rs")

    def test_misspelled_project_module_in_move_call_fails(self):
        self.assert_drift(
            "tx.moveCall({ target: `${PACKAGE_ID}::device_registrx::register_device`, arguments: [a, b] });",
            "no module device_registrx")

    def test_missing_function_in_move_call_fails(self):
        self.assert_drift(
            "tx.moveCall({ target: `${PACKAGE_ID}::device_registry::register_devic`, arguments: [a, b] });",
            "is not a function")

    def test_arity_drift_fails(self):
        self.assert_drift(
            "tx.moveCall({ target: `${PACKAGE_ID}::device_registry::register_device`, arguments: [a] });",
            "takes 2 arguments; client passes 1")

    def test_manifest_style_type_names_are_checked(self):
        source = 'export const REQUIRED = ["device_cap::DeviceAddressKey", "deployment_manager::ReceiptsKey"];\n' \
                 'moveType("deployment_manager::ReceiptsKey");'
        errors, checked = self.run_check(source)
        self.assertEqual(errors, [])
        self.assertEqual(checked, 3)

    def test_type_missing_from_compiled_summary_fails(self):
        modules = {**MODULES, "device_cap": {"functions": {}, "structs": {"DeviceCap": {}}}}
        self.assert_drift('const t = "device_cap::DeviceAddressKey";', "DeviceAddressKey is not a struct",
                          modules=modules)

    def test_manifest_json_keys_are_checked(self):
        self.assert_drift('{"type_origins": {"deployment_manager::ReceiptKey": "0x1"}}',
                          "ReceiptKey is not a struct", suffix=".json")

    def test_misspelled_module_in_bare_type_fails(self):
        self.assert_drift('moveType("device_capp::DeviceCap")', "no module device_capp")

    def test_rust_format_and_bare_references(self):
        source = 'format!("{}::deployment_manager::RecipeDigestKey", origin);\n' \
                 'sui.move_type("deployment_manager::RecipeDigstKey");'
        errors, checked = self.run_check(source, ".rs")
        self.assertEqual(checked, 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("RecipeDigstKey", errors[0])

    def test_daemon_private_non_entry_function_fails(self):
        modules = {"deployment_manager": {"structs": {}, "functions": {
            "report_status": function(visibility="Private", entry=False),
        }}}
        self.assert_drift('let mut function = "report_status"; json!(["deployment_manager", function, []]);',
                          "is not callable from a transaction", suffix=".rs", modules=modules)

    def test_daemon_grpc_module_constant_and_field_forms(self):
        modules = {"deployment_manager": {"structs": {}, "functions": {
            "report_status": function(),
            "report_status_for_group": function(),
        }}}
        source = ('const CALL_MODULE: &str = "deployment_manager";\n'
                  'let call = Call { function: "report_status" };\ncall.function = "report_status_for_group";')
        errors, checked = self.run_check(source, suffix=".rs", modules=modules)
        self.assertEqual((errors, checked), ([], 2))
        self.assert_drift(source.replace("report_status_for_group", "report_status_for_grp"),
                          "report_status_for_grp is not a function", suffix=".rs", modules=modules)
        self.assert_drift(source.replace('"deployment_manager"', '"deployment_mgr"'),
                          "is not a function", suffix=".rs", modules=modules)

    def test_daemon_public_and_private_entry_functions_pass(self):
        for visibility, entry in (("Public", False), ("Private", True)):
            with self.subTest(visibility=visibility, entry=entry):
                modules = {"deployment_manager": {"structs": {}, "functions": {
                    "report_status": function(visibility=visibility, entry=entry),
                }}}
                errors, checked = self.run_check(
                    'let mut function = "report_status"; json!(["deployment_manager", function, []]);',
                    suffix=".rs", modules=modules)
                self.assertEqual(errors, [])
                self.assertEqual(checked, 1)

    def test_framework_and_rust_paths_are_ignored(self):
        source = 'if (t !== "0x2::package::UpgradeCap" || rest !== "package::UpgradeCap") {}\n' \
                 'let x = std::fs::read(p); tokio::time::sleep(d); "0x1::string::String";'
        self.assertEqual(self.run_check(source), ([], 0))


if __name__ == "__main__":
    unittest.main()
