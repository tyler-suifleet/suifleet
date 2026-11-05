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

    def test_published_batch_sources_reproduce_reviewed_script_and_spoken_text(self):
        import hashlib
        script=json.loads((GUIDE/'narration.json').read_text())
        manifest=json.loads((GUIDE.parent/'media/guide/videos.json').read_text())
        clips={c['step']:c for c in script['clips']}
        self.assertEqual(manifest['narration']['script_sha256'],hashlib.sha256((GUIDE/'narration.json').read_bytes()).hexdigest())
        batches=manifest['narration']['render_batches'];self.assertEqual(len(batches),3)
        covered=[]
        for batch in batches:
            subset={**script,'clips':[clips[id] for id in batch['steps']]}
            self.assertEqual(hashlib.sha256((json.dumps(subset,indent=2)+'\n').encode()).hexdigest(),batch['script_sha256'])
            covered.extend(batch['steps'])
        self.assertEqual(sorted(covered),sorted(clips))
        for video in manifest['videos']:
            self.assertEqual([c['text'] for c in video['narration']['cues']], [c['text'] for c in clips[video['step']]['cues']])
            self.assertEqual(video['narration']['source_sha256'],clips[video['step']]['source_sha256'])


class CaptionsTest(unittest.TestCase):
    def test_complete_text_and_encoded_timing(self):
        from captions import webvtt
        text = 'A device & its <public> identity.'
        result = webvtt([dict(text=text, speech_start=1.234, speech_end=3.456)], 4)
        self.assertIn('00:00:01.234 --> 00:00:03.456', result)
        self.assertIn('A device &amp; its &lt;public&gt; identity.', result)
        self.assertTrue(result.startswith('WEBVTT\n\n'))

    def test_reject_overlap_truncation_nonfinite_and_invalid_text(self):
        from captions import webvtt
        good = dict(text='Hello.', speech_start=0, speech_end=1)
        for changes in [dict(speech_start=-1), dict(speech_end=5),
                        dict(speech_start=float('nan')), dict(text=''),
                        dict(text='a --> b'), dict(speech_end=0.0001)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                webvtt([good | changes], 4)
        with self.assertRaises(ValueError):
            webvtt([good, good], 4)

    def test_all_published_captions_match_full_encoded_narration(self):
        from captions import webvtt
        import hashlib
        media = GUIDE.parent / 'media/guide'
        manifest = json.loads((media / 'videos.json').read_text())
        for video in manifest['videos']:
            caption = media / video['captions']
            self.assertEqual(caption.read_text(), webvtt(video['narration']['cues'], video['duration_seconds']))
            self.assertEqual(hashlib.sha256(caption.read_bytes()).hexdigest(), video['captions_sha256'])
