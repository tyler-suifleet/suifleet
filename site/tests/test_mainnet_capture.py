"""Offline bundle checks. These media stubs provide no live acceptance evidence."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "guide"))
from check_mainnet_capture import GUIDE, REVIEW_CHECKS, check_bundle, file_digest
from captions import webvtt


class MainnetCaptureTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        manifest = self.root / "deployment-manifest.json"
        manifest.write_text(json.dumps({"network": "mainnet", "chain_identifier": "35834a8a"}))
        self.digest = "A" * 43
        self.evidence = {
            "schema": 1, "source": "deployed-app", "origin": "https://app.suifleet.io",
            "observed_chain": "35834a8a", "manifest_sha256": file_digest(manifest),
            "console_export_sha256": "a" * 64, "artifact_sha256": "b" * 64,
            "console_commit": "c" * 40,
            "demo_fleet": "0x" + "d" * 64, "demo_wallet": "0x" + "e" * 64,
            "authorization_ref": "fixture", "release_verification_ref": "fixture",
            "media_review_ref": "fixture", "approved_mist": 12, "other_mist": 2,
            "transactions": [{"digest": self.digest, "effects": "success", "charged_mist": 9},
                             {"digest": "B" * 43, "effects": "failure", "charged_mist": 1}],
            "device_results": [{
                "device": "0x" + "1" * 64, "deployment": "0x" + "2" * 64,
                "host": "fixture", "execution": "synthetic", "status": "Applied",
                "receipt_transaction": self.digest, "daemon_log_sha256": "3" * 64,
            }],
            "clips": [],
        }
        for step in json.loads((GUIDE / "steps.json").read_text())["steps"]:
            name = step["id"]
            cues = [{"speech_start": 0, "speech_end": 1, "text": "Offline fixture."}]
            hashes = {}
            for suffix in ("mp4", "webm", "jpg", "en.vtt"):
                path = self.root / f"{name}.{suffix}"
                path.write_text(webvtt(cues, 2) if suffix == "en.vtt" else "fixture")
                hashes[suffix] = file_digest(path)
            self.evidence["clips"].append({
                "step": name, "duration_seconds": 2, "cues": cues, "sha256": hashes,
                "transactions": [self.digest] if name == "deploy-group" else [],
                "review": dict.fromkeys(REVIEW_CHECKS, True),
            })

    def check(self, evidence):
        (self.root / "capture.json").write_text(json.dumps(evidence))
        check_bundle(self.root)

    def test_complete_record_at_approved_ceiling(self):
        self.check(self.evidence)

    def test_rejects_wrong_source_origin_and_chain(self):
        for field, value, error in [
            ("source", "scripted-wallet", "deployed-app"),
            ("origin", "https://app.suifleet.io.example.org", "console origin"),
            ("observed_chain", "4c78adac", "identity mismatch"),
        ]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                self.check(dict(self.evidence, **{field: value}))

    def test_failed_effects_count_against_ceiling_and_unknown_effects_stop(self):
        with self.assertRaisesRegex(ValueError, "exceed approved ceiling"):
            self.check(dict(self.evidence, approved_mist=11))
        self.evidence["transactions"][-1]["effects"] = "pending"
        with self.assertRaisesRegex(ValueError, "unresolved transaction"):
            self.check(self.evidence)

    def test_requires_successful_applied_result_linked_to_deployment_clip(self):
        evidence = deepcopy(self.evidence)
        evidence["device_results"][0]["receipt_transaction"] = "B" * 43
        with self.assertRaisesRegex(ValueError, "successful apply evidence"):
            self.check(evidence)
        clip = next(clip for clip in self.evidence["clips"] if clip["step"] == "deploy-group")
        clip["transactions"] = []
        with self.assertRaisesRegex(ValueError, "lacks a receipt reference"):
            self.check(self.evidence)

    def test_rejects_missing_stage_or_incomplete_review(self):
        with self.assertRaisesRegex(ValueError, "capture coverage"):
            self.check(dict(self.evidence, clips=self.evidence["clips"][1:]))
        self.evidence["clips"][0]["review"]["mobile_playback"] = False
        with self.assertRaisesRegex(ValueError, "incomplete media review"):
            self.check(self.evidence)

    def test_rejects_changed_media_and_rehashed_incorrect_captions(self):
        clip = self.evidence["clips"][0]
        path = self.root / f'{clip["step"]}.en.vtt'
        path.write_text(webvtt(clip["cues"], 2).replace("Offline fixture.", "Wrong narration."))
        with self.assertRaisesRegex(ValueError, "media hash mismatch"):
            self.check(self.evidence)
        clip["sha256"]["en.vtt"] = file_digest(path)
        with self.assertRaisesRegex(ValueError, "caption mismatch"):
            self.check(self.evidence)

    def test_rejects_linked_media(self):
        path = self.root / f'{self.evidence["clips"][0]["step"]}.mp4'
        path.rename(self.root / "linked.mp4")
        path.symlink_to(self.root / "linked.mp4")
        with self.assertRaisesRegex(ValueError, "unsafe artifact"):
            self.check(self.evidence)
