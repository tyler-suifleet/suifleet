import hashlib,importlib.util,json,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('overview_build',ROOT/'site/build.py');build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)
class OverviewTest(unittest.TestCase):
 def test_media_matches_narration_and_actual_render_sources(self):
  m=build.overview_manifest();story=json.loads((ROOT/'site/overview/storyboard.json').read_text())
  self.assertEqual(m['kind'],'illustrative-animation');self.assertTrue(m['synthetic_voice']);self.assertEqual(m['voice'],'af_heart')
  self.assertEqual([s['text'] for s in m['scenes']],[s['text'] for s in story['scenes']])
  self.assertEqual([c['text'] for c in m['cues']],[s['text'] for s in story['scenes']])
  end=0
  for cue in m['cues']:
   self.assertGreaterEqual(cue['speech_start'],end);self.assertGreater(cue['speech_end'],cue['speech_start']);self.assertLess(cue['speech_end'],m['duration_seconds']);end=cue['speech_end']
  for name,digest in m['source_sha256'].items():self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),digest)
 def test_changed_caption_or_file_is_rejected(self):
  with tempfile.TemporaryDirectory() as temp:
   source=Path(temp)/'media';shutil.copytree(build.OVERVIEW_MEDIA,source)
   m=json.loads((source/'manifest.json').read_text());(source/m['captions']).write_text('Replaced narration')
   with patch.object(build,'OVERVIEW_MEDIA',source),self.assertRaisesRegex(build.BuildError,'captions'):build.overview_manifest()
 def test_accessible_no_autoplay_player_and_full_transcript(self):
  from html.parser import HTMLParser
  class Parser(HTMLParser):
   def __init__(self):super().__init__();self.videos=[];self.tracks=[]
   def handle_starttag(self,tag,attrs):
    if tag=='video':self.videos.append(dict(attrs))
    if tag=='track':self.tracks.append(dict(attrs))
  markup=build.overview_player();p=Parser();p.feed(markup)
  self.assertEqual(len(p.videos),2);self.assertEqual(len(p.tracks),2)
  for video in p.videos:self.assertIn('controls',video);self.assertIn('playsinline',video);self.assertNotIn('autoplay',video);self.assertEqual(video['preload'],'none')
  for track in p.tracks:self.assertEqual(track['kind'],'captions');self.assertEqual(track['srclang'],'en')
  self.assertIn('Read the complete narration',markup);self.assertIn('Illustrative animation',markup)
