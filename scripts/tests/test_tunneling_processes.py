"""The tunnel cleanup probe must tolerate processes exiting during procfs reads."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('test_tunneling', Path(__file__).resolve().parents[1] / 'test_tunneling.py')
tunneling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunneling)


def process(pid, value):
    entry = Mock()
    entry.name = pid
    read = entry.__truediv__ = Mock()
    comm = read.return_value
    if isinstance(value, Exception):
        comm.read_text.side_effect = value
    else:
        comm.read_text.return_value = value
    return entry


class ProcessProbeTests(unittest.TestCase):
    def test_exit_during_enumeration_or_read_preserves_live_matches(self):
        proc = Mock()
        proc.iterdir.return_value = [process('1', FileNotFoundError()),
                                    process('2', ProcessLookupError()),
                                    process('3', 'suifleet-localp\n'),
                                    process('4', 'other\n'), process('self', 'suifleet-localp')]
        self.assertEqual(tunneling.proxy_pids(proc), [3])

    def test_unexpected_read_errors_still_fail(self):
        proc = Mock()
        proc.iterdir.return_value = [process('1', PermissionError())]
        with self.assertRaises(PermissionError):
            tunneling.proxy_pids(proc)
