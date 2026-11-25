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
                if "evidence" in profile:
                    self.assertTrue((ROOT / profile["evidence"]).is_file())
                if profile["status"] == "current":
                    self.assertIn("evidence", profile)
                else:
                    self.assertTrue(profile["blockers"])
                for path in profile.get("kas", []):
                    self.assertTrue((ROOT / path).is_file())

    def test_registered_checks(self):
        for profile, check in (
            ("linux-amd64-systemd", "ota-simulator"),
            ("linux-amd64-musl-5.15", "legacy-vm-smoke"),
            ("yocto-kirkstone", "image-build"),
        ):
            with self.subTest(profile=profile):
                result = self.query(profile, "--require-check", check)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(check, json.loads(result.stdout)["checks"])

    def test_legacy_yocto_support_scope(self):
        profiles = json.loads(MATRIX.read_text())["profiles"]
        kirkstone = profiles["yocto-kirkstone"]
        self.assertEqual(kirkstone["status"], "current")
        self.assertEqual(
            kirkstone["kas"],
            ["kas/packaging-kirkstone.yml", "kas/packaging-kirkstone-qemu.yml"],
        )
        self.assertEqual(kirkstone["evidence"], "docs/YOCTO_LEGACY_SERIES.md")
        self.assertIn("lack SO_PEERPIDFD", kirkstone["kernel"])
        self.assertTrue(any("SF-031/#32" in reason for reason in kirkstone["blockers"]))
        dunfell = profiles["yocto-dunfell"]
        self.assertEqual(dunfell["status"], "blocked")
        self.assertFalse(dunfell.get("kas"))
        self.assertEqual(dunfell["evidence"], kirkstone["evidence"])
        for profile in profiles.values():
            self.assertNotIn("SF-292/#818", " ".join(profile.get("blockers", [])))

    def test_unsupported_requests_explain_the_blocker(self):
        for profile, check, reasons in (
            ("linux-amd64-musl-5.15", "isolated-signer", ("Portable signer integration is unqualified",)),
            ("yocto-kirkstone", "isolated-signer", ("requires SO_PEERPIDFD", "SF-031/#32")),
            ("yocto-dunfell", "image-build", ("lacks a compatible Rust >= 1.90 toolchain", "SF-295/#820 strict log ingestion")),
            ("linux-legacy-target", "strict-log-ingestion", ("SF-296/#830 strict log ingestion",)),
            ("missing-profile", "ota-simulator", ("available profiles",)),
        ):
            with self.subTest(profile=profile):
                result = self.query(profile, "--require-check", check)
                self.assertEqual(result.returncode, 2)
                self.assertIn(profile, result.stderr)
                for reason in reasons:
                    self.assertIn(reason, result.stderr)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
