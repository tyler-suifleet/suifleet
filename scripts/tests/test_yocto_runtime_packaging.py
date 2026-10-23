"""Offline SF-120 checks: codec packaging and the Yocto Node/codec/daemon pins."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).parents[2]
LAYER = ROOT/'suifleet-edge/meta-suifleet'
spec = importlib.util.spec_from_file_location('package_walrus_codec', ROOT/'scripts/package_walrus_codec.py')
codec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codec)
spec = importlib.util.spec_from_file_location('test_pi_codec', ROOT/'scripts/test_pi_codec.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

DAEMON = (LAYER/'recipes-edge/suifleet/suifleet_1.0.bb').read_text()
CODEC = (LAYER/'recipes-edge/suifleet-walrus-codec/suifleet-walrus-codec_1.0.bb').read_text()
NODE_RECIPES = sorted((LAYER/'recipes-devtools/suifleet-node').glob('suifleet-node_*.bb'))


def var(text, name):
    match = re.search(r'^'+re.escape(name)+r' = "([^"]*)"$', text, re.M)
    if not match:
        raise AssertionError(name + ' not set')
    return match.group(1)


def fake_package(modules, store, name, version, license=None, files=()):
    root = modules/'.pnpm'/store/'node_modules'/name
    root.mkdir(parents=True)
    meta = dict(name=name, version=version)
    if license:
        meta['license'] = license
    (root/'package.json').write_text(json.dumps(meta))
    for f in files:
        (root/f).write_text(f'{name} {f} text\n')
    return f'// dapp/node_modules/.pnpm/{store}/node_modules/{name}/dist/index.js\n'


class BundledLicenseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.modules = Path(self.directory.name)/'node_modules'

    def tearDown(self):
        self.directory.cleanup()

    def test_collects_each_bundled_package_once_with_license_files(self):
        bundle = fake_package(self.modules, 'a@1.0.0', 'a', '1.0.0', 'MIT', ['LICENSE'])
        bundle += fake_package(self.modules, '@x+b@2.0.0', '@x/b', '2.0.0', 'Apache-2.0', ['LICENSE.md', 'README.md'])
        bundle += bundle + '  // dapp/scripts/entry.mjs\n'
        packages = codec.bundled_packages(bundle, self.modules)
        self.assertEqual([(p['name'], p['license'], p['license_files']) for p in packages],
                         [('@x/b', 'Apache-2.0', ['LICENSE.md']), ('a', 'MIT', ['LICENSE'])])
        text = codec.license_text(packages)
        self.assertIn('@x/b LICENSE.md text', text)
        self.assertEqual(text, codec.license_text(codec.bundled_packages(bundle, self.modules)))

    def test_undeclared_license_is_refused(self):
        bundle = fake_package(self.modules, 'c@1.0.0', 'c', '1.0.0')
        with self.assertRaisesRegex(ValueError, 'declares no license'):
            codec.bundled_packages(bundle, self.modules)

    def test_reviewed_undeclared_package_is_named_not_guessed(self):
        bundle = fake_package(self.modules, '@mysten+walrus-wasm@0.3.1', '@mysten/walrus-wasm', '0.3.1')
        [package] = codec.bundled_packages(bundle, self.modules)
        self.assertEqual(package['license'], codec.UNDECLARED_LICENSE)
        self.assertIn('UNDECLARED', codec.license_text([package]))

    def test_stale_undeclared_override_is_refused(self):
        bundle = fake_package(self.modules, '@mysten+walrus-wasm@0.3.1', '@mysten/walrus-wasm', '0.3.1', 'Apache-2.0')
        with self.assertRaisesRegex(ValueError, 'remove its undeclared override'):
            codec.bundled_packages(bundle, self.modules)

    def test_empty_bundle_is_refused(self):
        with self.assertRaises(ValueError):
            codec.bundled_packages('console.log(1)\n', self.modules)


class ArtifactPinTests(unittest.TestCase):
    def artifact(self, directory, revision):
        for name in codec.ARTIFACTS:
            (directory/name).write_text(name)
        (directory/'provenance.json').write_text(json.dumps(dict(source_revision=revision)))
        lines = [f'SUIFLEET_CODEC_SOURCE_REV = "{revision}"']
        lines += [f'SUIFLEET_CODEC_SHA256[{n}] = "{hashlib.sha256((directory/n).read_bytes()).hexdigest()}"' for n in codec.ARTIFACTS]
        recipe = directory.parent/'codec.bb'
        recipe.write_text('\n'.join(lines)+'\n')
        return recipe

    def test_matching_artifact_verifies_and_tampering_fails(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/'codec'; out.mkdir()
            recipe = self.artifact(out, 'a'*40)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(codec.main(['--output', str(out), '--recipe', str(recipe)]), 0)
                (out/'walrus_wasm_bg.wasm').write_text('tampered')
                self.assertEqual(codec.main(['--output', str(out), '--recipe', str(recipe)]), 1)

    def test_extra_or_missing_files_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/'codec'; out.mkdir()
            recipe = self.artifact(out, 'a'*40)
            (out/'node_modules').mkdir()
            with self.assertRaisesRegex(ValueError, 'exactly'):
                codec.main(['--output', str(out), '--recipe', str(recipe)])

    def test_recipe_pins_cover_every_artifact(self):
        revision, pins = codec.recipe_pins(CODEC)
        self.assertRegex(revision, '^[0-9a-f]{40}$')
        self.assertEqual(sorted(pins), sorted(codec.ARTIFACTS))
        with self.assertRaises(ValueError):
            codec.recipe_pins(CODEC.replace('SUIFLEET_CODEC_SHA256[provenance.json]', '# removed'))


class InstalledArtifactTests(unittest.TestCase):
    def test_manifest_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            files = {}
            for name in ['walrus-device-codec.cjs', 'walrus_wasm_bg.wasm']:
                (root/name).write_bytes(name.encode())
                files[name] = dict(bytes=len(name), sha256=hashlib.sha256(name.encode()).hexdigest())
            (root/'manifest.json').write_text(json.dumps(dict(schema=1, node='22.22.2', files=files)))
            (root/'provenance.json').write_text(json.dumps(dict(manifest_sha256=smoke.sha256(root/'manifest.json'))))
            smoke.check_artifact(root, '22.22.2')
            with self.assertRaisesRegex(ValueError, 'targets Node'):
                smoke.check_artifact(root, '24.0.0')
            (root/'walrus_wasm_bg.wasm').write_bytes(b'x')
            with self.assertRaisesRegex(ValueError, 'differs from its manifest'):
                smoke.check_artifact(root, '22.22.2')

    def test_vector_is_deterministic(self):
        self.assertEqual(smoke.ciphertext(100), smoke.ciphertext(100))
        self.assertEqual(len(smoke.ciphertext(1000)), 1000)


class RecipeTests(unittest.TestCase):
    def test_single_pinned_official_node_release(self):
        self.assertEqual(len(NODE_RECIPES), 1)
        text = NODE_RECIPES[0].read_text()
        version = NODE_RECIPES[0].stem.split('_')[1]
        self.assertRegex(text, r'SRC_URI = "https://nodejs\.org/dist/v\$\{PV\}/node-v\$\{PV\}-linux-arm64\.tar\.xz;sha256sum=[0-9a-f]{64}"')
        self.assertEqual(var(CODEC, 'SUIFLEET_CODEC_NODE_VERSION'), version)
        self.assertIn('COMPATIBLE_HOST = "aarch64.*-linux"', text)
        self.assertRegex(text, r'LIC_FILES_CHKSUM = "file://LICENSE;md5=[0-9a-f]{32}"')
        self.assertNotIn('npm', re.findall(r'^FILES:\$\{PN\} = "([^"]*)"', text, re.M)[0])

    def test_codec_recipe_requires_its_node_and_verifies_before_install(self):
        self.assertIn('RDEPENDS:${PN} = "suifleet-node (= ${SUIFLEET_CODEC_NODE_VERSION})"', CODEC)
        self.assertIn('addtask verify_codec after do_unpack before do_populate_lic do_configure', CODEC)
        self.assertRegex(CODEC, r'LIC_FILES_CHKSUM = "file://THIRD_PARTY_LICENSES\.txt;md5=[0-9a-f]{32}"')
        self.assertIn('SUIFLEET_WALRUS_CODEC_DIR ??= "${TOPDIR}/suifleet-walrus-codec/${SUIFLEET_CODEC_SOURCE_REV}"', CODEC)
        source = re.search(r'^SRC_URI = "([^"]*)"', CODEC, re.M | re.S).group(1)
        self.assertEqual(re.findall(r'file://([^;\s]+)', source), codec.ARTIFACTS)

    def test_installed_paths_match_configuration_examples(self):
        example = (LAYER/'recipes-edge/suifleet/files/config.toml.example').read_text()
        self.assertIn('# node = "/usr/lib/suifleet/node/bin/node"', example)
        self.assertIn('# codec = "/usr/lib/suifleet/walrus-codec/walrus-device-codec.cjs"', example)
        self.assertIn('/'+smoke.NODE, example)
        self.assertIn('SUIFLEET_CODEC_DIR = "${libdir}/suifleet/walrus-codec"', CODEC)
        self.assertIn('SUIFLEET_NODE_DIR = "${libdir}/suifleet/node"', NODE_RECIPES[0].read_text())

    def test_images_install_the_codec(self):
        image = (LAYER/'recipes-core/images/suifleet-pi-image.bb').read_text()
        self.assertIn(' suifleet-walrus-codec ', image)
        for profile in ['packaging-qemu.yml', 'packaging-wrynose-qemu.yml']:
            self.assertIn(' suifleet-walrus-codec ', (ROOT/'kas'/profile).read_text(), profile)

    def test_daemon_pins_are_immutable_and_consistent(self):
        self.assertRegex(var(DAEMON, 'SRCREV'), '^[0-9a-f]{40}$')
        self.assertNotIn('AUTOREV', DAEMON)
        self.assertRegex(var(DAEMON, 'SUIFLEET_LOCK_SHA256'), '^[0-9a-f]{64}$')
        self.assertRegex(var(DAEMON, 'SUIFLEET_CRATE_VERSION'), r'^\d+\.\d+\.\d+$')
        self.assertIn("'${SUIFLEET_CRATE_VERSION}' > ${D}${datadir}/suifleet/build-info", DAEMON)

    # SF-120 prepares the codec against published SF-082 ca002d3 while the
    # daemon still pins d616fa16. Remove this decorator in the final pin commit
    # that moves SRCREV to the merged SF-082/SF-084 source; an unexpected pass
    # fails the suite, so the two pins cannot silently stay apart.
    @unittest.expectedFailure
    def test_codec_and_daemon_share_source_revision(self):
        self.assertEqual(var(CODEC, 'SUIFLEET_CODEC_SOURCE_REV'), var(DAEMON, 'SRCREV'))


if __name__ == '__main__':
    unittest.main()
