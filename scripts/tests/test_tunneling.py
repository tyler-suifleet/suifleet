"""Regression coverage for process churn during tunnel containment assertions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from scripts.test_tunneling import proxy_pids


class ProxyPidsTests(unittest.TestCase):
    def test_only_matching_numeric_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid, name in (("123", "suifleet-localp\n"), ("456", "other\n"),
                              ("self", "suifleet-localp\n")):
                (proc / pid).mkdir()
                (proc / pid / "comm").write_text(name)
            (proc / "789").mkdir()  # Exited before comm could be opened.
            self.assertEqual(proxy_pids(proc), [123])

    def test_exit_during_read_does_not_hide_surviving_proxy(self):
        for error in (FileNotFoundError, ProcessLookupError):
            with self.subTest(error=error):
                vanished, survivor = Mock(name="vanished"), Mock(name="survivor")
                vanished.name, survivor.name = "123", "456"
                vanished.__truediv__ = Mock(return_value=Mock(read_text=Mock(side_effect=error)))
                survivor.__truediv__ = Mock(return_value=Mock(read_text=Mock(return_value="suifleet-localp\n")))
                proc = Mock(iterdir=Mock(return_value=iter([vanished, survivor])))
                self.assertEqual(proxy_pids(proc), [456])

    def test_unexpected_read_failure_is_not_suppressed(self):
        process = Mock()
        process.name = "123"
        process.__truediv__ = Mock(return_value=Mock(read_text=Mock(side_effect=PermissionError)))
        with self.assertRaises(PermissionError):
            proxy_pids(Mock(iterdir=Mock(return_value=iter([process]))))


if __name__ == "__main__":
    unittest.main()
