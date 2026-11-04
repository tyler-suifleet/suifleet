"""Narration timing tests run offline without models or a speech package."""
import json
from pathlib import Path
import sys
import unittest

GUIDE = Path(__file__).resolve().parents[1] / "guide"
sys.path.insert(0, str(GUIDE))
from narrate_guide import timing


class NarrationTest(unittest.TestCase):
    def test_long_speech_extends_action_without_truncation_or_overlap(self):
        cues = [{"source_start": 0}, {"source_start": 2}, {"source_start": 10}]
        plan, duration = timing(cues, 12, [5, 2, 4])
        self.assertGreater(duration, 12)
        self.assertEqual([(s["source_start"], s["source_end"]) for s in plan], [(0, 2), (2, 10), (10, 12)])
        for i, segment in enumerate(plan):
            self.assertGreaterEqual(segment["duration"], segment["source_end"] - segment["source_start"])
            self.assertGreater(segment["speech_start"], segment["start"])
            self.assertLess(segment["speech_end"], segment["start"] + segment["duration"])
            if i + 1 < len(plan):
                self.assertAlmostEqual(segment["start"] + segment["duration"], plan[i + 1]["start"])

    def test_invalid_or_unmatched_cues_fail(self):
        for starts, total, spoken in [([], 5, []), ([0], 5, []), ([1], 5, [1]),
                                      ([0, 0], 5, [1, 1]), ([0, 6], 5, [1, 1]),
                                      ([0], 5, [float("nan")]), ([0], 5, [0]),
                                      ([0, float("nan")], 5, [1, 1])]:
            with self.subTest(starts=starts, spoken=spoken), self.assertRaises(ValueError):
                timing([{"source_start": t} for t in starts], total, spoken)

    def test_reviewed_script_covers_all_stages_with_valid_source_timing(self):
        script = json.loads((GUIDE / "narration.json").read_text())
        steps = json.loads((GUIDE / "steps.json").read_text())["steps"]
        self.assertEqual(sorted(c["step"] for c in script["clips"]), sorted(s["id"] for s in steps))
        self.assertEqual(script["voice"], "af_heart")
        for clip in script["clips"]:
            self.assertTrue(all(cue["text"].strip() for cue in clip["cues"]))
            timing(clip["cues"], clip["source_duration_seconds"], [1] * len(clip["cues"]))
