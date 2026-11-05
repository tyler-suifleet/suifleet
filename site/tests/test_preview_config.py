import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import build
import check

class PreviewConfigTest(unittest.TestCase):
    def config(self, root, values):
        path=root/'preview.json';path.write_text(json.dumps(values));return build.load_config(path)
    def test_public_links_are_built_and_checked_without_rewriting_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=self.config(root,{'origin':'https://site.example','app_url':'https://console.example','docs_url':'https://help.example'})
            out=root/'out';build.build(out,config)
            self.assertEqual(check.check(out,True,config),[])
            html=(out/'guide/index.html').read_text()
            self.assertIn('https://console.example/',html);self.assertIn('https://help.example/',html)
            self.assertNotIn('172.27.',html);self.assertNotIn('localhost',html)
            self.assertIn('https://site.example/',(out/'sitemap.xml').read_text())
    def test_unsafe_or_unrelated_configuration_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for value in ['http://site.example','https://localhost/','https://172.27.90.223/','https://user:password@site.example/','https://site.example/?secret=x','https://site.example/#x']:
                with self.subTest(value=value),self.assertRaises(build.BuildError):self.config(root,{'app_url':value})
            with self.assertRaises(build.BuildError):self.config(root,{'repo_url':'https://other.example'})
