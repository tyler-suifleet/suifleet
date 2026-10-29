#!/usr/bin/env python3
"""Check a built suifleet.io site (SF-172): links, privacy policy and videos.

    python3 site/check.py site/dist [--require-videos]

Offline and deterministic, so it runs in CI without network access:
  - every internal href/src/poster resolves to a built file, and every
    #fragment to an element id on the target page;
  - external links use https and an allowlisted host (site.json); GitHub links
    into this repository must name a file or directory that exists here;
  - no <script>, <iframe>, <object>, <embed>, <form>, inline event handler,
    javascript: URL or externally hosted resource: the site runs no code and
    loads nothing from third parties, so it cannot track visitors;
  - every page has lang, a title, a description, exactly one <h1> and the
    Content-Security-Policy meta tag;
  - SHA256SUMS matches the files, and guide videos match their recorded
    digests (with --require-videos, every guide step needs one).
"""
import argparse
import hashlib
from html.parser import HTMLParser
import json
import re
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent
FORBIDDEN_TAGS = {"script", "iframe", "object", "embed", "form", "frame", "applet"}
URL_ATTRS = {"href", "src", "poster", "action", "data", "srcset"}
CSP = "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; media-src 'self'; base-uri 'none'; form-action 'none'"


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls, self.ids, self.problems = [], set(), []
        self.h1 = 0
        self.lang = self.title = self.description = self.csp = self.canonical = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in FORBIDDEN_TAGS:
            self.problems.append(f"forbidden <{tag}>")
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href")
            attrs = {k: v for k, v in attrs.items() if k != "href"}
        for name, value in attrs.items():
            if name.startswith("on"):
                self.problems.append(f"inline event handler {name} on <{tag}>")
            if name == "style":
                self.problems.append(f"inline style on <{tag}> (CSP blocks it)")
            if name in URL_ATTRS and value is not None:
                self.urls.append((tag, name, value.strip()))
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "html":
            self.lang = attrs.get("lang")
        if tag == "h1":
            self.h1 += 1
        if tag == "title":
            self._in_title = True
            self.title = ""
        if tag == "meta" and attrs.get("name") == "description":
            self.description = attrs.get("content")
        if tag == "meta" and (attrs.get("http-equiv") or "").lower() == "content-security-policy":
            self.csp = attrs.get("content")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def resolve(out, path):
    """The built file a site-relative path serves, or None."""
    target = out / unquote(path).lstrip("/")
    if path.endswith("/"):
        target = target / "index.html"
    elif target.is_dir():
        return None  # Hosts redirect /x to /x/ inconsistently; links must use the slash.
    return target if target.is_file() and out in target.resolve().parents else None


def check_external(url, config, where, errors):
    parts = urlsplit(url)
    if parts.scheme == "mailto":
        if parts.path != config["contact_email"]:
            errors.append(f"{where}: unexpected mail address {url}")
        return
    if parts.scheme != "https":
        errors.append(f"{where}: non-https external link {url}")
        return
    if parts.hostname not in config["external_hosts"]:
        errors.append(f"{where}: host {parts.hostname} is not in site.json external_hosts ({url})")
        return
    repo = config["repo_url"]
    if url == repo or url.startswith(repo + "/"):
        rest = url[len(repo):].strip("/").split("/")
        if rest[:1] in (["blob"], ["tree"]):
            if len(rest) < 3 or rest[1] != config["repo_branch"]:
                errors.append(f"{where}: repository link must use branch {config['repo_branch']}: {url}")
                return
            local = ROOT.joinpath(*rest[2:])
            if not local.exists() or (rest[0] == "blob") != local.is_file():
                errors.append(f"{where}: repository path does not exist here: {'/'.join(rest[2:])}")
        elif rest not in ([""], ["issues"]):
            errors.append(f"{where}: unexpected repository link {url}")


def check(out, require_videos=False):
    out = out.resolve()
    config = json.loads((SITE / "src" / "site.json").read_text(encoding="utf-8"))
    errors, pages = [], {}
    for path in sorted(out.rglob("*.html")):
        parser = Page()
        parser.feed(path.read_text(encoding="utf-8"))
        pages[path] = parser
    if not pages:
        errors.append(f"{out}: no pages built")
    for page in config["pages"]:
        if resolve(out, page["path"]) is None:
            errors.append(f"configured page {page['path']} was not built")
    for path, page in pages.items():
        where = path.relative_to(out).as_posix()
        errors += [f"{where}: {p}" for p in page.problems]
        if page.lang != "en":
            errors.append(f"{where}: missing <html lang>")
        if not (page.title or "").strip():
            errors.append(f"{where}: missing <title>")
        if not page.description:
            errors.append(f"{where}: missing meta description")
        if page.h1 != 1:
            errors.append(f"{where}: expected exactly one <h1>, found {page.h1}")
        if page.canonical is not None and (not page.canonical.startswith(config["origin"] + "/")
                                           or resolve(out, page.canonical[len(config["origin"]):]) != path):
            errors.append(f"{where}: canonical URL {page.canonical} does not name this page")
        if page.csp != CSP:
            errors.append(f"{where}: missing or changed Content-Security-Policy meta tag")
        for tag, attr, url in page.urls:
            parts = urlsplit(url)
            if parts.scheme in ("http", "https", "mailto"):
                if tag != "a" or attr != "href":
                    errors.append(f"{where}: externally hosted resource <{tag} {attr}={url}>")
                check_external(url, config, where, errors)
                continue
            if parts.scheme or parts.netloc:
                errors.append(f"{where}: disallowed URL {url}")
                continue
            if parts.path and not parts.path.startswith("/"):
                errors.append(f"{where}: relative link {url}; use a site-absolute path")
                continue
            target = path if not parts.path else resolve(out, parts.path)
            if target is None:
                errors.append(f"{where}: broken link {url}")
            elif parts.fragment and target.suffix == ".html" and parts.fragment not in pages[target].ids:
                errors.append(f"{where}: missing anchor {url}")
    errors += check_sums(out)
    errors += check_videos(out, require_videos)
    return errors


def check_sums(out):
    sums = out / "SHA256SUMS"
    if not sums.is_file():
        return ["SHA256SUMS missing"]
    listed = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        listed[name] = digest
    actual = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in out.rglob("*") if p.is_file() and p != sums}
    return [f"SHA256SUMS does not match {name}" for name in sorted(set(listed) | set(actual)) if listed.get(name) != actual.get(name)]


def check_videos(out, require):
    errors = []
    steps = [s["id"] for s in json.loads((SITE / "guide" / "steps.json").read_text(encoding="utf-8"))["steps"]]
    manifest_path = SITE / "media" / "guide" / "videos.json"
    if not manifest_path.exists():
        return ["guide videos.json missing"] if require else []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("network") != "testnet" or manifest.get("chain") != "4c78adac":
        errors.append("guide videos must be recorded on Sui testnet (chain 4c78adac)")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("console_commit", "")):
        errors.append("guide video console_commit must identify a full Git commit")
    seen = set()
    for video in manifest.get("videos", []):
        if not re.fullmatch(r"[0-9a-f]{40}", video.get("console_commit", manifest.get("console_commit", ""))):
            errors.append(f"guide video {video['step']} has an invalid console commit")
        if video["step"] not in steps:
            errors.append(f"video for unknown guide step {video['step']}")
        if video["step"] in seen:
            errors.append(f"duplicate video for {video['step']}")
        seen.add(video["step"])
        if require:
            for required in ("poster", "fallback"):
                if not video.get(required):
                    errors.append(f"guide {video['step']} missing {required}")
        for key in ("file", "poster", "fallback"):
            name = video.get(key)
            if not name:
                continue
            if Path(name).name != name:
                errors.append(f"unsafe guide media filename {name}")
                continue
            built = out / "media" / "guide" / name
            if built.exists() and key != "poster" and built.stat().st_size > 15_000_000:
                errors.append(f"guide media {name} exceeds 15 MB")
            digest = video.get(key + "_sha256")
            if not built.is_file() or hashlib.sha256(built.read_bytes()).hexdigest() != digest:
                errors.append(f"guide {key} {name} is missing or does not match its recorded digest")
    if require:
        errors += [f"guide step {s} has no screencast" for s in steps if s not in seen]
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path, nargs="?", default=SITE / "dist")
    parser.add_argument("--require-videos", action="store_true")
    args = parser.parse_args()
    errors = check(args.out, args.require_videos)
    for error in errors:
        print("ERROR:", error)
    if errors:
        sys.exit(f"site check failed with {len(errors)} problem(s)")
    print(f"site check passed: {args.out}")


if __name__ == "__main__":
    main()
