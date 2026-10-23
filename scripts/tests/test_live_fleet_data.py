"""Acceptance setup must refuse missing artifacts before wallet side effects."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import live_fleet_data as live

class ArtifactPreflightTests(unittest.TestCase):
    def test_missing_codec_never_initializes_or_funds_a_wallet(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'SUIFLEET_TESTNET_LOCK_TOKEN':'offline-test-token'}), patch.object(live,'Live') as wallet:
            with self.assertRaises(FileNotFoundError):live.run(SimpleNamespace(codec=Path(d)/'missing'))
            wallet.assert_not_called()
