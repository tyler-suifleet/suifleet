"""Offline tests for the contract drift gate, using synthetic Move summaries."""
from pathlib import Path
import sys
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

    def test_valid_call_passes(self):
        errors, checked = self.run_check(
            "tx.moveCall({ target: `${PACKAGE_ID}::device_registry::register_device`, arguments: [a, b] });")
        self.assertEqual(errors, [])
        self.assertEqual(checked, 1)

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
