"""Generic witness ABI stays reviewed without fabricating browser values."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate_interfaces as gen

class WitnessAbiTest(unittest.TestCase):
    def test_named_witness_and_reference_are_preserved(self):
        self.assertEqual(gen.move_type({"NamedTypeParameter": "W"}), "W")
        self.assertEqual(gen.move_type({"Reference": [False, {"NamedTypeParameter": "W"}]}), "&W")
        with self.assertRaisesRegex(ValueError, "unsupported compiled Move type"):
            gen.move_type({"UnexpectedType": "W"})

    def test_capture_keeps_witness_constraints(self):
        constraints = [{"name": "W", "constraints": ["Drop"]}]
        modules = {"customer": {"functions": {"charge": {"visibility": "Public", "entry": False,
            "type_parameters": constraints, "parameters": [{"type_": {"NamedTypeParameter": "W"}}], "return_": []}},
            "structs": {}, "enums": {}}}
        normalized = gen.normalize(modules)["customer"]["functions"]["charge"]
        self.assertEqual(normalized["parameters"], ["W"])
        self.assertEqual(normalized["type_parameters"], constraints)

if __name__ == "__main__":
    unittest.main()
