"""Marketing is product-facing; app/network provenance remains accurate."""
import importlib.util,json,re,tempfile,unittest
from pathlib import Path
from html.parser import HTMLParser
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('product_site_build',ROOT/'site/build.py');build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)
class VisibleText(HTMLParser):
 def __init__(self):super().__init__();self.parts=[];self.hidden=0
 def handle_starttag(self,tag,attrs):
  if tag in {'script','style'}:self.hidden+=1
  for key,value in attrs:
   if key in {'aria-label','content'} and value:self.parts.append(value)
 def handle_endtag(self,tag):
  if tag in {'script','style'}:self.hidden=max(0,self.hidden-1)
 def handle_data(self,text):
  if not self.hidden:self.parts.append(text)
class ProductMessagingTest(unittest.TestCase):
 def test_every_marketing_page_is_independent_of_app_network_stage(self):
  with tempfile.TemporaryDirectory() as temporary:
   out=Path(temporary)/'site';build.build(out,build.load_config())
   for file in out.rglob('*.html'):
    parser=VisibleText();parser.feed(file.read_text());text=' '.join(parser.parts)
    self.assertNotRegex(text,re.compile(r'\b(?:testnet|mainnet|test tokens)\b|public preview',re.I),str(file.relative_to(out)))
 def test_guide_speech_is_product_facing_without_falsifying_recording_provenance(self):
  script=json.loads((ROOT/'site/guide/narration.json').read_text())
  for clip in script['clips']:
   for cue in clip['cues']:self.assertNotRegex(cue['text'],re.compile(r'\b(?:testnet|mainnet|test tokens)\b',re.I))
  manifest=json.loads((ROOT/'site/media/guide/videos.json').read_text())
  self.assertEqual(manifest['network'],'testnet');self.assertEqual(manifest['chain'],'4c78adac')
  profile=json.loads((ROOT/'profiles/seal-testnet/deployment.json').read_text());self.assertEqual(profile['network'],'testnet')
