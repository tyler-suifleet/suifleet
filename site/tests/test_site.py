"""Offline tests for the suifleet.io site build and checker (SF-172).

    python3 -m unittest discover -s site/tests -v
"""
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SITE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SITE))
import build  # noqa: E402
import check  # noqa: E402
import verify_served  # noqa: E402


def rewrite_sums(out):
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out).as_posix()}\n" for p in files))


class SiteTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="sf172-site-"))
        self.addCleanup(shutil.rmtree, self.temp)
        self.out = self.temp / "dist"
        build.build(self.out)

    def edit(self, rel, old, new):
        path = self.out / rel
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1))
        rewrite_sums(self.out)

    def errors(self, **kwargs):
        return check.check(self.out, **kwargs)

    def test_build_passes_checks(self):
        self.assertEqual(self.errors(), [])

    def test_build_is_reproducible(self):
        again = self.temp / "again"
        build.build(again)
        first = {p.relative_to(self.out): p.read_bytes() for p in self.out.rglob("*") if p.is_file()}
        second = {p.relative_to(again): p.read_bytes() for p in again.rglob("*") if p.is_file()}
        self.assertEqual(first, second)

    def test_every_configured_page_and_launch_link(self):
        config = json.loads((SITE / "src" / "site.json").read_text())
        for page in config["pages"]:
            html = build.output_path(self.out, page["path"]).read_text()
            self.assertIn(f'href="{config["app_url"]}"', html, page["path"])
        home = (self.out / "index.html").read_text()
        self.assertIn(">Launch app<", home)
        guide = (self.out / "guide" / "index.html").read_text()
        steps = json.loads((SITE / "guide" / "steps.json").read_text())["steps"]
        self.assertEqual(len(steps), 7)
        for step in steps:
            self.assertIn(f'id="{step["id"]}"', guide)
            self.assertIn(f'href="{config["app_url"]}{step["console_route"]}"', guide)

    def test_broken_internal_link(self):
        self.edit("index.html", 'href="/guide/"', 'href="/missing/"')
        self.assertTrue(any("broken link /missing/" in e for e in self.errors()))

    def test_missing_anchor(self):
        self.edit("index.html", 'href="/security/#status"', 'href="/security/#nowhere"')
        self.assertTrue(any("missing anchor" in e for e in self.errors()))

    def test_no_scripts_or_trackers(self):
        self.edit("index.html", "</main>", '<script src="/assets/a.js"></script></main>')
        self.assertTrue(any("forbidden <script>" in e for e in self.errors()))

    def test_no_third_party_resources(self):
        self.edit("index.html", 'src="/assets/logo.svg"', 'src="https://github.com/pixel.png"')
        self.assertTrue(any("externally hosted resource" in e for e in self.errors()))

    def test_inline_handler(self):
        self.edit("index.html", '<a class="skip"', '<a onclick="x()" class="skip"')
        self.assertTrue(any("inline event handler" in e for e in self.errors()))

    def test_unlisted_host(self):
        self.edit("index.html", 'href="/guide/"', 'href="https://example.com/"')
        self.assertTrue(any("not in site.json external_hosts" in e for e in self.errors()))

    def test_repository_link_must_exist(self):
        self.edit("docs/index.html", "/blob/main/docs/ENROLLMENT.md", "/blob/main/docs/NOT_A_DOC.md")
        self.assertTrue(any("repository path does not exist" in e for e in self.errors()))

    def test_build_refuses_missing_repository_path(self):
        with self.assertRaises(build.BuildError):
            build.expand("{{repo:docs/NOT_A_DOC.md}}", json.loads((SITE / "src" / "site.json").read_text()), "x")

    def test_build_refuses_unknown_token(self):
        with self.assertRaises(build.BuildError):
            build.expand("{{tracking_pixel}}", json.loads((SITE / "src" / "site.json").read_text()), "x")

    def test_build_refuses_to_replace_arbitrary_directory(self):
        other = self.temp / "other"
        other.mkdir()
        (other / "keep.txt").write_text("keep")
        with self.assertRaises(build.BuildError):
            build.build(other)
        self.assertTrue((other / "keep.txt").exists())

    def test_tampered_file_fails_sums(self):
        (self.out / "assets" / "style.css").write_text("body{}")
        self.assertTrue(any("SHA256SUMS does not match assets/style.css" in e for e in self.errors()))

    def test_csp_meta_required(self):
        self.edit("pricing/index.html", "default-src 'none'", "default-src *")
        self.assertTrue(any("Content-Security-Policy" in e for e in self.errors()))

    def test_video_digest_and_required_videos(self):
        media = self.temp / "media"
        (media / "guide").mkdir(parents=True)
        video = b"\x1aE\xdf\xa3 not really webm"
        (media / "guide" / "sign-in.webm").write_bytes(video)
        manifest = {"schema_version": 1, "network": "testnet", "chain": "4c78adac", "console_commit": "0" * 40,
                    "recorded": "2026-10-27", "videos": [{"step": "sign-in", "file": "sign-in.webm",
                                                          "file_sha256": hashlib.sha256(video).hexdigest(),
                                                          "width": 1280, "height": 800, "caption": "Signing in."}]}
        (media / "guide" / "videos.json").write_text(json.dumps(manifest))
        with mock.patch.object(build, "MEDIA", media), mock.patch.object(check, "SITE", self.temp):
            for name in ("src", "guide"):
                (self.temp / name).symlink_to(SITE / name)
            build.build(self.out)
            self.assertEqual(self.errors(), [])
            guide = (self.out / "guide" / "index.html").read_text()
            self.assertIn('<source src="/media/guide/sign-in.webm" type="video/webm">', guide)
            missing = self.errors(require_videos=True)
            self.assertEqual(len(missing), 6, missing)
            (self.out / "media" / "guide" / "sign-in.webm").write_bytes(b"swapped")
            rewrite_sums(self.out)
            self.assertTrue(any("does not match its recorded digest" in e for e in self.errors()))
            manifest["chain"] = "35834a8a"
            (media / "guide" / "videos.json").write_text(json.dumps(manifest))
            self.assertTrue(any("recorded on Sui testnet" in e for e in self.errors()))

    def test_served_paths_match_static_hosting(self):
        self.assertEqual(verify_served.served_path("index.html"), "/")
        self.assertEqual(verify_served.served_path("guide/index.html"), "/guide/")
        self.assertEqual(verify_served.served_path("assets/style.css"), "/assets/style.css")

    def test_served_header_fetch_failure_is_reported(self):
        # All file reads succeed; only the final header request fails.
        def fetch(url):
            if fetch.remaining == 0:
                raise OSError("connection closed")
            fetch.remaining -= 1
            name = url.removeprefix("https://suifleet.io/")
            path = self.out / name
            if path.is_dir():
                path /= "index.html"
            return path.read_bytes(), {}

        fetch.remaining = sum(
            line.split("  ", 1)[1] not in ("_headers", "404.html")
            for line in (self.out / "SHA256SUMS").read_text().splitlines()
        )
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["verify_served.py", str(self.out), "https://suifleet.io"]), \
                mock.patch.object(verify_served, "fetch", side_effect=fetch), \
                mock.patch.object(sys, "stdout", output):
            with self.assertRaisesRegex(SystemExit, "1 problem\\(s\\)"):
                verify_served.main()
        self.assertIn("ERROR: home page headers: fetch failed: connection closed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
