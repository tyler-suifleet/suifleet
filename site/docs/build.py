#!/usr/bin/env python3
"""Build curated public docs. Install requirements.txt with --require-hashes first."""
import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
from urllib.parse import quote, unquote, urlsplit
from markdown_it import MarkdownIt

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent.parent
REPO = "https://github.com/tyler-suifleet/suifleet"
ORIGIN = "https://docs.suifleet.io"
MARKER = ".suifleet-docs-output"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MD = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def catalog():
    pages = json.loads((SITE / "pages.json").read_text())["pages"]
    seen = set()
    for page in pages:
        source = Path(page["source"])
        if (not SLUG.fullmatch(page["slug"]) or page["slug"] in seen
                or source.parts[0] != "docs" or len(source.parts) != 2
                or source.suffix != ".md" or not (ROOT / source).is_file()):
            raise ValueError("Invalid or duplicate curated page: " + str(page))
        seen.add(page["slug"])
    return pages


def heading_id(text):
    # GitHub-style heading slugs, including duplicate suffixes below.
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def rewrite_url(url, source, pages):
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in ("https", "http", "mailto"):
            raise ValueError("Unsupported link scheme: " + url)
        return url
    if not parsed.path:
        return url
    target = (ROOT / source).parent / unquote(parsed.path)
    target = target.resolve()
    if not target.is_relative_to(ROOT) or not target.exists():
        raise ValueError(source + ": missing repository link " + url)
    rel = target.relative_to(ROOT).as_posix()
    by_source = {p["source"]: "/" + p["slug"] + "/" for p in pages}
    suffix = ("?" + parsed.query if parsed.query else "") + ("#" + parsed.fragment if parsed.fragment else "")
    if rel in by_source:
        return by_source[rel] + suffix
    kind = "tree" if target.is_dir() else "blob"
    return REPO + "/" + kind + "/main/" + quote(rel) + suffix


def render(text, source, pages):
    tokens = MD.parse(text)
    ids = {}
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[index + 1]
            label = "".join(t.content for t in inline.children or [] if t.type in ("text", "code_inline"))
            slug = heading_id(label)
            count = ids.get(slug, 0)
            ids[slug] = count + 1
            token.attrSet("id", slug + ("-" + str(count) if count else ""))
        for child in token.children or []:
            if child.type == "link_open":
                child.attrSet("href", rewrite_url(child.attrGet("href"), source, pages))
            elif child.type == "image":
                # Public docs have no remote embedded resources.
                raise ValueError(source + ": images must be explicitly curated")
    result = MD.renderer.render(tokens, MD.options, {})
    # Preserve only explicit empty named anchors; all other raw HTML stays escaped.
    return re.sub(r'&lt;a id=&quot;([a-zA-Z0-9_-]+)&quot;&gt;&lt;/a&gt;',
                  r'<a id="\1"></a>', result)


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.links = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            if attrs["id"] in self.ids:
                raise ValueError("Duplicate HTML id: " + attrs["id"])
            self.ids.add(attrs["id"])
        for name in ("href", "src"):
            if name in attrs:
                self.links.append(attrs[name])


def check(out):
    files = {p.relative_to(out).as_posix(): p for p in out.rglob("*") if p.is_file()}
    if "index.html" not in files or "search.json" not in files:
        raise ValueError("Incomplete docs output: index.html and search.json required")
    parsed = {}
    for rel, path in files.items():
        if path.suffix == ".html":
            doc = Links()
            doc.feed(path.read_text())
            parsed[rel] = doc
    for rel, doc in parsed.items():
        for link in doc.links:
            url = urlsplit(link)
            if url.netloc and url.netloc != "docs.suifleet.io":
                continue
            if url.scheme and url.scheme not in ("http", "https"):
                continue
            path = unquote(url.path)
            target = path.lstrip("/") if path.startswith("/") else (Path(rel).parent / path).as_posix()
            if not path:
                target = rel
            elif path.endswith("/"):
                target += "index.html"
            if target not in files:
                raise ValueError(rel + ": broken output link " + link)
            if url.fragment and target in parsed and unquote(url.fragment) not in parsed[target].ids:
                raise ValueError(rel + ": broken anchor " + link)


def build(out):
    out = out.resolve()
    # Never erase a caller's directory or source tree.
    if out == ROOT or out in SITE.parents or out == SITE or SITE.is_relative_to(out):
        raise ValueError("Unsafe output path")
    if out.exists():
        if not (out / MARKER).is_file():
            raise ValueError("Output is not a previous docs build")
        shutil.rmtree(out)
    pages = catalog()
    out.mkdir(parents=True)
    (out / MARKER).write_text("SF-177\n")
    shutil.copytree(SITE / "assets", out / "assets")
    layout = (SITE / "layout.html").read_text()
    search = []
    def emit(path, title, content):
        nav = ""
        for group in dict.fromkeys(p["group"] for p in pages):
            nav += "<h2>" + html.escape(group) + "</h2><ul>"
            for p in pages:
                if p["group"] == group:
                    href = "/" + p["slug"] + "/"
                    current = ' aria-current="page"' if href == path else ""
                    nav += '<li><a href="' + href + '"' + current + ">" + html.escape(p["title"]) + "</a></li>"
            nav += "</ul>"
        values = dict(title=html.escape(title), path=path, content=content, nav=nav)
        result = re.sub(r"\{\{(title|path|content|nav)\}\}", lambda m: values[m[1]], layout)
        target = out / (path.lstrip("/") + "index.html" if path.endswith("/") else path.lstrip("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result, encoding="utf-8")
    emit("/", "Documentation", '<h1>SuiFleet documentation</h1><p>Install a client, enroll a device, publish components and administer your fleet. Start with <a href="/install/">the installation guide</a>.</p><p>Public testnet preview. Platform scripts own OS installation, reboot and firmware recovery. Physical, production provider, real-wallet and mainnet acceptance remain separate.</p>')
    for page in pages:
        text = (ROOT / page["source"]).read_text(encoding="utf-8")
        content = render(text, page["source"], pages)
        content += '<p>Source: <a href="' + REPO + '/blob/main/' + page["source"] + '">Edit this page</a></p>'
        path = "/" + page["slug"] + "/"
        emit(path, page["title"], content)
        search.append(dict(path=path, title=page["title"], text=text))
    emit("/404.html", "Page not found", '<h1>Page not found</h1><p><a href="/">Browse the documentation</a> or search for a page.</p>')
    (out / "search.json").write_text(json.dumps(search, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nAllow: /\nSitemap: " + ORIGIN + "/sitemap.xml\n")
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join("<url><loc>" + ORIGIN + path + "</loc></url>" for path in ["/"] + [p["path"] for p in search]) + "</urlset>\n")
    (out / "_headers").write_text("/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n  Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\n")
    check(out)
    sums = "".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(out).as_posix() + "\n" for p in sorted(out.rglob("*")) if p.is_file())
    (out / "SHA256SUMS").write_text(sums)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="Check an existing build without writing")
    args = parser.parse_args()
    try:
        check(args.out) if args.check else build(args.out)
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    print("docs link check passed" if args.check else "docs built and links checked")
