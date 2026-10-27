#!/usr/bin/env python3
"""Encode recorded guide frames into the site's WebM screencasts (SF-172).

Run on the host after site/guide/record_guide.py:

    python3 site/guide/encode_guide.py --runtime-dir /private/guide-run \
      --evidence /private/guide-run/evidence.json

Uses ffmpeg from the pinned scripts/video-test.Dockerfile image (built if
absent, as scripts/test_kvs_video.sh does) with no network. Waits in the
recording (chain confirmations, uploads) are shortened: no frame is held
longer than MAX_HOLD seconds, so the videos stay short. Writes WebM, JPEG
posters and videos.json into site/media/guide/, which site/check.py verifies.
Only frames and public chain identifiers are read; the runtime directory's
keys were deleted by the recorder.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "site" / "media" / "guide"
MAX_HOLD, MIN_HOLD, LAST_HOLD = 1.6, 0.04, 2.5
WIDTH, HEIGHT = 1280, 800


def image():
    dockerfile = ROOT / "scripts" / "video-test.Dockerfile"
    tag = "suifleet-video-test:" + hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode:
        subprocess.run(["docker", "build", "-t", tag, "-"], stdin=dockerfile.open("rb"), check=True)
    return tag


def concat_list(frames_dir):
    frames = json.loads((frames_dir / "frames.json").read_text())
    assert frames, f"{frames_dir} has no frames"
    lines = ["ffconcat version 1.0"]
    for index, frame in enumerate(frames):
        if index + 1 < len(frames):
            hold = min(MAX_HOLD, max(MIN_HOLD, frames[index + 1]["t"] - frame["t"]))
        else:
            hold = LAST_HOLD
        lines += [f"file {frame['file']}", f"duration {hold:.3f}"]
    lines.append(f"file {frames[-1]['file']}")  # The concat demuxer needs the last file repeated.
    (frames_dir / "list.ffconcat").write_text("\n".join(lines) + "\n")
    return sum(min(MAX_HOLD, max(MIN_HOLD, b["t"] - a["t"])) for a, b in zip(frames, frames[1:])) + LAST_HOLD


def ffmpeg(tag, work, *args):
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", "1001:1001", "-v", f"{work}:/work", "-w", "/work",
                    "--entrypoint", "ffmpeg", tag, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text())
    assert evidence.get("chain") == "4c78adac" and evidence.get("network") == "testnet", "not a testnet recording"
    steps = json.loads((ROOT / "site" / "guide" / "steps.json").read_text())["steps"]
    transactions = {}
    for phase in evidence["browser"]["phases"]:
        for tx in phase["transactions"]:
            assert tx.get("effects_status") == "success", f"{tx['digest']} has no successful effects"
            transactions.setdefault(tx["step"], []).append(tx["digest"])
    tag = image()
    MEDIA.mkdir(parents=True, exist_ok=True)
    videos = []
    with tempfile.TemporaryDirectory(prefix="sf172-encode-") as temp:
        for step in steps:
            source = args.runtime_dir / "frames" / step["id"]
            work = Path(temp) / step["id"]
            shutil.copytree(source, work)
            work.chmod(0o755)
            for f in work.iterdir():
                f.chmod(0o644)
            duration = concat_list(work)
            ffmpeg(tag, work, "-f", "concat", "-safe", "0", "-i", "list.ffconcat",
                   "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:white,fps=25,format=yuv420p",
                   "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "40", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4",
                   "-an", "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "video.webm")
            ffmpeg(tag, work, "-i", "poster.png", "-vf", f"scale={WIDTH}:{HEIGHT}", "-q:v", "4", "-map_metadata", "-1", "poster.jpg")
            video, poster = f"{step['id']}.webm", f"{step['id']}.jpg"
            shutil.copyfile(work / "video.webm", MEDIA / video)
            shutil.copyfile(work / "poster.jpg", MEDIA / poster)
            videos.append({"step": step["id"], "file": video, "file_sha256": sha256(MEDIA / video),
                           "poster": poster, "poster_sha256": sha256(MEDIA / poster), "width": WIDTH, "height": HEIGHT,
                           "duration_seconds": round(duration, 1), "caption": f"Screencast: {step['title']}.",
                           "transactions": transactions.get(step["id"], [])})
    finished = evidence.get("finished") or evidence["started"]
    manifest = {
        "schema_version": 1,
        "network": "testnet",
        "chain": evidence["chain"],
        "console_commit": evidence["console_commit"],
        "package": evidence["package"],
        "registry": evidence["registry"],
        "recorded": datetime.date.fromisoformat(finished[:10]).isoformat(),
        "recorder": "site/guide/record_guide.py (scripted Wallet Standard test wallet; key never in the page)",
        "browser_address": evidence["browser"]["address"],
        "spent_mist": evidence.get("spent_mist"),
        "videos": videos,
    }
    (MEDIA / "videos.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {len(videos)} screencasts and {MEDIA / 'videos.json'}")


if __name__ == "__main__":
    main()
