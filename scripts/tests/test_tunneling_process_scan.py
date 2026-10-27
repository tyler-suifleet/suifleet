"""Process-exit races must not hide failures in the tunnel cleanup assertion."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

SPEC = importlib.util.spec_from_file_location(
    "tunnel_harness", Path(__file__).resolve().parents[1] / "test_tunneling.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


class ProcessScanTests(unittest.TestCase):
    def scan(self, error):
        gone, live, other = Mock(), Mock(), Mock()
        gone.name, live.name, other.name = "100", "101", "102"
        gone.__truediv__ = Mock(return_value=Mock(read_text=Mock(side_effect=error)))
        live.__truediv__ = Mock(return_value=Mock(read_text=Mock(return_value="suifleet-localp\n")))
        other.__truediv__ = Mock(return_value=Mock(read_text=Mock(return_value="other\n")))
        proc = Mock(iterdir=Mock(return_value=[gone, live, other]))
        return harness.proxy_pids(proc)

    def test_exit_during_read_keeps_live_proxy_visible(self):
        for error in (FileNotFoundError(), ProcessLookupError()):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.scan(error), [101])

    def test_unexpected_read_failure_is_not_silenced(self):
        with self.assertRaises(PermissionError):
            self.scan(PermissionError())
