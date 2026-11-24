#!/usr/bin/env python3
"""Check a private guide capture bundle for completeness, without chain access."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from captions import webvtt

GUIDE = Path(__file__).resolve().parent
CHECKOUT = GUIDE.parents[1]
MAINNET = "35834a8a"
OBJECT_ID = r"0x[0-9a-f]{64}"
DIGEST = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
REVIEW_CHECKS = ("cursor_visible", "redaction", "woman_voice", "ui_labels",
                 "captions_toggle", "mobile_playback")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_digest(path):
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def check_bundle(root):
    root = root.resolve()
    require(not root.is_relative_to(CHECKOUT), "use a private directory outside the checkout")

    def artifact(name):
        path = root / name
        require(path.resolve() == path and path.is_file(), f"missing or unsafe artifact: {name}")
        require(path.stat().st_size > 0, f"empty artifact: {name}")
        return path

    evidence = json.loads(artifact("capture.json").read_text())
    manifest_path = artifact("deployment-manifest.json")
    manifest = json.loads(manifest_path.read_text())
    require(evidence["schema"] == 1 and evidence["source"] == "deployed-app",
            "expected a deployed-app capture record")
    require(evidence["origin"] == "https://app.suifleet.io", "unexpected console origin")
    require(manifest["network"] == "mainnet" and manifest["chain_identifier"] ==
            evidence["observed_chain"] == MAINNET, "mainnet identity mismatch")
    for field in ("manifest_sha256", "console_export_sha256", "artifact_sha256"):
        require(matches(r"[a-f0-9]{64}", evidence[field]), f"invalid {field}")
    require(file_digest(manifest_path) == evidence["manifest_sha256"], "deployment manifest hash mismatch")
    require(matches(r"[a-f0-9]{40}", evidence["console_commit"]), "invalid console commit")
    for field in ("demo_fleet", "demo_wallet"):
        require(matches(OBJECT_ID, evidence[field]), f"invalid {field}")
    for name in ("authorization_ref", "release_verification_ref", "media_review_ref"):
        require(isinstance(evidence[name], str) and evidence[name].strip(), f"missing {name}")

    limit, auxiliary = evidence["approved_mist"], evidence["other_mist"]
    require(type(limit) is int and limit > 0 and type(auxiliary) is int and auxiliary >= 0,
            "expected positive approved ceiling and nonnegative other charges")
    transactions = {}
    spent = auxiliary
    for tx in evidence["transactions"]:
        digest, charge = tx["digest"], tx["charged_mist"]
        require(matches(DIGEST, digest) and digest not in transactions, "invalid or duplicate digest")
        require(tx["effects"] in ("success", "failure"), "unresolved transaction effects")
        require(type(charge) is int and charge >= 0, "invalid transaction charge")
        transactions[digest] = tx["effects"]
        spent += charge
    require(spent <= limit, "recorded charges exceed approved ceiling")

    results = evidence["device_results"]
    require(results, "missing device results")
    devices = set()
    for result in results:
        device = result["device"]
        require(matches(OBJECT_ID, device) and device not in devices, "invalid or duplicate device")
        require(result["execution"] in ("physical", "synthetic"), "unclassified device execution")
        require(matches(OBJECT_ID, result["deployment"]) and result["status"] == "Applied" and
                transactions.get(result["receipt_transaction"]) == "success", "missing successful apply evidence")
        require(isinstance(result["host"], str) and result["host"].strip(), "missing demo host label")
        require(matches(r"[a-f0-9]{64}", result["daemon_log_sha256"]), "missing daemon log hash")
        devices.add(device)

    steps = {step["id"] for step in json.loads((GUIDE / "steps.json").read_text())["steps"]}
    clips = evidence["clips"]
    require(len(clips) == len(steps) and {clip["step"] for clip in clips} == steps,
            "capture coverage must match all guide stages exactly")
    for clip in clips:
        step = clip["step"]
        require(all(clip["review"][check] is True for check in REVIEW_CHECKS),
                f"{step}: incomplete media review")
        for digest in clip["transactions"]:
            require(transactions.get(digest) == "success", f"{step}: unverified transaction shown")
        if step == "deploy-group":
            require({r["receipt_transaction"] for r in results} <= set(clip["transactions"]),
                    "deployment clip lacks a receipt reference")
        for extension in ("mp4", "webm", "jpg", "en.vtt"):
            path = artifact(f"{step}.{extension}")
            if extension in ("mp4", "webm"):
                require(path.stat().st_size <= 15_000_000, f"{step}: video exceeds 15 MB")
            require(file_digest(path) == clip["sha256"][extension], f"{step}: media hash mismatch")
        expected = webvtt(clip["cues"], clip["duration_seconds"])
        require(artifact(f"{step}.en.vtt").read_text() == expected, f"{step}: caption mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        check_bundle(args.bundle)
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(1, f"Incomplete capture bundle: {error}\n")
    print("Capture bundle is internally consistent. Independent acceptance and publication review are required.")


if __name__ == "__main__":
    main()
