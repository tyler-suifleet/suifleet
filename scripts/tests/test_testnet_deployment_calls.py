"""Offline regression for same-package base/head live acceptance PTBs."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_package_upgrade import deployment_call


class DeploymentCalls(unittest.TestCase):
    def call(self, package, version, **kwargs):
        with patch("live_package_upgrade.live_common.deployment_args", return_value=["recipe"]):
            return deployment_call(package, version, walrus=None, run_id="run", new="head",
                                   inboxes=True, inbox="inbox", registry="registry", owner_cap="cap",
                                   **kwargs)

    def test_base_call_never_supersedes_head_even_when_package_is_reused(self):
        for base in ("base", "head"):
            with self.subTest(base=base):
                ptb = self.call(base, "3.0.0", devices=["device"], announce=False)
                self.assertEqual(ptb[:2], ["--move-call", base + "::deployment_manager::create_deployment_verified"])
                self.assertFalse(any("fleet_inbox" in arg for arg in ptb))
                self.assertNotIn("--assign", ptb)

    def test_head_device_and_group_still_announce_and_share(self):
        for target, announcement in (({"devices": ["device"]}, "announce_device"),
                                     ({"groups": ["group"]}, "announce_group")):
            ptb = self.call("head", "4.0.0", **target)
            self.assertIn("head::deployment_manager::new_deployment_verified", ptb)
            self.assertIn("head::fleet_inbox::" + announcement, ptb)
            self.assertEqual(ptb[-3:], ["--move-call", "head::deployment_manager::share_deployment", "record_4_0_0"])

    def test_older_package_uses_legacy_entry_without_inbox_api(self):
        ptb = self.call("base", "3.0.0", devices=["device"])
        self.assertIn("base::deployment_manager::create_deployment_verified", ptb)
        self.assertFalse(any("fleet_inbox" in arg for arg in ptb))
