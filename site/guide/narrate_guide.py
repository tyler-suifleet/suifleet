#!/usr/bin/env python3
"""Add offline female narration to the verified cursor recordings.

Never signs transactions or downloads models. Keep source media immutable and
write a separate output directory; inspect/check it before replacing site media.
"""
import argparse
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import wave

from encode_guide import ffmpeg, image, sha256


def timing(cues, source_duration, spoken_durations):
    """Preserve every source interval and give every spoken cue breathing room."""
    if not cues or len(cues) != len(spoken_durations):
        raise ValueError("one audio segment is required per cue")
    starts = [cue["source_start"] for cue in cues]
    if starts[0] != 0 or any(not math.isfinite(t) for t in starts):
        raise ValueError("cues must start at zero and have finite times")
    if not math.isfinite(source_duration) or source_duration <= starts[-1]:
        raise ValueError("cue outside source video")
    plan, output_start = [], 0
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else source_duration
        spoken = spoken_durations[i]
        if end <= start or not math.isfinite(spoken) or spoken <= 0:
            raise ValueError("non-increasing cue or invalid audio duration")
        duration = math.ceil(max(end - start, spoken + 0.7) * 25) / 25
        plan.append(dict(source_start=start, source_end=end, start=output_start,
                         duration=duration, speech_start=output_start + 0.2,
                         speech_end=output_start + 0.2 + spoken))
        output_start += duration
    return plan, output_start


def probe(tag, directory, filename):
    result = subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{directory}:/work", "--entrypoint", "ffprobe", tag, "-v", "error",
        "-show_streams", "-show_format", "-of", "json", "/work/" + filename,
    ], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-media", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--voices", type=Path, required=True)
    parser.add_argument("--script", type=Path, default=Path(__file__).with_name("narration.json"))
    args = parser.parse_args()
    config = json.loads(args.script.read_text())
    if args.output.resolve() == args.source_media.resolve() or args.output.exists():
        raise ValueError("output must be a new directory separate from source media")
    for path, key in ((args.model, "model_sha256"), (args.voices, "voices_sha256")):
        if sha256(path) != config[key]:
            raise ValueError(f"unverified {key}")
    if importlib.metadata.version("kokoro-onnx") != config["engine_version"]:
        raise ValueError("use the documented synthesis package version")
    # Imports are optional at site-build/test time; synthesis alone needs these.
    import numpy as np
    from kokoro_onnx import Kokoro

    manifest = json.loads((args.source_media / "videos.json").read_text())
    clips = {clip["step"]: clip for clip in config["clips"]}
    if len(clips) != len(config["clips"]) or set(clips) != {v["step"] for v in manifest["videos"]}:
        raise ValueError("narration must cover each recording exactly once")
    for video in manifest["videos"]:
        for key in ("file", "poster", "fallback"):
            name = video[key]
            if Path(name).name != name or sha256(args.source_media / name) != video[key + "_sha256"]:
                raise ValueError("source media failed digest verification")
        if video["file_sha256"] != clips[video["step"]]["source_sha256"]:
            raise ValueError("script belongs to a different recording; review cue times")
    tag = image()
    kokoro = Kokoro(str(args.model), str(args.voices))
    args.output.mkdir(parents=True)
    for video in manifest["videos"]:
        clip = clips[video["step"]]
        with tempfile.TemporaryDirectory(prefix="suifleet-narration-") as directory:
            work = Path(directory)
            shutil.copyfile(args.source_media / video["file"], work / "source.webm")
            source = probe(tag, work, "source.webm")
            source_duration = float(source["format"]["duration"])
            if abs(source_duration - clip["source_duration_seconds"]) > 0.3:
                raise ValueError("source duration differs from reviewed cue script")
            speech = []
            for cue in clip["cues"]:
                # Pronunciation is synthesis-only; captions retain the spelling.
                text = re.sub(r"\bSuiFleet\b", "Swee Fleet", cue["text"])
                text = re.sub(r"\bSui\b", "swee", text)
                samples, sample_rate = kokoro.create(text, voice=config["voice"], speed=1.0, lang=config["language"])
                if sample_rate != 24000 or not len(samples) or not np.isfinite(samples).all():
                    raise ValueError("invalid synthesized audio")
                speech.append(samples)
            plan, duration = timing(clip["cues"], source_duration, [len(s) / sample_rate for s in speech])
            audio = np.zeros(math.ceil(duration * sample_rate), dtype=np.float32)
            for samples, segment in zip(speech, plan):
                start = round(segment["speech_start"] * sample_rate)
                audio[start:start + len(samples)] = samples
            peak = float(np.max(np.abs(audio)))
            if peak < 0.01:
                raise ValueError("narration is silent")
            audio *= 0.89 / peak  # Keep headroom; the encoded stream is checked below.
            with wave.open(str(work / "narration.wav"), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes((audio * 32767).astype("<i2").tobytes())
            filters = [f"[0:v]split={len(plan)}" + "".join(f"[s{i}]" for i in range(len(plan)))]
            for i, segment in enumerate(plan):
                hold = segment["duration"] - (segment["source_end"] - segment["source_start"]) + 0.08
                filters.append(f"[s{i}]trim=start={segment['source_start']}:end={segment['source_end']},"
                               f"setpts=PTS-STARTPTS,fps=25,tpad=stop_mode=clone:stop_duration={hold},"
                               f"trim=duration={segment['duration']}[v{i}]")
            filters.append("".join(f"[v{i}]" for i in range(len(plan))) + f"concat=n={len(plan)}:v=1:a=0[v]")
            ffmpeg(tag, work, "-i", "source.webm", "-i", "narration.wav", "-filter_complex_threads", "2",
                   "-filter_complex", ";".join(filters), "-map", "[v]", "-map", "1:a", "-t", str(duration),
                   "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "36", "-row-mt", "1", "-cpu-used", "4",
                   "-c:a", "libopus", "-b:a", "96k", "-map_metadata", "-1", "video.webm")
            ffmpeg(tag, work, "-i", "video.webm", "-c:v", "libx264", "-crf", "26", "-preset", "medium",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                   "-map_metadata", "-1", "video.mp4")
            for name, key, codec in (("video.webm", "file", "opus"), ("video.mp4", "fallback", "aac")):
                facts = probe(tag, work, name)
                tracks = [s for s in facts["streams"] if s["codec_type"] == "audio"]
                if len(tracks) != 1 or tracks[0]["codec_name"] != codec:
                    raise ValueError("encoded narration track missing")
                if abs(float(facts["format"]["duration"]) - duration) > 0.3:
                    raise ValueError("encoded duration mismatch")
                if (work / name).stat().st_size > 15_000_000:
                    raise ValueError("video exceeds 15 MB budget")
                # Decode the complete encoded audio AND video, rather than trusting metadata.
                ffmpeg(tag, work, "-v", "error", "-xerror", "-i", name, "-f", "null", "-")
                shutil.copyfile(work / name, args.output / video[key])
                video[key + "_sha256"] = sha256(args.output / video[key])
            shutil.copyfile(args.source_media / video["poster"], args.output / video["poster"])
            video["duration_seconds"] = round(duration, 2)
            video["narration"] = {
                "voice": config["voice"], "language": config["language"], "synthetic": True,
                "source_sha256": clip["source_sha256"],
                "cues": [dict(text=cue["text"], **segment) for cue, segment in zip(clip["cues"], plan)],
            }
            print(f"Narrated {video['step']}: {duration:.2f}s; full WebM/MP4 decode passed", flush=True)
    manifest["narration"] = {key: config[key] for key in (
        "engine", "engine_version", "voice", "voice_description", "model_sha256", "voices_sha256")}
    manifest["narration"]["script_sha256"] = sha256(args.script)
    (args.output / "videos.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
