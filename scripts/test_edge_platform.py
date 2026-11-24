#!/usr/bin/env python3
"""Regressions for the compatibility profile CLI."""

import json
import subprocess
import sys
import unittest

from edge_platform import MATRIX


ROOT = MATRIX.parents[1]


class EdgePlatforms(unittest.TestCase):
    def query(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/edge_platform.py"), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_profile_metadata(self):
        profiles = json.loads(MATRIX.read_text())["profiles"]
        for name, profile in profiles.items():
            with self.subTest(profile=name):
                for field in ("arch", "libc", "kernel", "init", "rust"):
                    self.assertTrue(profile[field])
                if profile["status"] == "current":
                    self.assertTrue((ROOT / profile["evidence"]).is_file())
                else:
                    self.assertTrue(profile["blockers"])
                for path in profile.get("kas", []):
                    self.assertTrue((ROOT / path).is_file())

    def test_registered_checks(self):
        for profile, check in (
            ("linux-amd64-systemd", "ota-simulator"),
            ("linux-amd64-musl-5.15", "legacy-vm-smoke"),
        ):
            with self.subTest(profile=profile):
                result = self.query(profile, "--require-check", check)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(check, json.loads(result.stdout)["checks"])

    def test_unsupported_requests_explain_the_blocker(self):
        for profile, check, reason in (
            ("linux-amd64-musl-5.15", "isolated-signer", "SF-292/#818"),
            ("yocto-kirkstone", "image-build", "SF-295/#820"),
            ("yocto-dunfell", "image-build", "SF-295/#820"),
            ("missing-profile", "ota-simulator", "available profiles"),
        ):
            with self.subTest(profile=profile):
                result = self.query(profile, "--require-check", check)
                self.assertEqual(result.returncode, 2)
                self.assertIn(profile, result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
