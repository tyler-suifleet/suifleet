import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_pi_starter import (
    ENROLLMENT, IMAGE, OUTPUTS, TARGET, build_candidate, check_compatibility,
    compare_candidates, prepare_bundle, read_recipe, verify_unsigned,
)


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

    def test_effective_recipe_requires_reviewed_enrollment(self):
        service = 'suifleet-first-boot-enrollment.service'
        patch = 'first-boot-enrollment.patch'
        environment = (
            f'SRCREV="{"a" * 40}"\n'
            f'SUIFLEET_LOCK_SHA256="{"b" * 64}"\n'
            'SUIFLEET_CRATE_VERSION="0.2.3"\n'
            f'"SYSTEMD_SERVICE:suifleet"="{service}"\n'
            f'FILESPATH="{ENROLLMENT / ".." / "files"}"\n'
            f'SRC_URI="https://example.test/daemon.tar.gz;name=edge;subdir=source file://{service}"\n'
        )
        recipe = f'SRC_URI[edge.sha256sum] = "{"c" * 64}"'
        with self.assertRaisesRegex(ValueError, 'lacks reviewed SF-322 input'):
            read_recipe(environment, recipe, self.manifest)
        complete = environment.replace(f'file://{service}', f'file://{service} file://{patch}')
        pins, provenance = read_recipe(complete, recipe, self.manifest)
        self.assertEqual(pins['crate_version'], '0.2.3')
        self.assertEqual(set(provenance['enrollment_files']), {patch, service})
        self.assertEqual(provenance['archive_sha256'], 'c' * 64)
        with self.assertRaisesRegex(ValueError, 'does not register'):
            read_recipe(complete.replace(f'="{service}"', '=""'), recipe, self.manifest)

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

    def test_candidate_handoff_rejects_different_metadata_and_corrupt_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deploy = root / 'deploy'
            deploy.mkdir()
            for suffix in OUTPUTS:
                (deploy / (IMAGE + suffix)).write_text(suffix)
            manifest_path = root / 'target.json'
            manifest_path.write_text(json.dumps(self.manifest))
            first, second, out = (root / name for name in ('first', 'second', 'out'))
            commit = 'a' * 40
            build_result = deploy, {}, {'crate_version': '0.2.3'}, {}, {}
            with (patch('build_pi_starter.build', return_value=build_result),
                  patch('build_pi_starter.git', return_value=commit),
                  patch('build_pi_starter.subprocess.check_output', return_value='kas 4.7')):
                build_candidate(root / 'work-first', manifest_path, first)
                build_candidate(root / 'work-second', manifest_path, second)
                second_manifest = second / 'deployment-manifest.json'
                second_manifest.write_text('{}')
                with self.assertRaisesRegex(ValueError, 'metadata differs'):
                    compare_candidates(first, second, out)
                self.assertFalse(out.exists())
                second_manifest.write_bytes(manifest_path.read_bytes())
                compare_candidates(first, second, out)
            verify_unsigned(out, commit, '0.2.3')
            for wrong_commit, wrong_version in (('b' * 40, '0.2.3'), (commit, '0.2.4')):
                with self.assertRaisesRegex(ValueError, 'identity'):
                    verify_unsigned(out, wrong_commit, wrong_version)
            asset = out / OUTPUTS['.wic.bz2']
            asset.write_bytes(b'x' * asset.stat().st_size)
            with self.assertRaisesRegex(ValueError, 'file verification failed'):
                verify_unsigned(out, commit, '0.2.3')
            asset.unlink()
            asset.symlink_to(deploy / (IMAGE + '.wic.bz2'))
            with self.assertRaisesRegex(ValueError, 'unexpected unsigned bundle contents'):
                verify_unsigned(out, commit, '0.2.3')
