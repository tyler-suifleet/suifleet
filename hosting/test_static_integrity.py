import importlib.util,tempfile,unittest,hashlib
from pathlib import Path
spec=importlib.util.spec_from_file_location('host_build',Path(__file__).with_name('build.py'));build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)
class IntegrityTest(unittest.TestCase):
 def test_transform_optout_scopes_html_and_refreshes_final_inventory(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'contact').mkdir();(root/'assets').mkdir()
   (root/'index.html').write_text('home');(root/'contact/index.html').write_text('hello@example.com');(root/'assets/movie.mp4').write_bytes(b'video')
   (root/'_headers').write_text("/*\n  Content-Security-Policy: default-src 'none'\n")
   (root/'SHA256SUMS').write_text('stale inventory')
   build.finalize_static(root)
   headers=(root/'_headers').read_text();self.assertIn("Content-Security-Policy: default-src 'none'",headers)
   self.assertIn('\n/\n  Cache-Control: public, max-age=0, must-revalidate, no-transform',headers)
   self.assertIn('\n/contact/\n  Cache-Control:',headers);self.assertNotIn('/assets/',headers)
   sums=(root/'SHA256SUMS').read_text();self.assertIn(hashlib.sha256((root/'_headers').read_bytes()).hexdigest()+'  _headers',sums)
   self.assertIn('  contact/index.html',sums);self.assertNotIn('  SHA256SUMS',sums)
if __name__=='__main__':unittest.main()
