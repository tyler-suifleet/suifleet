"""Exercise the Layer Index report against ordinary Git branch fixtures."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_yocto_layer_index as index


class ReadinessReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.source = root / "source"
        self.source.mkdir()
        self.fetch = root / "fetch"
        self.fetch.mkdir()
        index.git(self.source, "init", "-b", "scarthgap")
        self.evidence = json.loads(index.EVIDENCE.read_text())
        self.evidence["repository"] = str(self.source)
        conf = self.source / "conf/layer.conf"
        conf.parent.mkdir()
        conf.write_text(
            'BBFILES += "${LAYERDIR}/recipes-*/*/*.bb"\n'
            'BBFILE_COLLECTIONS += "meta-suifleet"\n'
            'LAYERDEPENDS_meta-suifleet = "core openembedded-layer"\n'
            'LAYERSERIES_COMPAT_meta-suifleet = "scarthgap wrynose"\n'
        )
        (self.source / "README.md").write_text(
            f'Maintainer: {self.evidence["maintainer"]}\nMIT\n## Contributions\n'
        )
        (self.source / "COPYING.MIT").write_text("Permission is hereby granted")
        recipe_dir = self.source / "recipes-edge"
        for recipe in index.RECIPES:
            dest = recipe_dir / recipe
            dest.mkdir(parents=True)
            (dest / f"{recipe}_1.0.bb").touch()
        self.revision = self.commit()
        index.git(self.source, "branch", "wrynose", self.revision)
        for series in self.evidence["series"].values():
            series.update(revision=self.revision, layer_check_revision=self.revision)

    def commit(self):
        index.git(self.source, "add", ".")
        index.git(self.source, "-c", "user.name=Test", "-c", "user.email=test@example.org",
                  "commit", "-m", "Fixture")
        return index.git(self.source, "rev-parse", "HEAD")

    def test_fetch_reports_missing_older_ref_without_losing_current_series(self):
        report = index.inspect(self.fetch, self.evidence)
        self.assertIn("fetch_error", report["kirkstone"])
        for branch in ("scarthgap", "wrynose"):
            self.assertTrue(report[branch]["revision_matches_evidence"])
            self.assertTrue(report[branch]["layer_check_revision_matches"])
            self.assertEqual(len(report[branch]["recipes"]), 3)

    def test_branch_drift_invalidates_evidence_and_metadata(self):
        (self.source / "COPYING.MIT").unlink()
        (self.source / "COPYING.MIT").symlink_to("README.md")
        changed = self.commit()
        report = index.inspect(self.fetch, self.evidence)
        self.assertEqual(report["scarthgap"]["revision"], changed)
        self.assertFalse(report["scarthgap"]["revision_matches_evidence"])
        self.assertFalse(report["scarthgap"]["layer_check_revision_matches"])
        self.assertIn("regular committed file", report["scarthgap"]["metadata_error"])
        self.assertEqual(report["wrynose"]["revision"], self.revision)

    def test_command_rejects_missing_candidate(self):
        index.git(self.source, "branch", "-D", "wrynose")
        evidence = self.fetch / "evidence.json"
        evidence.write_text(json.dumps(self.evidence))
        with patch.object(index, "EVIDENCE", evidence), patch("builtins.print"), \
                patch("sys.argv", ["check_yocto_layer_index.py"]):
            self.assertEqual(index.main(), 1)
