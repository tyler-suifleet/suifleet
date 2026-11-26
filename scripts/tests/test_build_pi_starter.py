import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_pi_starter import IMAGE, RECIPE, TARGET, check_compatibility, prepare_bundle, read_pins


class PiStarterTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            'network': 'testnet',
            'chain_identifier': '4c78adac',
            'supported_clients': {'edge': {'min': '0.2.3', 'max_exclusive': '0.3.0'}},
        }

    def test_compatibility_boundaries(self):
        check_compatibility(self.manifest, '0.2.3')
        for invalid in ('0.2.2', '0.3.0', '0.2.3-pre'):
            with self.subTest(version=invalid), self.assertRaises(ValueError):
                check_compatibility(self.manifest, invalid)
        self.manifest['chain_identifier'] = '35834a8a'
        with self.assertRaisesRegex(ValueError, 'requires the Sui testnet'):
            check_compatibility(self.manifest, '0.2.3')

    def test_compatible_recipe_missing_enrollment_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recipe = root / RECIPE
            recipe.parent.mkdir(parents=True)
            recipe.write_text(
                'SRCREV = "' + 'a' * 40 + '"\n'
                'SUIFLEET_LOCK_SHA256 = "' + 'b' * 64 + '"\n'
                'SUIFLEET_CRATE_VERSION = "0.2.3"\n'
            )
            with self.assertRaisesRegex(ValueError, 'lacks SF-322'):
                read_pins(root, self.manifest)
            files = recipe.parent / 'files'
            files.mkdir()
            with recipe.open('a') as stream:
                for name in ('first-boot-enrollment.patch', 'suifleet-first-boot-enrollment.service'):
                    stream.write(f'SRC_URI += "file://{name}"\n')
                    (files / name).touch()
            self.assertEqual(read_pins(root, self.manifest)['crate_version'], '0.2.3')

    def test_mismatch_prevents_bundle_and_manifest_binds_every_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, out = (root / name for name in ('first', 'second', 'out'))
            first.mkdir()
            second.mkdir()
            target_manifest = root / 'network.json'
            target_manifest.write_text(json.dumps(self.manifest))
            for suffix in ('.wic.bz2', '.wic.bmap', '.spdx.tar.zst', '.manifest'):
                (first / (IMAGE + suffix)).write_text(suffix)
                (second / (IMAGE + suffix)).write_text(suffix)
            second_image = second / (IMAGE + '.wic.bz2')
            second_image.write_text('different')
            args = (out, first, second, 'a' * 40, {'crate_version': '0.2.3'}, target_manifest, {})
            with self.assertRaisesRegex(ValueError, 'independent builds differ'):
                prepare_bundle(*args)
            self.assertFalse(out.exists())
            second_image.write_text('.wic.bz2')
            prepare_bundle(*args)
            bundle = json.loads((out / 'release.json').read_text())
            self.assertEqual(bundle['target'], TARGET)
            self.assertEqual({item['path'] for item in bundle['files']},
                             {path.name for path in out.iterdir()} - {'release.json'})
            for item in bundle['files']:
                content = (out / item['path']).read_bytes()
                self.assertEqual(len(content), item['size'])
                self.assertEqual(hashlib.sha256(content).hexdigest(), item['sha256'])
