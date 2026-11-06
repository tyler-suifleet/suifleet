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
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent
SRC = SITE / "src"
GUIDE = SITE / "guide"
MEDIA = SITE / "media"
OVERVIEW_MEDIA = SITE / "media" / "overview"
TOKEN = re.compile(r"\{\{([a-z_]+)(?::([^}]*))?\}\}")


class BuildError(Exception):
    pass


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(overrides=None):
    config = load_json(SRC / "site.json")
    if overrides is None:
        return config
    values = load_json(overrides)
    if not isinstance(values, dict) or set(values) - {"origin", "app_url", "docs_url"}:
        raise BuildError("preview config may override only origin, app_url and docs_url")
    for key, value in values.items():
        if not isinstance(value, str):
            raise BuildError("preview URL must be a string")
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.hostname in {"localhost", "127.0.0.1", "::1"}
                or (key == "origin" and url.path not in {"", "/"})):
            raise BuildError("preview URLs must be public HTTPS addresses without credentials, queries or fragments")
        import ipaddress
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise BuildError("preview URLs must not point to a private network")
        config[key] = value.rstrip("/") + ("" if key == "origin" else "/")
        if url.hostname not in config["external_hosts"]:
            config["external_hosts"].append(url.hostname)
    return config


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
    for step in steps:
        title = html.escape(step["title"])
        out.append(f'<section class="guide-stage" id="{step["id"]}" data-title="{title}">')
        video = videos.get(step["id"])
        out.append('<figure class="guide-media">')
        if video:
            for key in ("file", "poster", "fallback", "captions"):
                name = video.get(key)
                if name and Path(name).name != name:
                    raise BuildError(f"unsafe guide media filename: {name}")
            poster = f' poster="/media/guide/{video["poster"]}"' if video.get("poster") else ""
            fallback = f'<source src="/media/guide/{video["fallback"]}" type="video/mp4">' if video.get("fallback") else ""
            captions = (f'<track kind="captions" src="/media/guide/{html.escape(video["captions"])}" '
                        'srclang="en" label="English" default>' if video.get("captions") else "")
            out.append(f'<video data-console-commit="{html.escape(video.get("console_commit", manifest["console_commit"]))}" controls playsinline preload="none" aria-label="{title} walkthrough" width="{video["width"]}" height="{video["height"]}"{poster}>'
                       f'<source src="/media/guide/{video["file"]}" type="video/webm">{fallback}{captions}'
                       'Your browser cannot play this recording.</video>')
            out.append(f'<div data-video-error hidden role="status"><p>Video unavailable. Follow the steps or <a href="/media/guide/{video.get("fallback", video["file"])}">download it</a>.</p></div>')
            out.append(f'<figcaption><span>Narrated product walkthrough · {html.escape(manifest["recorded"])}</span><a href="/media/guide/{video.get("fallback", video["file"])}" download>Download video</a></figcaption>')
        else:
            out.append('<p data-video-error>Video not yet available. Follow the written instructions.</p>')
        out.append('</figure><article class="guide-instruction-card">')
        out.append(f'<h2 tabindex="-1">{title}</h2><h3 data-guide-label>Overview</h3><div class="guide-copy"><p>{html.escape(step["summary"])}</p></div>')
        out.append(f'<div class="guide-instruction-meta"><div class="guide-instruction-paging"><button type="button" aria-label="Previous instruction" data-guide-prev-part>‹</button><span data-guide-part aria-live="polite"></span><button type="button" aria-label="Next instruction" data-guide-forward>›</button></div><a href="{config["app_url"]}{step["console_route"]}" target="_blank" rel="noopener">Open {html.escape(step["console_label"])} ↗</a></div></article>')
        instructions = [("Overview", step["summary"])]
        instructions += [("Before you start", text) for text in step.get("before", [])]
        instructions += [(f'Task {i} of {len(step["actions"])}', text) for i, text in enumerate(step["actions"], 1)]
        instructions += [("Expected result", step["result"])]
        out.append('<ol class="guide-instructions">')
        out += [f'<li data-guide-instruction data-label="{html.escape(label)}">{html.escape(text)}</li>' for label, text in instructions]
        out.append('</ol></section>')
    return "\n".join(out)

def overview_manifest():
    manifest=load_json(OVERVIEW_MEDIA/'manifest.json')
    if manifest.get('kind') != 'illustrative-animation' or {v.get('name') for v in manifest.get('variants',[])} != {'landscape','portrait'}:
        raise BuildError('Overview must contain both verified animation variants')
    expected_sources={'site/overview/storyboard.json','site/overview/scene.html','site/overview/render_frames.cjs','site/overview/render_overview.py'}
    if set(manifest.get('source_sha256',{})) != expected_sources or any(sha256(ROOT/name) != digest for name,digest in manifest['source_sha256'].items()):
        raise BuildError('Overview render source changed; regenerate the animation')
    story=load_json(ROOT/'site/overview/storyboard.json')
    if [s['text'] for s in manifest['scenes']] != [s['text'] for s in story['scenes']]:
        raise BuildError('Overview narration differs from reviewed storyboard')
    for variant in manifest['variants']:
        if (variant.get('width'),variant.get('height')) != ((1280,720) if variant['name']=='landscape' else (720,1280)):
            raise BuildError('Unexpected overview dimensions')
        for key in ['file','fallback','poster']:
            name=variant[key]
            if Path(name).name != name or sha256(OVERVIEW_MEDIA/name) != variant[key+'_sha256']:
                raise BuildError('Overview media failed digest verification')
    if Path(manifest['captions']).name != manifest['captions'] or sha256(OVERVIEW_MEDIA/manifest['captions']) != manifest['captions_sha256']:
        raise BuildError('Overview captions failed digest verification')
    import importlib.util
    spec=importlib.util.spec_from_file_location('overview_captions',GUIDE/'captions.py');captions=importlib.util.module_from_spec(spec);spec.loader.exec_module(captions)
    if (OVERVIEW_MEDIA/manifest['captions']).read_text() != captions.webvtt(manifest['cues'],manifest['duration_seconds']):
        raise BuildError('Overview captions differ from complete narration')
    return manifest


SAMPLE_MEDIA=SITE/'media/motion-sample'
def motion_sample_manifest():
    manifest=load_json(SAMPLE_MEDIA/'manifest.json')
    expected={'site/motion-sample/'+name for name in ['src/index.tsx','render.mjs','prepare.py','script.json','package.json','package-lock.json']}
    if manifest.get('renderer')!='Remotion' or manifest.get('version')!='4.0.541' or manifest.get('duration_seconds')!=20 or manifest.get('fps')!=30:
        raise BuildError('Unexpected motion sample renderer or timing')
    if set(manifest.get('source_sha256',{}))!=expected or any(sha256(ROOT/name)!=value for name,value in manifest['source_sha256'].items()):
        raise BuildError('Motion sample source changed; regenerate the sample')
    assets={'site/src/assets/inter-variable.woff2','site/src/assets/logo.svg'}|{'site/motion-sample/art/editorial-scene.png'}
    if set(manifest.get('assets_sha256',{}))!=assets or any(sha256(ROOT/name)!=value for name,value in manifest['assets_sha256'].items()):
        raise BuildError('Motion sample artwork or font changed')
    if {v['name'] for v in manifest.get('videos',[])}!={'landscape','portrait'}:
        raise BuildError('Motion sample needs landscape and portrait')
    for video in manifest['videos']:
        for key,hash_key in [('file','sha256'),('poster','poster_sha256')]:
            if Path(video[key]).name!=video[key] or sha256(SAMPLE_MEDIA/video[key])!=video[hash_key]:
                raise BuildError('Motion sample media failed digest verification')
    script=load_json(ROOT/'site/motion-sample/script.json')
    if manifest.get('voice')!=script['voice'] or [cue['text'] for cue in manifest['timing']['cues']]!=[cue['text'] for cue in script['cues']]:
        raise BuildError('Motion sample narration differs from script')
    previous=0
    for cue in manifest['timing']['cues']:
        if not previous<=cue['speech_start']<cue['speech_end']<=20:
            raise BuildError('Motion sample narration overlaps or exceeds video')
        previous=cue['speech_end']
    if manifest.get('captions')!='sample.en.vtt' or sha256(SAMPLE_MEDIA/manifest['captions'])!=manifest['captions_sha256']:
        raise BuildError('Motion sample captions failed digest verification')
    import importlib.util
    spec=importlib.util.spec_from_file_location('sample_captions',GUIDE/'captions.py');captions=importlib.util.module_from_spec(spec);spec.loader.exec_module(captions)
    if (SAMPLE_MEDIA/manifest['captions']).read_text()!=captions.webvtt(manifest['timing']['cues'],20):
        raise BuildError('Motion sample captions differ from narration timing')
    return manifest

def motion_sample_player():
    manifest=motion_sample_manifest();parts=['<section class="motion-sample" aria-label="Twenty-second animation sample">']
    for video in manifest['videos']:
        name=video['name'];w,h=(1600,900) if name=='landscape' else (900,1600)
        parts.append(f'<video class="sample-{name}" controls playsinline preload="metadata" width="{w}" height="{h}" poster="/media/motion-sample/{video["poster"]}?v={video["poster_sha256"][:16]}" aria-label="Animation sample, {name} version"><source src="/media/motion-sample/{video["file"]}?v={video["sha256"][:16]}" type="video/mp4"><track kind="captions" src="/media/motion-sample/{manifest["captions"]}?v={manifest["captions_sha256"][:16]}" srclang="en" label="English">Your browser cannot play this video. Read the transcript below.</video>')
    parts.append('<p class="sample-note">Female narration with optional English captions. Use the player’s captions menu to turn them on.</p><details><summary>Read the narration</summary><p>')
    parts.append(' '.join(html.escape(cue['text']) for cue in manifest['timing']['cues']))
    parts.append('</p></details><p><a href="/media/motion-sample/portrait.mp4" download>Download the phone version</a> · <a href="/media/motion-sample/landscape.mp4" download>Download the widescreen version</a></p></section>')
    return ''.join(parts)


def overview_player():
    manifest=overview_manifest();parts=['<section class="overview-intro" id="overview" aria-labelledby="overview-title"><p class="eyebrow">Start with the big picture</p><h2 id="overview-title">How SuiFleet works</h2><p id="overview-description">A two-minute animated introduction to approvals, shared records, software files and device clients. Female narration with optional English captions; use the player’s captions menu to turn them on or off.</p>']
    for video in manifest['variants']:
        parts.append(f'<video class="overview-video overview-{video["name"]}" data-mp4-sha256="{video["fallback_sha256"]}" controls playsinline preload="none" width="{video["width"]}" height="{video["height"]}" poster="/media/overview/{video["poster"]}?v={video["poster_sha256"][:16]}" aria-label="How SuiFleet works, {video["name"]} version" aria-describedby="overview-description"><source src="/media/overview/{video["file"]}?v={video["file_sha256"][:16]}" type="video/webm"><source src="/media/overview/{video["fallback"]}?v={video["fallback_sha256"][:16]}" type="video/mp4"><track kind="captions" src="/media/overview/{manifest["captions"]}?v={manifest["captions_sha256"][:16]}" srclang="en" label="English">Your browser cannot play the video. Read the transcript below.</video>')
    parts.append('<p class="overview-caption">Illustrative animation. Installation results and current health are different; your platform controls its installer and any reboot.</p><details><summary>Read the complete narration</summary><ol class="overview-transcript">')
    for scene in manifest['scenes']:
        parts.append('<li><strong>'+html.escape(scene['title'])+'</strong><p>'+html.escape(scene['text'])+'</p></li>')
    parts.append('</ol></details><p><a class="button primary" href="/guide/">Continue to the product guide →</a></p></section>')
    return ''.join(parts)


def expand(text, config, source):
    def replace(match):
        name, arg = match.group(1), match.group(2)
        if name in ("app_url", "docs_url", "repo_url", "issues_url", "contact_email") and arg is None:
            return config[name]
        if name == "app" and arg is not None:
            return config["app_url"] + arg.lstrip("/")
        if name == "repo" and arg is not None:
            return repo_url(config, arg)
        if name == "guide_count" and arg is None:
            return str(len(load_json(GUIDE / "steps.json")["steps"]))
        if name == "guide_options" and arg is None:
            steps = load_json(GUIDE / "steps.json")["steps"]
            return "".join(f'<option value="{step["id"]}">{index}. {html.escape(step["title"])}</option>' for index, step in enumerate(steps, 1)) + '<option value="complete">Walkthrough complete</option>'
        if name == "motion_sample_player" and arg is None:
            return motion_sample_player()
        if name == "overview_player" and arg is None:
            return overview_player()
        if name == "guide_steps" and arg is None:
            return render_guide(config)
        raise BuildError(f"{source}: unknown token {match.group(0)}")
    return TOKEN.sub(replace, text)


def output_path(out, page_path):
    rel = page_path.lstrip("/")
    return out / (rel + "index.html" if rel == "" or rel.endswith("/") else rel)


def build(out, config=None):
    config = config or load_config()
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
        if page.get("noindex"):
            rendered=rendered.replace("</head>", '<meta name="robots" content="noindex, nofollow">\n</head>')
        style_version = hashlib.sha256((SRC / "assets/style.css").read_bytes()).hexdigest()[:12]
        rendered = rendered.replace('href="/assets/style.css"', f'href="/assets/style.css?v={style_version}"')
        if page["path"] in ("/guide/", "/how-it-works/"):
            integrity = guide_integrity()
            if page['path'] == '/guide/': rendered = rendered.replace("<body>", '<body class="guide-page">')
            else: rendered = rendered.replace("media-src 'self';", "media-src 'self' blob:; connect-src 'self';")
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
            for key in ("file", "poster", "fallback", "captions"):
                if video.get(key):
                    shutil.copyfile(MEDIA / "guide" / video[key], out / "media" / "guide" / video[key])
    overview=overview_manifest()
    (out/'media/overview').mkdir(parents=True)
    names=[overview['captions'],'manifest.json']+[v[key] for v in overview['variants'] for key in ['file','fallback','poster']]
    for name in names:shutil.copyfile(OVERVIEW_MEDIA/name,out/'media/overview'/name)
    sample=motion_sample_manifest()
    (out/'media/motion-sample').mkdir(parents=True)
    names=['manifest.json',sample['captions']]+[video[key] for video in sample['videos'] for key in ['file','poster']]
    for name in names:shutil.copyfile(SAMPLE_MEDIA/name,out/'media/motion-sample'/name)
    for name in ("_headers", "robots.txt"):
        if name == "_headers":
            headers = (SRC / name).read_text(encoding="utf-8")
            headers = headers.replace("media-src 'self';", "media-src 'self' blob:; connect-src 'self';")
            headers = headers.replace("default-src 'none';", f"default-src 'none'; script-src '{guide_integrity()}';")
            (out / name).write_text(headers, encoding="utf-8")
        else:
            shutil.copyfile(SRC / name, out / name)
    urls = "".join(f"<url><loc>{escape(config['origin'] + page['path'])}</loc></url>"
                   for page in config["pages"] if page["path"] != "/404.html" and not page.get("noindex"))
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
    parser.add_argument("--config", type=Path, help="Public HTTPS origin/app/docs overrides for an evaluation deployment")
    args = parser.parse_args()
    try:
        files = build(args.out.resolve(), load_config(args.config))
    except BuildError as error:
        sys.exit(f"site build failed: {error}")
    print(f"built {len(files)} files into {args.out}")


if __name__ == "__main__":
    main()
