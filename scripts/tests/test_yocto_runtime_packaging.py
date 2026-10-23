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
spec = importlib.util.spec_from_file_location('test_pi_archive', ROOT/'scripts/test_pi_archive.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)

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

    def test_empty_bundle_is_refused(self):
        with self.assertRaises(ValueError):
            codec.bundled_packages('console.log(1)\n', self.modules)


class LicenseInventoryTests(unittest.TestCase):
    """A package without a license field ships only with a verified reviewed inventory."""
    NAME, VERSION, INTEGRITY = '@mysten/walrus-wasm', '0.3.1', 'sha512-fixture=='

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.modules, self.inventories = base/'node_modules', base/'inventories'
        self.inventories.mkdir()
        self.bundle = fake_package(self.modules, '@mysten+walrus-wasm@0.3.1', self.NAME, self.VERSION)
        self.root = self.modules/'.pnpm/@mysten+walrus-wasm@0.3.1/node_modules'/self.NAME
        (self.root/'index.js').write_text('// Copyright (c) Mysten Labs, Inc.\n// SPDX-License-Identifier: Apache-2.0\n')
        (self.root/'Cargo.lock').write_text('# lock\n')
        mit, apache = 'MIT text\n', 'Apache text\n'
        self.data = dict(schema=1, package=self.NAME, version=self.VERSION, npm_integrity=self.INTEGRITY,
                         cargo_lock_sha256=codec.sha256(self.root/'Cargo.lock'), wasm_sha256='0'*64,
                         package_license='Apache-2.0', package_license_evidence=['index.js'], target='wasm32-unknown-unknown',
                         crates=[dict(name='walrus-wasm', version='0.1.0', source=None, license='Apache-2.0',
                                      texts=[dict(file='LICENSE', sha256=hashlib.sha256(apache.encode()).hexdigest())]),
                                 dict(name='unicode-ident', version='1.0.22', source='registry+https://github.com/rust-lang/crates.io-index',
                                      license='(MIT OR Apache-2.0) AND Unicode-3.0', authors=['A <a@example.invalid>'],
                                      texts=[dict(file='LICENSE-MIT', sha256=hashlib.sha256(mit.encode()).hexdigest())])],
                         texts={hashlib.sha256(apache.encode()).hexdigest(): apache, hashlib.sha256(mit.encode()).hexdigest(): mit})
        self.lock = f"  '{self.NAME}@{self.VERSION}':\n    resolution: {{integrity: {self.INTEGRITY}}}\n"
        self.saved = codec.INVENTORIES
        codec.INVENTORIES = {self.NAME: self.inventories}

    def tearDown(self):
        codec.INVENTORIES = self.saved
        self.directory.cleanup()

    def packages(self, lock=None):
        (self.inventories/f'{self.VERSION}.json').write_text(json.dumps(self.data))
        return codec.bundled_packages(self.bundle, self.modules, self.lock if lock is None else lock)

    def test_verified_inventory_supplies_publisher_license_and_crates(self):
        [package] = self.packages()
        self.assertEqual(package['license'], 'Apache-2.0')
        self.assertEqual(codec.licenses([package]), ['Apache-2.0', 'Unicode-3.0'])
        text = codec.license_text([package])
        self.assertIn('SPDX-License-Identifier headers of index.js', text)
        self.assertIn('unicode-ident 1.0.22 (registry+https://github.com/rust-lang/crates.io-index): (MIT OR Apache-2.0) AND Unicode-3.0', text)
        self.assertIn('authors A <a@example.invalid>', text)
        self.assertIn('MIT text', text)
        self.assertNotIn('No license file is shipped', text)

    def test_missing_inventory_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'declares no license'):
            codec.bundled_packages(self.bundle, self.modules, self.lock)

    def test_changed_cargo_lock_is_refused(self):
        (self.root/'Cargo.lock').write_text('# another lock\n')
        with self.assertRaisesRegex(ValueError, 'Cargo.lock differs'):
            self.packages()

    def test_other_locked_tarball_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'not locked to the tarball'):
            self.packages(self.lock.replace('fixture', 'other'))

    def test_changed_publisher_header_is_refused(self):
        (self.root/'index.mjs').write_text('// SPDX-License-Identifier: MIT\n')
        with self.assertRaisesRegex(ValueError, 'SPDX headers'):
            self.packages()

    def test_corrupt_text_is_refused(self):
        digest = next(iter(self.data['texts']))
        self.data['texts'][digest] = 'edited\n'
        with self.assertRaisesRegex(ValueError, 'corrupt'):
            self.packages()

    def test_crate_without_text_is_refused(self):
        self.data['crates'][1]['texts'] = []
        with self.assertRaisesRegex(ValueError, 'lacks license data'):
            self.packages()

    def test_inventory_for_a_now_declared_package_is_refused(self):
        (self.root/'package.json').write_text(json.dumps(dict(name=self.NAME, version=self.VERSION, license='Apache-2.0')))
        with self.assertRaisesRegex(ValueError, 'remove its license inventory'):
            self.packages()

    def test_elected_licenses_follow_spdx_precedence(self):
        self.assertEqual(codec.elected('MIT OR Apache-2.0'), {'Apache-2.0'})
        self.assertEqual(codec.elected('MIT/Apache-2.0'), {'Apache-2.0'})
        self.assertEqual(codec.elected('Unlicense OR MIT'), {'MIT'})
        self.assertEqual(codec.elected('(Apache-2.0 AND BSD-3-Clause)'), {'Apache-2.0', 'BSD-3-Clause'})
        self.assertEqual(codec.elected('MIT AND BSD-3-Clause'), {'MIT', 'BSD-3-Clause'})
        self.assertEqual(codec.elected('MIT OR Apache-2.0 AND Unicode-3.0'), {'MIT'})
        self.assertEqual(codec.elected('CC0-1.0'), {'CC0-1.0'})
        for invalid in ['MIT OR', '(MIT', 'MIT)', 'AND MIT', '']:
            with self.assertRaises(ValueError, msg=invalid):
                codec.elected(invalid)

    def test_committed_inventory_is_complete_and_matches_the_recipe(self):
        [path] = sorted((ROOT/'scripts/walrus_wasm_licenses').glob('*.json'))
        data = json.loads(path.read_text())
        self.assertEqual((data['package'], data['target'], data['package_license']), (self.NAME, 'wasm32-unknown-unknown', 'Apache-2.0'))
        lock = (ROOT/'dapp/pnpm-lock.yaml').read_text()
        self.assertIn(f"'{data['package']}@{data['version']}':\n    resolution: {{integrity: {data['npm_integrity']}}}", lock)
        recipe = set(var(CODEC, 'LICENSE').split(' & '))
        for digest, text in data['texts'].items():
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)
        for crate in data['crates']:
            self.assertTrue(crate['texts'] and all(t['sha256'] in data['texts'] for t in crate['texts']), crate['name'])
            self.assertLessEqual(codec.elected(crate['license']), recipe, crate['name'])
        self.assertNotIn('NO_GENERIC_LICENSE', CODEC)
        self.assertEqual(codec.recipe_license(CODEC), sorted(recipe))
        self.assertIn("' & '.join(provenance.get('licenses') or []) != d.getVar('LICENSE')", CODEC)


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


class TargetArchiveHarnessTests(unittest.TestCase):
    def test_dependency_control_sysroot_omits_only_libstdcxx(self):
        with tempfile.TemporaryDirectory() as d:
            rootfs, sysroot = Path(d)/'rootfs', Path(d)/'sysroot'
            for sub in ['lib', 'usr/lib']:
                (rootfs/sub/'suifleet').mkdir(parents=True)
                for name in ['ld-linux-aarch64.so.1', 'libc.so.6', 'libstdc++.so.6', 'libstdc++.so.6.0.32']:
                    (rootfs/sub/name).write_text(name)
            self.assertEqual(archive.minimal_sysroot(rootfs, sysroot), ['libstdc++.so.6', 'libstdc++.so.6.0.32'])
            self.assertEqual(sorted(p.name for p in (sysroot/'usr/lib').iterdir()), ['ld-linux-aarch64.so.1', 'libc.so.6'])
            self.assertEqual((sysroot/'lib/libc.so.6').resolve(), (rootfs/'lib/libc.so.6').resolve())
            with self.assertRaises(AssertionError):
                archive.minimal_sysroot(sysroot, Path(d)/'again')

    def test_launcher_is_valid_and_retargets_only_prepare_to_testnet(self):
        source = archive.LAUNCHER % json.dumps({})
        compile(source, 'launcher', 'exec')
        self.assertIn("if request['operation'] == 'prepare':\n    request.update(C['testnet'])", source)
        self.assertEqual(archive.TESTNET['chain'], smoke.TESTNET_CHAIN)


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

    # The codec bundle must come from the daemon's exact source revision.
    def test_codec_and_daemon_share_source_revision(self):
        self.assertEqual(var(CODEC, 'SUIFLEET_CODEC_SOURCE_REV'), var(DAEMON, 'SRCREV'))


if __name__ == '__main__':
    unittest.main()
