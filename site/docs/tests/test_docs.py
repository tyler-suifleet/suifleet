import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

SITE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("docs_build", SITE / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class DocsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "output"

    def test_reproducible_curated_site_and_console_links(self):
        build.build(self.out)
        first = (self.out / "SHA256SUMS").read_bytes()
        build.build(self.out)
        self.assertEqual(first, (self.out / "SHA256SUMS").read_bytes())
        sources = {p["source"] for p in build.catalog()}
        self.assertFalse(any("tracking" in p or "COORDINATION" in p or "AGENTS" in p for p in sources))
        index = json.loads((self.out / "search.json").read_text())
        self.assertEqual(len(index), len(build.catalog()))
        self.assertTrue(any("provisioning" in p["text"] for p in index))
        console = (build.ROOT / "dapp/src/components/FirstRun.tsx").read_text()
        links = re.findall(r'https://docs.suifleet.io(/[^"\s]*)', console)
        self.assertGreaterEqual(len(links), 3)
        for link in links:
            self.assertTrue((self.out / link.strip("/") / "index.html").is_file(), link)

    def test_links_fail_on_missing_page_and_anchor(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            build.check(self.out)
        build.build(self.out)
        page = self.out / "install/index.html"
        original = page.read_text()
        for bad in ("/missing/", "/enrollment/#missing"):
            page.write_text(original + '<a href="' + bad + '">bad</a>')
            with self.assertRaisesRegex(ValueError, "broken"):
                build.check(self.out)

    def test_renderer_preserves_structure_and_safe_named_anchors(self):
        rendered = build.render('# Title\n\n## Heading\n\n## Heading\n\n<a id="stable"></a>\n\n<script>alert(1)</script>\n\n| A | B |\n|---|---|\n| one | two |\n\n~~~sh\necho "<x>"\n~~~\n', "docs/INSTALL.md", build.catalog())
        for expected in ('id="heading"', 'id="heading-1"', '<a id="stable"></a>', "<table>", "<pre>", "&lt;script&gt;", "&lt;x&gt;"):
            self.assertIn(expected, rendered)
        self.assertNotIn("<script>", rendered)

    def test_repository_links_and_sources_are_validated(self):
        pages = build.catalog()
        self.assertEqual(build.rewrite_url("ENROLLMENT.md#claim-enrollment-and-bulk-onboarding", "docs/INSTALL.md", pages), "/enrollment/#claim-enrollment-and-bulk-onboarding")
        self.assertIn("/blob/main/suifleet-edge/config.example.toml", build.rewrite_url("../suifleet-edge/config.example.toml", "docs/INSTALL.md", pages))
        for bad in ("MISSING.md", "../../../../../etc/passwd", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                build.rewrite_url(bad, "docs/INSTALL.md", pages)

    def test_build_does_not_erase_unowned_output(self):
        self.out.mkdir()
        keep = self.out / "important"
        keep.write_text("keep")
        with self.assertRaises(ValueError):
            build.build(self.out)
        self.assertEqual(keep.read_text(), "keep")
        with self.assertRaises(ValueError):
            build.build(SITE)

    def test_no_remote_embeds_or_tracking(self):
        build.build(self.out)
        for path in self.out.rglob("*.html"):
            doc = build.Links()
            doc.feed(path.read_text())
            self.assertNotRegex(path.read_text(), r'<(?:script|img)[^>]+src="https?:')
        script = (self.out / "assets/search.js").read_text()
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("localStorage", script)
        self.assertIn('fetch("/search.json")', script)


if __name__ == "__main__":
    unittest.main()
