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
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sys

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


def render_guide(config):
    steps = load_json(GUIDE / "steps.json")["steps"]
    videos, manifest = videos_by_step()
    out = ['<ol class="guide-index">']
    out += [f'<li><a href="#{s["id"]}">{html.escape(s["title"])}</a></li>' for s in steps]
    out.append("</ol>")
    for number, step in enumerate(steps, 1):
        route = step["console_route"]
        out.append(f'<section class="guide-step" id="{step["id"]}">')
        out.append(f'<h2><span class="step-number">{number}</span> {html.escape(step["title"])}</h2>')
        out.append(f'<p>{html.escape(step["summary"])}</p>')
        video = videos.get(step["id"])
        if video:
            for key in ("file", "poster", "fallback"):
                name = video.get(key)
                if name and Path(name).name != name:
                    raise BuildError(f"unsafe guide media filename: {name}")
            poster = f' poster="/media/guide/{video["poster"]}"' if video.get("poster") else ""
            fallback = (f'<source src="/media/guide/{video["fallback"]}" type="video/mp4">' if video.get("fallback") else "")
            out.append(f'<figure class="screencast"><video controls preload="none" width="{video["width"]}" height="{video["height"]}"{poster}>'
                       f'<source src="/media/guide/{video["file"]}" type="video/webm">{fallback}'
                       f'Your browser cannot play this WebM screencast. <a href="/media/guide/{video["file"]}">Download it</a>.</video>'
                       f'<figcaption>{html.escape(video["caption"])} Recorded on Sui {html.escape(manifest["network"])} '
                       f'from console build <code>{html.escape(video.get("console_commit", manifest["console_commit"])[:12])}</code> on {html.escape(manifest["recorded"])}.</figcaption></figure>')
        else:
            out.append('<p class="pending-video">Screencast not yet recorded for this step.</p>')
        out.append('<details class="transcript"><summary>Transcript and instructions</summary>')
        if step.get("before"):
            out.append("<h3>Before you start</h3><ul>")
            out += [f"<li>{html.escape(item)}</li>" for item in step["before"]]
            out.append("</ul>")
        out.append("<h3>Steps</h3><ol>")
        out += [f"<li>{html.escape(item)}</li>" for item in step["actions"]]
        out.append("</ol>")
        out.append(f'<p class="result"><strong>Result:</strong> {html.escape(step["result"])}</p>')
        links = [f'<a class="button" href="{config["app_url"]}{route}">Open {html.escape(step["console_label"])} in the console</a>']
        links += [f'<a href="{repo_url(config, d["path"])}">{html.escape(d["label"])}</a>' for d in step.get("docs", [])]
        out.append('<p class="step-links">' + " ".join(links) + "</p>")
        out.append("</details></section>")
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
        shutil.copyfile(SRC / name, out / name)
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
