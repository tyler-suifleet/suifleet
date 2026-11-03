#!/usr/bin/env python3
"""Build the static suifleet.io landing site (SF-172).

Standard library only, so the build needs no package install and produces the
same bytes on every machine: pages are rendered from site/src into the output
directory in sorted order, with no timestamps, and SHA256SUMS lists every file.

    python3 site/build.py --out site/dist

Page sources may use these tokens:
    {{app_url}} {{docs_url}} {{repo_url}} {{issues_url}} {{contact_email}}
    {{app:ROUTE}}   console URL, e.g. {{app:devices/}}
    {{repo:PATH}}   GitHub URL of a file or directory in this repository; the
                    build fails if PATH does not exist, so documentation links
                    cannot silently rot.
    {{guide_steps}} the guide sections, from site/guide/steps.json and the
                    recorded video manifest site/media/guide/videos.json.
"""
import argparse
import base64
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sys
from xml.sax.saxutils import escape

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent
SRC = SITE / "src"
GUIDE = SITE / "guide"
MEDIA = SITE / "media"
TOKEN = re.compile(r"\{\{([a-z_]+)(?::([^}]*))?\}\}")


class BuildError(Exception):
    pass


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repo_url(config, rel):
    rel = rel.strip("/")
    target = ROOT / rel
    if not rel or ".." in Path(rel).parts or not target.exists():
        raise BuildError(f"repository link target does not exist: {rel!r}")
    kind = "tree" if target.is_dir() else "blob"
    return f"{config['repo_url']}/{kind}/{config['repo_branch']}/{rel}"


def videos_by_step():
    manifest_path = MEDIA / "guide" / "videos.json"
    if not manifest_path.exists():
        return {}, None
    manifest = load_json(manifest_path)
    return {v["step"]: v for v in manifest.get("videos", [])}, manifest


def guide_integrity():
    """The one permitted guide script is bound to its exact reviewed bytes."""
    return "sha256-" + base64.b64encode(hashlib.sha256((SRC / "assets/guide.js").read_bytes()).digest()).decode("ascii")


def guide_script_url():
    return "/assets/guide.js?v=" + hashlib.sha256((SRC / "assets/guide.js").read_bytes()).hexdigest()[:12]


def render_guide(config):
    steps = load_json(GUIDE / "steps.json")["steps"]
    videos, manifest = videos_by_step()
    out = []
    for number, step in enumerate(steps, 1):
        hidden = " hidden" if number != 1 else ""
        title = html.escape(step["title"])
        out.append(f'<section class="guide-stage" id="{step["id"]}" data-title="{title}"{hidden}>')
        video = videos.get(step["id"])
        out.append('<figure class="guide-media">')
        if video:
            for key in ("file", "poster", "fallback"):
                name = video.get(key)
                if name and Path(name).name != name:
                    raise BuildError(f"unsafe guide media filename: {name}")
            poster = f' poster="/media/guide/{video["poster"]}"' if video.get("poster") else ""
            fallback = f'<source src="/media/guide/{video["fallback"]}" type="video/mp4">' if video.get("fallback") else ""
            out.append(f'<video data-console-commit="{html.escape(video.get("console_commit", manifest["console_commit"]))}" controls playsinline preload="none" aria-label="{title} walkthrough" width="{video["width"]}" height="{video["height"]}"{poster}>'
                       f'<source src="/media/guide/{video["file"]}" type="video/webm">{fallback}'
                       'Your browser cannot play this recording.</video>')
            out.append(f'<div data-video-error hidden role="status"><p>Video unavailable. Follow the steps or <a href="/media/guide/{video.get("fallback", video["file"])}">download it</a>.</p></div>')
            out.append(f'<figcaption><span>Testnet recording · {html.escape(manifest["recorded"])}</span><a href="/media/guide/{video.get("fallback", video["file"])}" download>Download video</a></figcaption>')
        else:
            out.append('<p data-video-error>Video not yet available. Follow the written instructions.</p>')
        out.append('</figure><article class="guide-instruction-card">')
        out.append(f'<h2>{title}</h2><h3 data-guide-label>Overview</h3><div class="guide-copy"><p>{html.escape(step["summary"])}</p></div>')
        out.append(f'<div class="guide-instruction-meta"><div class="guide-instruction-paging"><button type="button" aria-label="Previous instruction" data-guide-prev-part>‹</button><span data-guide-part aria-live="polite"></span><button type="button" aria-label="Next instruction" data-guide-forward>›</button></div><a href="{config["app_url"]}{step["console_route"]}" target="_blank" rel="noopener">Open {html.escape(step["console_label"])} ↗</a></div></article>')
        instructions = [("Overview", step["summary"])]
        instructions += [("Before you start", text) for text in step.get("before", [])]
        instructions += [(f'Task {i} of {len(step["actions"])}', text) for i, text in enumerate(step["actions"], 1)]
        instructions += [("Expected result", step["result"])]
        out.append('<ol hidden class="guide-instructions">')
        out += [f'<li data-guide-instruction data-label="{html.escape(label)}">{html.escape(text)}</li>' for label, text in instructions]
        out.append('</ol></section>')
    return "\n".join(out)

def expand(text, config, source):
    def replace(match):
        name, arg = match.group(1), match.group(2)
        if name in ("app_url", "docs_url", "repo_url", "issues_url", "contact_email") and arg is None:
            return config[name]
        if name == "app" and arg is not None:
            return config["app_url"] + arg.lstrip("/")
        if name == "repo" and arg is not None:
            return repo_url(config, arg)
        if name == "guide_options" and arg is None:
            steps = load_json(GUIDE / "steps.json")["steps"]
            return "".join(f'<option value="{step["id"]}">{index}. {html.escape(step["title"])}</option>' for index, step in enumerate(steps, 1)) + '<option value="complete">Walkthrough complete</option>'
        if name == "guide_steps" and arg is None:
            return render_guide(config)
        raise BuildError(f"{source}: unknown token {match.group(0)}")
    return TOKEN.sub(replace, text)


def output_path(out, page_path):
    rel = page_path.lstrip("/")
    return out / (rel + "index.html" if rel == "" or rel.endswith("/") else rel)


def build(out):
    config = load_json(SRC / "site.json")
    layout = (SRC / "layout.html").read_text(encoding="utf-8")
    if out.exists():
        # Only ever replace a previous build output, never an arbitrary directory.
        if any(out.iterdir()) and not (out / "SHA256SUMS").is_file():
            raise BuildError(f"{out} is not empty and is not a previous site build")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for page in config["pages"]:
        source = SRC / "pages" / page["source"]
        content = expand(source.read_text(encoding="utf-8"), config, page["source"])
        current = ' aria-current="page"'
        nav = "\n".join(
            f'<li><a href="{item["path"]}"{current if item["path"] == page["path"] else ""}>{html.escape(item["label"])}</a></li>'
            for item in config["nav"])
        title = page["title"] if page["path"] == "/" else f'{page["title"]} · {config["name"]}'
        # The error page is served for many paths, so it names no canonical URL.
        canonical = "" if page["path"] == "/404.html" else f'<link rel="canonical" href="{config["origin"]}{page["path"]}">\n'
        values = {"page_title": html.escape(title), "description": html.escape(page["description"]),
                  "canonical": canonical, "nav": nav, "app_url": config["app_url"],
                  "repo_url": config["repo_url"], "content": content.strip()}
        rendered = re.sub(r"\{\{(page_title|description|canonical|nav|app_url|repo_url|content)\}\}", lambda m: values[m.group(1)], layout)
        style_version = hashlib.sha256((SRC / "assets/style.css").read_bytes()).hexdigest()[:12]
        rendered = rendered.replace('href="/assets/style.css"', f'href="/assets/style.css?v={style_version}"')
        if page["path"] == "/guide/":
            integrity = guide_integrity()
            rendered = rendered.replace("<body>", '<body class="guide-page">')
            rendered = rendered.replace("base-uri 'none'; form-action 'none'", f"base-uri 'none'; form-action 'none'; script-src '{integrity}'")
            rendered = rendered.replace("</body>", f'<script src="{guide_script_url()}" integrity="{integrity}" defer></script>\n</body>')
        target = output_path(out, page["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8", newline="\n")
    shutil.copytree(SRC / "assets", out / "assets")
    _, manifest = videos_by_step()
    if manifest:
        (out / "media" / "guide").mkdir(parents=True)
        for video in manifest["videos"]:
            for key in ("file", "poster", "fallback"):
                if video.get(key):
                    shutil.copyfile(MEDIA / "guide" / video[key], out / "media" / "guide" / video[key])
    for name in ("_headers", "robots.txt"):
        if name == "_headers":
            headers = (SRC / name).read_text(encoding="utf-8")
            headers = headers.replace("default-src 'none';", f"default-src 'none'; script-src '{guide_integrity()}';")
            (out / name).write_text(headers, encoding="utf-8")
        else:
            shutil.copyfile(SRC / name, out / name)
    urls = "".join(f"<url><loc>{escape(config['origin'] + page['path'])}</loc></url>"
                   for page in config["pages"] if page["path"] != "/404.html")
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + '</urlset>\n',
        encoding="utf-8")
    files = sorted(p for p in out.rglob("*") if p.is_file())
    sums = "".join(f"{sha256(p)}  {p.relative_to(out).as_posix()}\n" for p in files)
    (out / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=SITE / "dist")
    args = parser.parse_args()
    try:
        files = build(args.out.resolve())
    except BuildError as error:
        sys.exit(f"site build failed: {error}")
    print(f"built {len(files)} files into {args.out}")


if __name__ == "__main__":
    main()
