import importlib.util,json,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('sample_build',ROOT/'site/build.py');build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)
class MotionSampleTest(unittest.TestCase):
 def test_real_remotion_artifacts_are_integrity_bound(self):
  m=build.motion_sample_manifest();self.assertEqual(m['renderer'],'Remotion');self.assertEqual(m['fps'],30);self.assertEqual(m['duration_seconds'],20);self.assertEqual(len(m['timing']['cues']),4)
  with tempfile.TemporaryDirectory() as tmp:
   copy=Path(tmp)/'media';shutil.copytree(build.SAMPLE_MEDIA,copy);(copy/'portrait.mp4').write_bytes(b'changed')
   with patch.object(build,'SAMPLE_MEDIA',copy),self.assertRaisesRegex(build.BuildError,'digest'):build.motion_sample_manifest()
 def test_new_player_has_optional_captions_and_no_autoplay(self):
  from html.parser import HTMLParser
  class Parser(HTMLParser):
   def __init__(self):super().__init__();self.videos=[];self.tracks=[]
   def handle_starttag(self,tag,attrs):
    if tag=='video':self.videos.append(dict(attrs))
    if tag=='track':self.tracks.append(dict(attrs))
  p=Parser();p.feed(build.motion_sample_player());self.assertEqual(len(p.videos),2);self.assertEqual(len(p.tracks),2)
  for v in p.videos:self.assertIn('controls',v);self.assertIn('playsinline',v);self.assertNotIn('autoplay',v)
  for t in p.tracks:self.assertNotIn('default',t);self.assertEqual(t['kind'],'captions')
 def test_review_page_is_unlisted_and_keeps_existing_overview(self):
  with tempfile.TemporaryDirectory() as tmp:
   out=Path(tmp)/'site';build.build(out,build.load_config());page=(out/'film-preview/index.html').read_text();self.assertIn('noindex, nofollow',page);self.assertNotIn('/film-preview/',(out/'sitemap.xml').read_text());self.assertIn('/media/overview/',(out/'how-it-works/index.html').read_text())
   for v in build.overview_manifest()['variants']:self.assertEqual(build.sha256(out/'media/overview'/v['file']),v['file_sha256'])
