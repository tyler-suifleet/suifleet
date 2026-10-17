"""Local-only safety and pinned-CLI chain-ID compatibility regressions."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "upgrade_security_localnet", Path(__file__).parents[1] / "upgrade_security_localnet.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class LocalChainIdentity(unittest.TestCase):
    def read(self, output, url="http://127.0.0.1:9000"):
        with patch.object(runner, "sui_json", return_value=([{"alias": "local", "rpc": url}], "local")), \
             patch.object(runner, "sui", return_value=SimpleNamespace(stdout=output)):
            return runner.require_localnet()[2]

    def test_pinned_cli_display(self):
        self.assertEqual(self.read("Base58: example\nHex: AB12cd34\n"), "ab12cd34")

    def test_legacy_raw_identifier(self):
        self.assertEqual(self.read("ab12cd34\n"), "ab12cd34")

    def test_rejects_unrecognized_output(self):
        for value in ("", "Base58: example", "Hex: ab12cd34 extra", "Hex: ab12cd345"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.read(value)

    def test_public_environment_still_refused(self):
        with self.assertRaises(SystemExit):
            self.read("Hex: 4c78adac", "https://fullnode.testnet.sui.io:443")
