"""SF-073 native component entry point (stdlib until verified runtime import).

Execute a built bundle with python3 -I -B BUNDLE install|infer --state PATH.
The recipe/outer artifact digest is the trust root. This is not a signature
scheme: a mutable bundle and mutable expected digest cannot authenticate each other.
"""
import argparse
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import sys
import tempfile
import zipfile

VERSION = "1.0.0"
TASKS = {"classifier": ([1, 16], [1, 2]), "detector": ([1, 4, 4, 1], [1, 2, 2, 1])}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def target():
    return f"{sys.platform}-{platform.machine()}-cp{sys.version_info.major}{sys.version_info.minor}"


def safe_name(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or "\\" in name or str(p) != name:
        raise ValueError("unsafe archive member")
    return p


def read_bundle(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate archive member")
        manifest = json.loads(archive.read("manifest.json"))
        if type(manifest["format_version"]) is not int or manifest["format_version"] != 1 or manifest["component_version"] != VERSION:
            raise ValueError("unsupported component version")
        if manifest["model_version"] != VERSION or manifest["task"] not in TASKS:
            raise ValueError("unsupported model/version")
        if manifest["runtime_version"] != "2.14.0" or manifest["numpy_version"] != "1.26.4":
            raise ValueError("unsupported runtime version")
        if manifest["platform"] != target():
            raise ValueError("unsupported platform")
        libc, version = platform.libc_ver()
        if libc != "glibc" or tuple(map(int, version.split("."))) < tuple(map(int, manifest["glibc_min"].split("."))):
            raise ValueError("unsupported libc")
        if set(names) != set(manifest["files"]) | {"manifest.json"}:
            raise ValueError("bundle inventory mismatch")
        files = {}
        for name, expected in manifest["files"].items():
            safe_name(name)
            raw = archive.read(name)
            if digest(raw) != expected:
                raise ValueError(f"digest mismatch: {name}")
            files[name] = raw
        return manifest, files


def runtime_files(manifest, files):
    result = {}
    for name in manifest["wheels"]:
        with zipfile.ZipFile(io.BytesIO(files[name])) as wheel:
            for info in wheel.infolist():
                if info.is_dir():
                    continue
                safe_name(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16) or info.filename in result:
                    raise ValueError("unsafe/duplicate wheel member")
                result[info.filename] = wheel.read(info)
    return result


def verify_state(state, expected):
    if state.is_symlink() or not state.is_dir():
        raise ValueError("missing/unsafe installed runtime")
    actual = {}
    for p in state.rglob("*"):
        if p.is_symlink():
            raise ValueError("runtime symlink")
        if p.is_file():
            actual[p.relative_to(state).as_posix()] = digest(p.read_bytes())
    if actual != {name: digest(raw) for name, raw in expected.items()}:
        raise ValueError("installed runtime digest mismatch")


def install(state, expected):
    # A replacement or ambiguous prior install is never overwritten. A staging
    # directory abandoned by SIGKILL is never imported or selected on restart.
    if state.exists() or state.is_symlink():
        verify_state(state, expected)
        return
    state.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".ml-stage-", dir=state.parent))
    try:
        for name, raw in expected.items():
            p = stage / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        verify_state(stage, expected)
        # rename won't replace a populated state created by a competing installer.
        os.rename(stage, state)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def pixels(raw):
    # Deliberately narrow PGM input: no implicit resize/crop/colorspace conversion.
    header = b"P5\n4 4\n255\n"
    if len(raw) != len(header) + 16 or not raw.startswith(header):
        raise ValueError("input must be canonical 4x4 8-bit grayscale PGM")
    return list(raw[len(header):])


def infer(manifest, files, state, raw):
    values = pixels(raw)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(state.resolve()))
    if importlib.metadata.version("tflite-runtime") != manifest["runtime_version"] or importlib.metadata.version("numpy") != manifest["numpy_version"]:
        raise ValueError("installed runtime version mismatch")
    import numpy as np
    from tflite_runtime.interpreter import Interpreter, OpResolverType

    task = manifest["task"]
    interpreter = Interpreter(model_content=files[f"models/{task}-1.0.0.tflite"], num_threads=1,
                              experimental_op_resolver_type=OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES)
    interpreter.allocate_tensors()
    inputs, outputs = interpreter.get_input_details(), interpreter.get_output_details()
    if (len(inputs) != 1 or len(outputs) != 1 or
            inputs[0]["shape"].tolist() != TASKS[task][0] or outputs[0]["shape"].tolist() != TASKS[task][1] or
            inputs[0]["dtype"] != np.float32 or outputs[0]["dtype"] != np.float32):
        raise ValueError("model tensor contract mismatch")
    tensor = (np.array(values, dtype=np.float32) / np.float32(255)).reshape(TASKS[task][0])
    interpreter.set_tensor(inputs[0]["index"], tensor)
    interpreter.invoke()
    scores = interpreter.get_tensor(outputs[0]["index"]).reshape(-1)
    if not np.isfinite(scores).all():
        raise ValueError("nonfinite inference result")
    result = {"task": task, "model_version": VERSION, "runtime_version": manifest["runtime_version"],
              "model_sha256": manifest["files"][f"models/{task}-1.0.0.tflite"],
              "input_sha256": digest(raw), "scores": scores.tolist()}
    if task == "classifier":
        result["label"] = ["dark", "bright"][int(np.argmax(scores))]
    else:
        # Four fixed 2x2 anchors; row-major; no NMS or dynamic shape assumptions.
        result["detections"] = [{"label": "bright-marker", "score": float(score),
                                 "box_xyxy": [(i % 2)*2, (i // 2)*2, (i % 2)*2+2, (i // 2)*2+2]}
                                for i, score in enumerate(scores) if score >= 0.75]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "infer"))
    parser.add_argument("--state", required=True, type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--image", type=Path)
    source.add_argument("--fixture", choices=("dark", "bright", "marker", "two-markers", "gray"))
    args = parser.parse_args()
    try:
        if not sys.flags.isolated:
            raise ValueError("use python3 -I -B to isolate runtime imports")
        manifest, files = read_bundle(sys.argv[0])
        expected = runtime_files(manifest, files)
        if args.command == "install":
            install(args.state, expected)
            result = {"status": "installed", "platform": manifest["platform"]}
        else:
            verify_state(args.state, expected)
            if args.fixture:
                raw = files[f"images/{args.fixture}.pgm"]
            elif args.image:
                # Bound before decoding; no image codecs or attacker dimensions.
                with args.image.open("rb") as image:
                    raw = image.read(64)
            else:
                raise ValueError("infer requires --image or --fixture")
            result = infer(manifest, files, args.state, raw)
        print(json.dumps(result, allow_nan=False, sort_keys=True))
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, RuntimeError, ImportError) as error:
        parser.exit(1, f"ml-component: {error}\n")


if __name__ == "__main__":
    main()
