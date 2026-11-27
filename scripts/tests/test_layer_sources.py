"""Offline tests for the edge source distribution builder and layer source audit."""
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
import warnings
from unittest import mock
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packager = load('package_edge_source')
audit = load('check_layer_sources')

CARGO_TOML = '''[package]
name = "suifleet"
version = "0.2.3"

[[bin]]
name = "suifleet"
path = "src/main.rs"

[workspace]
members = ["windows-runtime"]
resolver = "2"
'''
CARGO_LOCK = '''version = 4

[[package]]
name = "anyhow"
version = "1.0.102"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "%s"

[[package]]
name = "suifleet"
version = "0.2.3"
''' % ('a' * 64)
LICENSE = 'MIT License fixture\n'
MAIN_RS = '''#[path = "extra.rs"]
mod extra;
const SCHEMA: &str = include_str!("../../interfaces/schema.json");
fn main() {}
'''


def md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def sha256(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


# SF-307's parse-time availability guards, verbatim from suifleet-node and
# suifleet-walrus-codec: they only read the datastore and skip the recipe.
NODE_GUARD = "python () {\n    if d.getVar('HOST_ARCH') != 'aarch64':\n        raise bb.parse.SkipRecipe('the official Node.js linux-arm64 binary is little-endian AArch64; %s is unsupported' % d.getVar('HOST_SYS'))\n    if d.getVar('TCLIBC') != 'glibc':\n        raise bb.parse.SkipRecipe('the official Node.js linux-arm64 binary requires glibc; TCLIBC=%s is unsupported' % d.getVar('TCLIBC'))\n}\n"
CODEC_GUARD = (
    'python () {\n'
    "    if d.getVar('HOST_ARCH') != 'aarch64':\n"
    "        raise bb.parse.SkipRecipe('the Walrus codec runs only on the Linux AArch64 glibc suifleet-node runtime; %s is unsupported' % d.getVar('HOST_SYS'))\n"
    "    if d.getVar('TCLIBC') != 'glibc':\n"
    "        raise bb.parse.SkipRecipe('the Walrus codec runs only on the glibc suifleet-node runtime; TCLIBC=%s is unsupported' % d.getVar('TCLIBC'))\n"
    '    import os\n'
    "    local = [entry for entry in (d.getVar('SRC_URI') or '').split() if entry.startswith('file://')]\n"
    '    # Resolve local inputs exactly as the local fetcher will: its URL decoding,\n'
    '    # absolute paths, FILESPATH search and (BitBake 2.0) the DL_DIR fallback.\n'
    '    # Record every candidate as a parse dependency, absent ones included\n'
    '    # (mtime 0). BitBake caches a skipped recipe with only these __depends, so\n'
    '    # without them a cached skip would survive after the files are staged.\n'
    '    missing = []\n'
    '    if local:\n'
    '        fetcher = bb.fetch2.Fetch(local, d, cache=False, localonly=True)\n'
    '        for entry in local:\n'
    '            ud = fetcher.ud[entry]\n'
    '            # Renamed from localpaths after BitBake 2.0.\n'
    "            search = getattr(ud.method, 'localfile_searchpaths', None) or ud.method.localpaths\n"
    '            searched = search(ud, d)\n'
    '            for path in searched:\n'
    '                bb.parse.mark_dependency(d, path)\n'
    '            if not searched or not os.path.exists(searched[-1]):\n'
    "                missing.append(entry[len('file://'):].split(';')[0])\n"
    '    if missing:\n'
    "        raise bb.parse.SkipRecipe('optional Walrus codec inputs not found: %s. Build them with scripts/package_walrus_codec.py at SUIFLEET_CODEC_SOURCE_REV (docs/YOCTO_PACKAGING.md) or leave suifleet-walrus-codec out of the image' % ', '.join(missing))\n"
    '}\n'
)


class SourceRepo:
    """A throwaway Git repository shaped like the core checkout."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Source fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.write('LICENSE', LICENSE)
        self.write('suifleet-edge/Cargo.toml', CARGO_TOML)
        self.write('suifleet-edge/Cargo.lock', CARGO_LOCK)
        self.write('suifleet-edge/src/main.rs', MAIN_RS)
        self.write('suifleet-edge/src/extra.rs', 'pub const X: &[u8] = include_bytes!("data/blob.bin");\n')
        self.write('suifleet-edge/src/data/blob.bin', 'blob')
        self.write('suifleet-edge/windows-runtime/Cargo.toml', '[package]\nname = "suifleet-windows"\nversion = "0.1.0"\n')
        self.write('suifleet-edge/windows-runtime/src/lib.rs', '')
        self.write('interfaces/schema.json', '{}')
        self.write('interfaces/unrelated.json', '{}')
        self.write('docs/README.md', 'not part of the build')
        self.write('suifleet-edge/meta-suifleet-integration/conf/layer.conf', 'board policy')
        self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args])

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD').decode().strip()


class PackageEdgeSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = SourceRepo(Path(self.tmp.name) / 'repo')
        self.out = Path(self.tmp.name)

    def test_git_archive_attributes_cannot_drop_or_rewrite_inputs(self):
        cases = {
            'omitted planned files (export-ignore?): interfaces/schema.json': 'interfaces/schema.json export-ignore\n',
            'bytes for suifleet-edge/src/main.rs differ': 'suifleet-edge/src/main.rs export-subst\n',
        }
        for label, attributes in cases.items():
            with self.subTest(label):
                self.repo.write('.gitattributes', attributes)
                if 'export-subst' in attributes:
                    self.repo.write('suifleet-edge/src/main.rs', MAIN_RS + '// $Format:%H$\n')
                self.repo.commit()
                output = self.out / 'attributes.tar.gz'
                with self.assertRaisesRegex(ValueError, label.replace('(', r'\(').replace(')', r'\)').replace('?', r'\?')):
                    packager.build(self.repo.root, 'HEAD', output)
                self.assertFalse(output.exists())
        self.repo.git('rm', '-q', '.gitattributes')
        self.repo.write('suifleet-edge/src/main.rs', MAIN_RS)
        self.repo.commit()
        packager.build(self.repo.root, 'HEAD', self.out / 'clean.tar.gz')

    def test_closure_refuses_checked_in_cargo_configuration(self):
        # The integration reviewer's reproduction: a checked-in
        # suifleet-edge/.cargo/config.toml sets a compile-time [env] variable
        # read by env!. The closure used to omit it, so the packaged layout
        # passed cargo metadata yet failed to compile. Cargo reads
        # .cargo/config(.toml) in the crate directory and every ancestor, so
        # the packager must refuse it instead of silently dropping it.
        self.repo.write('suifleet-edge/src/main.rs', MAIN_RS + 'const _V: &str = env!("SUIFLEET_FIXTURE");\n')
        refused = ('suifleet-edge/.cargo/config.toml', 'suifleet-edge/.cargo/config', '.cargo/config.toml',
                   '.cargo/config', 'suifleet-edge/windows-runtime/.cargo/config.toml')
        for rel in refused:
            with self.subTest(rel):
                self.repo.write(rel, '[env]\nSUIFLEET_FIXTURE = "1"\n')
                self.repo.commit()
                output = self.out / 'configured.tar.gz'
                with self.assertRaisesRegex(ValueError, 'unsupported checked-in Cargo configuration ' + re.escape(rel)):
                    packager.build(self.repo.root, 'HEAD', output)
                self.assertFalse(output.exists())
                # A published file list that omits it is refused too.
                with self.assertRaisesRegex(ValueError, 'unsupported checked-in Cargo configuration'):
                    packager.plan(self.repo.root, 'HEAD', ['LICENSE', 'suifleet-edge/Cargo.toml'])
                self.repo.git('rm', '-q', rel)
                self.repo.commit()
        # A relocated crate is checked along its own path; configuration off
        # the crate's path is not read by Cargo and stays outside the closure.
        self.repo.write('docs/.cargo/config.toml', '[env]\nSUIFLEET_FIXTURE = "1"\n')
        self.repo.commit()
        paths = packager.plan(self.repo.root, 'HEAD')['paths']
        self.assertFalse(any('.cargo' in p for p in paths), paths)
        self.assertEqual(packager.cargo_configs(['crates/edge/.cargo/config.toml', 'crates/.cargo/config',
                                                 'crates/other/.cargo/config', 'suifleet-edge/.cargo/config'],
                                                'crates/edge'),
                         ['crates/.cargo/config', 'crates/edge/.cargo/config.toml'])
        self.assertEqual(packager.cargo_config_candidates('crates/edge'),
                         ('.cargo/config', '.cargo/config.toml', 'crates/.cargo/config', 'crates/.cargo/config.toml',
                          'crates/edge/.cargo/config', 'crates/edge/.cargo/config.toml'))

    def test_closure_contains_build_inputs_only(self):
        info = packager.plan(self.repo.root, 'HEAD')
        self.assertEqual(info['paths'], [
            'LICENSE', 'interfaces/schema.json', 'suifleet-edge/Cargo.lock', 'suifleet-edge/Cargo.toml',
            'suifleet-edge/src/data/blob.bin', 'suifleet-edge/src/extra.rs', 'suifleet-edge/src/main.rs',
            'suifleet-edge/windows-runtime/Cargo.toml', 'suifleet-edge/windows-runtime/src/lib.rs'])
        self.assertEqual(info['crate_version'], '0.2.3')
        self.assertEqual(info['lock_sha256'], sha256(CARGO_LOCK))
        self.assertEqual(info['license_md5'], md5(LICENSE))

    def test_archive_is_reproducible_committed_and_exclusive(self):
        (self.repo.root / 'suifleet-edge/src/main.rs').write_text('uncommitted edit')
        (self.repo.root / 'suifleet-edge/src/device.key').write_text('untracked secret')
        first = packager.build(self.repo.root, 'HEAD', self.out / 'one.tar.gz')
        second = packager.build(self.repo.root, 'HEAD', self.out / 'two.tar.gz')
        self.assertEqual(first['sha256'], second['sha256'])
        self.assertEqual((self.out / 'one.tar.gz').read_bytes(), (self.out / 'two.tar.gz').read_bytes())
        with tarfile.open(self.out / 'one.tar.gz') as tar:
            names = [m.name for m in tar.getmembers() if m.isfile()]
            self.assertEqual(sorted(names), first['paths'])
            self.assertEqual(tar.extractfile('suifleet-edge/src/main.rs').read().decode(), MAIN_RS)
            self.assertTrue(all(m.uid == 0 and m.gid == 0 for m in tar.getmembers() if m.isfile()))
        self.assertEqual((self.out / 'one.tar.gz').read_bytes()[4:8], b'\0\0\0\0', 'gzip header time must be zero')
        with self.assertRaises(FileExistsError):
            packager.build(self.repo.root, 'HEAD', self.out / 'one.tar.gz')

    def test_ambient_tar_umask_cannot_change_the_archive(self):
        # tar.umask decides the archived permission bits; user, repository or
        # GIT_CONFIG_* settings must not change the published bytes.
        self.repo.write('suifleet-edge/build.sh', '#!/bin/sh\n')
        (self.repo.root / 'suifleet-edge/build.sh').chmod(0o755)
        self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML.replace(
            'version = "0.2.3"\n', 'version = "0.2.3"\nbuild = "build.sh"\n', 1))
        self.repo.commit()
        reference = packager.build(self.repo.root, 'HEAD', self.out / 'reference.tar.gz')
        self.assertIn('suifleet-edge/build.sh', reference['paths'])
        with tarfile.open(self.out / 'reference.tar.gz') as tar:
            modes = {m.name.rstrip('/'): m.mode for m in tar.getmembers()}
        self.assertEqual(modes['LICENSE'], 0o664)
        self.assertEqual(modes['suifleet-edge/build.sh'], 0o775)
        self.assertEqual(modes['suifleet-edge'], 0o775)
        ambient = {
            'repository config 0022': ({}, '0022'),
            'repository config 0000': ({}, '0000'),
            'repository config user with process umask 0077': ({}, 'user'),
            'GIT_CONFIG_COUNT 0022': ({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'tar.umask',
                                       'GIT_CONFIG_VALUE_0': '0022'}, None),
            'GIT_CONFIG_PARAMETERS 0077': ({'GIT_CONFIG_PARAMETERS': "'tar.umask'='0077'"}, None),
        }
        for index, (label, (env, config)) in enumerate(ambient.items()):
            with self.subTest(label):
                subprocess.run(['git', '-C', str(self.repo.root), 'config', '--unset-all', 'tar.umask'])
                if config is not None:
                    self.repo.git('config', 'tar.umask', config)
                previous = os.umask(0o077)
                try:
                    with mock.patch.dict(os.environ, env):
                        # The unpinned command really is affected by the setting.
                        raw = subprocess.check_output(['git', '-C', str(self.repo.root), 'archive', '--format=tar',
                                                       'HEAD', '--', 'LICENSE'])
                        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as tar:
                            self.assertNotEqual(tar.getmember('LICENSE').mode, 0o664)
                        result = packager.build(self.repo.root, 'HEAD', self.out / f'ambient-{index}.tar.gz')
                finally:
                    os.umask(previous)
                self.assertEqual(result['sha256'], reference['sha256'])

    def test_archive_with_unexpected_modes_is_refused(self):
        info = packager.plan(self.repo.root, 'HEAD')
        raw = subprocess.check_output(['git', '-C', str(self.repo.root), '-c', 'tar.umask=0022', 'archive',
                                       '--format=tar', info['source_commit'], '--', *info['paths']])
        with self.assertRaisesRegex(ValueError, r'mode 0755 for directory|mode 0644 for LICENSE'):
            packager.verify_archive(self.repo.root, info['source_commit'], info['paths'], raw)

    def test_missing_workspace_member_or_include_is_refused(self):
        shutil.rmtree(self.repo.root / 'suifleet-edge/windows-runtime')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, 'workspace member windows-runtime'):
            packager.plan(self.repo.root, 'HEAD')
        self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML.replace('members = ["windows-runtime"]', 'members = []'))
        self.repo.write('suifleet-edge/src/main.rs', 'const A: &str = include_str!("missing.txt");\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, 'missing file suifleet-edge/src/missing.txt'):
            packager.plan(self.repo.root, 'HEAD')

    def test_noncanonical_workspace_member_spellings_are_packaged(self):
        # Cargo accepts ./member/ for a literal member; the packager once looked
        # the raw string up in the git tree and refused a valid manifest.
        base = 'members = ["windows-runtime"]'
        for spelling in ('members = ["./windows-runtime/"]', 'members = ["windows-runtime/"]',
                         'members = ["./windows-runtime", "windows-runtime/./"]'):
            with self.subTest(spelling):
                self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML.replace(base, spelling))
                self.repo.commit()
                info = packager.build(self.repo.root, 'HEAD', self.out / 'member.tar.gz')
                data = (self.out / 'member.tar.gz').read_bytes()
                (self.out / 'member.tar.gz').unlink()
                for path in ('suifleet-edge/windows-runtime/Cargo.toml', 'suifleet-edge/windows-runtime/src/lib.rs'):
                    self.assertIn(path, info['paths'])
                self.assertFalse([p for p in info['paths'] if '/./' in p or p.endswith('/') or '//' in p], info['paths'])
                with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
                    names = tar.getnames()
                self.assertTrue(any(n.endswith('suifleet-edge/windows-runtime/Cargo.toml') for n in names), names)
                self.assertTrue(any(n.endswith('suifleet-edge/windows-runtime/src/lib.rs') for n in names), names)
        # Globs and paths leaving the crate stay refused after normalization.
        for spelling, error in (('members = ["./windows-*"]', 'unsupported workspace member pattern'),
                                ('members = ["./x/../../interfaces"]', 'unsupported workspace member pattern'),
                                ('members = ["/abs/member"]', 'unsupported workspace member pattern')):
            with self.subTest(spelling):
                self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML.replace(base, spelling))
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, error):
                    packager.plan(self.repo.root, 'HEAD')

    def test_local_path_dependencies_are_packaged_transitively(self):
        # Cargo builds local path dependencies whether or not workspace.members
        # lists them; an archive without them only fails on the clean host.
        deps = ('\n[dependencies]\nlocal-helper = { path = "local-helper" }\n'
                "\n[target.'cfg(unix)'.dev-dependencies]\nunix-helper = { path = './helpers/unix' }\n"
                '\n[patch.crates-io]\nanyhow = { path = "vendor/anyhow" }\n')
        self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML + deps)
        self.repo.write('suifleet-edge/local-helper/Cargo.toml',
                        '[package]\nname = "local-helper"\nversion = "0.1.0"\n\n'
                        '[build-dependencies.nested]\npath = "../nested"\n')
        self.repo.write('suifleet-edge/local-helper/src/lib.rs', '')
        for crate in ('nested', 'helpers/unix', 'vendor/anyhow'):
            self.repo.write(f'suifleet-edge/{crate}/Cargo.toml', '[package]\nname = "x"\nversion = "0.1.0"\n')
            self.repo.write(f'suifleet-edge/{crate}/src/lib.rs', '')
        self.repo.commit()
        info = packager.build(self.repo.root, 'HEAD', self.out / 'local.tar.gz')
        with tarfile.open(self.out / 'local.tar.gz') as tar:
            names = {m.name for m in tar.getmembers() if m.isfile()}
        for crate in ('local-helper', 'nested', 'helpers/unix', 'vendor/anyhow', 'windows-runtime'):
            with self.subTest(crate):
                self.assertIn(f'suifleet-edge/{crate}/Cargo.toml', names)
                self.assertIn(f'suifleet-edge/{crate}/src/lib.rs', names)
        self.assertEqual(sorted(names), info['paths'])
        # Unresolvable or unsupported local dependencies are refused, not dropped.
        for dependency, error in (('{ path = "absent" }', 'path dependency absent has no Cargo.toml'),
                                  ('{ path = "../interfaces" }', 'outside the suifleet-edge source layout'),
                                  ('{ path = "/abs/crate" }', 'unsupported local dependency path'),
                                  ('{ path = 1 }', 'unsupported path for dependency extra')):
            with self.subTest(dependency):
                self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML + deps + f'extra = {dependency}\n'.replace(
                    'extra', '[workspace.dependencies]\nextra', 1))
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, error):
                    packager.plan(self.repo.root, 'HEAD')

    def test_manifest_declared_target_files_are_packaged(self):
        # Cargo refuses a manifest whose explicit target file is absent, so a
        # declared path outside the fixed roots is a build input too.
        targets = ('\n[lib]\npath = "lib/core.rs"\n'
                   '\n[[bin]]\nname = "standalone-check"\npath = "tools/standalone.rs"\n'
                   '\n[[example]]\nname = "demo"\npath = "demos/demo.rs"\n'
                   '\n[[bench]]\nname = "speed"\npath = "perf/speed.rs"\n')
        manifest = CARGO_TOML.replace('[package]\n', '[package]\nbuild = "scripts/gen.rs"\n', 1) + targets
        self.assertIn('build = "scripts/gen.rs"', manifest)
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        self.repo.write('suifleet-edge/lib/core.rs', 'pub const T: &str = include_str!("table.txt");\n')
        self.repo.write('suifleet-edge/lib/table.txt', 'table')
        for rel in ('tools/standalone.rs', 'demos/demo.rs', 'perf/speed.rs', 'scripts/gen.rs'):
            self.repo.write(f'suifleet-edge/{rel}', 'fn main() {}\n')
        self.repo.write('suifleet-edge/windows-runtime/Cargo.toml',
                        '[package]\nname = "suifleet-windows"\nversion = "0.1.0"\n\n'
                        '[[test]]\nname = "shared"\npath = "../shared-tests/win.rs"\n')
        self.repo.write('suifleet-edge/shared-tests/win.rs', '')
        self.repo.write('suifleet-edge/tools/unrelated.txt', 'not declared')
        self.repo.commit()
        info = packager.build(self.repo.root, 'HEAD', self.out / 'targets.tar.gz')
        with tarfile.open(self.out / 'targets.tar.gz') as tar:
            names = {m.name for m in tar.getmembers() if m.isfile()}
        for rel in ('lib/core.rs', 'lib/table.txt', 'tools/standalone.rs', 'demos/demo.rs', 'perf/speed.rs',
                    'scripts/gen.rs', 'shared-tests/win.rs'):
            with self.subTest(rel):
                self.assertIn(f'suifleet-edge/{rel}', names)
        self.assertNotIn('suifleet-edge/tools/unrelated.txt', names)
        self.assertEqual(sorted(names), info['paths'])
        # Absent, escaping, absolute or unmodelled declarations are refused.
        for declaration, error in (('[[bin]]\nname = "x"\npath = "tools/absent.rs"\n',
                                    'declares build file suifleet-edge/tools/absent.rs, absent'),
                                   ('[[bin]]\nname = "x"\npath = "../interfaces/x.rs"\n',
                                    'Cargo target file ../interfaces/x.rs is outside'),
                                   ('[[bin]]\nname = "x"\npath = "/abs/x.rs"\n', 'unsupported Cargo target file path'),
                                   ('[[bin]]\nname = "x"\npath = 1\n', 'unsupported bin target path'),
                                   ('test = 1\n', r'unsupported \[\[test\]\] declaration')):
            with self.subTest(declaration):
                # A top-level key must precede the first table header.
                text = declaration + manifest if declaration == 'test = 1\n' else manifest + '\n' + declaration
                self.repo.write('suifleet-edge/Cargo.toml', text)
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, error):
                    packager.plan(self.repo.root, 'HEAD')
        self.repo.write('suifleet-edge/Cargo.toml',
                        CARGO_TOML.replace('[package]\n', '[package]\nbuild = ["a.rs"]\n', 1))
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, 'unsupported package.build value'):
            packager.plan(self.repo.root, 'HEAD')
        self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML.replace('[package]\n', '[package]\nbuild = false\n', 1))
        self.repo.commit()
        packager.plan(self.repo.root, 'HEAD')

    def test_name_only_test_and_bench_targets_use_cargo_default_paths(self):
        # The integration reviewer's reproduction: a [[test]] declared by name
        # only compiles Cargo's inferred tests/<name>.rs, which lies outside
        # the fixed roots and must be packaged (or the declaration refused).
        manifest = (CARGO_TOML + '\n[[test]]\nname = "integration"\n'
                    '\n[[bench]]\nname = "speed"\n\n[[test]]\nname = "explicit"\npath = "checks/explicit.rs"\n')
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        self.repo.write('suifleet-edge/tests/integration.rs', 'const D: &str = include_str!("data/case.txt");\n')
        self.repo.write('suifleet-edge/tests/data/case.txt', 'case')
        self.repo.write('suifleet-edge/tests/undeclared.rs', '#[test]\nfn t() {}\n')
        self.repo.write('suifleet-edge/benches/speed/main.rs', 'fn main() {}\n')
        self.repo.write('suifleet-edge/checks/explicit.rs', '')
        self.repo.commit()
        info = packager.build(self.repo.root, 'HEAD', self.out / 'defaults.tar.gz')
        with tarfile.open(self.out / 'defaults.tar.gz') as tar:
            names = {m.name for m in tar.getmembers() if m.isfile()}
        for rel in ('tests/integration.rs', 'tests/data/case.txt', 'benches/speed/main.rs', 'checks/explicit.rs'):
            with self.subTest(rel):
                self.assertIn(f'suifleet-edge/{rel}', names)
        # Autodiscovered, undeclared tests are not build inputs of a build.
        self.assertNotIn('suifleet-edge/tests/undeclared.rs', names)
        self.assertEqual(sorted(names), info['paths'])
        # Unresolvable declarations are refused rather than silently dropped.
        cases = (
            ('[[test]]\nname = "absent"\n', {}, 'declares build file suifleet-edge/tests/absent.rs, absent'),
            ('[[bench]]\nname = "twice"\n', {'benches/twice.rs': '', 'benches/twice/main.rs': ''},
             'bench target twice matches both benches/twice.rs and benches/twice/main.rs'),
            ('[[test]]\nharness = false\n', {}, 'unsupported test target without a path'),
            ('[[test]]\nname = "../escape"\n', {}, 'unsupported test target without a path'),
            ('[[test]]\nname = "modular"\n', {'tests/modular/main.rs': 'mod common;\nfn main() {}\n',
                                               'tests/modular/common.rs': ''},
             r'tests/modular/main.rs declares module\(s\) common without #\[path\]'),
        )
        for declaration, extra, error in cases:
            with self.subTest(declaration):
                self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML + '\n' + declaration)
                for rel, text in extra.items():
                    self.repo.write(f'suifleet-edge/{rel}', text)
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, error):
                    packager.plan(self.repo.root, 'HEAD')
                for rel in extra:
                    self.repo.git('rm', '-q', f'suifleet-edge/{rel}')
        # Local crates are packaged whole, so their defaults resolve there.
        self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML)
        self.repo.write('suifleet-edge/windows-runtime/Cargo.toml',
                        '[package]\nname = "suifleet-windows"\nversion = "0.1.0"\n\n[[test]]\nname = "win"\n')
        self.repo.write('suifleet-edge/windows-runtime/tests/win.rs', '')
        self.repo.commit()
        self.assertIn('suifleet-edge/windows-runtime/tests/win.rs', packager.plan(self.repo.root, 'HEAD')['paths'])

    def test_fresh_unpack_builds_name_only_test_target(self):
        # End to end with the real Cargo: the archive of a crate with a
        # name-only [[test]] must build and test after extraction, exactly
        # like its checkout. Cargo refuses the whole manifest otherwise.
        cargo = os.environ.get('SUIFLEET_TEST_CARGO') or shutil.which('cargo')
        if not cargo or subprocess.run([cargo, '--version'], capture_output=True).returncode:
            self.skipTest('no working cargo')
        root = self.out / 'cargo-fixture'
        repo = SourceRepo.__new__(SourceRepo)
        repo.root = root
        root.mkdir()
        repo.git('init', '-q')
        repo.git('config', 'user.name', 'Source fixture')
        repo.git('config', 'user.email', 'fixture@example.invalid')
        repo.write('LICENSE', LICENSE)
        repo.write('suifleet-edge/Cargo.toml', '[package]\nname = "fixture"\nversion = "0.1.0"\nedition = "2021"\n'
                   '\n[[test]]\nname = "integration"\n')
        repo.write('suifleet-edge/src/main.rs', 'fn main() {}\n')
        repo.write('suifleet-edge/tests/integration.rs', '#[test]\nfn passes() {}\n')
        env = {**os.environ, 'CARGO_TARGET_DIR': str(self.out / 'cargo-target'), 'CARGO_NET_OFFLINE': 'true'}
        subprocess.run([cargo, 'generate-lockfile', '--offline'], cwd=root / 'suifleet-edge', env=env,
                       check=True, capture_output=True)
        rev = repo.commit()
        archive = self.out / 'fixture.tar.gz'
        packager.build(root, rev, archive)
        unpacked = self.out / 'unpacked'
        unpacked.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(unpacked, filter='data')
        for command in (['build', '--offline', '--locked', '--bin', 'fixture'],
                        ['test', '--offline', '--locked', '--test', 'integration']):
            with self.subTest(command[0]):
                result = subprocess.run([cargo, *command], cwd=unpacked / 'suifleet-edge', env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_implicit_modules_in_out_of_root_targets_are_refused(self):
        # The reviewer's reproduction: a [[bin]] outside src/ whose `mod helper;`
        # resolves to a sibling file compiles in the repository, but only the
        # entry point would be packaged.
        manifest = CARGO_TOML + '\n[[bin]]\nname = "standalone-check"\npath = "tools/standalone.rs"\n'
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        for source in ('mod helper;\nfn main() { helper::f() }\n',
                       '#[cfg(unix)]\npub(crate) mod helper ;\nfn main() {}\n',
                       '#[cfg_attr(unix, path = "helper.rs")]\nmod helper;\nfn main() {}\n'):
            with self.subTest(source):
                self.repo.write('suifleet-edge/tools/standalone.rs', source)
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, r'tools/standalone.rs declares module\(s\) helper without #\[path\]'):
                    packager.plan(self.repo.root, 'HEAD')
        # A #[path] file is followed and checked transitively, as is build.rs.
        self.repo.write('suifleet-edge/tools/standalone.rs', '#[path = "helper.rs"]\nmod helper;\nfn main() {}\n')
        self.repo.write('suifleet-edge/tools/helper.rs', 'mod nested;\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, r'tools/helper.rs declares module\(s\) nested'):
            packager.plan(self.repo.root, 'HEAD')
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        self.repo.write('suifleet-edge/build.rs', 'mod gen;\nfn main() {}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, r'suifleet-edge/build.rs declares module\(s\) gen'):
            packager.plan(self.repo.root, 'HEAD')
        # Explicit #[path] modules, inline modules and modules under src/ are packaged.
        self.repo.write('suifleet-edge/build.rs', 'mod inline { pub fn f() {} }\nfn main() {}\n')
        self.repo.write('suifleet-edge/src/main.rs', 'mod sub;\nfn main() {}\n')
        self.repo.write('suifleet-edge/src/sub.rs', '')
        self.repo.commit()
        paths = packager.plan(self.repo.root, 'HEAD')['paths']
        for rel in ('tools/standalone.rs', 'tools/helper.rs', 'build.rs', 'src/sub.rs'):
            self.assertIn(f'suifleet-edge/{rel}', paths)

    def test_implicit_modules_in_relocated_crate_targets_are_refused(self):
        # The source reviewer's reproduction: the same out-of-src [[bin]] with an
        # implicit sibling module, after the whole crate moves to crates/edge.
        # The packager must check loose sources under the effective crate
        # directory, not silently skip them because they are not under
        # suifleet-edge/.
        manifest = CARGO_TOML + '\n[[bin]]\nname = "standalone-check"\npath = "tools/standalone.rs"\n'
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        self.repo.write('suifleet-edge/tools/standalone.rs', 'mod helper;\nfn main() { helper::f() }\n')
        (self.repo.root / 'crates').mkdir()
        self.repo.git('mv', 'suifleet-edge', 'crates/edge')
        self.repo.write('crates/edge/src/main.rs', MAIN_RS.replace('../../interfaces', '../../../interfaces'))
        self.repo.commit()
        output = Path(self.tmp.name) / 'relocated.tar.gz'
        with self.assertRaisesRegex(ValueError, r'crates/edge/tools/standalone.rs declares module\(s\) helper without #\[path\]'):
            packager.plan(self.repo.root, 'HEAD', crate='crates/edge')
        with self.assertRaisesRegex(ValueError, r'tools/standalone.rs declares module\(s\) helper'):
            packager.build(self.repo.root, 'HEAD', output, crate='crates/edge')
        self.assertFalse(output.exists())
        # With an explicit #[path] the relocated helper is packaged.
        self.repo.write('crates/edge/tools/standalone.rs', '#[path = "helper.rs"]\nmod helper;\nfn main() {}\n')
        self.repo.commit()
        paths = packager.plan(self.repo.root, 'HEAD', crate='crates/edge')['paths']
        for rel in ('tools/standalone.rs', 'tools/helper.rs'):
            self.assertIn(f'crates/edge/{rel}', paths)

    def test_implicit_modules_reached_from_packaged_directories_are_refused(self):
        # The reviewer's cross-root reproduction: src/main.rs loads a file
        # outside the packaged directories through #[path], and that file's
        # `mod helper;` would resolve to an unpackaged sibling.
        self.repo.write('suifleet-edge/src/main.rs', '#[path = "../tools/entry.rs"]\nmod entry;\nfn main() {}\n')
        self.repo.write('suifleet-edge/tools/entry.rs', 'mod helper;\n')
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, r'tools/entry.rs declares module\(s\) helper without #\[path\]'):
            packager.plan(self.repo.root, 'HEAD')
        # Also through an include from examples/, and transitively.
        self.repo.write('suifleet-edge/src/main.rs', 'fn main() {}\n')
        self.repo.write('suifleet-edge/examples/demo.rs', 'include!("../tools/entry.rs");\nfn main() {}\n')
        self.repo.write('suifleet-edge/tools/entry.rs', '#[path = "helper.rs"]\nmod helper;\n')
        self.repo.write('suifleet-edge/tools/helper.rs', 'mod nested;\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, r'tools/helper.rs declares module\(s\) nested'):
            packager.plan(self.repo.root, 'HEAD')
        # With explicit paths the whole closure is packaged.
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        self.repo.commit()
        paths = packager.plan(self.repo.root, 'HEAD')['paths']
        for rel in ('examples/demo.rs', 'tools/entry.rs', 'tools/helper.rs'):
            self.assertIn(f'suifleet-edge/{rel}', paths)

    def test_every_literal_include_and_path_form_is_packaged(self):
        # The reviewer's reproduction used `include! { .. }`, which the old
        # parenthesis-only scanner skipped. Every delimiter, spacing, raw
        # string and cfg_attr path must reach the archive.
        self.repo.write('suifleet-edge/tools/entry.rs', 'pub fn f() {}\n')
        self.repo.write('suifleet-edge/tools/data.json', '{}\n')
        self.repo.write('suifleet-edge/tools/unix.rs', 'pub fn u() {}\n')
        forms = ('include! { "../tools/entry.rs" }\nfn main() {}\n',
                 'include ! [ "../tools/entry.rs", ];\nfn main() {}\n',
                 'std::include!(r#"../tools/entry.rs"#);\nfn main() {}\n',
                 'const D: &str = core::include_str! {\n    "../tools/data.json"\n};\nfn main() {}\n',
                 'const D: &[u8] = include_bytes![r"../tools/data.json"];\nfn main() {}\n',
                 '# [ path = "../tools/entry.rs" ]\nmod entry;\nfn main() {}\n',
                 '#[cfg_attr(unix, path = "../tools/unix.rs")]\nmod entry;\nfn main() {}\n')
        expected = ('entry.rs', 'entry.rs', 'entry.rs', 'data.json', 'data.json', 'entry.rs', 'unix.rs')
        for index, (source, name) in enumerate(zip(forms, expected)):
            with self.subTest(source):
                self.repo.write('suifleet-edge/src/main.rs', source)
                rev = self.repo.commit()
                output = self.out / f'form-{index}.tar.gz'
                info = packager.build(self.repo.root, rev, output)
                self.assertIn(f'suifleet-edge/tools/{name}', info['paths'])
                with tarfile.open(output) as tar:
                    self.assertIn(f'suifleet-edge/tools/{name}', tar.getnames())
        # Forms the scanner cannot resolve are refused, not skipped.
        for source in ('include!(concat!(env!("OUT_DIR"), "/gen.rs"));\n',
                       'include_str!("..\\\\tools\\\\entry.rs");\n',
                       'include!(PATH);\n',
                       '#[path = concat!("../tools/", "entry.rs")]\nmod entry;\n',
                       'include_str!("");\n'):
            with self.subTest(source):
                self.repo.write('suifleet-edge/src/main.rs', source + 'fn main() {}\n')
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, 'unsupported include form|unsupported #\\[path\\]|empty path'):
                    packager.plan(self.repo.root, 'HEAD')
        # A braced include of a missing file is an error like any other.
        self.repo.write('suifleet-edge/src/main.rs', 'include! { "../tools/absent.rs" }\nfn main() {}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, 'includes missing file suifleet-edge/tools/absent.rs'):
            packager.plan(self.repo.root, 'HEAD')

    def test_raw_identifier_attribute_names_are_packaged(self):
        # Review 5202: rustc accepts built-in attribute names as raw
        # identifiers (`#[r#path = ..]`, `#[r#cfg_attr(.., r#path = ..)]`), so a
        # module outside src/ named that way must reach the archive.
        self.repo.write('suifleet-edge/extra/helper.inc', 'pub fn f() {}\n')
        self.repo.write('suifleet-edge/extra/unix.rs', 'pub fn u() {}\n')
        self.repo.write('suifleet-edge/extra/data.txt', 'x\n')
        forms = (('#[r#path = "../extra/helper.inc"]\nmod helper;\nfn main() { helper::f() }\n', 'helper.inc'),
                 ('# [ r#path = "../extra/helper.inc" ]\nmod helper;\nfn main() {}\n', 'helper.inc'),
                 ('#[r#cfg_attr(unix, r#path = "../extra/unix.rs")]\nmod u;\nfn main() {}\n', 'unix.rs'),
                 ('#[cfg_attr(unix, r#path = "../extra/unix.rs")]\nmod u;\nfn main() {}\n', 'unix.rs'),
                 ('const D: &str = r#include_str!("../extra/data.txt");\nfn main() {}\n', 'data.txt'))
        for index, (source, name) in enumerate(forms):
            with self.subTest(source):
                self.repo.write('suifleet-edge/src/main.rs', source)
                rev = self.repo.commit()
                output = self.out / f'raw-{index}.tar.gz'
                info = packager.build(self.repo.root, rev, output)
                self.assertIn(f'suifleet-edge/extra/{name}', info['paths'])
                with tarfile.open(output) as tar:
                    self.assertIn(f'suifleet-edge/extra/{name}', tar.getnames())
        # A raw path module is not mistaken for an implicit `mod name;` file.
        self.assertEqual(packager.implicit_modules(b'#[r#path = "../extra/helper.inc"]\nmod helper;\n'), [])
        # A raw path that is not one string literal is refused, not skipped.
        self.repo.write('suifleet-edge/src/main.rs', '#[r#path = concat!("../extra/", "helper.inc")]\nmod helper;\n'
                        'fn main() {}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, 'unsupported #\\[path\\]'):
            packager.plan(self.repo.root, 'HEAD')
        # And a raw path to a missing file is an error like any other.
        self.repo.write('suifleet-edge/src/main.rs', '#[r#path = "../extra/absent.inc"]\nmod helper;\nfn main() {}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, 'missing file suifleet-edge/extra/absent.inc'):
            packager.plan(self.repo.root, 'HEAD')

    def test_comments_between_include_and_path_tokens_are_seen(self):
        # The reviewer's reproduction: rustc accepts a comment wherever
        # whitespace may appear, so `include_str /* .. */ !(..)` and
        # `#[/* .. */ path = ..]` compile, and their targets must be packaged.
        self.repo.write('interfaces/message.txt', 'hello\n')
        self.repo.write('suifleet-edge/tools/child.rs', 'pub fn run() {}\n')
        self.repo.write('suifleet-edge/tools/unix.rs', 'pub fn run() {}\n')
        forms = (('const M: &str = include_str /* embedded at build time */ !("../../interfaces/message.txt");\n'
                  'fn main() {}\n', 'interfaces/message.txt'),
                 ('const M: &str = include_str! /* a */ ( /* b */ "../../interfaces/message.txt" /* c */ );\n'
                  'fn main() {}\n', 'interfaces/message.txt'),
                 ('const M: &str = include_str!(// line comment\n"../../interfaces/message.txt");\nfn main() {}\n',
                  'interfaces/message.txt'),
                 ('#[/* reason */ path = "../tools/child.rs"]\nmod child;\nfn main() { child::run() }\n',
                  'suifleet-edge/tools/child.rs'),
                 ('# /* a */ [path /* b */ = /* c */ "../tools/child.rs" /* d */]\nmod child;\nfn main() {}\n',
                  'suifleet-edge/tools/child.rs'),
                 ('#[cfg_attr(/* a */ unix, /* b */ path = "../tools/unix.rs")]\nmod child;\nfn main() {}\n',
                  'suifleet-edge/tools/unix.rs'))
        for index, (source, name) in enumerate(forms):
            with self.subTest(source):
                self.repo.write('suifleet-edge/src/main.rs', source)
                rev = self.repo.commit()
                output = self.out / f'comment-form-{index}.tar.gz'
                info = packager.build(self.repo.root, rev, output)
                self.assertIn(name, info['paths'])
                with tarfile.open(output) as tar:
                    self.assertIn(name, tar.getnames())
        # A comment-looking sequence inside a string is not a comment.
        self.repo.write('suifleet-edge/src/main.rs', 'const S: &str = "/* not a comment */";\n'
                        'const M: &str = include_str!("../../interfaces/message.txt");\nfn main() {}\n')
        self.repo.commit()
        self.assertIn('interfaces/message.txt', packager.plan(self.repo.root, 'HEAD')['paths'])
        # An unresolvable form behind a comment is still refused, and so is a
        # renamed include macro whose calls the scanner could not see.
        for source in ('include /* a */ !(concat!("../tools/", "child.rs"));\n',
                       '#[/* a */ path = concat!("../tools/", "child.rs")]\nmod child;\n',
                       'use std::include_str as embed;\nconst M: &str = embed!("../../interfaces/message.txt");\n',
                       'use std::{include_bytes /* a */ as embed};\n'):
            with self.subTest(source):
                self.repo.write('suifleet-edge/src/main.rs', source + 'fn main() {}\n')
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, r'unsupported include form|unsupported #\[path\]|renaming an include macro'):
                    packager.plan(self.repo.root, 'HEAD')
        # Outside the packaged directories a commented `mod` is still an
        # implicit module, and a commented #[path] still counts as explicit.
        self.repo.write('suifleet-edge/src/main.rs', 'fn main() {}\n')
        manifest = CARGO_TOML + '\n[[bin]]\nname = "standalone-check"\npath = "tools/standalone.rs"\n'
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        self.repo.write('suifleet-edge/tools/standalone.rs', 'mod /* why */ helper;\nfn main() {}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, r'tools/standalone.rs declares module\(s\) helper without #\[path\]'):
            packager.plan(self.repo.root, 'HEAD')
        self.repo.write('suifleet-edge/tools/standalone.rs', '#[/* why */ path = "helper.rs"]\nmod helper;\nfn main() {}\n')
        self.repo.commit()
        self.assertIn('suifleet-edge/tools/helper.rs', packager.plan(self.repo.root, 'HEAD')['paths'])

    def test_implicit_modules_of_targets_outside_the_crate_are_refused(self):
        # The reviewer's reproduction: a #[path] target outside suifleet-edge/
        # that declares `mod child;` compiles in the repository, because rustc
        # finds interfaces/child.rs beside it, but the closure only packages
        # literal targets, so the archive would silently omit the child.
        self.repo.write('suifleet-edge/src/main.rs', '#[path = "../../interfaces/helper.rs"]\nmod helper;\n'
                        'fn main() { helper::child::run() }\n')
        self.repo.write('interfaces/helper.rs', 'pub mod child;\n')
        self.repo.write('interfaces/child.rs', 'pub fn run() {}\n')
        self.repo.commit()
        output = self.out / 'outside-implicit.tar.gz'
        with self.assertRaisesRegex(ValueError, r'interfaces/helper.rs declares module\(s\) child without #\[path\]'):
            packager.build(self.repo.root, 'HEAD', output)
        self.assertFalse(output.exists())
        # Reached through an include! of an .rs file from a declared target
        # outside src/, the same declaration is refused too.
        self.repo.write('suifleet-edge/src/main.rs', 'fn main() {}\n')
        self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML + '\n[[bin]]\nname = "standalone-check"\n'
                        'path = "tools/standalone.rs"\n')
        self.repo.write('suifleet-edge/tools/standalone.rs', 'include!("../../interfaces/helper.rs");\nfn main() {}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, r'interfaces/helper.rs declares module\(s\) child without #\[path\]'):
            packager.plan(self.repo.root, 'HEAD')
        # With an explicit #[path] the child is a literal target and packaged.
        self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML)
        self.repo.git('rm', '-q', 'suifleet-edge/tools/standalone.rs')
        self.repo.write('suifleet-edge/src/main.rs', '#[path = "../../interfaces/helper.rs"]\nmod helper;\n'
                        'fn main() { helper::child::run() }\n')
        self.repo.write('interfaces/helper.rs', '#[path = "child.rs"]\npub mod child;\n')
        rev = self.repo.commit()
        info = packager.build(self.repo.root, rev, output)
        self.assertIn('interfaces/helper.rs', info['paths'])
        self.assertIn('interfaces/child.rs', info['paths'])
        with tarfile.open(output) as tar:
            self.assertIn('interfaces/child.rs', tar.getnames())

    def test_paths_that_cancel_a_directory_are_refused(self):
        # The reviewer's reproduction: rustc traverses tools/anchor in
        # ../tools/anchor/../helper.rs, so the path compiles in the repository
        # only because that directory exists; the closure packages just
        # tools/helper.rs, and the extracted archive fails to compile.
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        self.repo.write('suifleet-edge/tools/anchor/README', 'anchor\n')
        for source in ('#[path = "../tools/anchor/../helper.rs"]\nmod helper;\nfn main() { helper::f() }\n',
                       'include!("../tools/anchor/../helper.rs");\nfn main() { f() }\n',
                       'const M: &str = include_str!("./../tools/anchor/./../helper.rs");\nfn main() {}\n'):
            with self.subTest(source):
                self.repo.write('suifleet-edge/src/main.rs', source)
                rev = self.repo.commit()
                output = self.out / 'cancelled-directory.tar.gz'
                with self.assertRaisesRegex(ValueError, r'src/main.rs references a path that cancels a directory'):
                    packager.build(self.repo.root, rev, output)
                self.assertFalse(output.exists())
        # Leading parent references, also after `.`, keep working.
        self.repo.write('suifleet-edge/src/main.rs', '#[path = "./../tools/helper.rs"]\nmod helper;\n'
                        'fn main() { helper::f() }\n')
        rev = self.repo.commit()
        info = packager.build(self.repo.root, rev, self.out / 'leading-parent.tar.gz')
        self.assertIn('suifleet-edge/tools/helper.rs', info['paths'])
        self.assertNotIn('suifleet-edge/tools/anchor/README', info['paths'])

    def test_directory_only_terminal_syntax_is_refused(self):
        # Review 5204: a trailing `/` or a final `.`/`..` component names a
        # directory, so rustc fails with "Not a directory" even though the
        # normalized path is a regular file that the closure would package.
        self.repo.write('suifleet-edge/extra/helper.inc', 'pub fn f() {}\n')
        for source in ('#[r#path = "../extra/helper.inc/"]\nmod helper;\nfn main() { helper::f() }\n',
                       '#[path = "../extra/helper.inc/."]\nmod helper;\nfn main() {}\n',
                       '#[cfg_attr(unix, path = "../extra/helper.inc//")]\nmod helper;\nfn main() {}\n',
                       'include!("../extra/helper.inc/");\nfn main() {}\n',
                       'const D: &str = include_str!("../extra/helper.inc/.");\nfn main() {}\n',
                       'const D: &[u8] = include_bytes!("../extra/..");\nfn main() {}\n',
                       'const D: &str = include_str!(".");\nfn main() {}\n'):
            with self.subTest(source):
                self.repo.write('suifleet-edge/src/main.rs', source)
                rev = self.repo.commit()
                output = self.out / 'directory-syntax.tar.gz'
                with self.assertRaisesRegex(ValueError, r'src/main.rs references a directory, not a file'):
                    packager.build(self.repo.root, rev, output)
                self.assertFalse(output.exists())
        # Leading `./` and `../`, and `.` inside a file name, keep working.
        self.repo.write('suifleet-edge/extra/v1.0.inc', 'pub fn g() {}\n')
        self.repo.write('suifleet-edge/src/main.rs', '#[path = "./../extra/helper.inc"]\nmod helper;\n'
                        'include!("../extra/v1.0.inc");\nfn main() { helper::f(); g() }\n')
        rev = self.repo.commit()
        info = packager.build(self.repo.root, rev, self.out / 'file-syntax.tar.gz')
        self.assertIn('suifleet-edge/extra/helper.inc', info['paths'])
        self.assertIn('suifleet-edge/extra/v1.0.inc', info['paths'])

    def test_code_includes_are_followed_whatever_their_suffix(self):
        # The reviewer's reproduction: include! compiles its target as Rust
        # even when it is not named .rs, so that file's own includes are
        # build inputs too. #[path] targets are code in the same way.
        self.repo.write('suifleet-edge/src/main.rs', 'include!("../tools/entry.inc");\nfn main() { f() }\n')
        self.repo.write('suifleet-edge/tools/entry.inc', 'include!("helper.rs");\n')
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        rev = self.repo.commit()
        output = self.out / 'code-include.tar.gz'
        info = packager.build(self.repo.root, rev, output)
        with tarfile.open(output) as tar:
            names = tar.getnames()
        for rel in ('tools/entry.inc', 'tools/helper.rs'):
            self.assertIn(f'suifleet-edge/{rel}', info['paths'])
            self.assertIn(f'suifleet-edge/{rel}', names)
        self.repo.write('suifleet-edge/src/main.rs', '#[path = "../tools/module.txt"]\nmod entry;\nfn main() {}\n')
        self.repo.write('suifleet-edge/tools/module.txt', 'include!("deep.inc");\n')
        self.repo.write('suifleet-edge/tools/deep.inc', 'pub fn d() {}\n')
        rev = self.repo.commit()
        paths = packager.plan(self.repo.root, rev)['paths']
        for rel in ('tools/module.txt', 'tools/deep.inc'):
            self.assertIn(f'suifleet-edge/{rel}', paths)
        # A missing include inside a non-.rs code file is an error.
        self.repo.write('suifleet-edge/tools/deep.inc', 'include!("absent.rs");\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, 'deep.inc includes missing file suifleet-edge/tools/absent.rs'):
            packager.plan(self.repo.root, 'HEAD')
        # Outside src/ the same chain is followed and checked for implicit modules.
        self.repo.write('suifleet-edge/src/main.rs', 'fn main() {}\n')
        self.repo.write('suifleet-edge/examples/demo.rs', 'include!("../tools/entry.inc");\nfn main() {}\n')
        self.repo.write('suifleet-edge/tools/entry.inc', 'include!("helper.inc");\n')
        self.repo.write('suifleet-edge/tools/helper.inc', 'mod nested;\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, r'tools/helper.inc declares module\(s\) nested'):
            packager.plan(self.repo.root, 'HEAD')
        # include_str!/include_bytes! targets are data: their text is not scanned.
        self.repo.write('suifleet-edge/examples/demo.rs', 'fn main() {}\n')
        self.repo.write('suifleet-edge/src/main.rs', 'const T: &str = include_str!("../tools/template.txt");\nfn main() {}\n')
        self.repo.write('suifleet-edge/tools/template.txt', 'include!(concat!("not", "code"));\n')
        rev = self.repo.commit()
        self.assertIn('suifleet-edge/tools/template.txt', packager.plan(self.repo.root, rev)['paths'])

    def test_declared_targets_are_scanned_whatever_their_suffix(self):
        # The reviewer's reproduction: Cargo hands a manifest-declared target
        # to rustc whatever its file name, so its includes are build inputs.
        manifest = CARGO_TOML + '\n[[bin]]\nname = "standalone-check"\npath = "tools/entry.inc"\n'
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        self.repo.write('suifleet-edge/tools/entry.inc',
                        'const M: &str = include_str!("message.txt");\ninclude!("helper.inc");\nfn main() { f() }\n')
        self.repo.write('suifleet-edge/tools/message.txt', 'hello\n')
        self.repo.write('suifleet-edge/tools/helper.inc', 'fn f() {}\n')
        rev = self.repo.commit()
        output = self.out / 'declared-entry.tar.gz'
        info = packager.build(self.repo.root, rev, output)
        with tarfile.open(output) as tar:
            names = tar.getnames()
        for rel in ('tools/entry.inc', 'tools/message.txt', 'tools/helper.inc'):
            self.assertIn(f'suifleet-edge/{rel}', info['paths'])
            self.assertIn(f'suifleet-edge/{rel}', names)
        # A missing include or an implicit module in such an entry point is refused.
        self.repo.write('suifleet-edge/tools/entry.inc', 'include_bytes!("absent.bin");\nfn main() {}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, 'entry.inc includes missing file suifleet-edge/tools/absent.bin'):
            packager.plan(self.repo.root, 'HEAD')
        self.repo.write('suifleet-edge/tools/entry.inc', 'mod helper;\nfn main() {}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, r'tools/entry.inc declares module\(s\) helper without #\[path\]'):
            packager.plan(self.repo.root, 'HEAD')
        # A non-.rs build script is a compiler entry point too.
        self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML.replace('[package]\n', '[package]\nbuild = "tools/build.inc"\n', 1))
        self.repo.write('suifleet-edge/tools/build.inc', 'const D: &[u8] = include_bytes!("data.bin");\nfn main() {}\n')
        self.repo.write('suifleet-edge/tools/data.bin', 'x')
        rev = self.repo.commit()
        self.assertIn('suifleet-edge/tools/data.bin', packager.plan(self.repo.root, rev)['paths'])

    def test_path_attributes_in_inline_modules_are_refused(self):
        # The reviewer's reproduction: rustc resolves this #[path] against
        # tools/nested/, so archiving tools/child.rs instead built a tree that
        # compiles in the repository and fails after extraction.
        manifest = CARGO_TOML + '\n[[bin]]\nname = "nested-target"\npath = "tools/entry.rs"\n'
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        self.repo.write('suifleet-edge/tools/entry.rs',
                        'mod nested {\n    #[path = "child.rs"]\n    mod child;\n    pub fn run() { child::run() }\n}\n'
                        'fn main() { nested::run() }\n')
        self.repo.write('suifleet-edge/tools/child.rs', 'pub fn run() {}\n')
        self.repo.write('suifleet-edge/tools/nested/child.rs', 'pub fn run() {}\n')
        self.repo.commit()
        unsupported = r'tools/entry.rs: #\[path\] attribute .child.rs. inside or on an inline'
        with self.assertRaisesRegex(ValueError, unsupported):
            packager.plan(self.repo.root, 'HEAD')
        # The same with a Unicode module identifier: rustc needs tools/café/child.rs
        # (verified with rustc 1.94.0), which an ASCII-only matcher missed.
        self.repo.write('suifleet-edge/tools/entry.rs',
                        'mod café {\n    #[path = "child.rs"]\n    mod child;\n    pub fn run() { child::run() }\n}\n'
                        'fn main() { café::run() }\n')
        self.repo.write('suifleet-edge/tools/café/child.rs', 'pub fn run() {}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, unsupported):
            packager.plan(self.repo.root, 'HEAD')
        # Rust's non-ASCII whitespace (here U+2028 and U+0085) would hide a block
        # or attribute from the byte patterns, so it is refused outright.
        for source in ('mod\u2028nested { #[path = "child.rs"] mod child; }\nfn main() {}\n',
                       '#\u0085[path = "child.rs"]\nmod child;\nfn main() {}\n'):
            with self.subTest(source):
                self.repo.write('suifleet-edge/tools/entry.rs', source)
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, r'tools/entry.rs:1: Unicode whitespace U\+(2028|0085)'):
                    packager.plan(self.repo.root, 'HEAD')
        # Also refused: cfg_attr inside a block, a nested block, braces in
        # literals and comments that must not close the block early, a #[path]
        # on an inline module, and inside src/ (followed for its references).
        for source in ('mod nested { #[cfg_attr(unix, path = "child.rs")] mod child; }\nfn main() {}\n',
                       'mod a { fn f() {} mod b { #[path = "child.rs"] mod child; } }\nfn main() {}\n',
                       "mod a {\n const C: char = '}'; const S: &str = \"}}\"; const R: &str = r#\"}\"#;\n"
                       " // }\n /* } /* } */ } */ fn g<'x>(v: &'x u8) -> &'x u8 { v }\n"
                       ' #[path = "child.rs"] mod child;\n}\nfn main() {}\n',
                       '#[path = "nested"]\n#[allow(dead_code)]\npub(crate) mod renamed { }\nfn main() {}\n'):
            with self.subTest(source):
                self.repo.write('suifleet-edge/tools/entry.rs', source)
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, 'inside or on an inline'):
                    packager.plan(self.repo.root, 'HEAD')
        self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML)
        self.repo.write('suifleet-edge/src/lib.rs', 'mod inner {\n    #[path = "child.rs"]\n    mod child;\n}\n')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, r'src/lib.rs: #\[path\] attribute'):
            packager.plan(self.repo.root, 'HEAD')
        # File-level #[path] modules before and after closed inline blocks
        # (as in the daemon's sui_client.rs), and ones only named inside a
        # comment or string in a block, are still packaged file-relative.
        self.repo.write('suifleet-edge/src/lib.rs', 'pub fn lib() {}\n')
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        for source in ('#[path = "child.rs"]\nmod child;\n#[cfg(test)]\nmod tests {}\nfn main() { child::run() }\n',
                       "mod done { const C: char = '{'; fn f() {} }\n#[cfg(test)]\n#[path = \"child.rs\"]\nmod child;\n"
                       'fn main() {}\n',
                       'mod done {\n    // #[path = "child.rs"] mod old;\n}\n#[path = "child.rs"]\nmod child;\nfn main() {}\n'):
            with self.subTest(source):
                self.repo.write('suifleet-edge/tools/entry.rs', source)
                rev = self.repo.commit()
                paths = packager.plan(self.repo.root, rev)['paths']
                self.assertIn('suifleet-edge/tools/child.rs', paths)
                self.assertNotIn('suifleet-edge/tools/nested/child.rs', paths)

    def test_every_toml_spelling_of_workspace_members_is_packaged(self):
        # Each spelling once dropped windows-runtime from the archive: literal
        # strings, then escaped keys and headers that only decode to
        # workspace.members. The whole manifest is parsed, so keys are compared
        # after decoding, as Cargo compares them.
        base = '[workspace]\nmembers = ["windows-runtime"]'
        spellings = ("[workspace]\nmembers = ['windows-runtime']",
                     '[workspace] # the daemon\nmembers = [\n  "windows-runtime", # the runtime\n]',
                     '[workspace]\n"memb\\u0065rs" = ["windows-runtime"]',
                     '["worksp\\u0061ce"]\nmembers = ["windows-runtime"]',
                     "['workspace']\n'members' = [\"windows\\u002druntime\"]",
                     '[workspace]\nmembers = ["\\U00000077indows-runtime"]',
                     'workspace.members = ["windows-runtime"]',
                     '"worksp\\u0061ce" . "members" = ["windows-runtime"]',
                     'workspace = { "memb\\u0065rs" = ["windows-runtime"] }')
        for workspace in spellings:
            with self.subTest(workspace):
                # Root-level keys must precede the first table header.
                text = (workspace + '\n' + CARGO_TOML.replace(base, '') if workspace.startswith(('workspace', '"'))
                        else CARGO_TOML.replace(base, workspace))
                self.repo.write('suifleet-edge/Cargo.toml', text)
                self.repo.commit()
                self.assertEqual(packager.workspace_members(text), ['windows-runtime'])
                info = packager.build(self.repo.root, 'HEAD', self.out / 'spelling.tar.gz')
                (self.out / 'spelling.tar.gz').unlink()
                self.assertIn('suifleet-edge/windows-runtime/Cargo.toml', info['paths'])
                self.assertIn('suifleet-edge/windows-runtime/src/lib.rs', info['paths'])
        # Syntax outside the supported subset is refused, never skipped. The
        # multi-line string could otherwise hide a later members key.
        for workspace in ('[workspace]\nmembers = ["""windows-runtime"""]',
                          '[workspace]\nx = """\n[other]\n"""\nmembers = ["windows-runtime"]',
                          '[workspace]\nmembers = "windows-runtime"',
                          '[workspace]\nmembers = ["windows-runtime", 1]',
                          '[[workspace.members]]\npath = "windows-runtime"',
                          '[workspace]\nmembers = ["windows\\qruntime"]',
                          '[workspace]\nmembers = ["\\ud800"]',
                          '[workspace]\nmembers = ["windows-runtime"]\n"memb\\u0065rs" = []',
                          '[workspace]\nmembers = ["windows-runtime"]\n["worksp\\u0061ce"]\nresolver = "2"'):
            with self.subTest(workspace):
                self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML.replace(base, workspace))
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, 'unsupported|duplicate'):
                    packager.plan(self.repo.root, 'HEAD')

    def test_toml_reader_matches_a_reference_parser(self):
        try:
            import tomllib as reference
        except ImportError:
            try:
                import tomli as reference
            except ImportError:
                self.skipTest('no reference TOML parser (tomllib or tomli) is importable')

        def plain(value):
            if isinstance(value, dict):
                return {k: plain(v) for k, v in value.items()}
            if isinstance(value, list):
                return [plain(v) for v in value]
            return str(value).lower() if isinstance(value, bool) else str(value)
        manifests = [CARGO_TOML, (ROOT / 'suifleet-edge/Cargo.toml').read_text()]
        for text in manifests:
            self.assertEqual(plain(packager.parse_toml(text)), plain(reference.loads(text)))

    def test_runtime_state_symlinks_and_escapes_are_refused(self):
        cases = [
            ('config.toml', 'const C: &str = include_str!("../config.toml");\n', 'runtime identity/state'),
            ('../../../etc/passwd', 'const C: &str = include_str!("../../../etc/passwd");\n', 'outside the repository'),
        ]
        for target, source, message in cases:
            with self.subTest(target=target):
                if target == 'config.toml':
                    self.repo.write('suifleet-edge/config.toml', 'device = "secret"')
                self.repo.write('suifleet-edge/src/main.rs', source)
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, message):
                    packager.plan(self.repo.root, 'HEAD')
        self.repo.write('suifleet-edge/src/main.rs', MAIN_RS)
        (self.repo.root / 'suifleet-edge/src/link.rs').symlink_to('/etc/hostname')
        self.repo.commit()
        with self.assertRaisesRegex(ValueError, 'regular tracked files'):
            packager.plan(self.repo.root, 'HEAD')

    def test_published_file_list_is_reproduced_with_the_same_audit(self):
        closure = packager.plan(self.repo.root, 'HEAD')['paths']
        # A broader published inventory is allowed; it must still cover the
        # build closure.
        listing = closure + ['docs/README.md', 'interfaces/unrelated.json']
        info = packager.build(self.repo.root, 'HEAD', self.out / 'listed.tar.gz', listing)
        self.assertEqual(info['files'], len(closure) + 2)
        with tarfile.open(self.out / 'listed.tar.gz') as tar:
            self.assertEqual(sorted(m.name for m in tar.getmembers() if m.isfile()), sorted(listing))
        exact = packager.build(self.repo.root, 'HEAD', self.out / 'exact.tar.gz', list(closure))
        default = packager.build(self.repo.root, 'HEAD', self.out / 'default.tar.gz')
        self.assertEqual(exact['sha256'], default['sha256'])
        with self.assertRaisesRegex(ValueError, 'absent from revision'):
            packager.build(self.repo.root, 'HEAD', self.out / 'bad.tar.gz', closure + ['nope'])

    def test_published_file_list_must_cover_the_build_closure(self):
        # The reviewer's reproduction: a list holding only LICENSE produced a
        # one-member archive while reporting the crate version and lock digest.
        closure = packager.plan(self.repo.root, 'HEAD')['paths']
        omissions = {
            'only LICENSE': [p for p in closure if p != 'LICENSE'],
            'package manifest': ['suifleet-edge/Cargo.toml'],
            'lockfile': ['suifleet-edge/Cargo.lock'],
            'workspace member manifest': ['suifleet-edge/windows-runtime/Cargo.toml'],
            'workspace member source': ['suifleet-edge/windows-runtime/src/lib.rs'],
            'crate root': ['suifleet-edge/src/main.rs'],
            'transitive include_bytes target': ['suifleet-edge/src/data/blob.bin'],
            'shared interface root': ['interfaces/schema.json'],
        }
        for label, dropped in omissions.items():
            with self.subTest(label):
                listing = [p for p in closure if p not in dropped] + ['docs/README.md']
                output = self.out / 'incomplete.tar.gz'
                with self.assertRaisesRegex(ValueError, 'omits .*build-closure path'):
                    packager.build(self.repo.root, 'HEAD', output, listing)
                self.assertFalse(output.exists())

    def test_paths_from_cli_refuses_an_incomplete_list(self):
        listing = self.out / 'list.txt'
        listing.write_text('LICENSE\n')
        output = self.out / 'cli.tar.gz'
        result = subprocess.run([sys.executable, str(Path(packager.__file__)), '--repo', str(self.repo.root),
                                 '--revision', 'HEAD', '--output', str(output), '--paths-from', str(listing)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('build-closure', result.stderr)
        self.assertFalse(output.exists())
        listing.write_text('\n'.join(packager.plan(self.repo.root, 'HEAD')['paths']) + '\n')
        result = subprocess.run([sys.executable, str(Path(packager.__file__)), '--repo', str(self.repo.root),
                                 '--revision', 'HEAD', '--output', str(output), '--paths-from', str(listing)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.exists())


def recipe_text(archive_url, archive_sha, srcrev, extra=''):
    # subdir= is an archive unpack parameter; git sources place the checkout
    # with destsuffix= and the audit refuses git parameters it does not model.
    params = ';name=edge' if archive_url.startswith(('git://', 'gitsm://')) else ';subdir=source;name=edge'
    return f'''SUMMARY = "fixture"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5={md5(LICENSE)}"
SRC_URI = " \\
    {archive_url}{params} \\
    file://suifleet.service \\
"
SRC_URI[edge.sha256sum] = "{archive_sha}"
SRCREV = "{srcrev}"
SUIFLEET_UNPACKDIR = "${{@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}}"
S = "${{SUIFLEET_UNPACKDIR}}/source"
inherit cargo
require suifleet-crates.inc
SUIFLEET_LOCK_SHA256 = "{sha256(CARGO_LOCK)}"
SUIFLEET_CRATE_VERSION = "0.2.3"
PE = "1"
PV = "${{SUIFLEET_CRATE_VERSION}}"
CARGO_SRC_DIR = "suifleet-edge"
python do_verify_lock() {{
    bb.fatal('not parsed as an assignment')
}}
{extra}'''


CRATES_INC = f'''SRC_URI += " \\
    crate://crates.io/anyhow/1.0.102 \\
"
SRC_URI[anyhow-1.0.102.sha256sum] = "{'a' * 64}"
'''


class LayerAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = SourceRepo(base / 'repo')
        self.rev = self.repo.git('rev-parse', 'HEAD').decode().strip()
        self.archive = base / f'suifleet-edge-{self.rev}.tar.gz'
        self.info = packager.build(self.repo.root, self.rev, self.archive)
        self.url = f'https://raw.githubusercontent.com/example/meta-suifleet/{"b" * 40}/{self.archive.name}'
        self.layer = base / 'meta-suifleet'
        (self.layer / 'conf').mkdir(parents=True)
        (self.layer / 'conf/layer.conf').write_text(
            'BBFILES += "${LAYERDIR}/recipes-*/*/*.bb"\n'
            'BBFILES += "${LAYERDIR}/recipes-*/*/*.bbappend"\n'
            'LAYERSERIES_COMPAT_meta-suifleet = "scarthgap wrynose"\n')
        self.recipe_dir = self.layer / 'recipes-edge/suifleet'
        (self.recipe_dir / 'files').mkdir(parents=True)
        (self.recipe_dir / 'files/suifleet.service').write_text('[Unit]\n')
        (self.recipe_dir / 'suifleet-crates.inc').write_text(CRATES_INC)
        self.write_recipe()

    def write_recipe(self, extra='', url=None, sha=None, srcrev=None):
        (self.recipe_dir / 'suifleet_1.0.bb').write_text(
            recipe_text(url or self.url, sha or self.info['sha256'], srcrev or self.rev, extra))

    def errors(self, **kwargs):
        return audit.check(self.layer, **kwargs)['errors']

    def test_coherent_layer_passes_and_reports_mapping(self):
        report = audit.check(self.layer, repo=self.repo.root)
        self.assertEqual(report['errors'], [])
        entry = report['recipes']['suifleet']
        self.assertEqual((entry['pv'], entry['crate_version'], entry['source_revision']), ('0.2.3', '0.2.3', self.rev))
        self.assertEqual(entry['sources'][0]['sha256'], self.info['sha256'])
        self.assertEqual(entry['license_files'], {'LICENSE': md5(LICENSE)})
        self.assertEqual((entry['crates'], report['series']), (1, ['scarthgap', 'wrynose']))

    def shipped_verify_lock(self, source, cargo_src_dir, lock_sha):
        """Run the shipped recipe's do_verify_lock body against `source`/S.

        Returns the bb.fatal message, or None when the task passes."""
        text = (ROOT / 'suifleet-edge/meta-suifleet/recipes-edge/suifleet/suifleet_1.0.bb').read_text()
        body = re.search(r'^python do_verify_lock\(\) \{\n(.*?)^\}', text, re.M | re.S).group(1)
        values = {'S': str(source), 'CARGO_SRC_DIR': cargo_src_dir, 'SUIFLEET_CRATE_VERSION': '0.2.3',
                  'SUIFLEET_LOCK_SHA256': lock_sha,
                  'CARGO_LOCK_PATH': str(Path(source, cargo_src_dir, 'Cargo.lock'))}

        class Fatal(Exception):
            pass

        def fatal(message):
            raise Fatal(message)

        scope = {'d': mock.Mock(getVar=values.get), 'bb': mock.Mock(fatal=fatal)}
        try:
            with warnings.catch_warnings():
                # The recipe task leaves its files to BitBake's interpreter to close.
                warnings.simplefilter('ignore', ResourceWarning)
                exec(textwrap.dedent(body), scope)
        except Fatal as error:
            return str(error)
        return None

    def test_shipped_verify_lock_pattern_is_the_audited_pattern(self):
        text = (ROOT / 'suifleet-edge/meta-suifleet/recipes-edge/suifleet/suifleet_1.0.bb').read_text()
        self.assertIn(f"re.search(r'{audit.RECIPE_VERSION_PATTERN}', manifest, re.M | re.S)", text)
        self.assertIn("os.path.join(d.getVar('S'), d.getVar('CARGO_SRC_DIR'), 'Cargo.toml')", text)

    def test_shipped_verify_lock_reads_the_effective_cargo_src_dir(self):
        # A relocated crate: do_verify_lock must read the manifest the audit
        # resolves under CARGO_SRC_DIR, not a hardcoded suifleet-edge path.
        source = Path(self.tmp.name) / 'relocated'
        for rel, text in (('crates/edge/Cargo.toml', CARGO_TOML), ('crates/edge/Cargo.lock', CARGO_LOCK)):
            (source / rel).parent.mkdir(parents=True, exist_ok=True)
            (source / rel).write_text(text)
        self.assertIsNone(self.shipped_verify_lock(source, 'crates/edge', sha256(CARGO_LOCK)))
        self.assertEqual(audit.recipe_crate_version(CARGO_TOML), packager.crate_version(CARGO_TOML))

    def test_manifest_version_spellings_the_recipe_cannot_read_are_refused(self):
        # Valid TOML that package_edge_source parses but do_verify_lock's regex
        # does not read identically; the audit must refuse it rather than
        # certify a layer whose verification task then calls bb.fatal.
        spellings = {
            'single quoted': CARGO_TOML.replace('version = "0.2.3"', "version = '0.2.3'", 1),
            'no spaces': CARGO_TOML.replace('version = "0.2.3"', 'version="0.2.3"', 1),
            'array before version': CARGO_TOML.replace('version = "0.2.3"',
                                                       'authors = ["x"]\nversion = "0.2.3"', 1),
        }
        for label, manifest in spellings.items():
            with self.subTest(label):
                # Both readers against the same manifest: TOML says 0.2.3,
                # the shipped recipe task fails.
                self.assertEqual(packager.crate_version(manifest), '0.2.3')
                source = Path(tempfile.mkdtemp(dir=self.tmp.name))
                (source / 'suifleet-edge').mkdir()
                (source / 'suifleet-edge/Cargo.toml').write_text(manifest)
                (source / 'suifleet-edge/Cargo.lock').write_text(CARGO_LOCK)
                self.assertIn('crate version differs',
                              self.shipped_verify_lock(source, 'suifleet-edge', sha256(CARGO_LOCK)) or '')
                self.repo.write('suifleet-edge/Cargo.toml', manifest)
                rev = self.repo.commit()
                archive = Path(self.tmp.name) / f'suifleet-edge-{rev}.tar.gz'
                info = packager.build(self.repo.root, rev, archive)
                self.write_recipe(url=f'https://raw.githubusercontent.com/example/meta-suifleet/{"b" * 40}/'
                                      f'{archive.name}', sha=info['sha256'], srcrev=rev)
                errors = self.errors(repo=self.repo.root)
                self.assertTrue(any("do_verify_lock's pattern" in e for e in errors), errors)
        # The shipped spelling passes both the audit and the recipe task.
        self.repo.write('suifleet-edge/Cargo.toml', CARGO_TOML)
        rev = self.repo.commit()
        archive = Path(self.tmp.name) / f'suifleet-edge-{rev}.tar.gz'
        info = packager.build(self.repo.root, rev, archive)
        self.write_recipe(url=f'https://raw.githubusercontent.com/example/meta-suifleet/{"b" * 40}/{archive.name}',
                          sha=info['sha256'], srcrev=rev)
        self.assertEqual(self.errors(repo=self.repo.root), [])

    def test_cargo_lock_and_manifest_paths_must_be_the_audited_files(self):
        # do_verify_lock opens the effective CARGO_LOCK_PATH, so the audit must
        # not certify suifleet-edge/Cargo.lock while metadata points BitBake at
        # another (possibly missing) lockfile or manifest.
        accepted = {
            'shipped lock dir': 'CARGO_LOCK_SRC_DIR = "${S}/suifleet-edge"',
            'explicit lock path': 'CARGO_LOCK_PATH = "${S}/${CARGO_SRC_DIR}/Cargo.lock"',
            'explicit manifest path': 'CARGO_MANIFEST_PATH = "${S}/suifleet-edge//Cargo.toml"',
            'weak default': 'CARGO_LOCK_PATH ??= "${S}/suifleet-edge/Cargo.lock"',
            'interior dot': 'CARGO_LOCK_PATH = "${S}/./suifleet-edge//Cargo.lock"',
            'lock dir trailing slash': 'CARGO_LOCK_SRC_DIR = "${S}/suifleet-edge/"',
        }
        for label, extra in accepted.items():
            with self.subTest(label):
                self.write_recipe(extra=extra)
                self.assertEqual(audit.check(self.layer, repo=self.repo.root)['errors'], [])
        rejected = {
            'missing lock inside S': ('CARGO_LOCK_PATH = "${S}/suifleet-edge/missing/Cargo.lock"', 'CARGO_LOCK_PATH'),
            'other lock dir': ('CARGO_LOCK_SRC_DIR = "${S}/other"', 'CARGO_LOCK_SRC_DIR'),
            'other manifest': ('CARGO_MANIFEST_PATH = "${S}/other/Cargo.toml"', 'CARGO_MANIFEST_PATH'),
            'outside S': ('CARGO_LOCK_PATH = "/tmp/Cargo.lock"', 'CARGO_LOCK_PATH'),
            'weak default elsewhere': ('CARGO_LOCK_PATH ??= "${S}/Cargo.lock"', 'CARGO_LOCK_PATH'),
            'append': ('CARGO_LOCK_PATH = "${S}/suifleet-edge/Cargo.lock"\nCARGO_LOCK_PATH:append = ".alt"',
                       'CARGO_LOCK_PATH'),
            'indirect': ('LOCKDIR = "${S}/other"\nCARGO_LOCK_PATH = "${LOCKDIR}/Cargo.lock"', 'CARGO_LOCK_PATH'),
            # open() raises NotADirectoryError for a regular file named with
            # trailing directory syntax, so these must not normalize to a pass.
            'lock trailing slash': ('CARGO_LOCK_PATH = "${S}/suifleet-edge/Cargo.lock/"', 'CARGO_LOCK_PATH'),
            'lock trailing dot': ('CARGO_LOCK_PATH = "${S}/suifleet-edge/Cargo.lock/."', 'CARGO_LOCK_PATH'),
            'manifest trailing slash': ('CARGO_MANIFEST_PATH = "${S}/suifleet-edge/Cargo.toml/"',
                                        'CARGO_MANIFEST_PATH'),
        }
        for label, (extra, name) in rejected.items():
            for fetch in (False, True):
                with self.subTest(label, fetch=fetch):
                    self.write_recipe(extra=extra)
                    with mock.patch.object(audit, 'download', self.fake_download):
                        report = audit.check(self.layer, repo=self.repo.root, fetch=fetch)
                    if report.get('fetch_dir'):
                        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                    self.assertFalse(report['ok'])
                    self.assertTrue(any(f'{name} ' in e and 'the audit verifies suifleet-edge/Cargo.lock' in e
                                        for e in report['errors']), report['errors'])
        # A machine/distro override (directly or through a referenced variable)
        # could select another lockfile at build time; it is refused outright.
        overrides = {
            'CARGO_LOCK_PATH:qemuarm64': 'CARGO_LOCK_PATH:qemuarm64 = "${S}/other/Cargo.lock"',
            'CARGO_LOCK_SRC_DIR:raspberrypi3': 'CARGO_LOCK_SRC_DIR:raspberrypi3 = "${S}/other"',
            'CARGO_MANIFEST_PATH:aarch64': 'CARGO_MANIFEST_PATH:aarch64 = "${S}/other/Cargo.toml"',
            'LOCKDIR:qemuarm64': 'LOCKDIR = "${S}/suifleet-edge"\nLOCKDIR:qemuarm64 = "${S}/other"\n'
                                 'CARGO_LOCK_PATH = "${LOCKDIR}/Cargo.lock"',
        }
        for label, extra in overrides.items():
            with self.subTest(label):
                self.write_recipe(extra=extra)
                errors = self.errors(repo=self.repo.root)
                self.assertTrue(any(f'conditional override {label}' in e for e in errors), errors)
        # An append is part of the same metadata and is checked the same way.
        self.write_recipe()
        append = self.recipe_dir / 'suifleet_%.bbappend'
        for text, message in (('CARGO_LOCK_PATH = "${S}/suifleet-edge/missing/Cargo.lock"\n', 'CARGO_LOCK_PATH '),
                              ('CARGO_LOCK_PATH:qemuarm64 = "${S}/other/Cargo.lock"\n',
                               'conditional override CARGO_LOCK_PATH:qemuarm64')):
            with self.subTest(append=text):
                append.write_text(text)
                errors = self.errors(repo=self.repo.root)
                self.assertTrue(any(message in e for e in errors), errors)
        append.unlink()

    def test_mutable_unpinned_and_credentialed_sources_are_rejected(self):
        cases = {
            'AUTOREV': dict(extra='SRCREV_other = "${AUTOREV}"'),
            'branch/tag': dict(url='https://github.com/example/suifleet/archive/refs/heads/main.tar.gz'),
            'raw file at a branch': dict(url=f'https://raw.githubusercontent.com/example/m/main/{self.archive.name}'),
            'expiring release-asset': dict(url='https://objects.githubusercontent.com/x/y.tar.gz'),
            'query/fragment': dict(url=f'https://example.com/{self.archive.name}?X-Amz-Signature=abc'),
            'credentials': dict(url=f'https://user:token@example.com/{self.archive.name}'),
            'must use https': dict(url=f'http://example.com/{self.archive.name}'),
            'lacks a SHA-256 pin': dict(sha='0' * 63),
            'names commit': dict(url=f'https://example.com/suifleet-edge-{"c" * 40}.tar.gz'),
        }
        for message, kwargs in cases.items():
            with self.subTest(message):
                self.write_recipe(**kwargs)
                self.assertTrue(any(message in e for e in self.errors()), self.errors())

    def test_source_urls_without_a_fetchable_authority_are_refused_with_a_report(self):
        # A valid checksum cannot make a URL fetchable: an empty or malformed
        # host, or a nonnumeric/out-of-range port, must be a static finding that
        # blocks fetching, and an unparseable URL must not crash the report.
        name = self.archive.name
        cases = {
            'missing host': f'https:///immutable/{name}',
            'missing host with port': f'https://:443/immutable/{name}',
            'nonnumeric port': f'https://example.invalid:bad/immutable/{name}',
            'port out of range': f'https://example.invalid:70000/immutable/{name}',
            'port zero': f'https://example.invalid:0/immutable/{name}',
            'invalid host name': f'https://exa_mple..com/immutable/{name}',
            'invalid bracketed IPv6': f'https://[not-ipv6]/immutable/{name}',
            'unterminated IPv6 bracket': f'https://[invalid/{name}',
            'scoped IPv6 literal': f'https://[::1%bogus]/immutable/{name}',
            'percent-encoded scoped IPv6 literal': f'https://[::1%25bogus]/immutable/{name}',
            'scoped link-local IPv6 with port': f'https://[fe80::1%25eth0]:443/immutable/{name}',
            'text after IPv6 bracket': f'https://[::1]junk/immutable/{name}',
            'git scoped IPv6 literal': 'git://[fe80::1%25eth0]/example/suifleet.git;protocol=https;nobranch=1',
            'git missing host': 'git:///example/suifleet.git;protocol=https;nobranch=1',
            'git nonnumeric port': 'git://github.com:bad/example/suifleet.git;protocol=https;nobranch=1',
            'git unterminated bracket': 'git://[invalid/example/suifleet.git;protocol=https;nobranch=1',
        }
        fetched = []
        for label, url in cases.items():
            for fetch in (False, True):
                with self.subTest(label, fetch=fetch):
                    self.write_recipe(url=url, extra=f'SRCREV_edge = "{self.rev}"')
                    with mock.patch.object(audit, 'download', lambda *a, **k: fetched.append(a)), \
                            mock.patch.object(audit, 'fetch_recipe', lambda *a, **k: fetched.append(a) or ([], [])):
                        report = audit.check(self.layer, fetch=fetch)
                    if fetch:
                        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                        self.assertEqual(report['recipes']['suifleet']['fetch_skipped'], 'static source findings')
                    self.assertFalse(report['ok'])
                    self.assertTrue(any('unfetchable authority' in e or 'unparseable source URL' in e
                                        for e in report['errors']), report['errors'])
                    json.dumps(report)
        self.assertEqual(fetched, [])
        # Ordinary authorities (explicit port, root dot, IP literals) stay accepted.
        for url in (f'https://example.com:8443/immutable/{name}', f'https://example.com./immutable/{name}',
                    f'https://192.0.2.1/immutable/{name}', f'https://[2001:db8::1]:443/immutable/{name}',
                    f'https://[2001:db8::1]/immutable/{name}'):
            with self.subTest('accepted', url=url):
                self.write_recipe(url=url)
                self.assertFalse(any('authority' in e or 'unparseable' in e for e in self.errors()), self.errors())

    def test_unparseable_source_url_via_command_line_still_emits_a_json_report(self):
        self.write_recipe(url=f'https://[invalid/{self.archive.name}')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_layer_sources.py'), '--layer', str(self.layer)],
                                capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report['ok'])
        self.assertTrue(any('unparseable source URL' in e for e in report['errors']), report['errors'])
        self.assertNotIn('Traceback', result.stderr)

    def test_mutable_source_hosts_match_case_insensitively(self):
        # DNS hostnames and schemes are case-insensitive and a trailing root
        # dot or the default port names the same host, so each spelling of a
        # mutable source must be refused exactly like the canonical one.
        name = self.archive.name
        mutable = {
            'GitHub archive of a branch/tag': '{host}/example/suifleet/archive/refs/heads/main.tar.gz',
            'codeload archive of a branch/tag': '{host}/example/suifleet/tar.gz/refs/heads/main',
            'raw file at a branch/tag': '{host}/example/m/main/' + name,
            'expiring release-asset URL': '{host}/x/' + name,
        }
        hosts = {
            'GitHub archive of a branch/tag': 'github.com',
            'codeload archive of a branch/tag': 'codeload.github.com',
            'raw file at a branch/tag': 'raw.githubusercontent.com',
            'expiring release-asset URL': 'objects.githubusercontent.com',
        }
        for label, template in mutable.items():
            host = hosts[label]
            spellings = (host, host.upper(), host.title(), host + '.', host.upper() + ':443')
            for spelling in spellings:
                with self.subTest(label, host=spelling):
                    self.write_recipe(url='https://' + template.format(host=spelling))
                    errors = self.errors()
                    self.assertTrue(any(f'mutable or expiring source ({label})' in e for e in errors), errors)

    def test_commit_pinned_github_archive_with_mixed_case_host_is_not_mutable(self):
        # Normalizing the host must not turn a commit-pinned archive into a
        # mutable finding, and the path itself stays case-sensitive.
        pinned = f'https://GitHub.com/example/suifleet/archive/{"c" * 40}.tar.gz'
        self.write_recipe(url=pinned)
        self.assertFalse(any('mutable or expiring' in e for e in self.errors()), self.errors())
        upper = f'https://GitHub.com/example/suifleet/archive/{"C" * 40}.tar.gz'
        self.write_recipe(url=upper)
        self.assertTrue(any('GitHub archive of a branch/tag' in e for e in self.errors()), self.errors())

    def test_rejected_credentials_and_signatures_never_reach_the_report(self):
        # The audit rejects credentialed and signed URLs; it must not then copy
        # the secret into the JSON report (stdout, saved reports, CI logs).
        secrets = ('USERMARK', 'PASSMARK', 'SIGMARK', 'FRAGMARK')
        name = self.archive.name
        cases = {
            'https userinfo': (f'https://USERMARK:PASSMARK@example.com/{name}', 'credentials'),
            'https query': (f'https://example.com/{name}?X-Amz-Signature=SIGMARK&X-Amz-Credential=USERMARK', 'query/fragment'),
            'https fragment': (f'https://example.com/{name}#FRAGMARK', 'query/fragment'),
            'mutable and signed': (f'https://objects.githubusercontent.com/x/{name}?sig=SIGMARK', 'mutable or expiring'),
            'git userinfo': ('git://USERMARK:PASSMARK@github.com/example/suifleet.git;protocol=https;nobranch=1',
                             'credentials'),
            'gitsm userinfo': ('gitsm://USERMARK:PASSMARK@github.com/example/suifleet.git;protocol=https;nobranch=1',
                               'gitsm sources are not supported'),
        }
        for label, (url, message) in cases.items():
            for fetch in (False, True):
                with self.subTest(label, fetch=fetch):
                    self.write_recipe(url=url, extra=f'SRCREV_edge = "{self.rev}"')
                    with mock.patch.object(audit, 'download', self.fake_download):
                        report = audit.check(self.layer, fetch=fetch)
                    if fetch:
                        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                    self.assertFalse(report['ok'])
                    self.assertTrue(any(message in e for e in report['errors']), report['errors'])
                    serialized = json.dumps(report)
                    for secret in secrets:
                        self.assertNotIn(secret, serialized)
                    self.assertIn('<redacted>', serialized)
            # The fetcher's own refusals (direct callers) are redacted too.
            if label.startswith('gitsm'):
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(errors)
                self.assertFalse(any(secret in json.dumps(errors) for secret in secrets), errors)
        # A refused expanded-name assignment reports its file and variable name
        # but never its value, through both direct helpers and the report.
        signed = f'https://USERMARK:PASSMARK@example.com/{name}?sig=SIGMARK#FRAGMARK'
        include = self.recipe_dir / 'extra.inc'
        append = self.recipe_dir / 'suifleet_1.0.bbappend'
        self.addCleanup(include.unlink, missing_ok=True)
        self.addCleanup(append.unlink, missing_ok=True)
        expanded = {
            'plain': f'SUFFIX = "URI"\nSRC_${{SUFFIX}} = "{signed}"',
            'flag': f'SUFFIX = "URI"\nSRC_${{SUFFIX}}[USERMARK] = "{signed}"',
            'unquoted': f'SUFFIX = "URI"\nSRC_${{SUFFIX}} += {signed}',
        }
        for label, text in expanded.items():
            for where in ('bb', 'inc', 'bbappend'):
                with self.subTest('expanded name', case=label, where=where):
                    include.unlink(missing_ok=True)
                    append.unlink(missing_ok=True)
                    if where == 'bb':
                        self.write_recipe(extra=text)
                    elif where == 'inc':
                        include.write_text(text + '\n')
                        self.write_recipe(extra='require extra.inc')
                    else:
                        append.write_text(text + '\n')
                        self.write_recipe()
                    recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer,
                                          [append] if append.exists() else ())
                    audited, _ = audit.audit_recipe(recipe)
                    self.assertTrue(any('key expansion' in e and 'SRC_${SUFFIX}' in e for e in audited), audited)
                    workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                    with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                        errors, fetched = audit.fetch_recipe(recipe, workdir)
                    self.assertEqual(fetched, [])
                    self.assertTrue(errors)
                    with mock.patch.object(audit, 'download', self.fake_download):
                        report = audit.check(self.layer, fetch=True)
                    self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                    self.assertFalse(report['ok'])
                    for diagnostics in (audited, errors, report):
                        serialized = json.dumps(diagnostics)
                        for secret in secrets:
                            self.assertNotIn(secret, serialized)
        include.unlink(missing_ok=True)
        append.unlink(missing_ok=True)
        # A failing git command line carries the remote; the report still
        # redacts it (defence in depth for subprocess diagnostics).
        url = 'git://github.com/example/suifleet.git;protocol=https;nobranch=1;name=edge;destsuffix=source'
        self.write_recipe(url=url, extra=f'SRCREV_edge = "{self.rev}"')
        failure = subprocess.CalledProcessError(
            128, ['git', 'fetch', 'https://USERMARK:PASSMARK@example.com/x.git?sig=SIGMARK#FRAGMARK'])
        with mock.patch.object(audit.subprocess, 'run', side_effect=failure):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
        self.assertTrue(any('fetch/unpack failed' in e for e in report['errors']), report['errors'])
        serialized = json.dumps(report)
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        self.assertIn('https://<redacted>@example.com/x.git?<redacted>#<redacted>', serialized)

    def test_git_sources_need_https_and_full_srcrev(self):
        git = 'git://github.com/example/suifleet.git;protocol=https;nobranch=1'
        self.write_recipe(url=git, srcrev='main')
        self.assertTrue(any('40-hex SRCREV' in e for e in self.errors()))
        self.write_recipe(url=git.replace('protocol=https', 'protocol=ssh'))
        self.assertTrue(any('protocol=https' in e for e in self.errors()))

    def test_git_sources_need_a_branch_or_nobranch(self):
        base = 'git://github.com/example/suifleet.git;protocol=https'
        self.write_recipe(url=base)
        self.assertTrue(any('needs branch=NAME' in e for e in self.errors()), self.errors())
        for params in (';nobranch=1', ';branch=main'):
            with self.subTest(params):
                self.write_recipe(url=base + params)
                self.assertFalse(any('needs branch=NAME' in e for e in self.errors()), self.errors())
        self.write_recipe(url=base + ';branch=a,b;name=x,y', extra=f'SRCREV_x = "{self.rev}"\nSRCREV_y = "{self.rev}"')
        self.assertTrue(any('several names or branches' in e for e in self.errors()), self.errors())

    def test_fetch_verifies_srcrev_is_on_the_named_branch(self):
        # BitBake rejects a SRCREV that is not on branch= (unless nobranch=1);
        # the audit must not report such a git source as verified.
        self.repo.git('branch', 'release', self.rev)
        self.repo.git('checkout', '-q', '-b', 'side')
        self.repo.write('side.txt', 'off the release branch')
        side = self.repo.commit()
        self.repo.git('checkout', '-q', 'release')
        remote = f'file://{self.repo.root}'
        cases = [
            ('branch=missing', self.rev, "branch 'missing' does not exist", False),
            ('branch=release', side, "is not on branch 'release'", False),
            ('branch=release', self.rev, None, True),
            ('nobranch=1', side, None, True),
        ]
        for params, rev, message, verified in cases:
            with self.subTest(params=params, rev=rev):
                url = f'git://github.com/example/suifleet.git;protocol=https;{params};name=edge;destsuffix=source'
                self.write_recipe(url=url, extra=f'SRCREV_edge = "{rev}"')
                with mock.patch.object(audit, 'git_remote', return_value=remote):
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                entry = report['recipes']['suifleet']
                self.assertNotIn('fetch_skipped', entry, report['errors'])
                fetched = [f for f in entry['fetched'] if 'revision' in f]
                self.assertEqual([f['verified'] for f in fetched], [verified], report['errors'])
                branch_errors = [e for e in report['errors'] if 'branch' in e]
                if message:
                    self.assertFalse(report['ok'])
                    self.assertTrue(any(message in e for e in branch_errors), report['errors'])
                else:
                    self.assertEqual(branch_errors, [])

    def git_fetch_report(self, **patches):
        url = 'git://github.com/example/suifleet.git;protocol=https;nobranch=1;name=edge;destsuffix=source'
        self.write_recipe(url=url, extra=f'SRCREV_edge = "{self.repo.git("rev-parse", "HEAD").decode().strip()}"')
        with mock.patch.multiple(audit, git_remote=mock.Mock(return_value=f'file://{self.repo.root}'), **patches):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
        return report

    def test_git_checkout_links_must_stay_inside_the_unpack_directory(self):
        # A pinned commit can track symlinks. One that leaves the unpack root
        # would make the checks read the review host's bytes (here a copy of
        # the expected LICENSE, so following it would pass); it is refused
        # before any source, manifest or license byte is read.
        host = Path(self.tmp.name) / 'host-license'
        host.write_text(LICENSE)
        never = AssertionError('source bytes read through an escaping link')
        for name, target in (('absolute', str(host)), ('relative', '../../../../host-license')):
            with self.subTest(name):
                (self.repo.root / 'LICENSE').unlink()
                os.symlink(target, self.repo.root / 'LICENSE')
                self.repo.commit()
                report = self.git_fetch_report(daemon_errors=mock.Mock(side_effect=never),
                                               load_packager=mock.Mock(side_effect=never))
                self.assertFalse(report['ok'])
                self.assertTrue(any('links resolve outside the unpack directory after checkout: source/LICENSE' in e
                                    for e in report['errors']), report['errors'])
        # An in-tree link (relative, resolving inside the checkout) is allowed.
        (self.repo.root / 'LICENSE').unlink()
        self.repo.write('LICENSE', LICENSE)
        os.symlink('README.md', self.repo.root / 'docs/alias.md')
        os.symlink('../LICENSE', self.repo.root / 'docs/LICENSE')
        self.repo.commit()
        report = self.git_fetch_report()
        self.assertEqual(report['errors'], [])
        self.assertTrue(report['ok'])
        self.assertEqual([f['verified'] for f in report['recipes']['suifleet']['fetched'] if 'revision' in f], [True])

    def test_git_never_runs_in_metadata_planted_by_an_earlier_archive(self):
        # An earlier pinned archive in the shared unpack tree can carry
        # git/.git/config. Git would honour that repository-local config
        # (core.fsmonitor, hooks, ...) despite the isolated HOME, so the audit
        # must never run Git in a destination an earlier source populated.
        plant = Path(self.tmp.name) / 'plant.tar.gz'
        marker = '[core]\n\tfsmonitor = /nonexistent/planted-marker\n'
        with tarfile.open(plant, 'w:gz') as tar:
            for name, data in (('git/.git/config', marker.encode()), ('other/README', b'unrelated\n')):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
            # A dangling in-tree symlink at a later Git destination.
            link = tarfile.TarInfo('dangling')
            link.type, link.linkname = tarfile.SYMTYPE, 'dangling-target'
            tar.addfile(link)
        plant_sha = sha256(plant.read_bytes())
        plant_url = f'https://raw.githubusercontent.com/example/meta-suifleet/{"b" * 40}/plant.tar.gz'
        head = self.repo.git('rev-parse', 'HEAD').decode().strip()

        def download(url, target):
            source = plant if url.startswith(plant_url) else self.archive
            shutil.copyfile(source, target)
            return sha256(source.read_bytes())

        calls, real_run = [], subprocess.run

        def recording_run(command, *args, **kwargs):
            calls.append([str(part) for part in command])
            return real_run(command, *args, **kwargs)

        def report_for(git_params):
            self.write_recipe(extra=f'SRC_URI += "{plant_url};name=plant {git_params}"\n'
                                    f'SRC_URI[plant.sha256sum] = "{plant_sha}"\n'
                                    f'SRCREV_tool = "{head}"\n')
            calls.clear()
            with mock.patch.multiple(audit, download=download,
                                     git_remote=mock.Mock(return_value=f'file://{self.repo.root}')), \
                    mock.patch.object(audit.subprocess, 'run', side_effect=recording_run):
                report = audit.check(self.layer, fetch=True)
            self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
            return report, Path(report['fetch_dir']).resolve() / 'suifleet'

        git_url = 'git://github.com/example/suifleet.git;protocol=https;nobranch=1;name=tool'
        # Conflicting destination (the default destsuffix=git): refused, and
        # Git is never invoked at all for that source.
        report, unpack = report_for(git_url)
        self.assertFalse(report['ok'])
        self.assertTrue(any('git destination git already exists' in e for e in report['errors']), report['errors'])
        self.assertEqual([c for c in calls if c[:1] == ['git']], [])
        self.assertNotIn('revision', json.dumps(report['recipes']['suifleet']['fetched']))
        self.assertEqual((unpack / 'git/.git/config').read_text(), marker)
        # A dangling symlink at the declared destination is the existing entry:
        # it is refused as declared, not followed to its absent target.
        report, unpack = report_for(git_url + ';destsuffix=dangling')
        self.assertFalse(report['ok'])
        self.assertTrue(any('git destination dangling already exists' in e for e in report['errors']), report['errors'])
        self.assertEqual([c for c in calls if c[:1] == ['git']], [])
        self.assertNotIn('revision', json.dumps(report['recipes']['suifleet']['fetched']))
        self.assertTrue((unpack / 'dangling').is_symlink())
        self.assertFalse(os.path.lexists(unpack / 'dangling-target'))
        # A fresh destination: Git runs only in a private repository outside
        # the unpack tree, which is then moved into place.
        report, unpack = report_for(git_url + ';destsuffix=tool')
        git_calls = [c for c in calls if c[:1] == ['git']]
        self.assertTrue(git_calls)
        for call in git_calls:
            for part in call:
                if part.startswith('/'):
                    path = Path(part).resolve()
                    self.assertNotIn(unpack, (path, *path.parents), call)
        self.assertEqual([f['verified'] for f in report['recipes']['suifleet']['fetched'] if 'revision' in f], [True],
                         report['errors'])
        self.assertFalse(any('tool' in e for e in report['errors']), report['errors'])
        self.assertEqual(subprocess.check_output(['git', '-C', str(unpack / 'tool'), 'rev-parse', 'HEAD'],
                                                 text=True).strip(), head)
        self.assertNotIn('planted-marker', (unpack / 'tool/.git/config').read_text())

    def test_assembled_tree_links_are_checked_before_source_checks(self):
        # The final assembled tree is confined again before S is read, even if
        # each source passed its own check.
        never = AssertionError('source bytes read through an escaping link')
        calls = iter([[], ['source/late-link']])
        report = self.git_fetch_report(links_escaping=mock.Mock(side_effect=lambda root: next(calls)),
                                       daemon_errors=mock.Mock(side_effect=never),
                                       load_packager=mock.Mock(side_effect=never))
        self.assertFalse(report['ok'])
        self.assertTrue(any('links resolve outside the unpack directory: source/late-link; source checks not run' in e
                            for e in report['errors']), report['errors'])

    def test_git_parameters_the_audit_does_not_model_are_refused(self):
        # subpath/nocheckout/etc. change what BitBake checks out; the audit
        # verifies the whole tree, so it must refuse instead of passing.
        self.repo.git('branch', 'release', self.rev)
        remote = f'file://{self.repo.root}'
        base = 'git://github.com/example/suifleet.git;protocol=https;branch=release;name=edge;destsuffix=source'
        for extra in ('subpath=missing-directory', 'nocheckout=1', 'bareclone=1', 'rev=release',
                      'tag=v1', 'usehead=1', 'lfs=1'):
            with self.subTest(extra):
                self.write_recipe(url=f'{base};{extra}', extra=f'SRCREV_edge = "{self.rev}"')
                key = extra.split('=')[0]
                self.assertTrue(any('does not model' in e and key in e for e in self.errors()), self.errors())
                with mock.patch.object(audit, 'git_remote', return_value=remote), \
                        mock.patch.object(audit.subprocess, 'run', side_effect=AssertionError('git ran')):
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertFalse(report['ok'])
                self.assertEqual(report['recipes']['suifleet']['fetch_skipped'], 'static source findings')
                # The fetcher itself refuses before cloning anything.
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit.subprocess, 'run', side_effect=AssertionError('git ran')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(any('does not model' in e and 'nothing fetched' in e for e in errors), errors)
        # The modelled parameters alone still fetch and verify.
        self.write_recipe(url=base, extra=f'SRCREV_edge = "{self.rev}"')
        self.assertFalse(any('does not model' in e for e in self.errors()), self.errors())
        with mock.patch.object(audit, 'git_remote', return_value=remote):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
        fetched = [f for f in report['recipes']['suifleet']['fetched'] if 'revision' in f]
        self.assertEqual([f['verified'] for f in fetched], [True], report['errors'])

    def test_gitsm_sources_are_refused_and_never_fetched(self):
        # gitsm would also fetch submodules that the audit never verifies, so a
        # fully pinned gitsm source must still fail, statically and under --fetch.
        url = 'gitsm://github.com/example/suifleet.git;protocol=https;nobranch=1;name=edge'
        self.write_recipe(url=url, extra=f'SRCREV_edge = "{self.rev}"')
        self.assertTrue(any('gitsm sources are not supported' in e for e in self.errors()), self.errors())
        with mock.patch.object(audit.subprocess, 'run', side_effect=AssertionError('git ran')), \
                mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertFalse(report['ok'])
        self.assertEqual(report['recipes']['suifleet']['fetch_skipped'], 'static source findings')
        # The fetcher itself refuses gitsm too, before creating anything.
        recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
        workdir = Path(self.tmp.name) / 'gitsm-fetch'
        workdir.mkdir()
        with mock.patch.object(audit.subprocess, 'run', side_effect=AssertionError('git ran')), \
                mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
            errors, fetched = audit.fetch_recipe(recipe, workdir)
        self.assertEqual(fetched, [])
        self.assertTrue(any('gitsm submodule inputs cannot be verified' in e and 'nothing fetched' in e
                            for e in errors), errors)

    def test_local_artifacts_outside_layer_are_rejected(self):
        self.write_recipe(extra='FILESEXTRAPATHS:prepend := "${TOPDIR}/artifacts:"\nSRC_URI += "file://codec.cjs"')
        errors = self.errors()
        self.assertTrue(any('FILESEXTRAPATHS points outside' in e for e in errors))
        self.assertTrue(any('not in the layer: codec.cjs' in e for e in errors))

    def test_local_sources_follow_the_bitbake_search_path(self):
        sibling = self.layer.parent / 'host-files'
        sibling.mkdir()
        (sibling / 'suifleet.service').write_text('[Unit]\nDescription=host only\n')
        # An explicit FILESPATH selecting only an out-of-layer directory: the
        # in-layer files/suifleet.service must not make the audit pass.
        self.write_recipe(extra=f'FILESPATH = "{sibling}"')
        errors = self.errors(repo=self.repo.root)
        self.assertTrue(any('explicit FILESPATH is not supported' in e for e in errors), errors)
        for extra in (f'FILESPATH:append = ":{sibling}"', f'FILESPATH ?= "{sibling}"'):
            with self.subTest(extra):
                self.write_recipe(extra=extra)
                self.assertTrue(any('FILESPATH' in e for e in self.errors()), self.errors())
        # An out-of-layer FILESEXTRAPATHS entry is searched first by BitBake, so
        # the same-named host file is the one it would use.
        self.write_recipe(extra=f'FILESEXTRAPATHS:prepend := "{sibling}:"')
        errors = self.errors()
        self.assertTrue(any('FILESEXTRAPATHS points outside' in e for e in errors), errors)
        self.assertTrue(any('suifleet.service resolves outside the layer' in e for e in errors), errors)
        self.write_recipe(extra='FILESEXTRAPATHS:prepend := "${THISDIR}/../../../host-files:"')
        self.assertTrue(any('FILESEXTRAPATHS points outside' in e for e in self.errors()), self.errors())
        # A deferred THISDIR cannot be attributed to one file and is rejected.
        self.write_recipe(extra='FILESEXTRAPATHS:prepend = "${THISDIR}/extra:"')
        self.assertTrue(any('FILESEXTRAPATHS points outside' in e for e in self.errors()), self.errors())
        # A valid in-layer FILESEXTRAPATHS directory is searched before files/,
        # as in BitBake, and a file only there is found.
        extra_dir = self.recipe_dir / 'extra'
        extra_dir.mkdir()
        (extra_dir / 'suifleet.service').write_text('[Unit]\nDescription=extra\n')
        (extra_dir / 'only-extra.conf').write_text('x\n')
        recipe = 'FILESEXTRAPATHS:prepend := "${THISDIR}/extra:"\nSRC_URI += "file://only-extra.conf"'
        self.write_recipe(extra=recipe)
        self.assertEqual(self.errors(repo=self.repo.root), [])
        parsed = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
        self.assertEqual(audit.file_source(parsed, 'suifleet.service'), (extra_dir / 'suifleet.service').resolve())
        self.assertNotIn('THISDIR', parsed.vars)
        # A file directly beside the recipe is not on BitBake's search path.
        (self.recipe_dir / 'beside.conf').write_text('x\n')
        self.write_recipe(extra='SRC_URI += "file://beside.conf"')
        self.assertTrue(any('not in the layer: beside.conf' in e for e in self.errors()), self.errors())

    def test_override_specific_local_files_are_rejected(self):
        # BitBake tries every FILESOVERRIDES subdirectory of every search
        # directory before the unqualified ones, so files/qemuarm64/x replaces
        # files/x on qemuarm64. The audit must not report the generic file.
        machine = self.recipe_dir / 'files/qemuarm64'
        machine.mkdir()
        (machine / 'suifleet.service').write_text('[Unit]\nDescription=machine-specific\n')
        errors = self.errors(repo=self.repo.root)
        self.assertTrue(any('override-specific candidate' in e and 'qemuarm64' in e for e in errors), errors)
        parsed = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
        with self.assertRaisesRegex(ValueError, 'override-specific'):
            audit.file_source(parsed, 'suifleet.service')
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        if report.get('fetch_dir'):
            self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertFalse(report['ok'])
        self.assertFalse((Path(report.get('fetch_dir') or self.tmp.name) / 'suifleet/suifleet.service').exists())
        # The same applies under the BPN directory and an in-layer extra path.
        shutil.rmtree(machine)
        bpn = self.recipe_dir / 'suifleet/raspberrypi3'
        bpn.mkdir(parents=True)
        (bpn / 'suifleet.service').write_text('[Unit]\n')
        self.assertTrue(any('override-specific candidate' in e for e in self.errors()), self.errors())
        shutil.rmtree(self.recipe_dir / 'suifleet')
        (self.recipe_dir / 'extra/poky').mkdir(parents=True)
        (self.recipe_dir / 'extra/poky/suifleet.service').write_text('[Unit]\n')
        self.write_recipe(extra='FILESEXTRAPATHS:prepend := "${THISDIR}/extra:"')
        self.assertTrue(any('override-specific candidate' in e for e in self.errors()), self.errors())
        # Unrelated override subdirectories, and in-layer search directories
        # nested under files/, are not candidates.
        shutil.rmtree(self.recipe_dir / 'extra')
        (machine / 'other.conf').parent.mkdir()
        (machine / 'other.conf').write_text('x\n')
        (self.recipe_dir / 'files/extra').mkdir()
        (self.recipe_dir / 'files/extra/suifleet.service').write_text('[Unit]\n')
        self.write_recipe(extra='FILESEXTRAPATHS:prepend := "${THISDIR}/files/extra:"')
        self.assertEqual(self.errors(repo=self.repo.root), [])

    def test_higher_priority_directory_candidates_are_rejected(self):
        # BitBake's local fetcher selects the first FILESPATH candidate that
        # exists (bb.utils.which), directories included. A directory at
        # ${BP}/payload therefore wins over files/payload; the audit must not
        # skip it and report (or unpack) the lower-priority regular file.
        (self.recipe_dir / 'files/payload').write_text('lower priority\n')
        self.write_recipe(extra='SRC_URI += "file://payload"')
        self.assertEqual(self.errors(repo=self.repo.root), [])
        parsed = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
        bp = self.recipe_dir / parsed.get('BP')
        (bp / 'payload').mkdir(parents=True)
        (bp / 'payload/inner').write_text('directory content\n')
        errors = self.errors(repo=self.repo.root)
        self.assertTrue(any('file:// source payload' in e and 'non-file' in e for e in errors), errors)
        with self.assertRaisesRegex(ValueError, 'non-file'):
            audit.file_source(parsed, 'payload')
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        if report.get('fetch_dir'):
            self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertFalse(report['ok'])
        self.assertFalse((Path(report.get('fetch_dir') or self.tmp.name) / 'suifleet/payload').exists())
        # A directory as the only candidate is rejected the same way.
        shutil.rmtree(bp)
        (self.recipe_dir / 'files/payload').unlink()
        (self.recipe_dir / 'files/payload').mkdir()
        self.assertTrue(any('file:// source payload' in e and 'non-file' in e for e in self.errors()), self.errors())
        (self.recipe_dir / 'files/payload').rmdir()
        (self.recipe_dir / 'files/payload').write_text('lower priority\n')
        # A directory in an override location is an override-specific
        # candidate, exactly like a regular file there.
        machine = self.recipe_dir / 'files/qemuarm64/payload'
        machine.mkdir(parents=True)
        errors = self.errors(repo=self.repo.root)
        self.assertTrue(any('override-specific candidate' in e and 'qemuarm64' in e for e in errors), errors)
        with self.assertRaisesRegex(ValueError, 'override-specific'):
            audit.file_source(audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer), 'payload')
        shutil.rmtree(self.recipe_dir / 'files/qemuarm64')
        self.assertEqual(self.errors(repo=self.repo.root), [])

    def test_override_directory_that_is_also_a_search_root_keeps_precedence(self):
        # files/qemuarm64 is both a FILESEXTRAPATHS root and the qemuarm64
        # override subdirectory of files/. BitBake tries files/qemuarm64/payload
        # (qualified) before extra/payload (unqualified, despite being first in
        # FILESEXTRAPATHS), so the audit must not report extra/payload.
        extra = self.recipe_dir / 'extra'
        extra.mkdir()
        (extra / 'payload').write_text('extra\n')
        machine = self.recipe_dir / 'files/qemuarm64'
        machine.mkdir()
        (machine / 'payload').write_text('machine\n')
        self.write_recipe(extra='FILESEXTRAPATHS:prepend := "${THISDIR}/extra:${THISDIR}/files/qemuarm64:"\n'
                                'SRC_URI += "file://payload"')
        errors = self.errors(repo=self.repo.root)
        self.assertTrue(any('override-specific candidate' in e and 'qemuarm64' in e for e in errors), errors)
        parsed = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
        with self.assertRaisesRegex(ValueError, 'override-specific'):
            audit.file_source(parsed, 'payload')
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        if report.get('fetch_dir'):
            self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertFalse(report['ok'])
        self.assertFalse((Path(report.get('fetch_dir') or self.tmp.name) / 'suifleet/payload').exists())
        # When the override root is the ordinary selection, every candidate is
        # the same file, so the source is accepted.
        (extra / 'payload').unlink()
        self.assertEqual(self.errors(repo=self.repo.root), [])
        parsed = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
        self.assertEqual(audit.file_source(parsed, 'payload'), (machine / 'payload').resolve())

    def test_conditional_source_overrides_are_rejected(self):
        cases = (
            'SRC_URI:qemuarm64 = "https://example.com/other.tar.gz;sha256sum=%s"' % ('0' * 64),
            'SRCREV:pn-suifleet = "%s"' % ('0' * 40),
            'SRC_URI:append:raspberrypi3 = " file://suifleet.service"',
            'SRC_URI:remove = "file://suifleet.service"',
            'SRC_URI:mydistro[edge.sha256sum] = "%s"' % ('0' * 64),
            'PV:class-native = "9.9.9"',
            'SUIFLEET_LOCK_SHA256:qemux86-64 = "%s"' % ('0' * 64),
            # An indirect input: a variable SRC_URI expands.
            'SRC_URI += "https://example.com/${ARCHIVE_NAME};sha256sum=%s"\nARCHIVE_NAME = "x.tar.gz"\nARCHIVE_NAME:arm = "y.tar.gz"' % ('0' * 64),
        )
        for extra in cases:
            with self.subTest(extra):
                self.write_recipe(extra=extra)
                errors = self.errors()
                self.assertTrue(any('conditional override' in e for e in errors), errors)
        # Package-scoped overrides of non-source variables and plain appends stay accepted.
        self.write_recipe(extra='RDEPENDS:${PN} += "ca-certificates"\nFILES:${PN} += "/etc/x"\n'
                                'CARGO_BUILD_FLAGS:append = " --bin suifleet"\nSRC_URI:append = " file://suifleet.service"')
        self.assertEqual(self.errors(), [])

    def test_appends_apply_after_every_assignment_like_bitbake(self):
        recipe_path = self.recipe_dir / 'order_1.0.bb'
        recipe_path.write_text(
            'SRC_URI:append = " https://example.com/late.tar.gz"\n'
            'SRC_URI:prepend = "https://example.com/first.tar.gz "\n'
            'SRC_URI = "https://example.com/base.tar.gz"\n'
            'SRC_URI += "https://example.com/plus.tar.gz"\n'
            'SRC_URI:append += "https://example.com/last.tar.gz"\n'
            'SRC_URI:prepend = "https://example.com/front.tar.gz "\n'
            'X ??= "weak"\nX += "y"\nY ??= "weak"\nY ?= "soft"\nZ ??= "weak"\n'
            'W ??= "first"\nW ??= "second"\nV ?= "first"\nV ?= "second"\n'
            'SRCREV ??= "%s"\nSRCREV ??= "%s"\n' % ('1' * 40, '2' * 40))
        recipe = audit.Recipe(recipe_path, self.layer)
        self.assertEqual([url.rsplit('/', 1)[1] for url, _ in recipe.sources()],
                         ['front.tar.gz', 'first.tar.gz', 'base.tar.gz', 'plus.tar.gz', 'late.tar.gz', 'last.tar.gz'])
        self.assertEqual((recipe.get('X'), recipe.get('Y'), recipe.get('Z')), (' y', 'soft', 'weak'))
        # BitBake: each ??= replaces the previous weak default; ?= keeps the first assignment.
        self.assertEqual((recipe.get('W'), recipe.get('V'), recipe.get('SRCREV')), ('second', 'first', '2' * 40))
        recipe_path.unlink()
        # An append before the base assignment is still audited: an unpinned source is reported.
        self.write_recipe(extra='')
        text = (self.recipe_dir / 'suifleet_1.0.bb').read_text()
        (self.recipe_dir / 'suifleet_1.0.bb').write_text(
            'SRC_URI:append = " https://example.com/unpinned.tar.gz"\n' + text)
        self.assertTrue(any('lacks a SHA-256 pin: https://example.com/unpinned.tar.gz' in e for e in self.errors()),
                        self.errors())
        # Flagged or conditional-default appends on source variables are not evaluated, so they are rejected.
        for extra in ('SRC_URI:append ?= " file://suifleet.service"', 'SRC_URI:append[edge.sha256sum] = "%s"' % ('0' * 64)):
            with self.subTest(extra):
                self.write_recipe(extra=extra)
                self.assertTrue(any('conditional override' in e for e in self.errors()), self.errors())

    def test_checksum_flags_use_bitbake_assignment_operators(self):
        good, other = self.info['sha256'], '0' * 64
        # ?= keeps the checksum already assigned, so the effective pin is unchanged.
        self.write_recipe(extra=f'SRC_URI[edge.sha256sum] ?= "{other}"')
        report = audit.check(self.layer)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['recipes']['suifleet']['sources'][0]['sha256'], good)
        # := expands with the helper value at that point, not a later reassignment.
        self.write_recipe(extra=f'EDGE_SUM = "{good}"\nSRC_URI[edge.sha256sum] := "${{EDGE_SUM}}"\nEDGE_SUM = "{other}"')
        report = audit.check(self.layer)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['recipes']['suifleet']['sources'][0]['sha256'], good)
        # A plain = after := still replaces it; the bad digest is reported.
        self.write_recipe(extra=f'SRC_URI[edge.sha256sum] = "{other}"')
        report = audit.check(self.layer)
        self.assertEqual(report['recipes']['suifleet']['sources'][0]['sha256'], other)
        # Joining operators act on the current flag text, as BitBake does.
        recipe_path = self.recipe_dir / 'flags_1.0.bb'
        recipe_path.write_text('A[f] = "b"\nA[f] += "c"\nA[f] =+ "a"\nA[f] .= "d"\nA[f] =. "z"\nA[g] ?= "1"\nA[g] ?= "2"\n')
        recipe = audit.Recipe(recipe_path, self.layer)
        self.assertEqual(recipe.flags['A'], {'f': 'za b cd', 'g': '1'})
        recipe_path.unlink()
        # ??= on a source flag differs between BitBake releases and is rejected.
        self.write_recipe(extra=f'SRC_URI[edge.sha256sum] ??= "{other}"')
        self.assertTrue(any('conditional override' in e and '??=' in e for e in self.errors()), self.errors())

    def test_single_quoted_and_unreadable_source_assignments_are_not_dropped(self):
        unpinned = 'https://example.com/unpinned.tar.gz'
        # Single-quoted values are read like double-quoted ones.
        self.write_recipe(extra=f"SRC_URI:append = ' {unpinned}'")
        self.assertTrue(any(f'lacks a SHA-256 pin: {unpinned}' in e for e in self.errors()), self.errors())
        self.write_recipe(extra=f"SRC_URI:qemuarm64 = '{unpinned}'")
        self.assertTrue(any('conditional override SRC_URI:qemuarm64' in e for e in self.errors()), self.errors())
        self.write_recipe(extra="SRCREV = '%s'" % ('0' * 40))
        self.assertTrue(any('SRCREV' in e for e in self.errors()), self.errors())
        # A line that looks like an assignment but cannot be read is reported.
        self.write_recipe(extra=f'SRC_URI += {unpinned}')
        self.assertTrue(any('unparsed assignment to source-affecting variable SRC_URI' in e for e in self.errors()),
                        self.errors())
        # A single-quoted non-source assignment stays accepted.
        self.write_recipe(extra="SUMMARY = 'SuiFleet daemon'\nCARGO_BUILD_FLAGS:append = ' --bin suifleet'")
        self.assertEqual(self.errors(), [])

    def test_bbappends_combine_with_their_recipe(self):
        unpinned = 'https://example.com/unpinned.tar.gz'
        self.write_recipe(extra='SRC_URI:append = "${HELPER}"\nHELPER = ""')
        report = audit.check(self.layer, repo=self.repo.root)
        self.assertEqual(report['errors'], [])
        base_sources = report['recipes']['suifleet']['sources']
        # An append assigning a helper the base recipe expands changes its sources.
        for name in ('suifleet_%.bbappend', 'suifleet_1.0.bbappend'):
            with self.subTest(name):
                append = self.recipe_dir / name
                append.write_text(f'HELPER = " {unpinned}"\n')
                report = audit.check(self.layer, repo=self.repo.root)
                self.assertTrue(any(f'lacks a SHA-256 pin: {unpinned}' in e for e in report['errors']), report['errors'])
                self.assertEqual(list(report['recipes']), ['suifleet'])
                self.assertEqual(report['recipes']['suifleet']['appends'], [f'recipes-edge/suifleet/{name}'])
                self.assertEqual(report['recipes']['suifleet']['sources'][:len(base_sources)], base_sources)
                # A conditional override in the append is rejected like one in the recipe.
                append.write_text(f'HELPER:raspberrypi3 = " {unpinned}"\n')
                self.assertTrue(any('conditional override HELPER:raspberrypi3' in e for e in self.errors()), self.errors())
                append.unlink()
        # A harmless append keeps the base report.
        append = self.recipe_dir / 'suifleet_%.bbappend'
        append.write_text('RDEPENDS:${PN} += "ca-certificates"\n')
        report = audit.check(self.layer, repo=self.repo.root)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['recipes']['suifleet']['sources'], base_sources)
        append.unlink()
        # An append for a recipe outside the layer cannot be audited.
        other = self.layer / 'recipes-core/busybox/busybox_%.bbappend'
        other.parent.mkdir(parents=True)
        other.write_text(f'SRC_URI += "{unpinned}"\n')
        self.assertTrue(any('busybox_%.bbappend: bbappend matches no recipe' in e for e in self.errors()), self.errors())
        # A recipe whose name merely shares a prefix without the separator does not match an exact append.
        self.assertFalse(audit.append_matches('suifleet_2.0.bbappend', 'suifleet_1.0.bb'))
        self.assertTrue(audit.append_matches('suifleet_%.bbappend', 'suifleet_1.0.bb'))

    def test_included_metadata_must_come_from_inside_the_layer(self):
        host = Path(self.tmp.name) / 'host-only.inc'
        host.write_text('SUMMARY = "host only"\nHELPER = " https://example.com/unpinned.tar.gz"\n')
        cases = {
            f'require {host}': 'resolves outside the layer',
            f'include {host}': 'resolves outside the layer',
            'require ../../../host-only.inc': 'resolves outside the layer',
            'include ${COREBASE}/meta/conf/x.inc': 'cannot be resolved inside the layer',
            'include not-here.inc': 'BitBake would search BBPATH outside it',
            'require not-here.inc': 'required file missing',
        }
        for line, message in cases.items():
            with self.subTest(line):
                self.write_recipe(extra=f'{line}\nSRC_URI:append = "${{HELPER}}"\nHELPER = ""')
                report = audit.check(self.layer)
                self.assertTrue(any(message in e and 'suifleet_1.0.bb' in e for e in report['errors']), report['errors'])
                # The out-of-layer file is never read: its unpinned source is not seen.
                self.assertFalse(any('unpinned' in e for e in report['errors']), report['errors'])
        # An in-layer symlink to an outside file is rejected after resolution.
        link = self.recipe_dir / 'linked.inc'
        link.symlink_to(host)
        self.write_recipe(extra='require linked.inc')
        self.assertTrue(any('require linked.inc resolves outside the layer' in e for e in self.errors()), self.errors())
        link.unlink()
        # A valid in-layer include is read normally, including from a subdirectory.
        (self.recipe_dir / 'inc').mkdir()
        (self.recipe_dir / 'inc/extra.inc').write_text('HELPER = " https://example.com/unpinned.tar.gz"\n')
        self.write_recipe(extra='SRC_URI:append = "${HELPER}"\nHELPER = ""\ninclude inc/extra.inc')
        errors = self.errors()
        self.assertTrue(any('lacks a SHA-256 pin: https://example.com/unpinned.tar.gz' in e for e in errors), errors)
        (self.recipe_dir / 'inc/extra.inc').write_text('SUMMARY = "in layer"\n')
        self.assertEqual(self.errors(), [])

    def test_file_sources_must_resolve_inside_the_layer(self):
        outside = Path(self.tmp.name) / 'outside.service'
        outside.write_text('[Unit]\n')
        service = self.recipe_dir / 'files/suifleet.service'
        service.unlink()
        service.symlink_to(outside)
        errors = self.errors()
        self.assertTrue(any('file:// source suifleet.service resolves outside the layer' in e for e in errors), errors)
        self.assertTrue(any('symlink leaves the layer: recipes-edge/suifleet/files/suifleet.service' in e for e in errors), errors)
        # A directory symlink leaving the layer is caught the same way.
        service.unlink()
        (self.recipe_dir / 'files').rmdir()
        (self.recipe_dir / 'files').symlink_to(outside.parent)
        (outside.parent / 'suifleet.service').write_text('[Unit]\n')
        self.assertTrue(any('resolves outside the layer' in e for e in self.errors()))
        # An ordinary in-layer symlink is accepted.
        (self.recipe_dir / 'files').unlink()
        (self.recipe_dir / 'files').mkdir()
        (self.recipe_dir / 'files/real.service').write_text('[Unit]\n')
        (self.recipe_dir / 'files/suifleet.service').symlink_to('real.service')
        self.assertEqual(self.errors(), [])

    def test_crates_need_checksums_and_must_match_the_lock(self):
        (self.recipe_dir / 'suifleet-crates.inc').write_text(CRATES_INC.replace('a' * 64, 'b' * 64))
        errors = self.errors(repo=self.repo.root)
        self.assertTrue(any('checksum differs' in e for e in errors), errors)
        (self.recipe_dir / 'suifleet-crates.inc').write_text(CRATES_INC.split('SRC_URI[')[0])
        self.assertTrue(any('crate anyhow 1.0.102 lacks a SHA-256' in e for e in self.errors()))

    def test_crate_registry_and_effective_checksum_must_match_the_lock(self):
        # The include is compared as BitBake fetches it: a crate from another
        # registry, or an inline checksum overriding the correct generated
        # flag, must not be reported as the verified Cargo.lock pin.
        inc = self.recipe_dir / 'suifleet-crates.inc'
        cases = {
            'other registry': (CRATES_INC.replace('crate://crates.io/', 'crate://example.invalid/'),
                               "registry 'example.invalid' is not crates.io"),
            'registry with a path': (CRATES_INC.replace('crate://crates.io/', 'crate://crates.io/mirror/'),
                                     "registry 'crates.io/mirror' is not crates.io"),
            'inline checksum': (CRATES_INC.replace('anyhow/1.0.102 ', f'anyhow/1.0.102;sha256sum={"0" * 64} '),
                                'crate source parameters the audit does not model: sha256sum'),
            'name parameter': (CRATES_INC.replace('anyhow/1.0.102 ', 'anyhow/1.0.102;name=other ')
                               + f'SRC_URI[other.sha256sum] = "{"a" * 64}"\n',
                               'crate source parameters the audit does not model: name'),
            'duplicate entry': (CRATES_INC.replace('anyhow/1.0.102 \\\n', 'anyhow/1.0.102 \\\n    crate://crates.io/anyhow/1.0.102 \\\n'),
                                'duplicate crate entry'),
        }
        for label, (text, expected) in cases.items():
            with self.subTest(label):
                self.assertNotEqual(text, CRATES_INC)
                inc.write_text(text)
                report = audit.check(self.layer, repo=self.repo.root)
                self.assertFalse(report['ok'], report['errors'])
                self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
                # The plain layer check refuses the form too, without the lock.
                if label != 'duplicate entry':
                    self.assertTrue(any(expected in e for e in self.errors()), self.errors())
        inc.write_text(CRATES_INC)
        self.assertEqual(self.errors(repo=self.repo.root), [])

    def test_lock_sources_other_than_crates_io_are_refused(self):
        self.assertEqual(audit.lock_packages(CARGO_LOCK), ({('anyhow', '1.0.102'): ('crates.io', 'a' * 64)}, []))
        other = CARGO_LOCK.replace('registry+https://github.com/rust-lang/crates.io-index',
                                   'registry+https://user:token@example.invalid/index')
        packages, unsupported = audit.lock_packages(other)
        self.assertEqual(packages, {})
        self.assertEqual(unsupported, ['anyhow 1.0.102 (registry+https://<redacted>@example.invalid/index)'])

    def test_lock_row_from_another_registry_is_refused_beside_the_crates_io_row(self):
        # Cargo package identity includes the source: an alternate-registry row
        # with the same name and version as the crates.io row must be refused
        # by the full audit, whichever order the rows appear in, even when the
        # recipe's lock digest, SRCREV and archive match the committed lock.
        alternate = ('\n[[package]]\nname = "anyhow"\nversion = "1.0.102"\n'
                     'source = "registry+https://example.invalid/index"\n'
                     f'checksum = "{"c" * 64}"\n')
        head, tail = CARGO_LOCK.split('\n[[package]]', 1)
        locks = {
            'alternate first': head + alternate + '\n[[package]]' + tail,
            'alternate last': CARGO_LOCK + alternate,
            'duplicate crates.io row': CARGO_LOCK + alternate.replace(
                'registry+https://example.invalid/index', 'registry+https://github.com/rust-lang/crates.io-index'),
        }
        expected = {'alternate first': 'anyhow 1.0.102 (registry+https://example.invalid/index)',
                    'alternate last': 'anyhow 1.0.102 (registry+https://example.invalid/index)',
                    'duplicate crates.io row': 'anyhow 1.0.102 (duplicate crates.io entry)'}
        for label, lock in locks.items():
            with self.subTest(label):
                self.repo.write('suifleet-edge/Cargo.lock', lock)
                rev = self.repo.commit()
                archive = Path(self.tmp.name) / f'suifleet-edge-{rev}.tar.gz'
                info = packager.build(self.repo.root, rev, archive)
                url = f'https://raw.githubusercontent.com/example/meta-suifleet/{"b" * 40}/{archive.name}'
                self.write_recipe(url=url, sha=info['sha256'], srcrev=rev,
                                  extra=f'SUIFLEET_LOCK_SHA256 = "{sha256(lock)}"')
                report = audit.check(self.layer, repo=self.repo.root)
                self.assertFalse(report['ok'], report['errors'])
                self.assertFalse(any('Cargo.lock sha256' in e for e in report['errors']), report['errors'])
                self.assertTrue(any('sources other than the crates.io registry' in e and expected[label] in e
                                    for e in report['errors']), report['errors'])
        # The unmodified lock at a fresh revision is clean again.
        self.repo.write('suifleet-edge/Cargo.lock', CARGO_LOCK)
        rev = self.repo.commit()
        archive = Path(self.tmp.name) / f'suifleet-edge-{rev}.tar.gz'
        info = packager.build(self.repo.root, rev, archive)
        self.write_recipe(url=f'https://raw.githubusercontent.com/example/meta-suifleet/{"b" * 40}/{archive.name}',
                          sha=info['sha256'], srcrev=rev)
        self.assertEqual(self.errors(repo=self.repo.root), [])

    def commit_lock_and_audit(self, lock):
        self.repo.write('suifleet-edge/Cargo.lock', lock)
        rev = self.repo.commit()
        archive = Path(self.tmp.name) / f'suifleet-edge-{rev}.tar.gz'
        info = packager.build(self.repo.root, rev, archive)
        url = f'https://raw.githubusercontent.com/example/meta-suifleet/{"b" * 40}/{archive.name}'
        self.write_recipe(url=url, sha=info['sha256'], srcrev=rev,
                          extra=f'SUIFLEET_LOCK_SHA256 = "{sha256(lock)}"')
        report = audit.check(self.layer, repo=self.repo.root)
        self.assertFalse(any('Cargo.lock sha256' in e for e in report['errors']), report['errors'])
        return report

    def test_lock_rows_are_read_as_toml_whatever_the_spelling(self):
        # Valid TOML may space or quote keys differently; the audit must read
        # the source field as Cargo does, not as one exact `key = "value"`
        # spelling, or the row would pass as a source-less workspace package.
        alternate = 'registry+https://example.invalid/index'
        spellings = {
            'no spaces': f'\n[[package]]\nname="anyhow"\nversion="1.0.102"\nsource="{alternate}"\nchecksum="{"c" * 64}"\n',
            'tab and wide spacing': f'\n[[package]]\nname = "anyhow"\nversion = "1.0.102"\n\tsource\t=   "{alternate}"\n',
            'quoted key': f'\n[[package]]\nname = "anyhow"\nversion = "1.0.102"\n"source" = "{alternate}"\n',
            'literal string': f"\n[[package]]\nname = 'anyhow'\nversion = '1.0.102'\nsource = '{alternate}'\n",
            'spaced table header': f'\n[[ package ]]\nname = "anyhow"\nversion = "1.0.102"\nsource = "{alternate}"\n',
        }
        for label, row in spellings.items():
            with self.subTest(label):
                report = self.commit_lock_and_audit(CARGO_LOCK + row)
                self.assertFalse(report['ok'], report['errors'])
                self.assertTrue(any('sources other than the crates.io registry' in e
                                    and f'anyhow 1.0.102 ({alternate})' in e for e in report['errors']),
                                report['errors'])
        # A crates.io row spelled differently is compared like the canonical one.
        respaced = CARGO_LOCK.replace('name = "anyhow"', 'name="anyhow"').replace('source = ', '"source"\t= ')
        self.assertNotEqual(respaced, CARGO_LOCK)
        self.assertEqual(audit.lock_packages(respaced), audit.lock_packages(CARGO_LOCK))
        wrong = respaced.replace('a' * 64, 'f' * 64)
        report = self.commit_lock_and_audit(wrong)
        self.assertTrue(any('checksum differs in' in e for e in report['errors']), report['errors'])
        # Syntax outside the strict reader, and non-string fields, are refused
        # instead of being skipped.
        refused = {
            'multi-line string': (CARGO_LOCK + f'\n[[package]]\nname = "anyhow"\nversion = "1.0.102"\nsource = """{alternate}"""\n',
                                  'Cargo.lock: unsupported TOML syntax'),
            'duplicate key': (CARGO_LOCK.replace('checksum = ', f'source = "{alternate}"\nchecksum = '),
                              'Cargo.lock: unsupported TOML syntax'),
            'non-string source': (CARGO_LOCK + '\n[[package]]\nname = "anyhow"\nversion = "1.0.102"\nsource = 1\n',
                                  'anyhow 1.0.102 (source/checksum not plain strings)'),
            'package table': ('version = 4\n\n[package]\nname = "anyhow"\n',
                              'package is not an array of [[package]] tables'),
        }
        for label, (lock, expected) in refused.items():
            with self.subTest(label):
                report = self.commit_lock_and_audit(lock)
                self.assertFalse(report['ok'], report['errors'])
                self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        report = self.commit_lock_and_audit(CARGO_LOCK)
        self.assertEqual(report['errors'], [])

    def test_coupled_pins_must_agree(self):
        self.write_recipe(extra='PV = "1.0"')
        self.assertTrue(any("PV '1.0' differs" in e for e in self.errors()))
        self.write_recipe(extra='SUIFLEET_LOCK_SHA256 = "%s"' % ('d' * 64))
        self.assertTrue(any('Cargo.lock sha256' in e for e in self.errors(repo=self.repo.root)))
        self.write_recipe()
        codec = self.layer / 'recipes-edge/suifleet-walrus-codec'
        codec.mkdir()
        (codec / 'suifleet-walrus-codec_1.0.bb').write_text(f'LICENSE = "MIT"\nSUIFLEET_CODEC_SOURCE_REV = "{"e" * 40}"\n')
        self.assertTrue(any('differs from daemon SRCREV' in e for e in self.errors()))

    def test_daemon_is_found_by_pn_and_its_pins_are_required(self):
        # Removing or blanking a pin must fail the audit, not skip the
        # Cargo.lock, crate include and layout checks it selects.
        recipe = self.recipe_dir / 'suifleet_1.0.bb'
        pinned = recipe.read_text()
        lock_line = f'SUIFLEET_LOCK_SHA256 = "{sha256(CARGO_LOCK)}"\n'
        cases = {
            'missing lock pin': (pinned.replace(lock_line, ''), 'lacks required SUIFLEET_LOCK_SHA256'),
            'empty lock pin': (pinned.replace(lock_line, 'SUIFLEET_LOCK_SHA256 = ""\n'), 'lacks required SUIFLEET_LOCK_SHA256'),
            'short lock pin': (pinned.replace(lock_line, f'SUIFLEET_LOCK_SHA256 = "{"a" * 63}"\n'), 'SUIFLEET_LOCK_SHA256'),
            'uppercase lock pin': (pinned.replace(lock_line, f'SUIFLEET_LOCK_SHA256 = "{sha256(CARGO_LOCK).upper()}"\n'),
                                   'is not a lowercase'),
            'missing crate version': (pinned.replace('SUIFLEET_CRATE_VERSION = "0.2.3"\n', 'PV = "0.2.3"\n'),
                                      'lacks required SUIFLEET_CRATE_VERSION'),
        }
        for name, (text, message) in cases.items():
            with self.subTest(name):
                self.assertNotEqual(text, pinned)
                recipe.write_text(text)
                report = audit.check(self.layer, repo=self.repo.root)
                self.assertTrue(any(message in e for e in report['errors']), report['errors'])
                # The repository comparison still runs for the daemon recipe.
                if 'lock pin' in name:
                    self.assertTrue(any('Cargo.lock sha256' in e for e in report['errors']), report['errors'])
        # A layer without the daemon recipe cannot be audited for its pins.
        recipe.unlink()
        self.assertTrue(any("no daemon recipe with PN 'suifleet'" in e for e in self.errors()))

    def test_coupled_pins_on_other_recipes_are_refused(self):
        other = self.layer / 'recipes-edge/other'
        other.mkdir()
        (other / 'other_1.0.bb').write_text(f'LICENSE = "MIT"\nSUIFLEET_LOCK_SHA256 = "{"a" * 64}"\n'
                                            f'SUIFLEET_CODEC_SOURCE_REV = "{self.rev}"\n')
        errors = self.errors()
        self.assertTrue(any("SUIFLEET_LOCK_SHA256 is only verified for PN 'suifleet'" in e for e in errors), errors)
        self.assertTrue(any("SUIFLEET_CODEC_SOURCE_REV is only verified for PN 'suifleet-walrus-codec'" in e
                            for e in errors), errors)
        (other / 'other_1.0.bb').unlink()
        codec = self.layer / 'recipes-edge/suifleet-walrus-codec'
        codec.mkdir()
        for value, message in (('', 'lacks required SUIFLEET_CODEC_SOURCE_REV'), ('e' * 39, 'not a full lowercase commit')):
            with self.subTest(codec_rev=value):
                (codec / 'suifleet-walrus-codec_1.0.bb').write_text(f'LICENSE = "MIT"\nSUIFLEET_CODEC_SOURCE_REV = "{value}"\n')
                self.assertTrue(any(message in e for e in self.errors()), self.errors())

    def test_runtime_state_in_layer_or_export_list_is_rejected(self):
        (self.recipe_dir / 'files/device.key').write_text('secret')
        (self.layer / 'export-files.json').write_text(json.dumps(['conf/layer.conf', 'journal.db']))
        errors = self.errors()
        self.assertTrue(any('state-like file in layer: recipes-edge/suifleet/files/device.key' in e for e in errors))
        self.assertTrue(any('export-files.json lists runtime identity/state-like file: journal.db' in e for e in errors))

    def fake_download(self, url, target):
        shutil.copyfile(self.archive, target)
        return sha256(self.archive.read_bytes())

    def test_fetch_unpacks_exact_layout_and_checks_it(self):
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])
        fetched = report['recipes']['suifleet']['fetched'][0]
        self.assertTrue(fetched['verified'])
        source = Path(report['fetch_dir']) / 'suifleet/source'
        self.assertTrue((source / 'suifleet-edge/windows-runtime/Cargo.toml').is_file())
        self.assertTrue((Path(report['fetch_dir']) / 'suifleet/suifleet.service').is_file())

    def test_recipes_sharing_one_pinned_archive_each_fetch_and_unpack_it(self):
        # BitBake shares one DL_DIR, so two recipes may consume the same
        # immutable URL and SHA-256. Each must unpack it independently, and an
        # earlier (even failed) fetch of the same file name must not decide a
        # later recipe's result. Only urllib's response is faked: download,
        # checksum and unpack run for real.
        data = self.archive.read_bytes()
        requested = []

        def urlopen(request, timeout=None):
            requested.append(request.full_url)
            return io.BytesIO(data)
        sibling = self.recipe_dir / 'suifleet-mirror_1.0.bb'
        self.addCleanup(sibling.unlink, missing_ok=True)
        for label, sibling_sha in (('same pin', self.info['sha256']), ('failing pin', 'f' * 64)):
            with self.subTest(label):
                # A plain (non-cargo) recipe that unpacks the daemon's archive.
                sibling.write_text(f'''SUMMARY = "fixture sharing the daemon archive"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5={md5(LICENSE)}"
SRC_URI = "{self.url};subdir=source;name=edge"
SRC_URI[edge.sha256sum] = "{sibling_sha}"
SUIFLEET_UNPACKDIR = "${{@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}}"
S = "${{SUIFLEET_UNPACKDIR}}/source"
''')
                requested.clear()
                with mock.patch.object(audit.urllib.request, 'urlopen', urlopen):
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
                self.assertFalse(any('File exists' in e for e in report['errors']), report['errors'])
                self.assertEqual(requested, [self.url, self.url], report['errors'])
                daemon = report['recipes']['suifleet']
                self.assertTrue(daemon['fetched'][0]['verified'], daemon)
                self.assertFalse(any(e.startswith('suifleet_1.0.bb:') for e in report['errors']), report['errors'])
                self.assertTrue((Path(report['fetch_dir']) / 'suifleet/source/LICENSE').is_file())
                mirror = report['recipes']['suifleet-mirror']['fetched'][0]
                if label == 'same pin':
                    self.assertEqual(report['errors'], [])
                    self.assertTrue(mirror['verified'])
                    self.assertTrue((Path(report['fetch_dir']) / 'suifleet-mirror/source/LICENSE').is_file())
                else:
                    self.assertFalse(mirror['verified'])
                    self.assertTrue(any(e.startswith('suifleet-mirror_1.0.bb:') and 'differs from pin' in e
                                        for e in report['errors']), report['errors'])
                    self.assertFalse(report['ok'])

    def test_license_and_cargo_paths_are_confined_to_the_unpack_root(self):
        # LIC_FILES_CHKSUM and CARGO_SRC_DIR are joined to the unpacked S; an
        # absolute or parent-relative operand (or another URL scheme) would make
        # the audit verify host files instead of the pinned source layout.
        # Disposable host-side decoys match what the audit would accept.
        outside = Path(tempfile.mkdtemp(dir=self.tmp.name, prefix='host-'))
        (outside / 'LICENSE').write_text(LICENSE)
        (outside / 'crate').mkdir()
        (outside / 'crate/Cargo.toml').write_text(CARGO_TOML)
        (outside / 'crate/Cargo.lock').write_text(CARGO_LOCK)
        license_cases = {
            'absolute': f'file://{outside}/LICENSE',
            'parent traversal': 'file://../../../' + os.path.relpath(outside / 'LICENSE', '/'),
            'single parent': 'file://../LICENSE',
            'interior parent': 'file://suifleet-edge/../../LICENSE',
            'other scheme': f'https://example.com/{outside.name}/LICENSE',
            'no scheme': 'LICENSE',
            'empty path': 'file://',
            'unexpanded': 'file://${TOPDIR}/LICENSE',
        }
        cargo_cases = {
            'absolute': str(outside / 'crate'),
            'parent traversal': '../../../' + os.path.relpath(outside / 'crate', '/'),
            'interior parent': 'suifleet-edge/../..',
            'empty': '',
            'S itself': '.',
            'S itself, trailing slash': './',
        }
        cases = [('LIC_FILES_CHKSUM', label, f'LIC_FILES_CHKSUM = "{value};md5={md5(LICENSE)}"')
                 for label, value in license_cases.items()]
        cases += [('CARGO_SRC_DIR', label, f'CARGO_SRC_DIR = "{value}"') for label, value in cargo_cases.items()]
        never = AssertionError('ran a subprocess with an unconfined path')
        for variable, label, extra in cases:
            with self.subTest(variable=variable, case=label):
                self.write_recipe(extra=extra)
                # Statically refused, so --fetch skips the recipe; the report
                # does not crash on the refused license entry.
                report = audit.check(self.layer)
                self.assertTrue(any(variable in e for e in report['errors']), report['errors'])
                with mock.patch.object(audit, 'download', self.fake_download):
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertEqual(report['recipes']['suifleet']['fetch_skipped'], 'static source findings')
                # Repository mode refuses before reading anything.
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                read = mock.Mock(side_effect=AssertionError('read an unconfined path'))
                errors = audit.daemon_errors(recipe, read)
                self.assertTrue(any(variable in e and 'source checks not run' in e for e in errors), errors)
                read.assert_not_called()
                # The fetcher itself refuses after unpacking, before any read,
                # listing or Cargo run, even when called without the static audit.
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', self.fake_download), \
                        mock.patch.object(audit, 'load_packager', side_effect=never):
                    errors, fetched = audit.fetch_recipe(recipe, workdir, cargo='cargo-must-not-run')
                self.assertTrue(fetched and fetched[0]['verified'], fetched)
                self.assertTrue(any(variable in e and 'source checks not run' in e for e in errors), errors)
        # A non-daemon recipe's license check is confined as well.
        sibling = self.recipe_dir / 'suifleet-mirror_1.0.bb'
        self.addCleanup(sibling.unlink, missing_ok=True)
        self.write_recipe()
        for label, value in license_cases.items():
            with self.subTest('generic recipe', case=label):
                sibling.write_text(f'''SUMMARY = "fixture sharing the daemon archive"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "{value};md5={md5(LICENSE)}"
SRC_URI = "{self.url};subdir=source;name=edge"
SRC_URI[edge.sha256sum] = "{self.info['sha256']}"
SUIFLEET_UNPACKDIR = "${{@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}}"
S = "${{SUIFLEET_UNPACKDIR}}/source"
''')
                report = audit.check(self.layer)
                self.assertTrue(any(e.startswith('suifleet-mirror_1.0.bb:') and 'LIC_FILES_CHKSUM' in e
                                    for e in report['errors']), report['errors'])
                self.assertIsNone(report['recipes']['suifleet-mirror']['license_files'])
                recipe = audit.Recipe(sibling, self.layer)
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', self.fake_download):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertTrue(fetched and fetched[0]['verified'], fetched)
                self.assertTrue(any('LIC_FILES_CHKSUM' in e for e in errors), errors)
        # The shipped form still passes the static and fetched checks.
        sibling.unlink()
        self.write_recipe()
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
        self.assertEqual(report['errors'], [])

    def test_source_path_rejects_resolution_outside_the_unpack_root(self):
        # Lexically plain paths can still resolve elsewhere through a link in
        # the unpacked tree; confinement is checked on the resolved path.
        base = Path(tempfile.mkdtemp(dir=self.tmp.name))
        unpack, outside = base / 'unpack', base / 'outside'
        (unpack / 'source').mkdir(parents=True)
        outside.mkdir()
        (outside / 'LICENSE').write_text(LICENSE)
        (unpack / 'source/escape').symlink_to(outside, target_is_directory=True)
        (unpack / 'source/sibling').symlink_to(unpack, target_is_directory=True)
        s_dir = (unpack / 'source').resolve()
        with self.assertRaisesRegex(ValueError, 'resolves outside the unpack directory'):
            audit.source_path(unpack, s_dir, 'escape/LICENSE', 'LIC_FILES_CHKSUM file')
        for bad in ('/etc/passwd', '../outside/LICENSE', '', 'a/../../b', '${S}/x', 'a\\..\\b'):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, 'not a plain relative path'):
                audit.source_path(unpack, s_dir, bad, 'LIC_FILES_CHKSUM file')
        # A link that stays inside the unpack root is accepted.
        self.assertEqual(audit.source_path(unpack, s_dir, 'sibling/source', 'path'), s_dir)
        # The md5 check reports an escaping license path instead of reading it.
        (self.recipe_dir / 'suifleet_1.0.bb').write_text(
            recipe_text(self.url, self.info['sha256'], self.rev,
                        f'LIC_FILES_CHKSUM = "file://escape/LICENSE;md5={md5(LICENSE)}"'))
        recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
        errors = audit.license_file_errors(recipe, s_dir, unpack)
        self.assertTrue(any('resolves outside the unpack directory' in e and 'not read' in e for e in errors), errors)

    def test_fetch_unpacks_by_the_downloadfilename_like_bitbake(self):
        # BitBake stores an https source under downloadfilename and picks the
        # unpack method from that name's suffix, not from the URL path.
        targets = []

        def recording_download(url, target):
            targets.append(Path(target).name)
            return self.fake_download(url, target)
        self.write_recipe(url=f'{self.url};downloadfilename=source.bin')
        self.assertEqual(self.errors(repo=self.repo.root), [])
        with mock.patch.object(audit, 'download', recording_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(targets, ['source.bin'])
        self.assertFalse(report['ok'])
        source = Path(report['fetch_dir']) / 'suifleet/source'
        # Copied as a loose file, exactly what a clean BitBake build would get.
        self.assertTrue((source / 'source.bin').is_file())
        self.assertFalse((source / 'LICENSE').exists())
        self.assertTrue(report['errors'], report)
        # A tarball downloadfilename (the published recipe's form) is extracted.
        targets.clear()
        self.write_recipe(url=f'{self.url};downloadfilename=suifleet-edge-{self.rev}-workspace.tar.gz')
        with mock.patch.object(audit, 'download', recording_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])
        self.assertEqual(targets, [f'suifleet-edge-{self.rev}-workspace.tar.gz'])
        self.assertTrue((Path(report['fetch_dir']) / 'suifleet/source/LICENSE').is_file())
        self.assertEqual(report['recipes']['suifleet']['sources'][0]['file'], targets[0])
        # The local name must agree with SRCREV too, and must be a plain name.
        self.write_recipe(url=f'{self.url};downloadfilename=suifleet-edge-{"c" * 40}.tar.gz')
        self.assertTrue(any(f'names commit {"c" * 40}' in e for e in self.errors()), self.errors())
        for bad in ('../escape.tar.gz', 'dir/x.tar.gz', '..'):
            with self.subTest(bad):
                self.write_recipe(url=f'{self.url};downloadfilename={bad}')
                self.assertTrue(any('no usable local file name' in e for e in self.errors()), self.errors())
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(any('nothing fetched' in e for e in errors), errors)

    def test_local_files_keep_their_url_directories_like_bitbake(self):
        # BitBake's FetchMethod.unpack copies file://nested/NOTICE to
        # subdir/nested/NOTICE, so a license reference must use that path.
        notice = 'nested notice\n'
        (self.recipe_dir / 'files/nested').mkdir()
        (self.recipe_dir / 'files/nested/NOTICE').write_text(notice)

        def fetch(license_path):
            self.write_recipe(extra=f'SRC_URI += "file://nested/NOTICE;subdir=source"\n'
                                    f'LIC_FILES_CHKSUM = "file://LICENSE;md5={md5(LICENSE)} '
                                    f'file://{license_path};md5={md5(notice)}"\n')
            with mock.patch.object(audit, 'download', self.fake_download):
                report = audit.check(self.layer, fetch=True)
            self.addCleanup(shutil.rmtree, report['fetch_dir'])
            return report, Path(report['fetch_dir']) / 'suifleet/source'
        report, source = fetch('nested/NOTICE')
        self.assertEqual(report['errors'], [])
        self.assertTrue((source / 'nested/NOTICE').is_file())
        self.assertFalse((source / 'NOTICE').exists())
        # The former false positive: a top-level reference BitBake would not satisfy.
        report, source = fetch('NOTICE')
        self.assertFalse(report['ok'])
        self.assertTrue(any('license file NOTICE does not match' in e for e in report['errors']), report['errors'])
        self.assertFalse((source / 'NOTICE').exists())

    def test_local_file_parameters_the_audit_does_not_model_are_refused(self):
        # BitBake's FetchData.setup_localpath uses an explicit localpath instead
        # of the layer-relative file, so the audit must not vouch for the
        # in-layer NOTICE while BitBake reads a different (here missing) path.
        (self.recipe_dir / 'files/NOTICE').write_text('notice\n')
        for params in (f';localpath={self.tmp.name}/missing/NOTICE', ';downloadfilename=OTHER',
                       ';striplevel=1', ';unpack=yes'):
            with self.subTest(params):
                self.write_recipe(extra=f'SRC_URI += "file://NOTICE{params}"\n')
                self.assertTrue(any('file:// source parameters the audit does not model' in e
                                    for e in self.errors()), self.errors())
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(any('file:// source parameters the audit does not model' in e
                                    and 'nothing fetched' in e for e in errors), errors)
                self.assertFalse((workdir / 'suifleet').exists())
                with mock.patch.object(audit, 'download', self.fake_download):
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertFalse(report['ok'])
        # The modelled parameters still pass: subdir and unpack=0/1.
        self.write_recipe(extra='SRC_URI += "file://NOTICE;subdir=source;unpack=1"\n')
        self.assertEqual(self.errors(), [])
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])
        self.assertTrue((Path(report['fetch_dir']) / 'suifleet/source/NOTICE').is_file())

    def test_metadata_that_runs_after_parsing_is_refused(self):
        # BitBake runs anonymous Python and event handlers after parsing, and
        # they can rewrite SRC_URI. The reader does not execute them, so a
        # recipe that relies on them must fail before anything is fetched.
        added = 'https://example.invalid/unpinned.tar.gz'
        anonymous = 'python () {\n    d.appendVar("SRC_URI", " %s")\n}\n' % added
        cases = {
            'recipe anonymous': ('bb', anonymous),
            'named anonymous': ('bb', anonymous.replace('python () {', 'python __anonymous() {')),
            'fakeroot anonymous': ('bb', anonymous.replace('python () {', 'fakeroot python(){')),
            'include anonymous': ('inc', anonymous),
            'append anonymous': ('bbappend', anonymous),
            'event handler': ('bb', 'addhandler suifleet_rewrite\n'),
            'in-layer class': ('bb', 'inherit suifleet-rewrite\n'),
            'unresolved class': ('bb', 'inherit ${SUIFLEET_CLASS}\n'),
        }
        (self.layer / 'classes').mkdir(exist_ok=True)
        (self.layer / 'classes/suifleet-rewrite.bbclass').write_text(anonymous)
        self.addCleanup((self.layer / 'classes/suifleet-rewrite.bbclass').unlink)
        include = self.recipe_dir / 'extra.inc'
        append = self.recipe_dir / 'suifleet_1.0.bbappend'
        for label, (where, text) in cases.items():
            with self.subTest(label):
                include.unlink(missing_ok=True)
                append.unlink(missing_ok=True)
                if where == 'bb':
                    self.write_recipe(extra=text)
                elif where == 'inc':
                    include.write_text(text)
                    self.write_recipe(extra='require extra.inc\n')
                else:
                    append.write_text(text)
                    self.write_recipe()
                self.assertTrue(any('runs after parsing' in e for e in self.errors()), self.errors())
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer,
                                      [append] if append.exists() else ())
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(any('runs after parsing' in e and 'nothing fetched' in e for e in errors), errors)
                self.assertFalse((workdir / 'suifleet').exists())
                with mock.patch.object(audit, 'download', self.fake_download):
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertFalse(report['ok'])
                self.assertIsNone(report['recipes']['suifleet']['fetched'])
        include.unlink(missing_ok=True)
        append.unlink(missing_ok=True)
        # Named task functions, inheriting classes from other layers and a
        # Python function that only mentions anonymous in its name still pass.
        self.write_recipe(extra='inherit cargo\npython do_verify_lock() {\n    pass\n}\n'
                                'python do_anonymous_check() {\n    pass\n}\ndo_install() {\n    true\n}\n')
        self.assertEqual(self.errors(), [])


    def test_read_only_skip_guards_are_accepted(self):
        # A guard that only reads the datastore and raises SkipRecipe changes
        # whether the recipe builds, never its pins: it is reported, not refused,
        # wherever it appears, and the recipe is still fetched and verified.
        include = self.recipe_dir / 'extra.inc'
        append = self.recipe_dir / 'suifleet_1.0.bbappend'
        self.addCleanup(include.unlink, missing_ok=True)
        self.addCleanup(append.unlink, missing_ok=True)
        include.write_text(CODEC_GUARD)
        append.write_text(NODE_GUARD)
        self.write_recipe(extra=NODE_GUARD + CODEC_GUARD + 'require extra.inc\n')
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])
        entry = report['recipes']['suifleet']
        self.assertEqual(entry['skip_guards'], [
            'recipes-edge/suifleet/suifleet_1.0.bb: anonymous Python function (python ())',
            'recipes-edge/suifleet/suifleet_1.0.bb: anonymous Python function (python ())',
            'recipes-edge/suifleet/extra.inc: anonymous Python function (python ())',
            'recipes-edge/suifleet/suifleet_1.0.bbappend: anonymous Python function (python ())'])
        self.assertTrue(entry['fetched'])
        # The reader still sees the assignments after each guard.
        self.write_recipe(extra=CODEC_GUARD + 'SRC_URI += "https://example.invalid/unpinned.tar.gz"\n')
        self.assertTrue(any('unpinned.tar.gz' in e for e in self.errors()), self.errors())

    def test_anonymous_python_that_could_rewrite_pins_is_refused(self):
        # Each variant changes one thing in an accepted guard so that it could
        # write the datastore (or escape the model); all must stay refused and
        # nothing may be fetched.
        insert = '    missing = []\n'
        assert insert in CODEC_GUARD
        additions = {
            'setVar': '    d.setVar("SRCREV", "x")\n',
            'appendVar': '    d.appendVar("SRC_URI", " https://example.invalid/x.tar.gz")\n',
            'setVarFlag': '    d.setVarFlag("SRC_URI", "sha256sum", "0")\n',
            'delVar': '    d.delVar("SRC_URI")\n',
            'renameVar': '    d.renameVar("SRC_URI", "OLD")\n',
            'method reference': '    f = d.getVar\n',
            'datastore alias': '    data = d\n',
            'datastore in container': '    data = [d]\n',
            'datastore to other helper': '    bb.data.setVar("SRCREV", "x", d)\n',
            'datastore to lib function': '    oe.utils.rewrite(d)\n',
            'datastore to local function': '    helper(d)\n',
            'datastore format escape': '    "{0.__class__}".format(d)\n',
            'getattr on datastore': '    getattr(d, "setVar")("SRCREV", "x")\n',
            'fetcher datastore': '    bb.fetch2.Fetch([], d, localonly=True).d.setVar("SRCREV", "x")\n',
            'non-local fetch': '    bb.fetch2.Fetch(["https://example.invalid/x"], d)\n',
            'search rebound': '    search = helper\n    search(ud, d)\n',
            'exec': '    exec("d.setVar(1, 2)")\n',
            'eval': '    eval("1")\n',
            'import': '    import oe.utils\n',
            'from import': '    from bb import data\n',
            'os process': '    os.system("true")\n',
            'os environ': '    os.environ["X"] = "1"\n',
            'other bb name': '    bb.build.exec_func("do_x", d)\n',
            'dunder attribute': '    x = ().__class__\n',
            'nested function': '    def f():\n        pass\n',
            'lambda': '    f = lambda: None\n',
            'global': '    global SRC_URI\n',
            'rebind datastore': '    d = None\n',
            'rebind bb': '    bb = None\n',
            'syntax error': '    if :\n',
            'explicit inline python expansion': '    d.expand("${@d.setVar(\'SRCREV\', \'x\')}")\n',
            'expand of a reference': '    d.expand("${SRCREV}")\n',
            'computed variable name': '    name = "SRC_URI"\n    d.getVar(name)\n',
            'computed flag name': '    flag = "sha256sum"\n    d.getVarFlag("SRC_URI", flag)\n',
            'computed expand flag': '    d.getVar("SRC_URI", expand=local)\n',
            'starred read arguments': '    d.getVar(*["SRC_URI"])\n',
            'extra read argument': '    d.getVar("SRC_URI", True, True)\n',
            'other read keyword': '    d.getVar("SRC_URI", parsing=True)\n',
            'non-string variable name': '    d.getVar(1)\n',
        }
        for label, addition in additions.items():
            with self.subTest(label):
                self.write_recipe(extra=CODEC_GUARD.replace(insert, insert + addition))
                errors = self.errors()
                self.assertTrue(any('not a read-only skip guard' in e for e in errors), errors)
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                self.assertEqual(recipe.guards, [])
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(any('runs after parsing' in e and 'nothing fetched' in e for e in errors), errors)
        # An unterminated function is refused too.
        self.write_recipe(extra='python () {\n    pass\n')
        self.assertTrue(any('without a closing brace' in e for e in self.errors()), self.errors())

    def test_only_reviewed_guard_forms_are_accepted(self):
        # The guard grammar is positive: a receiver, call or statement outside
        # the reviewed SF-307 forms is refused even when it looks read-only,
        # and nothing is fetched. Comments and skip messages may differ.
        fetch = 'bb.fetch2.Fetch(local, d, cache=False, localonly=True)'
        search = "getattr(ud.method, 'localfile_searchpaths', None) or ud.method.localpaths"
        node_check = "if d.getVar('TCLIBC') != 'glibc':"
        variants = {
            'other fetch receiver': (CODEC_GUARD, 'ud = fetcher.ud[entry]', 'ud = helper.ud[entry]'),
            'fetch method replaced': (CODEC_GUARD, 'bb.fetch2.Fetch(local', 'bb.fetch2.FetchData(local'),
            'fetch extra keyword': (CODEC_GUARD, fetch, fetch[:-1] + ', connectivity=True)'),
            'fetch of other URLs': (CODEC_GUARD, 'Fetch(local, d', 'Fetch(urls, d'),
            'search on other receiver': (CODEC_GUARD, search, search.replace('ud.method', 'helper.method')),
            'other method attribute': (CODEC_GUARD, search, search.replace('ud.method.localpaths', 'ud.method.download')),
            'search with other arguments': (CODEC_GUARD, 'search(ud, d)', 'search(ud, d, True)'),
            'datastore to other call': (CODEC_GUARD, 'bb.parse.mark_dependency(d, path)', 'bb.utils.mkdirhier(d, path)'),
            'extra statement in block': (CODEC_GUARD, '            searched = search(ud, d)\n',
                                         '            searched = search(ud, d)\n            ud.method.urldata_init(ud, d)\n'),
            'call in condition': (NODE_GUARD, node_check, "if helper(d.getVar('TCLIBC')) != 'glibc':"),
            'os call in condition': (NODE_GUARD, node_check, "if os.system('true'):"),
            'non-literal comparison': (NODE_GUARD, node_check, "if d.getVar('TCLIBC') != d.getVar('X'):"),
            'chained comparison': (NODE_GUARD, node_check, "if 'a' < d.getVar('TCLIBC') < 'z':"),
            'computed read name': (NODE_GUARD, node_check, "if d.getVar('TC' + 'LIBC') != 'glibc':"),
            'call in message': (NODE_GUARD, "% d.getVar('HOST_SYS'))", '% helper(d))'),
            'raise other exception': (NODE_GUARD, 'raise bb.parse.SkipRecipe(', 'raise bb.parse.ParseError('),
            'statement beside raise': (NODE_GUARD, "        raise bb.parse.SkipRecipe('the official Node.js linux-arm64 binary requires glibc",
                                       "        bb.warn(d.getVar('TCLIBC'))\n        raise bb.parse.SkipRecipe('x"),
            'else branch': (NODE_GUARD, "% d.getVar('TCLIBC'))\n}", "% d.getVar('TCLIBC'))\n    else:\n        bb.note('ok')\n}"),
            'top-level read binding': (NODE_GUARD, '    ' + node_check, "    x = d.getVar('TCLIBC')\n    " + node_check),
            'top-level log call': (NODE_GUARD, '    ' + node_check, "    bb.note(d.getVar('TCLIBC'))\n    " + node_check),
            'import alias': (CODEC_GUARD, '    import os\n', '    import os as path\n'),
            'other import': (CODEC_GUARD, '    import os\n', '    import os, oe\n'),
            'empty body': ('python () {\n}\n', None, None),
        }
        for label, (guard, old, new) in variants.items():
            with self.subTest(label):
                if old is not None:
                    self.assertIn(old, guard)
                    guard = guard.replace(old, new, 1)
                self.write_recipe(extra=guard)
                errors = self.errors()
                self.assertTrue(any('not a read-only skip guard' in e for e in errors), errors)
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                self.assertEqual(recipe.guards, [])
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
        # Reworded messages, changed comments and other literal platform
        # checks remain the reviewed forms.
        reworded = (CODEC_GUARD.replace('optional Walrus codec inputs not found', 'inputs missing')
                    .replace('# Renamed from localpaths after BitBake 2.0.', '# Renamed later.')
                    .replace(node_check.replace('TCLIBC', 'HOST_ARCH').replace('glibc', 'aarch64'),
                             "if not (d.getVar('HOST_ARCH') in 'aarch64' and d.getVar('MACHINE', True) == 'x'):"))
        self.write_recipe(extra=NODE_GUARD + reworded)
        self.assertEqual(self.errors(), [])

    def test_inline_python_reached_by_a_guard_read_is_refused(self):
        # A guard's literal reads still expand their values, and BitBake runs
        # inline Python found on the way, directly or through references, so
        # inline Python anywhere in the audited metadata is refused before any
        # download, with or without a guard. The UNPACKDIR idiom stays allowed.
        include = self.recipe_dir / 'extra.inc'
        append = self.recipe_dir / 'suifleet_1.0.bbappend'
        self.addCleanup(include.unlink, missing_ok=True)
        self.addCleanup(append.unlink, missing_ok=True)
        payload = "${@d.setVar('SRCREV', 'c' * 40) or 'glibc'}"
        cases = {
            'read directly': (f'TCLIBC = "{payload}"\n', None, None),
            'read transitively': (f'SUIFLEET_LIBC = "{payload}"\nTCLIBC = "${{SUIFLEET_LIBC}}"\n', None, None),
            'in an include': ('require extra.inc\n', f'TCLIBC = "{payload}"\n', None),
            'in a bbappend': ('', None, f'TCLIBC:append = "{payload}"\n'),
            'in a flag': (f'TCLIBC[doc] = "{payload}"\n', None, None),
            'split by a continuation': ('TCLIBC = "$\\\n{@d.setVar(\'SRCREV\', \'x\')}"\n', None, None),
            'in a task body': (f'do_install() {{\n    echo {payload}\n}}\n', None, None),
        }
        for label, (extra, inc, app) in cases.items():
            for guard in (CODEC_GUARD, ''):
                with self.subTest(label, guarded=bool(guard)):
                    for path, text in ((include, inc), (append, app)):
                        if text is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.write_text(text)
                    self.write_recipe(extra=guard + extra)
                    errors = self.errors()
                    self.assertTrue(any('uses inline Python' in e for e in errors), errors)
                    self.assertFalse(any(payload in e for e in errors), errors)
                    recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer,
                                          [append] if app is not None else [])
                    workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                    with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                        errors, fetched = audit.fetch_recipe(recipe, workdir)
                    self.assertEqual(fetched, [])
                    self.assertTrue(any('inline Python' in e and 'nothing fetched' in e for e in errors), errors)
        include.unlink(missing_ok=True)
        append.unlink(missing_ok=True)
        # Both SF-307 guards with the UNPACKDIR idiom still pass.
        self.write_recipe(extra=NODE_GUARD + CODEC_GUARD
                          + 'SUIFLEET_UNPACKDIR = "${@d.getVar(\'UNPACKDIR\') or d.getVar(\'WORKDIR\')}"\n')
        self.assertEqual(self.errors(), [])

    def recipe(self):
        return audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer, [])

    def test_continuations_join_physical_lines_as_bitbake_does(self):
        # BitBake (BBHandler.feeder, ConfHandler.handle) strips each physical
        # line's trailing whitespace, drops a final backslash and concatenates
        # the next line with its indentation, inserting nothing. An unspaced
        # continuation is therefore one token, never two sources.
        for name in ('first', 'second', 'firstsecond'):
            (self.recipe_dir / 'files' / name).write_text(name)
        sources = lambda: [url for url, _ in self.recipe().sources() if url.startswith('file://')]
        self.write_recipe(extra='SRC_URI:append = " file://first\\\nfile://second"\n')
        self.assertIn('file://firstfile://second', sources())
        self.assertNotIn('file://second', sources())
        errors = self.errors()
        self.assertTrue(any('firstfile://second' in e for e in errors), errors)
        # The concatenated token is fetched when it names a layer file.
        self.write_recipe(extra='SRC_URI:append = " file://first\\\nsecond"\n')
        self.assertIn('file://firstsecond', sources())
        self.assertEqual(self.errors(), [])
        # Indentation and whitespace before the backslash are kept, and
        # whitespace after it is stripped, so these lines still continue.
        for label, extra in {
            'indented': 'SRC_URI:append = " file://first \\\n    file://second"\n',
            'tab': 'SRC_URI:append = " file://first\\\n\tfile://second"\n',
            'trailing whitespace': 'SRC_URI:append = " file://first \\  \t\n    file://second"\n',
        }.items():
            with self.subTest(label):
                self.write_recipe(extra=extra)
                self.assertEqual(sources()[-2:], ['file://first', 'file://second'])
                self.assertEqual(self.errors(), [])
        for name in ('first', 'second', 'firstsecond'):
            (self.recipe_dir / 'files' / name).unlink()

    def test_continued_include_operands_and_pins_are_concatenated(self):
        include = self.recipe_dir / 'first.inc'
        include.write_text('SUIFLEET_FROM_INCLUDE = "yes"\n')
        self.addCleanup(include.unlink)
        self.write_recipe(extra='require fir\\\nst.inc\n')
        self.assertEqual(self.recipe().get('SUIFLEET_FROM_INCLUDE'), 'yes')
        self.assertEqual(self.errors(), [])
        self.write_recipe(extra='require fir \\\nst.inc\n')
        errors = self.errors()
        self.assertTrue(any('required file missing: fir' in e for e in errors), errors)
        # A pin split without a space is the whole pin; with one it is not.
        self.write_recipe(extra=f'SRCREV = "{self.rev[:20]}\\\n{self.rev[20:]}"\n')
        self.assertEqual(self.recipe().get('SRCREV'), self.rev)
        self.assertEqual(self.errors(), [])
        self.write_recipe(extra=f'SRCREV = "{self.rev[:20]} \\\n{self.rev[20:]}"\n')
        self.assertEqual(self.recipe().get('SRCREV'), f'{self.rev[:20]} {self.rev[20:]}')

    def test_function_bodies_are_raw_and_every_anonymous_form_is_found(self):
        # BitBake keeps function body lines raw: a body line ending in a
        # backslash does not swallow the closing brace or later statements.
        missing = 'SRC_URI:append = " file://not-in-layer"\n'
        self.write_recipe(extra='do_install() {\n    install -d \\\n}\n' + missing)
        self.assertIn('file://not-in-layer', [url for url, _ in self.recipe().sources()])
        errors = self.errors()
        self.assertTrue(any('not-in-layer' in e for e in errors), errors)
        # A function without a name or named __anonymous is anonymous Python
        # whether or not it is marked python (ast.MethodNode).
        body = '    d.appendVar("SRC_URI", " file://not-in-layer")\n}\n'
        for header in ('() {', '__anonymous () {', 'python fakeroot () {', 'fakeroot python(){'):
            with self.subTest(header):
                self.write_recipe(extra=header + '\n' + body)
                errors = self.errors()
                self.assertTrue(any('anonymous Python function' in e for e in errors), errors)
        # Python def bodies are kept raw too and are not read as assignments.
        self.write_recipe(extra='def helper(d):\n    SRCREV = "x" \\\n        "y"\n    return SRCREV\n')
        self.assertEqual(self.recipe().get('SRCREV'), self.rev)
        self.assertEqual(self.errors(), [])

    def test_continuations_bitbake_refuses_are_reported(self):
        for label, extra in {
            'comment into code': '# note \\\nSRC_URI:append = " file://not-in-layer"\n',
            'code into comment': 'SRC_URI:append = " \\\n# file://not-in-layer"\n',
            'end of file': 'SUMMARY = "x \\',
        }.items():
            with self.subTest(label):
                self.write_recipe(extra=extra)
                errors = self.errors()
                self.assertTrue(any('BitBake refuses to parse' in e for e in errors), errors)
                with self.assertRaisesRegex(ValueError, 'refuses to parse'):
                    audit.unpack_plan(self.recipe(), Path(self.tmp.name) / 'unpack')

    def test_every_include_operand_is_read_and_checked(self):
        # BitBake expands the whole include/require operand and includes every
        # file name in it; a valid first file must not hide the others.
        first = self.recipe_dir / 'first.inc'
        first.write_text('SUMMARY = "first"\n')
        second = self.recipe_dir / 'second.inc'
        self.addCleanup(first.unlink)
        self.addCleanup(second.unlink, missing_ok=True)
        cases = {
            'require first.inc missing.inc': 'required file missing: missing.inc',
            'include first.inc missing.inc': 'missing.inc is not a file in the layer',
            f'require first.inc {Path(self.tmp.name) / "host.inc"}': 'resolves outside the layer',
            'require first.inc ${COREBASE}/x.inc': 'cannot be resolved inside the layer',
        }
        for line, message in cases.items():
            with self.subTest(line):
                self.write_recipe(extra=line)
                errors = self.errors()
                self.assertTrue(any(message in e for e in errors), errors)
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(any(message in e and 'nothing fetched' in e for e in errors), errors)
                with mock.patch.object(audit, 'download', self.fake_download):
                    report = audit.check(self.layer, repo=self.repo.root, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertFalse(report['ok'])
        # Source metadata in a second file, or in a file named by an expanded
        # variable, is read like the first.
        second.write_text('SRC_URI += "https://example.com/unpinned.tar.gz"\n')
        for line in ('require first.inc second.inc', 'SUIFLEET_INCS = "first.inc second.inc"\ninclude ${SUIFLEET_INCS}'):
            with self.subTest(line):
                self.write_recipe(extra=line)
                errors = self.errors()
                self.assertTrue(any('lacks a SHA-256 pin: https://example.com/unpinned.tar.gz' in e for e in errors), errors)
        second.write_text('SUMMARY = "second"\n')
        self.write_recipe(extra='require first.inc second.inc')
        self.assertEqual(self.errors(), [])

    def test_conditional_override_cannot_select_an_include(self):
        # BitBake expands an include/require operand with the active overrides,
        # so SOURCE_SETTINGS:qemuarm64 would read a different file (and its
        # sources) on that machine. The audit evaluates no overrides, so every
        # variable an operand references, directly or through other variables,
        # is source-affecting and a conditional override of it is refused.
        benign = self.recipe_dir / 'benign.inc'
        benign.write_text('SUMMARY = "benign"\n')
        unsafe = self.recipe_dir / 'unsafe.inc'
        unsafe.write_text('SRC_URI += "http://example.com/unpinned.tar.gz"\n')
        self.addCleanup(benign.unlink)
        self.addCleanup(unsafe.unlink)
        # Positive controls: unconditional variable-driven selection is read
        # normally, and a machine override of an unrelated variable is accepted.
        for extra in ('SOURCE_SETTINGS = "benign.inc"\ninclude ${SOURCE_SETTINGS}',
                      'SOURCE_CHOICE = "benign"\nSOURCE_SETTINGS := "${SOURCE_CHOICE}.inc"\n'
                      'UNRELATED:qemuarm64 = "x"\ninclude ${SOURCE_SETTINGS}',
                      'SOURCE_SETTINGS = "benign.inc"\nUNRELATED:qemuarm64 = "x"\nrequire ${SOURCE_SETTINGS}'):
            with self.subTest(extra):
                self.write_recipe(extra=extra)
                self.assertEqual(self.errors(), [])
        cases = {
            'direct': ('SOURCE_SETTINGS = "benign.inc"\nSOURCE_SETTINGS:qemuarm64 = "unsafe.inc"\n'
                       'include ${SOURCE_SETTINGS}', 'SOURCE_SETTINGS:qemuarm64'),
            'override before the include, require': ('SOURCE_SETTINGS:qemuarm64 = "unsafe.inc"\n'
                                                     'SOURCE_SETTINGS = "benign.inc"\nrequire ${SOURCE_SETTINGS}',
                                                     'SOURCE_SETTINGS:qemuarm64'),
            'conditional append': ('SOURCE_SETTINGS = "benign.inc"\nSOURCE_SETTINGS:append:qemuarm64 = " unsafe.inc"\n'
                                   'include ${SOURCE_SETTINGS}', 'SOURCE_SETTINGS:append:qemuarm64'),
            # Transitive at the point of expansion: SOURCE_SETTINGS referenced
            # SOURCE_CHOICE when the include ran, even though it is replaced later.
            'transitive, later reassigned': ('SOURCE_CHOICE = "benign"\nSOURCE_CHOICE:qemuarm64 = "unsafe"\n'
                                             'SOURCE_SETTINGS = "${SOURCE_CHOICE}.inc"\ninclude ${SOURCE_SETTINGS}\n'
                                             'SOURCE_SETTINGS = "benign.inc"', 'SOURCE_CHOICE:qemuarm64'),
            # BitBake's := expansion already sees active overrides, so an
            # immediately expanded alias still depends on its selector even
            # though the stored value no longer references it.
            'immediate alias': ('SOURCE_CHOICE = "benign"\nSOURCE_CHOICE:qemuarm64 = "unsafe"\n'
                                'SOURCE_SETTINGS := "${SOURCE_CHOICE}.inc"\ninclude ${SOURCE_SETTINGS}',
                                'SOURCE_CHOICE:qemuarm64'),
            'chained immediate aliases': ('SOURCE_CHOICE = "benign"\nSOURCE_CHOICE:qemuarm64 = "unsafe"\n'
                                          'SOURCE_STEM := "${SOURCE_CHOICE}"\nSOURCE_SETTINGS = "${SOURCE_STEM}.inc"\n'
                                          'SOURCE_ALIAS := "${SOURCE_SETTINGS}"\nrequire ${SOURCE_ALIAS}',
                                          'SOURCE_CHOICE:qemuarm64'),
            'immediate append': ('SOURCE_CHOICE = ""\nSOURCE_CHOICE:qemuarm64 = "unsafe.inc"\n'
                                 'SOURCE_SETTINGS = "benign.inc"\nSOURCE_SETTINGS:append := " ${SOURCE_CHOICE}"\n'
                                 'include ${SOURCE_SETTINGS}', 'SOURCE_CHOICE:qemuarm64'),
        }
        for label, (extra, message) in cases.items():
            with self.subTest(label):
                self.write_recipe(extra=extra)
                errors = self.errors()
                self.assertTrue(any(f'conditional override {message}' in e for e in errors), errors)
                # The finding stops the fetch before any download.
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    report = audit.check(self.layer, repo=self.repo.root, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertFalse(report['ok'])
                self.assertIsNone(report['recipes']['suifleet']['fetched'])
                self.assertEqual(report['recipes']['suifleet']['fetch_skipped'], 'static source findings')

    def test_immediate_expansion_keeps_source_provenance(self):
        # A source-affecting variable assigned with := stores the expanded
        # text, but BitBake expanded it with the active overrides, so the
        # variables it was expanded from stay source-affecting.
        self.write_recipe(extra='NOTE_CHOICE = "a"\nSUIFLEET_NOTE := "${NOTE_CHOICE}"\nUNRELATED:qemuarm64 = "x"')
        self.assertEqual(self.errors(), [])
        cases = {
            'variable': 'NOTE_CHOICE = "a"\nNOTE_CHOICE:qemuarm64 = "b"\nSUIFLEET_NOTE := "${NOTE_CHOICE}"',
            'through a lazy variable': ('NOTE_CHOICE = "a"\nNOTE_CHOICE:qemuarm64 = "b"\nNOTE_STEM = "${NOTE_CHOICE}"\n'
                                        'SUIFLEET_NOTE := "${NOTE_STEM}"'),
            'flag': 'NOTE_CHOICE = "a"\nNOTE_CHOICE:qemuarm64 = "b"\nSUIFLEET_NOTE[doc] := "${NOTE_CHOICE}"',
        }
        for label, extra in cases.items():
            with self.subTest(label):
                self.write_recipe(extra=extra)
                errors = self.errors()
                self.assertTrue(any('conditional override NOTE_CHOICE:qemuarm64' in e for e in errors), errors)
        self.write_recipe()
        self.assertEqual(self.errors(), [])

    def test_unmodelled_directives_are_refused(self):
        # unset removes a variable or flag BitBake would otherwise use; the
        # reader does not model it (nor include_all, addfragments, addpylib),
        # so a recipe using them must fail before anything is fetched.
        include = self.recipe_dir / 'extra.inc'
        append = self.recipe_dir / 'suifleet_1.0.bbappend'
        cases = {
            'unset variable': ('bb', 'unset SRC_URI'),
            'unset flag': ('bb', 'unset SRC_URI[sha256sum]'),
            'unset in include': ('inc', 'unset SRCREV'),
            'unset in append': ('bbappend', 'unset SRC_URI'),
            'include_all': ('bb', 'include_all conf/distro/include/x.inc'),
            'addpylib': ('bb', 'addpylib ${LAYERDIR}/lib suifleet'),
            'addfragments': ('bb', 'addfragments conf/fragments OE_FRAGMENTS'),
        }
        for label, (where, text) in cases.items():
            with self.subTest(label):
                include.unlink(missing_ok=True)
                append.unlink(missing_ok=True)
                if where == 'bb':
                    self.write_recipe(extra=text)
                elif where == 'inc':
                    include.write_text(text + '\n')
                    self.write_recipe(extra='require extra.inc')
                else:
                    append.write_text(text + '\n')
                    self.write_recipe()
                self.assertTrue(any('directive the audit does not model' in e and text in e
                                    for e in self.errors()), self.errors())
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer,
                                      [append] if append.exists() else ())
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(any('unmodelled' in e and 'nothing fetched' in e for e in errors), errors)
                with mock.patch.object(audit, 'download', self.fake_download):
                    report = audit.check(self.layer, repo=self.repo.root, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertFalse(report['ok'])
        include.unlink(missing_ok=True)
        append.unlink(missing_ok=True)
        self.write_recipe()
        self.assertEqual(self.errors(), [])

    def test_assignments_with_expanded_variable_names_are_refused(self):
        # BitBake expands variable references in assignment keys when it
        # finalizes the datastore: SRC_${SOURCE_SUFFIX} becomes SRC_URI and
        # replaces the audited value. The reader does not model key expansion,
        # so any such assignment must fail before anything is fetched.
        include = self.recipe_dir / 'extra.inc'
        append = self.recipe_dir / 'suifleet_1.0.bbappend'
        expanded = 'SOURCE_SUFFIX = "URI"\nSRC_${SOURCE_SUFFIX} = "file://missing-notice"'
        cases = {
            'reviewer case': ('bb', expanded),
            'whole name': ('bb', 'NAME = "SRCREV"\n${NAME} = "0000000000000000000000000000000000000000"'),
            'append operator': ('bb', 'SUFFIX = "URI"\nSRC_${SUFFIX} += "file://notice"'),
            'flag': ('bb', 'SUFFIX = "URI"\nSRC_${SUFFIX}[sha256sum] = "00"'),
            'override on expanded name': ('bb', 'SUFFIX = "URI"\nSRC_${SUFFIX}:append = " file://notice"'),
            'exported': ('bb', 'SUFFIX = "URI"\nexport SRC_${SUFFIX} = "file://notice"'),
            'unquoted': ('bb', 'SUFFIX = "URI"\nSRC_${SUFFIX} = file://notice'),
            'in include': ('inc', expanded),
            'in append': ('bbappend', expanded),
        }
        for label, (where, text) in cases.items():
            with self.subTest(label):
                include.unlink(missing_ok=True)
                append.unlink(missing_ok=True)
                if where == 'bb':
                    self.write_recipe(extra=text)
                elif where == 'inc':
                    include.write_text(text + '\n')
                    self.write_recipe(extra='require extra.inc')
                else:
                    append.write_text(text + '\n')
                    self.write_recipe()
                self.assertTrue(any('key expansion' in e for e in self.errors()), self.errors())
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer,
                                      [append] if append.exists() else ())
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(any('unmodelled' in e and 'nothing fetched' in e for e in errors), errors)
                with mock.patch.object(audit, 'download', self.fake_download):
                    report = audit.check(self.layer, repo=self.repo.root, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertFalse(report['ok'])
        include.unlink(missing_ok=True)
        append.unlink(missing_ok=True)
        # Package-scoped overrides keep a literal variable name and stay accepted.
        self.write_recipe(extra='RDEPENDS:${PN} += "bash"\nFILES:${PN} += "/opt/x"')
        self.assertEqual(self.errors(), [])
        self.write_recipe()
        self.assertEqual(self.errors(), [])

    def test_layer_configuration_cannot_change_recipe_sources(self):
        # BitBake parses conf/layer.conf into the base configuration every
        # recipe inherits, so a source change there must be refused before any
        # download, even though no recipe mentions it.
        conf = self.layer / 'conf/layer.conf'
        registration = (Path(audit.__file__).resolve().parents[1]
                        / 'suifleet-edge/meta-suifleet/conf/layer.conf').read_text()
        cases = {
            'package-scoped append': 'SRC_URI:append:pn-suifleet = " file://notice.txt"',
            'global SRCREV': f'SRCREV = "{"c" * 40}"',
            'pinned variable': 'SUIFLEET_CRATE_VERSION = "9.9.9"',
            'include': 'include conf/extra.conf',
            'inherit': 'INHERIT += "suifleet-core-fixes"',
            'premirrors': 'PREMIRRORS:prepend = "https://.* https://mirror.example/ "',
            'inline python': 'BBFILE_PRIORITY_meta-suifleet = "${@6}"',
            'override on registration name': 'LAYERVERSION_meta-suifleet:pn-suifleet = "2"',
            'extra BBFILES': 'BBFILES += "${LAYERDIR}/../other/*.bb"',
            'second collection': 'BBFILE_COLLECTIONS += "other"',
            'anonymous python': 'python () {\n    d.setVar("SRCREV", "x")\n}',
            # Continuations BitBake's ConfHandler refuses: the layer would not
            # parse, so the audit must not certify it.
            'comment continued into code': '# note \\\nBBMASK = ".*"',
            'continuation at end of file': '# trailing note \\',
        }
        for label, text in cases.items():
            with self.subTest(label):
                conf.write_text(registration + text + '\n')
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    report = audit.check(self.layer, repo=self.repo.root, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertFalse(report['ok'])
                self.assertTrue(any(e.startswith('conf/layer.conf:') for e in report['errors']), report['errors'])
                entry = report['recipes']['suifleet']
                self.assertEqual((entry['fetched'], entry['fetch_skipped']), (None, 'layer configuration findings'))
        # The repository's own registration-only layer.conf is accepted.
        conf.write_text(registration)
        self.assertEqual(self.errors(), [])
        self.assertEqual(audit.layer_conf_errors(registration), [])

    def test_reported_series_follow_bitbake_logical_assignments(self):
        # The report's series field is the per-series evidence, so it must be
        # what BitBake applies: continuations joined, and a repeated assignment
        # (where the last one wins) refused rather than reported from the first.
        conf = self.layer / 'conf/layer.conf'
        registration = (Path(audit.__file__).resolve().parents[1]
                        / 'suifleet-edge/meta-suifleet/conf/layer.conf').read_text()
        line = next(l for l in registration.splitlines() if l.startswith('LAYERSERIES_COMPAT_meta-suifleet'))
        declared = audit.layer_conf_series(registration)
        conf.write_text(registration)
        self.assertEqual(self.errors(), [])
        self.assertTrue(declared)
        self.assertEqual(audit.check(self.layer)['series'], declared)
        # A key split by a continuation is one BitBake assignment.
        conf.write_text(registration.replace(line, line.replace('meta-', 'meta-\\\n', 1)))
        report = audit.check(self.layer)
        self.assertEqual((report['ok'], report['errors'], report['series']), (True, [], declared))
        # A later assignment replaces the declared compatibility: refused, and
        # the series reported are the effective ones.
        conf.write_text(registration + 'LAYERSERIES_COMPAT_meta-suifleet = "kirkstone"\n')
        with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
        self.assertFalse(report['ok'])
        self.assertEqual(report['series'], ['kirkstone'])
        self.assertTrue(any('LAYERSERIES_COMPAT_meta-suifleet is assigned 2 times' in e for e in report['errors']),
                        report['errors'])
        self.assertEqual(report['recipes']['suifleet']['fetch_skipped'], 'layer configuration findings')
        conf.write_text(registration)

    def test_missing_bbfiles_registration_is_refused(self):
        # The audit globs the recipe and append files itself; if layer.conf no
        # longer registers them, BitBake would not parse them and a clean report
        # would describe recipes a standalone build never uses.
        conf = self.layer / 'conf/layer.conf'
        registration = (Path(audit.__file__).resolve().parents[1]
                        / 'suifleet-edge/meta-suifleet/conf/layer.conf').read_text()
        recipe_line = 'BBFILES += "${LAYERDIR}/recipes-*/*/*.bb"\n'
        append_line = 'BBFILES += "${LAYERDIR}/recipes-*/*/*.bbappend"\n'
        self.assertIn(recipe_line, registration)
        self.assertIn(append_line, registration)
        append = self.recipe_dir / 'suifleet_%.bbappend'
        cases = {
            'no registration': (registration.replace(recipe_line, '').replace(append_line, ''), False, ['recipes']),
            'recipe registration missing': (registration.replace(recipe_line, ''), False, ['recipes']),
            'append registration missing with an append': (registration.replace(append_line, ''), True, ['appends']),
        }
        for label, (text, with_append, missing) in cases.items():
            with self.subTest(label):
                conf.write_text(text)
                if with_append:
                    append.write_text('# no source change\n')
                else:
                    append.unlink(missing_ok=True)
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    report = audit.check(self.layer, repo=self.repo.root, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertFalse(report['ok'])
                found = [e for e in report['errors'] if e.startswith('conf/layer.conf:') and 'is missing' in e]
                self.assertEqual([kind for kind in ('recipes', 'appends') if any(f'audited {kind}' in e for e in found)], missing, found)
                entry = report['recipes']['suifleet']
                self.assertEqual((entry['fetched'], entry['fetch_skipped']), (None, 'layer configuration findings'))
        # Without any append, the append registration is not required.
        append.unlink(missing_ok=True)
        conf.write_text(registration.replace(append_line, ''))
        self.assertEqual(self.errors(), [])
        conf.write_text(registration)
        self.assertEqual(self.errors(), [])

    def test_archives_bitbake_unpacks_in_unmodelled_formats_are_refused(self):
        # BitBake runs unzip (and other extractors) on these names unless
        # unpack=0; the audit must not report the packed file's layout instead.
        packed = Path(self.tmp.name) / 'source.zip'
        with zipfile.ZipFile(packed, 'w') as archive:
            archive.writestr('LICENSE', LICENSE)
        self.archive = packed
        sha = sha256(packed.read_bytes())
        self.write_recipe(url=f'{self.url};downloadfilename=source.zip', sha=sha)
        self.assertTrue(any('format the audit does not extract' in e for e in self.errors()), self.errors())
        recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
        workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
        with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
            errors, fetched = audit.fetch_recipe(recipe, workdir)
        self.assertEqual(fetched, [])
        self.assertTrue(any('format the audit does not extract' in e and 'nothing fetched' in e for e in errors), errors)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
        self.assertFalse(report['ok'])
        self.assertFalse((Path(report['fetch_dir']) / 'suifleet/source/source.zip').exists())
        for name in ('source.tar.lz', 'source.7z', 'source.deb', 'source.gz', 'source.TAR.GZ', 'source.jar'):
            with self.subTest(name):
                self.write_recipe(url=f'{self.url};downloadfilename={name}', sha=sha)
                self.assertTrue(any('format the audit does not extract' in e for e in self.errors()), self.errors())
        # unpack=0 is the intentional loose-file case: copied as source.zip, not extracted.
        self.write_recipe(url=f'{self.url};downloadfilename=source.zip;unpack=0', sha=sha)
        self.assertFalse(any('format the audit does not extract' in e for e in self.errors()), self.errors())
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        source = Path(report['fetch_dir']) / 'suifleet/source'
        self.assertTrue(report['recipes']['suifleet']['fetched'][0]['verified'])
        self.assertTrue((source / 'source.zip').is_file())
        self.assertFalse((source / 'LICENSE').exists())
        self.assertFalse(any('format the audit does not extract' in e for e in report['errors']), report['errors'])
        # Local files are only ever copied, so a local archive needs unpack=0 too.
        self.write_recipe()
        shutil.copyfile(packed, self.recipe_dir / 'files/extra.zip')
        self.write_recipe(extra='SRC_URI += "file://extra.zip"\n')
        self.assertTrue(any('file:// source extra.zip is an archive' in e for e in self.errors()), self.errors())
        recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
        workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
        with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
            errors, fetched = audit.fetch_recipe(recipe, workdir)
        self.assertTrue(any('is an archive' in e and 'nothing fetched' in e for e in errors), errors)
        self.write_recipe(extra='SRC_URI += "file://extra.zip;unpack=0;subdir=source"\n')
        self.assertEqual(self.errors(), [])
        self.archive = Path(self.tmp.name) / f'suifleet-edge-{self.rev}.tar.gz'
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])
        self.assertTrue((Path(report['fetch_dir']) / 'suifleet/source/extra.zip').is_file())

    def test_percent_escaped_downloadfilename_is_refused_not_decoded(self):
        # BitBake keeps a downloadfilename literally, so source%2Etar.gz would stay
        # a packed loose file there; the audit must never report it unpacked.
        name = 'source%2Etar.gz'
        self.assertEqual(audit.download_name(self.url, {'downloadfilename': name}), name)
        self.write_recipe(url=f'{self.url};downloadfilename={name}')
        self.assertTrue(any('does not model' in e and 'percent escape' in e
                            for e in self.errors(repo=self.repo.root)), self.errors())
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
        self.assertFalse(report['ok'])
        self.assertFalse(any(f.get('verified') for f in report['recipes']['suifleet'].get('fetched') or []))
        self.assertFalse((Path(report['fetch_dir']) / 'suifleet/source/LICENSE').exists())
        # The URL path, by contrast, is percent-decoded like BitBake's decodeurl.
        self.assertEqual(audit.download_name('https://example.com/a/source%2Etar.gz', {}), 'source.tar.gz')

    def test_https_parameters_the_audit_does_not_model_are_refused(self):
        for extra in ('striplevel=1', 'md5sum=' + '0' * 32, 'localpath=x', 'unpack=yes', 'unpack=false'):
            with self.subTest(extra):
                self.write_recipe(url=f'{self.url};{extra}')
                key = extra.split('=')[0]
                self.assertTrue(any('does not model' in e and key in e for e in self.errors()), self.errors())
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                self.assertFalse(report['ok'])
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(any('does not model' in e and 'nothing fetched' in e for e in errors), errors)
        # unpack=0 is modelled: the archive is copied, not extracted.
        self.write_recipe(url=f'{self.url};unpack=0')
        self.assertFalse(any('does not model' in e for e in self.errors()), self.errors())

    def test_zstandard_tarballs_are_refused_statically(self):
        # The supported host Python's tarfile cannot decode Zstandard, so a
        # correctly pinned .tar.zst must be refused before download rather
        # than fail inside the extractor.
        raw = subprocess.check_output(['git', '-C', str(self.repo.root), '-c', 'tar.umask=0002', 'archive',
                                       '--format=tar', self.rev, '--', *self.info['paths']])
        if shutil.which('zstd'):
            packed = subprocess.run(['zstd', '-q', '-c'], input=raw, stdout=subprocess.PIPE, check=True).stdout
        else:
            packed = b'\x28\xb5\x2f\xfd' + raw  # Zstandard frame magic; undecodable either way
        archive = Path(self.tmp.name) / 'source.tar.zst'
        archive.write_bytes(packed)
        self.archive = archive
        sha = sha256(packed)
        for name in ('source.tar.zst', 'source.tzst'):
            with self.subTest(name):
                self.write_recipe(url=f'{self.url};downloadfilename={name}', sha=sha)
                self.assertTrue(any('format the audit does not extract' in e for e in self.errors()), self.errors())
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('downloaded')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertEqual(fetched, [])
                self.assertTrue(any('nothing fetched' in e for e in errors), errors)
        # unpack=0 keeps it a loose file, which the audit can model.
        self.write_recipe(url=f'{self.url};downloadfilename=source.tar.zst;unpack=0', sha=sha)
        self.assertFalse(any('format the audit does not extract' in e for e in self.errors()), self.errors())

    def test_undecodable_pinned_tarballs_are_findings_not_tracebacks(self):
        # Correctly pinned bytes that do not decode as the tarball their name
        # claims (Zstandard data saved as .tar.gz, a truncated .tar.xz, plain
        # garbage) must come back as structured findings in the JSON report.
        raw = subprocess.check_output(['git', '-C', str(self.repo.root), '-c', 'tar.umask=0002', 'archive',
                                       '--format=tar', self.rev, '--', *self.info['paths']])
        cases = {}
        if shutil.which('zstd'):
            cases['source-zstd.tar.gz'] = subprocess.run(['zstd', '-q', '-c'], input=raw, stdout=subprocess.PIPE,
                                                         check=True).stdout
        xz = subprocess.run(['xz', '-c'], input=raw, stdout=subprocess.PIPE, check=True).stdout
        cases['source-truncated.tar.xz'] = xz[:len(xz) // 2]
        gz = subprocess.run(['gzip', '-n', '-c'], input=raw, stdout=subprocess.PIPE, check=True).stdout
        cases['source-truncated.tar.gz'] = gz[:len(gz) // 2]
        cases['source-garbage.tar'] = b'not a tarball\n' * 64
        for name, data in cases.items():
            with self.subTest(name):
                archive = Path(self.tmp.name) / f'bad-{name}'
                archive.write_bytes(data)
                self.archive = archive
                self.write_recipe(url=f'{self.url};downloadfilename={name}', sha=sha256(data))
                self.assertEqual(self.errors(), [])
                with mock.patch.object(audit, 'download', self.fake_download):
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
                json.dumps(report)
                self.assertFalse(report['ok'])
                self.assertTrue(report['recipes']['suifleet']['fetched'][0]['verified'])
                self.assertTrue(any(f'{name} could not be decoded as a tarball' in e for e in report['errors']),
                                report['errors'])

    def test_compression_must_match_the_bitbake_file_name(self):
        # BitBake picks the decompressor from the local file name (tar -z for
        # .tar.gz/.tgz, bzip2 -dc for .tar.bz2/.tbz2, xz -dc for .tar.xz/.txz)
        # and fails on other bytes, so a correctly pinned valid tarball whose
        # compression differs from its name must fail the complete audit.
        raw = subprocess.check_output(['git', '-C', str(self.repo.root), '-c', 'tar.umask=0002', 'archive',
                                       '--format=tar', self.rev, '--', *self.info['paths']])
        packed = {
            'none': raw,
            'gz': subprocess.run(['gzip', '-n', '-c'], input=raw, stdout=subprocess.PIPE, check=True).stdout,
            'bz2': subprocess.run(['bzip2', '-c'], input=raw, stdout=subprocess.PIPE, check=True).stdout,
            'xz': subprocess.run(['xz', '-c'], input=raw, stdout=subprocess.PIPE, check=True).stdout,
        }
        names = {'gz': ('source.tar.gz', 'source.tgz'), 'bz2': ('source.tar.bz2', 'source.tbz2'),
                 'xz': ('source.tar.xz', 'source.txz')}

        def run(name, data):
            archive = Path(tempfile.mkdtemp(dir=self.tmp.name)) / name
            archive.write_bytes(data)
            self.archive = archive
            self.write_recipe(url=f'{self.url};downloadfilename={name}', sha=sha256(data))
            self.assertEqual(self.errors(), [])
            with mock.patch.object(audit, 'download', self.fake_download):
                report = audit.check(self.layer, repo=self.repo.root, fetch=True)
            self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
            json.dumps(report)
            self.assertTrue(report['recipes']['suifleet']['fetched'][0]['verified'])
            return report

        for compression, aliases in names.items():
            for name in aliases:
                with self.subTest(name=name, data=compression):
                    report = run(name, packed[compression])
                    self.assertTrue(report['ok'], report['errors'])
                for other, data in packed.items():
                    if other == compression:
                        continue
                    with self.subTest(name=name, data=other):
                        report = run(name, data)
                        self.assertFalse(report['ok'])
                        self.assertTrue(any(f'{name} could not be decoded as a tarball' in e
                                            for e in report['errors']), report['errors'])
        # A plain .tar goes to 'tar x -f', where GNU tar detects the
        # compression itself, so compressed bytes named .tar still unpack.
        for other, data in packed.items():
            with self.subTest(name='source.tar', data=other):
                report = run('source.tar', data)
                self.assertTrue(report['ok'], report['errors'])
        # The tar(1) commands BitBake runs agree with the audit on these bytes.
        if shutil.which('tar'):
            mislabelled = Path(tempfile.mkdtemp(dir=self.tmp.name)) / 'source.tar.gz'
            mislabelled.write_bytes(packed['none'])
            self.assertNotEqual(subprocess.run(['tar', '-tzf', str(mislabelled)], stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL).returncode, 0)

    def test_truncated_compressed_streams_fail_the_complete_audit(self):
        # tarfile stops once it has read the tar end-of-archive blocks, but the
        # decompressors BitBake runs read to EOF and fail on a stream cut short
        # (gzip without its CRC/size trailer, bzip2/xz without their stream
        # footer). A correctly pinned truncated stream must fail the audit.
        raw = subprocess.check_output(['git', '-C', str(self.repo.root), '-c', 'tar.umask=0002', 'archive',
                                       '--format=tar', self.rev, '--', *self.info['paths']])
        packed = {
            'gz': subprocess.run(['gzip', '-n', '-c'], input=raw, stdout=subprocess.PIPE, check=True).stdout,
            'bz2': subprocess.run(['bzip2', '-c'], input=raw, stdout=subprocess.PIPE, check=True).stdout,
            'xz': subprocess.run(['xz', '-c'], input=raw, stdout=subprocess.PIPE, check=True).stdout,
        }
        # Bytes dropped from the end: gzip's 8-byte trailer; for bzip2 and xz
        # the end-of-stream footer.
        cuts = {'gz': 8, 'bz2': 4, 'xz': 12}
        corrupt_crc = bytearray(packed['gz'])
        corrupt_crc[-8] ^= 0xFF
        cases = [(f'source.tar.{kind}', data[:-cuts[kind]], 'truncated') for kind, data in packed.items()]
        cases += [('source.tar', packed['gz'][:-8], 'truncated gzip named .tar'),
                  ('source.tar.gz', bytes(corrupt_crc), 'gzip CRC mismatch')]

        def run(name, data):
            archive = Path(tempfile.mkdtemp(dir=self.tmp.name)) / name
            archive.write_bytes(data)
            self.archive = archive
            self.write_recipe(url=f'{self.url};downloadfilename={name}', sha=sha256(data))
            self.assertEqual(self.errors(), [])
            with mock.patch.object(audit, 'download', self.fake_download):
                report = audit.check(self.layer, repo=self.repo.root, fetch=True)
            self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
            json.dumps(report)
            self.assertTrue(report['recipes']['suifleet']['fetched'][0]['verified'])
            return archive, report

        for kind, data in packed.items():
            with self.subTest(case=f'complete {kind}'):
                _, report = run(f'source.tar.{kind}', data)
                self.assertTrue(report['ok'], report['errors'])
        for name, data, what in cases:
            with self.subTest(name=name, case=what):
                # The truncation leaves every tar member readable by tarfile.
                with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                    self.assertTrue(tar.getmembers())
                archive, report = run(name, data)
                self.assertFalse(report['ok'])
                self.assertTrue(any(f'{name} could not be decoded as a tarball' in e and 'through its end' in e
                                    for e in report['errors']), report['errors'])
                if shutil.which('tar') and name.endswith('.gz'):
                    self.assertNotEqual(subprocess.run(['tar', '-xzf', str(archive), '-O'],
                                                       stdout=subprocess.DEVNULL,
                                                       stderr=subprocess.DEVNULL).returncode, 0)

    def test_xz_trailing_bytes_follow_xz_utils(self):
        # lzma.open drops bytes after a complete stream that do not decode as
        # another stream, and rejects legal null stream padding. The audit must
        # instead accept and reject exactly what 'xz -dc' does.
        raw = subprocess.check_output(['git', '-C', str(self.repo.root), '-c', 'tar.umask=0002', 'archive',
                                       '--format=tar', self.rev, '--', *self.info['paths']])
        stream = subprocess.run(['xz', '-c'], input=raw, stdout=subprocess.PIPE, check=True).stdout
        cases = [
            ('junk', stream + b'junk', False),
            ('three padding bytes', stream + b'\0' * 3, False),
            ('five padding bytes', stream + b'\0' * 5, False),
            ('leading padding', b'\0' * 4 + stream, False),
            ('partial second stream', stream + stream[:6], False),
            ('four padding bytes', stream + b'\0' * 4, True),
            ('eight padding bytes', stream + b'\0' * 8, True),
            ('concatenated streams', stream + b'\0' * 4 + stream, True),
        ]

        def run(name, data):
            archive = Path(tempfile.mkdtemp(dir=self.tmp.name)) / name
            archive.write_bytes(data)
            self.archive = archive
            self.write_recipe(url=f'{self.url};downloadfilename={name}', sha=sha256(data))
            self.assertEqual(self.errors(), [])
            with mock.patch.object(audit, 'download', self.fake_download):
                report = audit.check(self.layer, repo=self.repo.root, fetch=True)
            self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
            json.dumps(report)
            self.assertTrue(report['recipes']['suifleet']['fetched'][0]['verified'])
            return archive, report

        for name in ('source.tar.xz', 'source.txz'):
            for what, data, accepted in cases:
                with self.subTest(name=name, case=what):
                    archive, report = run(name, data)
                    if shutil.which('xz'):
                        decoded = subprocess.run(['xz', '-dc', str(archive)], stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL).returncode == 0
                        self.assertEqual(decoded, accepted)
                    if accepted:
                        self.assertTrue(report['ok'], report['errors'])
                    else:
                        self.assertFalse(report['ok'])
                        self.assertTrue(any(f'{name} could not be decoded as a tarball' in e
                                            and 'through its end' in e for e in report['errors']),
                                        report['errors'])

    def test_xz_members_after_stream_padding_are_extracted_and_audited(self):
        # tarfile's xz reader (lzma.open) stops at null stream padding and
        # silently drops every later stream; 'xz -dc' decodes them all, so
        # BitBake unpacks members the audit would never see. Split the tar
        # between members, compress the halves separately and join them with
        # legal padding: the audit must extract and check the second half too.
        if not shutil.which('xz'):
            self.skipTest('xz is required to build and cross-check the archive')
        buffer = io.BytesIO()
        with tarfile.open(self.archive) as source, tarfile.open(fileobj=buffer, mode='w', format=tarfile.GNU_FORMAT) as tar:
            for member in source.getmembers():
                tar.addfile(member, source.extractfile(member) if member.isfile() else None)
            first = buffer.tell()
            info = tarfile.TarInfo('suifleet-edge/examples/device.key')
            info.size = 6
            tar.addfile(info, io.BytesIO(b'secret'))
        raw = buffer.getvalue()
        halves = [subprocess.run(['xz', '-c'], input=part, stdout=subprocess.PIPE, check=True).stdout
                  for part in (raw[:first], raw[first:])]
        data = halves[0] + b'\0' * 4 + halves[1]
        archive = Path(tempfile.mkdtemp(dir=self.tmp.name)) / 'source.tar.xz'
        archive.write_bytes(data)

        # Same inventory as 'xz -dc | tar -xf -'.
        reference = Path(tempfile.mkdtemp(dir=self.tmp.name))
        decoded = subprocess.run(['xz', '-dc', str(archive)], stdout=subprocess.PIPE, check=True).stdout
        subprocess.run(['tar', '-xf', '-', '-C', str(reference)], input=decoded, check=True)
        extracted = Path(tempfile.mkdtemp(dir=self.tmp.name))
        audit.safe_extract(archive, extracted)

        def inventory(root):
            return sorted(p.relative_to(root).as_posix() for p in root.rglob('*'))
        self.assertIn('suifleet-edge/examples/device.key', inventory(reference))
        self.assertEqual(inventory(extracted), inventory(reference))
        self.assertEqual((extracted / 'suifleet-edge/examples/device.key').read_bytes(), b'secret')

        # And the full fetch audit applies its checks to the member after the padding.
        self.archive = archive
        self.write_recipe(url=f'{self.url};downloadfilename=source.tar.xz', sha=sha256(data))
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
        self.assertIn('suifleet_1.0.bb: runtime identity/state-like file in fetched source: '
                      'source/suifleet-edge/examples/device.key', report['errors'])
        self.assertFalse(report['ok'])

    def test_fetch_reports_checksum_and_layout_defects(self):
        self.write_recipe(sha='f' * 64)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertTrue(any('differs from pin' in e for e in report['errors']))
        # The published-archive defect: a workspace member left out of the file list.
        # The packager now refuses such a list, so write the archive directly.
        broken = self.archive.with_name('broken.tar.gz')
        raw = subprocess.check_output(['git', '-C', str(self.repo.root), '-c', 'tar.umask=0002', 'archive',
                                       '--format=tar', self.rev, '--',
                                       *[p for p in self.info['paths'] if 'windows-runtime' not in p]])
        broken.write_bytes(subprocess.run(['gzip', '-n', '-9'], input=raw, stdout=subprocess.PIPE,
                                          check=True).stdout)
        self.archive = broken
        self.write_recipe(sha=sha256(broken.read_bytes()))
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertTrue(any('workspace member suifleet-edge/windows-runtime is missing' in e for e in report['errors']),
                        report['errors'])

    def test_fetch_finds_escaped_workspace_members(self):
        # A published manifest whose members key is only spelled with an
        # escape still declares windows-runtime; leaving it out of the archive
        # must be reported, and syntax the reader does not model is an error.
        for spelling, expected in (('"memb\\u0065rs" = ["windows-runtime"]',
                                    'workspace member suifleet-edge/windows-runtime is missing'),
                                   ('members = ["""windows-runtime"""]',
                                    'suifleet-edge/Cargo.toml: unsupported Cargo.toml syntax')):
            with self.subTest(spelling):
                manifest = CARGO_TOML.replace('members = ["windows-runtime"]', spelling).encode()
                archive = self.archive.with_name(f'escaped-{len(manifest)}.tar.gz')
                with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
                    for member in source.getmembers():
                        if 'windows-runtime' in member.name:
                            continue
                        data = source.extractfile(member) if member.isfile() else None
                        if member.name == 'suifleet-edge/Cargo.toml':
                            member.size, data = len(manifest), io.BytesIO(manifest)
                        tar.addfile(member, data)
                self.write_recipe(sha=sha256(archive.read_bytes()))
                original, self.archive = self.archive, archive
                try:
                    with mock.patch.object(audit, 'download', self.fake_download):
                        report = audit.check(self.layer, fetch=True)
                finally:
                    self.archive = original
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
                self.assertTrue(any(expected in e for e in report['errors']), report['errors'])

    def test_fetch_reports_missing_local_path_dependency(self):
        # The pinned manifest needs local-helper only through a path
        # dependency (not a workspace member); the archive lacks it.
        manifest = (CARGO_TOML + '\n[dependencies]\nlocal-helper = { path = "local-helper" }\n').encode()
        archive = self.archive.with_name('path-dependency.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name == 'suifleet-edge/Cargo.toml':
                    member.size, data = len(manifest), io.BytesIO(manifest)
                tar.addfile(member, data)
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertTrue(any('path dependency suifleet-edge/local-helper is missing from the source layout' in e
                            for e in report['errors']), report['errors'])

    def test_fetch_and_repo_report_missing_declared_target_file(self):
        # The reviewer's reproduction: an explicit [[bin]] outside the fixed
        # roots that a published archive lacks must not audit clean.
        manifest = CARGO_TOML + '\n[[bin]]\nname = "standalone-check"\npath = "tools/standalone.rs"\n'
        archive = self.archive.with_name('declared-target.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name == 'suifleet-edge/Cargo.toml':
                    member.size, data = len(manifest), io.BytesIO(manifest.encode())
                tar.addfile(member, data)
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        expected = 'Cargo.toml declares build file suifleet-edge/tools/standalone.rs, missing from the source layout'
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        # --repo: a SRCREV whose manifest declares a target file it never committed.
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        rev = self.repo.commit()
        self.write_recipe(srcrev=rev)
        self.assertTrue(any(expected in e for e in audit.check(self.layer, repo=self.repo.root)['errors']))
        # With the file committed and packaged, both audits are clean.
        self.repo.write('suifleet-edge/tools/standalone.rs', 'fn main() {}\n')
        rev = self.repo.commit()
        complete = self.archive.with_name(f'suifleet-edge-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, complete)
        self.assertIn('suifleet-edge/tools/standalone.rs', info['paths'])
        self.archive = complete
        self.write_recipe(url=self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}'),
                          sha=info['sha256'], srcrev=rev)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])

    def test_fetch_and_repo_bind_unpacked_compiler_inputs_to_srcrev(self):
        # The integration reviewer's reproduction: a stale archive verified
        # against its own SHA-256, labelled with a newer SRCREV that changes
        # only Rust code (same lock, license and version), must not audit
        # clean when --repo and --fetch run together.
        stale = self.archive
        self.repo.write('suifleet-edge/src/main.rs', MAIN_RS + '// changed after the archive was built\n')
        rev = self.repo.commit()
        self.assertNotEqual(self.repo.git('show', f'{self.rev}:suifleet-edge/src/main.rs'),
                            self.repo.git('show', f'{rev}:suifleet-edge/src/main.rs'))
        for rel in ('LICENSE', 'suifleet-edge/Cargo.toml', 'suifleet-edge/Cargo.lock'):
            self.assertEqual(self.repo.git('show', f'{self.rev}:{rel}'), self.repo.git('show', f'{rev}:{rel}'))
        renamed = stale.with_name(f'suifleet-edge-{rev}.tar.gz')
        shutil.copyfile(stale, renamed)
        self.archive = renamed
        self.write_recipe(url=self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}'),
                          sha=sha256(renamed.read_bytes()), srcrev=rev)
        # Each half alone cannot see the mismatch.
        self.assertEqual(self.errors(repo=self.repo.root), [])
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertFalse(report['ok'])
        self.assertTrue(any('1 unpacked daemon source files differ from the Git tree at SRCREV' in e
                            and 'suifleet-edge/src/main.rs' in e for e in report['errors']), report['errors'])
        # A file the archive carries but SRCREV never committed is refused too.
        extra = stale.with_name('extra-file.tar.gz')
        with tarfile.open(stale) as source, tarfile.open(extra, 'w:gz') as tar:
            for member in source.getmembers():
                tar.addfile(member, source.extractfile(member) if member.isfile() else None)
            planted = tarfile.TarInfo('suifleet-edge/src/planted.rs')
            planted.size, planted.mode = 3, 0o644
            tar.addfile(planted, io.BytesIO(b'//\n'))
        self.archive = extra
        self.write_recipe(sha=sha256(extra.read_bytes()))
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertTrue(any('are absent from the Git tree at SRCREV' in e and 'suifleet-edge/src/planted.rs' in e
                            for e in report['errors']), report['errors'])
        # The archive built at the declared revision is clean, including the
        # broader published file list (windows-runtime is outside the closure).
        fresh = stale.with_name(f'fresh-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, fresh)
        self.assertIn('suifleet-edge/windows-runtime/Cargo.toml', info['paths'])
        self.archive = fresh
        self.write_recipe(url=self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}'),
                          sha=info['sha256'], srcrev=rev)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])

    def test_fetch_and_repo_refuse_archive_omitting_srcrev_closure_file(self):
        # The source reviewer's reproduction: SRCREV commits an implicit module
        # (`mod required_module;` with src/required_module.rs) and the pinned
        # archive at that SRCREV carries every other file byte-identically but
        # omits the module. Every present file binds, yet rustc fails with
        # E0583, so the omission itself must be refused.
        self.repo.write('suifleet-edge/src/main.rs', 'mod required_module;\n' + MAIN_RS)
        self.repo.write('suifleet-edge/src/required_module.rs', 'pub fn f() {}\n')
        rev = self.repo.commit()
        complete = self.archive.with_name(f'suifleet-edge-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, complete)
        self.assertIn('suifleet-edge/src/required_module.rs', info['paths'])
        url = self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}')
        omitted = complete.with_name('omitted-module.tar.gz')
        with tarfile.open(complete) as source, tarfile.open(omitted, 'w:gz') as tar:
            for member in source.getmembers():
                if member.name != 'suifleet-edge/src/required_module.rs':
                    tar.addfile(member, source.extractfile(member) if member.isfile() else None)
        self.archive = omitted
        self.write_recipe(url=url, sha=sha256(omitted.read_bytes()), srcrev=rev)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertFalse(report['ok'])
        self.assertTrue(any('omit 1 build-closure path(s)' in e and 'suifleet-edge/src/required_module.rs' in e
                            for e in report['errors']), report['errors'])
        # The complete archive at the same SRCREV audits clean.
        self.archive = complete
        self.write_recipe(url=url, sha=info['sha256'], srcrev=rev)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])

    def test_audit_refuses_cargo_configuration_in_any_mode(self):
        # The integration reviewer's reproduction for the audit: the
        # metadata probe does not compile, so a checked-in Cargo configuration
        # (here a compile-time [env] variable read by env!) must be refused by
        # the source checks, whether the archive omits or carries it.
        config = '[env]\nSUIFLEET_FIXTURE = "1"\n'
        self.repo.write('suifleet-edge/.cargo/config.toml', config)
        self.repo.write('suifleet-edge/src/main.rs', MAIN_RS + 'const _V: &str = env!("SUIFLEET_FIXTURE");\n')
        rev = self.repo.commit()
        url = self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}')

        def repack(name, rel=None):
            # The SRCREV bytes of the original closure (everything but the
            # configuration), optionally carrying the configuration at `rel`.
            archive = self.archive.with_name(name)
            with tarfile.open(archive, 'w:gz') as tar:
                for path in self.info['paths']:
                    data = self.repo.git('show', f'{rev}:{path}')
                    member = tarfile.TarInfo(path)
                    member.size, member.mode = len(data), 0o644
                    tar.addfile(member, io.BytesIO(data))
                if rel:
                    member = tarfile.TarInfo(rel)
                    member.size, member.mode = len(config), 0o644
                    tar.addfile(member, io.BytesIO(config.encode()))
            return archive

        omitted, carried = repack('config-omitted.tar.gz'), repack('config-carried.tar.gz',
                                                                     'suifleet-edge/.cargo/config.toml')

        def audit_errors(archive, **kwargs):
            self.archive = archive
            self.write_recipe(url=url, sha=sha256(archive.read_bytes()), srcrev=rev)
            with mock.patch.object(audit, 'download', self.fake_download):
                report = audit.check(self.layer, **kwargs)
            if 'fetch_dir' in report:
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
            return report['errors']

        layout = 'unsupported Cargo configuration in the source layout: suifleet-edge/.cargo/config.toml'
        self.assertTrue(any(layout in e for e in audit_errors(omitted, repo=self.repo.root)))
        self.assertTrue(any(layout in e for e in audit_errors(carried, fetch=True)))
        for archive in (omitted, carried):
            with self.subTest(archive.name):
                errors = audit_errors(archive, repo=self.repo.root, fetch=True)
                self.assertTrue(any(layout in e for e in errors), errors)
                self.assertTrue(any('cannot derive the build closure' in e
                                    and 'unsupported checked-in Cargo configuration' in e for e in errors), errors)
        # An ancestor of the crate inside S is on Cargo's path too.
        legacy = repack('config-legacy.tar.gz', '.cargo/config')
        self.assertTrue(any('unsupported Cargo configuration in the source layout: .cargo/config' in e
                            for e in audit_errors(legacy, fetch=True)))
        # So is a directory above S inside the unpack directory, placed by
        # another fetched source.
        (self.recipe_dir / 'files/.cargo').mkdir()
        (self.recipe_dir / 'files/.cargo/config.toml').write_text(config)
        self.archive = self.archive.with_name(f'suifleet-edge-{self.rev}.tar.gz')
        self.write_recipe(extra='SRC_URI += "file://.cargo/config.toml"')
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertTrue(any('unsupported Cargo configuration above S in the unpack directory: .cargo' in e
                            for e in report['errors']), report['errors'])

    def test_fetch_and_repo_closure_follows_relocated_cargo_src_dir(self):
        # The source reviewer's reproduction: the whole crate moves to
        # crates/edge and the recipe sets CARGO_SRC_DIR to match. The combined
        # --repo --fetch audit must derive the closure for that layout, as the
        # repo-only and fetch-only audits and do_verify_lock already do,
        # instead of requiring suifleet-edge/Cargo.toml.
        (self.repo.root / 'crates').mkdir()
        self.repo.git('mv', 'suifleet-edge', 'crates/edge')
        self.repo.write('crates/edge/src/main.rs', MAIN_RS.replace('../../interfaces', '../../../interfaces'))
        rev = self.repo.commit()
        complete = self.archive.with_name(f'suifleet-edge-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, complete, crate='crates/edge')
        self.assertIn('crates/edge/src/extra.rs', info['paths'])
        self.assertNotIn('crates/edge/meta-suifleet-integration/conf/layer.conf', info['paths'])
        url = self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}')

        def audit_archive(archive, sha, **kwargs):
            self.archive = archive
            self.write_recipe(url=url, sha=sha, srcrev=rev)
            text = (self.recipe_dir / 'suifleet_1.0.bb').read_text()
            (self.recipe_dir / 'suifleet_1.0.bb').write_text(
                text.replace('CARGO_SRC_DIR = "suifleet-edge"', 'CARGO_SRC_DIR = "crates/edge"'))
            with mock.patch.object(audit, 'download', self.fake_download):
                report = audit.check(self.layer, **kwargs)
            if 'fetch_dir' in report:
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
            return report['errors']

        for kwargs in ({'repo': self.repo.root}, {'fetch': True}, {'repo': self.repo.root, 'fetch': True}):
            with self.subTest(**{k: bool(v) for k, v in kwargs.items()}):
                self.assertEqual(audit_archive(complete, info['sha256'], **kwargs), [])
        # The same layout missing a closure file is still refused by the
        # combined audit, under the relocated path.
        incomplete = complete.with_name('relocated-incomplete.tar.gz')
        with tarfile.open(complete) as source, tarfile.open(incomplete, 'w:gz') as tar:
            for member in source.getmembers():
                if member.name != 'crates/edge/src/data/blob.bin':
                    tar.addfile(member, source.extractfile(member) if member.isfile() else None)
        errors = audit_archive(incomplete, sha256(incomplete.read_bytes()), repo=self.repo.root, fetch=True)
        self.assertTrue(any('build-closure path(s)' in e and 'crates/edge/src/data/blob.bin' in e for e in errors),
                        errors)
        self.assertFalse(any('suifleet-edge/Cargo.toml' in e for e in errors), errors)

    def test_fetch_and_repo_report_missing_name_only_test_target(self):
        # cargo metadata --no-deps accepts a layout lacking a name-only
        # [[test]]'s inferred tests/<name>.rs, so the static audit must not.
        manifest = CARGO_TOML + '\n[[test]]\nname = "integration"\n'
        archive = self.archive.with_name('name-only-test.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name == 'suifleet-edge/Cargo.toml':
                    member.size, data = len(manifest), io.BytesIO(manifest.encode())
                tar.addfile(member, data)
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        expected = 'Cargo.toml declares build file suifleet-edge/tests/integration.rs, missing from the source layout'
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        # --repo: a SRCREV declaring the test without committing its file.
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        rev = self.repo.commit()
        self.write_recipe(srcrev=rev)
        self.assertTrue(any(expected in e for e in audit.check(self.layer, repo=self.repo.root)['errors']))
        # Committed and packaged, both audits are clean.
        self.repo.write('suifleet-edge/tests/integration.rs', '#[test]\nfn passes() {}\n')
        rev = self.repo.commit()
        complete = self.archive.with_name(f'suifleet-edge-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, complete)
        self.assertIn('suifleet-edge/tests/integration.rs', info['paths'])
        self.archive = complete
        self.write_recipe(url=self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}'),
                          sha=info['sha256'], srcrev=rev)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])

    def test_fetch_and_repo_refuse_implicit_modules_outside_packaged_directories(self):
        # A pinned archive with an out-of-root target whose sibling module file
        # is absent must not audit clean, and neither must its SRCREV.
        manifest = CARGO_TOML + '\n[[bin]]\nname = "standalone-check"\npath = "tools/standalone.rs"\n'
        entry = 'mod helper;\nfn main() { helper::f() }\n'
        archive = self.archive.with_name('implicit-module.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name == 'suifleet-edge/Cargo.toml':
                    member.size, data = len(manifest), io.BytesIO(manifest.encode())
                tar.addfile(member, data)
            added = tarfile.TarInfo('suifleet-edge/tools/standalone.rs')
            added.size = len(entry)
            tar.addfile(added, io.BytesIO(entry.encode()))
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        expected = 'suifleet-edge/tools/standalone.rs declares module(s) helper without #[path]'
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        self.repo.write('suifleet-edge/tools/standalone.rs', entry)
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        rev = self.repo.commit()
        self.write_recipe(srcrev=rev)
        self.assertTrue(any(expected in e for e in audit.check(self.layer, repo=self.repo.root)['errors']))

    def test_fetch_and_repo_refuse_implicit_modules_reached_from_src(self):
        # The cross-root variant: no manifest change, src/main.rs reaches an
        # out-of-root file whose sibling module is absent from the archive.
        main = '#[path = "../tools/entry.rs"]\nmod entry;\nfn main() {}\n'
        entry = 'mod helper;\n'
        archive = self.archive.with_name('cross-root-module.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            names = set()
            for member in source.getmembers():
                names.add(member.name)
                data = source.extractfile(member) if member.isfile() else None
                if member.name == 'suifleet-edge/src/main.rs':
                    member.size, data = len(main), io.BytesIO(main.encode())
                tar.addfile(member, data)
            self.assertIn('suifleet-edge/src/main.rs', names)
            added = tarfile.TarInfo('suifleet-edge/tools/entry.rs')
            added.size = len(entry)
            tar.addfile(added, io.BytesIO(entry.encode()))
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        expected = 'suifleet-edge/tools/entry.rs declares module(s) helper without #[path]'
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        self.repo.write('suifleet-edge/src/main.rs', main)
        self.repo.write('suifleet-edge/tools/entry.rs', entry)
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        rev = self.repo.commit()
        self.write_recipe(srcrev=rev)
        self.assertTrue(any(expected in e for e in audit.check(self.layer, repo=self.repo.root)['errors']))

    def test_fetch_follows_braced_includes(self):
        # The reviewer's fetched-layout reproduction: a pinned archive whose
        # src/main.rs uses `include! { .. }` for a file the archive lacks.
        main = 'include! { "../tools/entry.rs" }\nfn main() {}\n'
        archive = self.archive.with_name('braced-include.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name == 'suifleet-edge/src/main.rs':
                    member.size, data = len(main), io.BytesIO(main.encode())
                tar.addfile(member, data)
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        expected = 'suifleet-edge/src/main.rs references suifleet-edge/tools/entry.rs, absent from the source layout'
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        # An unresolvable include form in the fetched layout is refused too.
        main = 'include!(concat!(env!("OUT_DIR"), "/gen.rs"));\nfn main() {}\n'
        unsupported = self.archive.with_name('unsupported-include.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(unsupported, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name == 'suifleet-edge/src/main.rs':
                    member.size, data = len(main), io.BytesIO(main.encode())
                tar.addfile(member, data)
        self.write_recipe(sha=sha256(unsupported.read_bytes()))
        self.archive = unsupported
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertTrue(any('unsupported include form' in e for e in report['errors']), report['errors'])
        # Committed and packaged, the braced include audits clean end to end.
        self.repo.write('suifleet-edge/src/main.rs', 'include! { "../tools/entry.rs" }\nfn main() {}\n')
        self.repo.write('suifleet-edge/tools/entry.rs', 'pub fn f() {}\n')
        rev = self.repo.commit()
        complete = self.archive.with_name(f'suifleet-edge-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, complete)
        self.assertIn('suifleet-edge/tools/entry.rs', info['paths'])
        self.archive = complete
        self.write_recipe(url=self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}'),
                          sha=info['sha256'], srcrev=rev)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])

    def test_fetch_follows_raw_identifier_path_attributes(self):
        # Review 5202's fetched-layout reproduction: a pinned archive whose
        # src/main.rs names a module outside src/ with `#[r#path = ..]` that
        # the archive lacks must be reported, not audited clean.
        main = '#[r#path = "../extra/helper.inc"]\nmod helper;\nfn main() { helper::f() }\n'
        archive = self.archive.with_name('raw-path.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name == 'suifleet-edge/src/main.rs':
                    member.size, data = len(main), io.BytesIO(main.encode())
                tar.addfile(member, data)
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        expected = 'suifleet-edge/src/main.rs references suifleet-edge/extra/helper.inc, absent from the source layout'
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        # Committed and packaged, the same source audits clean end to end.
        self.repo.write('suifleet-edge/src/main.rs', main)
        self.repo.write('suifleet-edge/extra/helper.inc', 'pub fn f() {}\n')
        rev = self.repo.commit()
        complete = self.archive.with_name(f'suifleet-edge-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, complete)
        self.assertIn('suifleet-edge/extra/helper.inc', info['paths'])
        self.archive = complete
        self.write_recipe(url=self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}'),
                          sha=info['sha256'], srcrev=rev)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])

    def test_fetch_sees_includes_behind_comments(self):
        # The reviewer's fetched-layout reproduction: a pinned archive whose
        # src/main.rs uses `include_str /* .. */ !(..)` for a file the archive
        # lacks must not audit clean.
        main = ('const M: &str = include_str /* embedded at build time */ !("../../interfaces/message.txt");\n'
                'fn main() {}\n')
        archive = self.archive.with_name('commented-include.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name == 'suifleet-edge/src/main.rs':
                    member.size, data = len(main), io.BytesIO(main.encode())
                tar.addfile(member, data)
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        expected = 'suifleet-edge/src/main.rs references interfaces/message.txt, absent from the source layout'
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        self.assertFalse(report['ok'])
        # Committed and packaged, the same source audits clean end to end.
        self.repo.write('suifleet-edge/src/main.rs', main)
        self.repo.write('interfaces/message.txt', 'hello\n')
        rev = self.repo.commit()
        complete = self.archive.with_name(f'suifleet-edge-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, complete)
        self.assertIn('interfaces/message.txt', info['paths'])
        self.archive = complete
        self.write_recipe(url=self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}'),
                          sha=info['sha256'], srcrev=rev)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])

    def test_fetch_refuses_implicit_modules_outside_the_crate(self):
        # The reviewer's fetched-layout reproduction: a pinned archive whose
        # #[path] target outside the crate declares `mod child;` must not
        # audit clean, whether or not the child happens to be in the archive.
        files = {'suifleet-edge/src/main.rs': '#[path = "../../interfaces/helper.rs"]\nmod helper;\nfn main() {}\n',
                 'interfaces/helper.rs': 'pub mod child;\n'}
        for with_child in (False, True):
            with self.subTest(with_child=with_child):
                pending = dict(files, **({'interfaces/child.rs': 'pub fn run() {}\n'} if with_child else {}))
                archive = self.archive.with_name(f'outside-implicit-{with_child}.tar.gz')
                with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
                    for member in source.getmembers():
                        data = source.extractfile(member) if member.isfile() else None
                        if member.name in pending:
                            text = pending.pop(member.name).encode()
                            member.size, data = len(text), io.BytesIO(text)
                        tar.addfile(member, data)
                    for name, text in pending.items():
                        added = tarfile.TarInfo(name)
                        added.size, added.mode = len(text), 0o644
                        tar.addfile(added, io.BytesIO(text.encode()))
                self.write_recipe(sha=sha256(archive.read_bytes()))
                original, self.archive = self.archive, archive
                try:
                    with mock.patch.object(audit, 'download', self.fake_download):
                        report = audit.check(self.layer, fetch=True)
                finally:
                    self.archive = original
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
                expected = 'interfaces/helper.rs declares module(s) child without #[path]'
                self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
                self.assertFalse(report['ok'])

    def test_fetch_refuses_paths_that_cancel_a_directory(self):
        # The reviewer's fetched-layout reproduction: a pinned archive holding
        # tools/helper.rs but not tools/anchor must not audit clean for
        # ../tools/anchor/../helper.rs, and neither may one that has it.
        files = {'suifleet-edge/src/main.rs': '#[path = "../tools/anchor/../helper.rs"]\nmod helper;\n'
                 'fn main() { helper::f() }\n',
                 'suifleet-edge/tools/helper.rs': 'pub fn f() {}\n'}
        for with_anchor in (False, True):
            with self.subTest(with_anchor=with_anchor):
                pending = dict(files, **({'suifleet-edge/tools/anchor/README': 'anchor\n'} if with_anchor else {}))
                archive = self.archive.with_name(f'cancelled-directory-{with_anchor}.tar.gz')
                with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
                    for member in source.getmembers():
                        data = source.extractfile(member) if member.isfile() else None
                        if member.name in pending:
                            text = pending.pop(member.name).encode()
                            member.size, data = len(text), io.BytesIO(text)
                        tar.addfile(member, data)
                    for name, text in pending.items():
                        added = tarfile.TarInfo(name)
                        added.size, added.mode = len(text), 0o644
                        tar.addfile(added, io.BytesIO(text.encode()))
                self.write_recipe(sha=sha256(archive.read_bytes()))
                original, self.archive = self.archive, archive
                try:
                    with mock.patch.object(audit, 'download', self.fake_download):
                        report = audit.check(self.layer, fetch=True)
                finally:
                    self.archive = original
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
                expected = 'suifleet-edge/src/main.rs references a path that cancels a directory with ".."'
                self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
                self.assertFalse(report['ok'])

    def test_fetch_refuses_directory_only_terminal_syntax(self):
        # Review 5204's fetched-layout reproduction: a pinned archive holding
        # extra/helper.inc must not audit clean for "../extra/helper.inc/" or
        # "../extra/helper.inc/.", which rustc rejects as "Not a directory".
        for index, literal in enumerate(('../extra/helper.inc/', '../extra/helper.inc/.')):
            with self.subTest(literal):
                pending = {'suifleet-edge/src/main.rs': f'#[r#path = "{literal}"]\nmod helper;\n'
                           'fn main() { helper::f() }\n',
                           'suifleet-edge/extra/helper.inc': 'pub fn f() {}\n'}
                archive = self.archive.with_name(f'directory-syntax-{index}.tar.gz')
                with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
                    for member in source.getmembers():
                        data = source.extractfile(member) if member.isfile() else None
                        if member.name in pending:
                            text = pending.pop(member.name).encode()
                            member.size, data = len(text), io.BytesIO(text)
                        tar.addfile(member, data)
                    for name, text in pending.items():
                        added = tarfile.TarInfo(name)
                        added.size, added.mode = len(text), 0o644
                        tar.addfile(added, io.BytesIO(text.encode()))
                self.write_recipe(sha=sha256(archive.read_bytes()))
                original, self.archive = self.archive, archive
                try:
                    with mock.patch.object(audit, 'download', self.fake_download):
                        report = audit.check(self.layer, fetch=True)
                finally:
                    self.archive = original
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
                expected = f'suifleet-edge/src/main.rs references a directory, not a file: {literal}'
                self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
                self.assertFalse(report['ok'])

    def test_fetch_follows_code_includes_whatever_their_suffix(self):
        # The reviewer's reproduction: a pinned archive containing a non-.rs
        # include! target whose own include is absent must not audit clean.
        files = {'suifleet-edge/src/main.rs': 'include!("../tools/entry.inc");\nfn main() { f() }\n',
                 'suifleet-edge/tools/entry.inc': 'include!("helper.rs");\n'}
        archive = self.archive.with_name('code-include.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name in files:
                    text = files.pop(member.name).encode()
                    member.size, data = len(text), io.BytesIO(text)
                tar.addfile(member, data)
            for name, text in files.items():
                added = tarfile.TarInfo(name)
                added.size, added.mode = len(text), 0o644
                tar.addfile(added, io.BytesIO(text.encode()))
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        expected = 'suifleet-edge/tools/entry.inc references suifleet-edge/tools/helper.rs, absent from the source layout'
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        # Committed and packaged, the chain audits clean end to end.
        self.repo.write('suifleet-edge/src/main.rs', 'include!("../tools/entry.inc");\nfn main() { f() }\n')
        self.repo.write('suifleet-edge/tools/entry.inc', 'include!("helper.rs");\n')
        self.repo.write('suifleet-edge/tools/helper.rs', 'pub fn f() {}\n')
        rev = self.repo.commit()
        complete = self.archive.with_name(f'suifleet-edge-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, complete)
        self.assertIn('suifleet-edge/tools/helper.rs', info['paths'])
        self.archive = complete
        self.write_recipe(url=self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}'),
                          sha=info['sha256'], srcrev=rev)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])

    def test_fetch_scans_declared_targets_whatever_their_suffix(self):
        # The reviewer's reproduction: a pinned archive whose manifest declares
        # a non-.rs [[bin]] that includes an absent file must not audit clean.
        manifest = CARGO_TOML + '\n[[bin]]\nname = "standalone-check"\npath = "tools/entry.inc"\n'
        entry = 'const M: &str = include_str!("message.txt");\nfn main() {}\n'
        files = {'suifleet-edge/Cargo.toml': manifest, 'suifleet-edge/tools/entry.inc': entry}
        archive = self.archive.with_name('declared-entry.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name in files:
                    text = files.pop(member.name).encode()
                    member.size, data = len(text), io.BytesIO(text)
                tar.addfile(member, data)
            for name, text in files.items():
                added = tarfile.TarInfo(name)
                added.size, added.mode = len(text), 0o644
                tar.addfile(added, io.BytesIO(text.encode()))
        self.archive = archive
        # BitBake and Cargo resolve every one of these spellings to the same
        # crate directory, so each must find the missing input (the reviewer's
        # second reproduction used "./suifleet-edge" and audited clean).
        spellings = ('', 'CARGO_SRC_DIR = "suifleet-edge"', 'CARGO_SRC_DIR = "./suifleet-edge"',
                     'CARGO_SRC_DIR = "suifleet-edge/"', 'CARGO_SRC_DIR = ".//suifleet-edge/."')
        expected = 'suifleet-edge/tools/entry.inc references suifleet-edge/tools/message.txt, absent from the source layout'
        for extra in spellings:
            with self.subTest(extra=extra):
                self.write_recipe(extra=extra, sha=sha256(archive.read_bytes()))
                with mock.patch.object(audit, 'download', self.fake_download):
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
                self.assertFalse(report['ok'])
                self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        # Committed and packaged, the entry point and its data audit clean
        # under every equivalent spelling, in repository and fetch mode.
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        self.repo.write('suifleet-edge/tools/entry.inc', entry)
        self.repo.write('suifleet-edge/tools/message.txt', 'hello\n')
        rev = self.repo.commit()
        complete = self.archive.with_name(f'suifleet-edge-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, complete)
        self.assertIn('suifleet-edge/tools/message.txt', info['paths'])
        self.archive = complete
        for extra in spellings:
            with self.subTest(extra=extra, complete=True):
                self.write_recipe(extra=extra, url=self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}'),
                                  sha=info['sha256'], srcrev=rev)
                with mock.patch.object(audit, 'download', self.fake_download):
                    report = audit.check(self.layer, repo=self.repo.root, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
                self.assertEqual(report['errors'], [])

    def test_fetch_rejects_directory_symlinks_in_the_daemon_layout(self):
        # The reviewer's reproduction: a confined directory alias lets the
        # manifest declare alias/entry.inc, which rglob never yields, so its
        # absent include_str! input escaped the scan. The layout is refused.
        manifest = CARGO_TOML + '\n[[bin]]\nname = "standalone-check"\npath = "alias/entry.inc"\n'
        entry = 'const M: &str = include_str!("message.txt");\nfn main() {}\n'
        files = {'suifleet-edge/Cargo.toml': manifest, 'suifleet-edge/tools/entry.inc': entry}
        archive = self.archive.with_name('aliased-entry.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name in files:
                    text = files.pop(member.name).encode()
                    member.size, data = len(text), io.BytesIO(text)
                tar.addfile(member, data)
            for name, text in files.items():
                added = tarfile.TarInfo(name)
                added.size, added.mode = len(text), 0o644
                tar.addfile(added, io.BytesIO(text.encode()))
            alias = tarfile.TarInfo('suifleet-edge/alias')
            alias.type, alias.linkname = tarfile.SYMTYPE, 'tools'
            tar.addfile(alias)
        self.archive = archive
        self.write_recipe(sha=sha256(archive.read_bytes()))
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertFalse(report['ok'])
        expected = 'directory symlinks in the daemon source layout: suifleet-edge/alias'
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])

    def test_fetch_rejects_path_attributes_in_inline_modules(self):
        # The reviewer's reproduction as a pinned archive: both child.rs files
        # are present, yet rustc needs the one under tools/nested/. The audit
        # must refuse the unmodelled resolution instead of reporting clean.
        manifest = CARGO_TOML + '\n[[bin]]\nname = "nested-target"\npath = "tools/entry.rs"\n'
        base = self.archive
        for module in ('nested', 'café'):
            with self.subTest(module):
                self.archive = base
                self._check_fetch_rejects_inline_path(manifest, module)

    def _check_fetch_rejects_inline_path(self, manifest, module):
        entry = (f'mod {module} {{\n    #[path = "child.rs"]\n    mod child;\n    pub fn run() {{ child::run() }}\n}}\n'
                 f'fn main() {{ {module}::run() }}\n')
        files = {'suifleet-edge/Cargo.toml': manifest, 'suifleet-edge/tools/entry.rs': entry,
                 'suifleet-edge/tools/child.rs': 'pub fn run() {}\n',
                 f'suifleet-edge/tools/{module}/child.rs': 'pub fn run() {}\n'}
        archive = self.archive.with_name('nested-path.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(archive, 'w:gz') as tar:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name in files:
                    text = files.pop(member.name).encode()
                    member.size, data = len(text), io.BytesIO(text)
                tar.addfile(member, data)
            for name, text in files.items():
                added = tarfile.TarInfo(name)
                added.size, added.mode = len(text), 0o644
                tar.addfile(added, io.BytesIO(text.encode()))
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        expected = "suifleet-edge/tools/entry.rs: #[path] attribute 'child.rs' inside or on an inline"
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        self.assertFalse(report['ok'])

    def test_fetch_rejects_runtime_state_in_a_pinned_archive(self):
        # A checksum-pinned, otherwise coherent archive that carries runtime state.
        evil = self.archive.with_name('evil.tar.gz')
        with tarfile.open(self.archive) as source, tarfile.open(evil, 'w:gz') as tar:
            for member in source.getmembers():
                tar.addfile(member, source.extractfile(member) if member.isfile() else None)
            for name in ('suifleet-edge/examples/device.key', 'var/lib/suifleet/journal.sqlite'):
                info = tarfile.TarInfo(name)
                info.size = 6
                tar.addfile(info, io.BytesIO(b'secret'))
        self.archive = evil
        self.write_recipe(sha=sha256(evil.read_bytes()))
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        for rel in ('source/suifleet-edge/examples/device.key', 'source/var/lib/suifleet/journal.sqlite'):
            self.assertIn(f'suifleet_1.0.bb: runtime identity/state-like file in fetched source: {rel}', report['errors'])
        self.assertFalse(report['ok'])

    def test_unpack_destinations_are_confined_before_any_write(self):
        outside = Path(self.tmp.name) / 'escaped'
        cases = {
            f"subdir '{outside}' is not a plain relative path": ('subdir=source', f'subdir={outside}'),
            "subdir '../../escaped' is not a plain relative path": ('subdir=source', 'subdir=../../escaped'),
            "S '../escaped' is not a plain relative path": ('S = "${SUIFLEET_UNPACKDIR}/source"', 'S = "${SUIFLEET_UNPACKDIR}/../escaped"'),
            "file:// subdir '../escaped/suifleet.service'": ('file://suifleet.service', 'file://suifleet.service;subdir=../escaped'),
        }
        downloads = []

        def recording_download(url, target):
            downloads.append(url)
            return self.fake_download(url, target)
        for label, (old, new) in cases.items():
            with self.subTest(label):
                self.write_recipe()
                path = self.recipe_dir / 'suifleet_1.0.bb'
                path.write_text(path.read_text().replace(old, new))
                downloads.clear()
                with mock.patch.object(audit, 'download', recording_download):
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
                self.assertFalse(report['ok'])
                self.assertTrue(any(label in e and 'nothing fetched' in e for e in report['errors']), report['errors'])
                self.assertEqual(downloads, [])
                self.assertFalse(outside.exists())
                self.assertEqual(list(Path(report['fetch_dir']).iterdir()), [])
        # A destination reached through a symlink unpacked by an earlier source is re-checked before writing.
        link = self.archive.with_name('link.tar.gz')
        with tarfile.open(link, 'w:gz') as tar:
            info = tarfile.TarInfo('hop')
            info.type, info.linkname = tarfile.SYMTYPE, '.'
            tar.addfile(info)
        with self.assertRaisesRegex(ValueError, 'outside the unpack directory'):
            root = Path(tempfile.mkdtemp(dir=self.tmp.name))
            (root / 'pn').mkdir()
            audit.safe_extract(link, root / 'pn')
            (root / 'pn/hop').unlink()
            (root / 'pn/hop').symlink_to(outside.parent)
            audit.confined(root / 'pn', 'hop/escaped', 'subdir')

    def test_unpack_root_must_be_a_new_plain_name_in_the_scratch_directory(self):
        outside = Path(self.tmp.name) / 'escaped'
        for pn in ('../escaped', f'{outside}', 'a/b', '..', 'downloads'):
            with self.subTest(pn=pn):
                recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
                recipe.vars['PN'] = pn
                workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
                with mock.patch.object(audit, 'download', side_effect=AssertionError('fetched')):
                    errors, fetched = audit.fetch_recipe(recipe, workdir)
                self.assertTrue(any('not a usable unpack directory name' in e and 'nothing fetched' in e for e in errors), errors)
                self.assertEqual((fetched, list(workdir.iterdir())), ([], []))
                self.assertFalse(outside.exists())
        # An existing entry (here a symlink leaving the scratch directory) is never reused.
        workdir = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (workdir / 'suifleet').symlink_to(outside)
        recipe = audit.Recipe(self.recipe_dir / 'suifleet_1.0.bb', self.layer)
        with mock.patch.object(audit, 'download', side_effect=AssertionError('fetched')):
            errors, _ = audit.fetch_recipe(recipe, workdir)
        self.assertTrue(any('already exists' in e and 'nothing fetched' in e for e in errors), errors)
        self.assertFalse(outside.exists())

    def test_final_file_writes_never_follow_links(self):
        root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        outside = Path(tempfile.mkdtemp(dir=self.tmp.name))
        victim = outside / 'victim'
        victim.write_text('original')
        source = Path(self.tmp.name) / 'payload'
        source.write_text('payload')
        (root / 'link').symlink_to(victim)
        (root / 'dangling').symlink_to(outside / 'created')
        (root / 'inside').symlink_to('kept')
        (root / 'kept').write_text('kept')
        (root / 'dir').symlink_to(outside)
        for rel, message in (('link', 'already exists'), ('dangling', 'already exists'), ('inside', 'already exists'),
                             ('kept', 'already exists'), ('dir/new', 'outside the unpack directory')):
            with self.subTest(rel):
                with self.assertRaisesRegex(ValueError, message):
                    audit.write_new(root, rel, source)
        self.assertEqual((victim.read_text(), (root / 'kept').read_text()), ('original', 'kept'))
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ['victim'])
        audit.write_new(root, 'sub/new', source)
        self.assertEqual((root / 'sub/new').read_text(), 'payload')
        # Through fetch: an archive plants a link where a later loose (unpack=0) source would be copied.
        planted = self.archive.with_name('planted.tar.gz')
        with tarfile.open(self.archive) as original, tarfile.open(planted, 'w:gz') as tar:
            for member in original.getmembers():
                tar.addfile(member, original.extractfile(member) if member.isfile() else None)
            info = tarfile.TarInfo('loose.bin')
            info.type, info.linkname = tarfile.SYMTYPE, 'LICENSE'
            tar.addfile(info)
        loose = b'loose payload'
        loose_url = f'https://raw.githubusercontent.com/example/meta-suifleet/{"c" * 40}/loose.bin'
        contents = {self.url: planted.read_bytes(), loose_url: loose}

        def download(url, target):
            Path(target).write_bytes(contents[url])
            return sha256(contents[url])
        self.write_recipe(sha=sha256(contents[self.url]),
                          extra=f'SRC_URI += "{loose_url};subdir=source;name=loose"\n'
                                f'SRC_URI[loose.sha256sum] = "{sha256(loose)}"\n')
        with mock.patch.object(audit, 'download', download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertTrue(any("unpack destination 'source/loose.bin' already exists" in e for e in report['errors']),
                        report['errors'])
        self.assertEqual((Path(report['fetch_dir']) / 'suifleet/source/LICENSE').read_text(), LICENSE)

    def test_fetch_is_skipped_for_recipes_with_static_findings(self):
        self.write_recipe(url=f'http://example.com/{self.archive.name}')
        with mock.patch.object(audit, 'download', side_effect=AssertionError('fetched')):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertTrue(any('must use https' in e for e in report['errors']))
        entry = report['recipes']['suifleet']
        self.assertEqual((entry['fetched'], entry['fetch_skipped']), (None, 'static source findings'))
        self.assertEqual(list(Path(report['fetch_dir']).iterdir()), [])

    def test_several_recipes_for_one_pn_are_rejected_not_collapsed(self):
        base = (self.recipe_dir / 'suifleet_1.0.bb').read_text()
        cases = {
            'second version': ('suifleet_2.0.bb', base),
            'explicit PN in another file': ('zz-suifleet_1.0.bb', base + 'PN = "suifleet"\n'),
        }
        for label, (name, text) in cases.items():
            with self.subTest(label):
                extra = self.recipe_dir / name
                extra.write_text(text)
                self.addCleanup(extra.unlink, missing_ok=True)
                with mock.patch.object(audit, 'fetch_recipe', side_effect=AssertionError('fetched')) as fetch:
                    report = audit.check(self.layer, fetch=True)
                self.addCleanup(shutil.rmtree, report['fetch_dir'])
                fetch.assert_not_called()
                self.assertFalse(report['ok'])
                self.assertTrue(any(f'{name}: PN \'suifleet\' is also provided by recipes-edge/suifleet/suifleet_1.0.bb' in e
                                    for e in report['errors']), report['errors'])
                self.assertEqual(list(report['recipes']), ['suifleet'])
                entry = report['recipes']['suifleet']
                self.assertEqual((entry['file'], entry['duplicates']),
                                 ('recipes-edge/suifleet/suifleet_1.0.bb', [f'recipes-edge/suifleet/{name}']))
                self.assertEqual(entry['fetch_skipped'], 'static source findings')
                extra.unlink()
        self.assertEqual(audit.check(self.layer)['errors'], [])

    def test_unpack_refuses_escaping_members_and_links(self):
        for member in ('../escape', '/abs'):
            for kind in ('file', 'symlink'):
                with self.subTest(member=member, kind=kind):
                    data = io.BytesIO()
                    with tarfile.open(fileobj=data, mode='w:gz') as tar:
                        info = tarfile.TarInfo(member if kind == 'file' else 'link')
                        if kind == 'symlink':
                            info.type, info.linkname = tarfile.SYMTYPE, member
                        tar.addfile(info, io.BytesIO(b''))
                    archive = Path(self.tmp.name) / 'evil.tar.gz'
                    archive.write_bytes(data.getvalue())
                    with self.assertRaisesRegex(ValueError, 'escapes'):
                        audit.safe_extract(archive, Path(tempfile.mkdtemp(dir=self.tmp.name)))


    def link_archive(self, *entries):
        """A .tar.gz of (name, kind, data_or_linkname) entries in order."""
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as tar:
            for name, kind, value in entries:
                info = tarfile.TarInfo(name)
                if kind == 'dir':
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                elif kind in ('sym', 'hard'):
                    info.type, info.linkname = (tarfile.SYMTYPE if kind == 'sym' else tarfile.LNKTYPE), value
                    tar.addfile(info)
                else:
                    info.size = len(value)
                    tar.addfile(info, io.BytesIO(value))
        archive = Path(tempfile.mkdtemp(dir=self.tmp.name)) / 'links.tar.gz'
        archive.write_bytes(data.getvalue())
        return archive

    def test_chained_and_traversing_links_are_refused_before_anything_is_written(self):
        # Each link here is lexically inside the unpack directory when checked
        # alone; together (or through a link earlier in the archive) they leave it.
        cases = {
            'chain whose parts each look confined': (
                [('a/b/s', 'sym', '../../c'), ('c', 'dir', None), ('l', 'sym', 'a/b/s/../..')], 'passes through a link'),
            'link to a link': ([('t', 'file', b't'), ('one', 'sym', 't'), ('two', 'sym', 'one')], 'chained link'),
            'link to a later link': ([('two', 'sym', 'one'), ('one', 'sym', '.')], 'chained link'),
            'member written through an archive link': ([('hop', 'sym', '.'), ('hop/x', 'file', b'x')], 'written through a link'),
            'member written through a link to a parent': ([('d', 'dir', None), ('d/up', 'sym', '..'), ('d/up/d/up/x', 'file', b'x')],
                                                          'written through a link'),
            'target through a missing directory': ([('l', 'sym', 'later/..')], 'not a directory of the archive'),
            'target through a regular file': ([('f', 'file', b'f'), ('l', 'sym', 'f/..')], 'not a directory of the archive'),
            'hard link': ([('f', 'file', b'f'), ('h', 'hard', 'f')], 'hard link members are not supported'),
            'duplicate member': ([('d', 'dir', None), ('d', 'sym', '.')], 'duplicate member'),
            'dot-dot member': ([('d/../x', 'file', b'x')], 'escapes'),
            'empty link target': ([('l', 'sym', '')], 'escapes'),
        }
        for label, (entries, message) in cases.items():
            with self.subTest(label):
                archive = self.link_archive(*entries)
                outer = Path(tempfile.mkdtemp(dir=self.tmp.name))
                destination = outer / 'pn'
                destination.mkdir()
                with self.assertRaisesRegex(ValueError, message):
                    audit.safe_extract(archive, destination)
                self.assertEqual(list(destination.iterdir()), [])
                self.assertEqual([p.name for p in outer.iterdir()], ['pn'])

    def test_links_unpacked_earlier_are_part_of_the_graph(self):
        root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        outside = Path(tempfile.mkdtemp(dir=self.tmp.name))
        unpack = root / 'pn'
        unpack.mkdir()
        (unpack / 'up').symlink_to('.')
        (unpack / 'away').symlink_to(outside)
        for entries, message in (([('up/up/x', 'file', b'x')], 'written through a link'),
                                 ([('away/x', 'file', b'x')], 'written through a link'),
                                 ([('l', 'sym', 'up/..')], 'passes through a link'),
                                 ([('l', 'sym', 'up')], 'chained link')):
            with self.subTest(entries=entries):
                with self.assertRaisesRegex(ValueError, message):
                    audit.safe_extract(self.link_archive(*entries), unpack)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(sorted(p.name for p in unpack.iterdir()), ['away', 'up'])
        # A later source cannot complete an earlier dangling link: the link
        # target must traverse real directories, and every link under the
        # recipe's unpack directory is re-resolved after each extraction.
        fresh = Path(tempfile.mkdtemp(dir=self.tmp.name)) / 'pn'
        (fresh / 'sub').mkdir(parents=True)
        (fresh / 'sub/x').symlink_to('..')
        (fresh / 'l').symlink_to('sub/x/..')
        with self.assertRaisesRegex(ValueError, 'resolve outside the unpack directory after extraction: l'):
            audit.safe_extract(self.link_archive(('f', 'file', b'f')), fresh / 'sub', root=fresh)

    def test_writes_never_follow_a_link_swapped_in_after_validation(self):
        destination = Path(tempfile.mkdtemp(dir=self.tmp.name)) / 'pn'
        destination.mkdir()
        outside = Path(tempfile.mkdtemp(dir=self.tmp.name))
        archive = self.link_archive(('d', 'dir', None), ('d/x', 'file', b'x'))
        real = audit.link_graph_errors

        def swap(*args):
            entries = real(*args)
            (destination / 'd').symlink_to(outside)
            return entries
        with mock.patch.object(audit, 'link_graph_errors', swap), self.assertRaises(OSError):
            audit.safe_extract(archive, destination)
        self.assertEqual(list(outside.iterdir()), [])

    def test_confined_links_like_the_published_node_archive_unpack(self):
        destination = Path(tempfile.mkdtemp(dir=self.tmp.name)) / 'pn'
        destination.mkdir()
        top = 'node-v1-linux-arm64'
        archive = self.link_archive(
            (f'{top}/', 'dir', None), (f'{top}/bin/', 'dir', None),
            (f'{top}/bin/npm', 'sym', '../lib/node_modules/npm/bin/npm-cli.js'),
            (f'{top}/lib/node_modules/npm/bin/npm-cli.js', 'file', b'cli'),
            (f'./{top}/here', 'sym', './bin/../lib'))
        audit.safe_extract(archive, destination)
        self.assertEqual((destination / top / 'bin/npm').read_bytes(), b'cli')
        self.assertEqual(os.readlink(destination / top / 'bin/npm'), '../lib/node_modules/npm/bin/npm-cli.js')
        self.assertTrue((destination / top / 'here/node_modules').is_dir())


class CodecFetchedLayoutTests(unittest.TestCase):
    """codec_errors checks a fetched codec as do_verify_codec in the recipe does."""

    REV = 'c' * 40
    NODE = '22.22.2'
    LICENSE = 'Apache-2.0 & MIT'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.layer = root / 'layer'
        (self.layer / 'recipes-edge/suifleet-walrus-codec').mkdir(parents=True)
        self.s_dir = root / 'codec'
        self.s_dir.mkdir()

    INSTALL = ('walrus-device-codec.cjs', 'walrus_wasm_bg.wasm', 'manifest.json',
               'THIRD_PARTY_LICENSES.txt', 'provenance.json')

    def build(self, schema=1, manifest_files=None, manifest_text=None, drop=(), install=None, pin=None):
        payload = {'walrus-device-codec.cjs': b'bundle\n', 'walrus_wasm_bg.wasm': b'\0asm',
                   'THIRD_PARTY_LICENSES.txt': b'licenses\n'}
        # drop removes a file together with its pin and manifest entry, so the
        # rest of the metadata stays coherent.
        payload = {name: data for name, data in payload.items() if name not in drop}
        for name, data in payload.items():
            (self.s_dir / name).write_bytes(data)
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
        if manifest_files is None:
            manifest_files = {name: {'sha256': sha} for name, sha in hashes.items()}
        manifest = {'schema': schema, 'node': self.NODE, 'files': manifest_files}
        (self.s_dir / 'manifest.json').write_text(manifest_text if manifest_text is not None else json.dumps(manifest))
        (self.s_dir / 'provenance.json').write_text(json.dumps(
            {'source_revision': self.REV, 'licenses': self.LICENSE.split(' & ')}))
        for name in ('manifest.json', 'provenance.json'):
            hashes[name] = hashlib.sha256((self.s_dir / name).read_bytes()).hexdigest()
        md5 = hashlib.md5(payload['THIRD_PARTY_LICENSES.txt']).hexdigest()
        lines = [f'LICENSE = "{self.LICENSE}"',
                 f'LIC_FILES_CHKSUM = "file://THIRD_PARTY_LICENSES.txt;md5={md5}"',
                 f'SUIFLEET_CODEC_SOURCE_REV = "{self.REV}"',
                 f'SUIFLEET_CODEC_NODE_VERSION = "{self.NODE}"']
        hashes.update(pin or {})
        lines += [f'SUIFLEET_CODEC_SHA256[{name}] = "{sha}"' for name, sha in sorted(hashes.items())]
        loop = install if install is not None else f'    for f in {" ".join(self.INSTALL)}; do'
        lines += ['do_install() {', '    install -d -m 0755 ${D}${SUIFLEET_CODEC_DIR}', loop,
                  '        install -m 0644 ${S}/$f ${D}${SUIFLEET_CODEC_DIR}/$f', '    done', '}']
        path = self.layer / 'recipes-edge/suifleet-walrus-codec/suifleet-walrus-codec_1.0.bb'
        path.write_text('\n'.join(lines) + '\n')
        return audit.codec_errors(audit.Recipe(path, self.layer), self.s_dir)

    def test_valid_codec_passes(self):
        self.assertEqual(self.build(), [])

    def test_manifest_schema_must_be_1(self):
        for schema in (2, 0, '1', None):
            with self.subTest(schema=schema):
                errors = self.build(schema=schema)
                self.assertTrue(any('codec manifest schema' in e and 'is not 1' in e for e in errors), errors)

    def test_manifest_hashes_must_match_the_recipe_pins(self):
        wrong = {'walrus-device-codec.cjs': {'sha256': 'f' * 64}}
        errors = self.build(manifest_files=wrong)
        self.assertTrue(any('codec manifest and recipe disagree on walrus-device-codec.cjs' in e for e in errors), errors)
        # A manifest entry for a file the recipe does not pin is refused, as BitBake does.
        errors = self.build(manifest_files={'other.js': {'sha256': 'f' * 64}})
        self.assertTrue(any('disagree on other.js' in e for e in errors), errors)
        errors = self.build(manifest_files={'walrus_wasm_bg.wasm': 'not-an-object'})
        self.assertTrue(any('entry for walrus_wasm_bg.wasm is not an object' in e for e in errors), errors)

    def test_unpinned_manifest_entries_are_refused_whatever_their_value(self):
        # An unpinned name has no pin to compare, so a missing hash must not
        # match it; a non-object entry would crash do_verify_codec.
        for value in (None, 'not-an-object', [], 7, {}, {'sha256': None}):
            with self.subTest(value=value):
                errors = self.build(manifest_files={'other.js': value})
                self.assertTrue(any('other.js' in e for e in errors), errors)
                errors = [e for e in errors if 'other.js' in e]
                expected = 'disagree on other.js' if isinstance(value, dict) else 'entry for other.js is not an object'
                self.assertTrue(any(expected in e for e in errors), errors)

    def test_manifest_without_files_object_is_refused(self):
        for files in ([], 'x'):
            with self.subTest(files=files):
                errors = self.build(manifest_files=files)
                self.assertTrue(any('no "files" object' in e for e in errors), errors)

    def test_every_do_install_file_needs_a_pin_and_a_fetched_file(self):
        # Removing a required file with its pin and manifest entry leaves the
        # pins, manifest and fetched files coherent, but do_install would fail.
        for name in ('walrus_wasm_bg.wasm', 'walrus-device-codec.cjs'):
            with self.subTest(dropped=name):
                errors = self.build(drop=(name,))
                self.assertTrue(any(f'do_install file {name} has no lowercase SHA-256' in e for e in errors), errors)
                recipe = audit.Recipe(self.layer / 'recipes-edge/suifleet-walrus-codec/suifleet-walrus-codec_1.0.bb',
                                      self.layer)
                self.assertTrue(any(f'do_install file {name}' in e for e in audit.pin_errors(recipe)))

    def test_do_install_pins_must_be_sha256(self):
        errors = self.build(pin={'walrus_wasm_bg.wasm': 'F' * 64})
        self.assertTrue(any('do_install file walrus_wasm_bg.wasm has no lowercase SHA-256' in e for e in errors), errors)

    def test_unauditable_do_install_file_list_is_refused(self):
        for loop in ('    for f in ${FILES}; do', '    install -m 0644 ${S}/x ${D}/x', ''):
            with self.subTest(loop=loop):
                errors = self.build(install=loop)
                self.assertTrue(any('no single literal "for f in ...; do" file list' in e for e in errors), errors)

    def test_repository_codec_recipe_installs_only_pinned_files(self):
        path = ROOT / 'suifleet-edge/meta-suifleet/recipes-edge/suifleet-walrus-codec/suifleet-walrus-codec_1.0.bb'
        recipe = audit.Recipe(path, ROOT / 'suifleet-edge/meta-suifleet')
        self.assertEqual(sorted(audit.codec_install_files(recipe)), sorted(self.INSTALL))
        self.assertEqual(audit.codec_install_errors(recipe), [])

    def test_unreadable_manifest_is_reported_not_raised(self):
        errors = self.build(manifest_text='{not json')
        self.assertTrue(any('codec manifest.json is not readable JSON' in e for e in errors), errors)
        errors = self.build(manifest_text='[]')
        self.assertTrue(any('codec manifest.json is not a JSON object' in e for e in errors), errors)


class NodeRecipeTests(unittest.TestCase):
    """suifleet-node, on a copy of the shipped layer: the fetched archive must
    hold what do_install copies, and its version must satisfy the codec."""

    NODE_RECIPE = 'recipes-devtools/suifleet-node/suifleet-node_22.22.2.bb'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.layer = Path(self.tmp.name) / 'meta-suifleet'
        shutil.copytree(ROOT / 'suifleet-edge/meta-suifleet', self.layer)
        self.recipe = self.layer / self.NODE_RECIPE
        self.archive = Path(self.tmp.name) / 'node.tar.xz'

    @staticmethod
    def elf(machine, kind=2, phnum=1, p_type=1, filesz=8, phoff=64, version=1, size=None, memsz=None, vaddr=None, align=0,
            entry=None, flags=5):
        """A minimal ELF64 LE file: header, phnum program headers, then a segment.

        e_entry defaults to the first byte of the (R+X) segment."""
        def le(value, n):
            return value.to_bytes(n, 'little')
        offset = 64 + 56 * phnum
        memsz = filesz if memsz is None else memsz
        vaddr = offset if vaddr is None else vaddr
        entry = vaddr if entry is None else entry
        header = (b'\x7fELF\x02\x01\x01' + bytes(9) + le(kind, 2) + le(machine, 2) + le(version, 4) + le(entry, 8)
                  + le(phoff, 8) + le(0, 8) + le(0, 4) + le(64, 2) + le(56, 2) + le(phnum, 2) + le(0, 6))
        program = (le(p_type, 4) + le(flags, 4) + le(offset, 8) + le(vaddr, 8) + le(vaddr, 8) + le(filesz, 8)
                   + le(memsz, 8) + le(align, 8)) * phnum
        data = header + program + b'\xaa' * 8
        return data if size is None else data[:size]

    def fetch(self, files):
        """Fetch the Node recipe offline from a checksum-correct archive of files."""
        with tarfile.open(self.archive, 'w:xz') as tar:
            for rel, data in files.items():
                info = tarfile.TarInfo(f'node-v22.22.2-linux-arm64/{rel}')
                info.size, info.mode = len(data), 0o755
                tar.addfile(info, io.BytesIO(data))
        text = self.recipe.read_text()
        text = re.sub(r'sha256sum=[0-9a-f]{64}', f'sha256sum={sha256(self.archive.read_bytes())}', text)
        text = re.sub(r'LICENSE;md5=[0-9a-f]{32}', f'LICENSE;md5={hashlib.md5(files.get("LICENSE", b"")).hexdigest()}', text)
        self.recipe.write_text(text)

        def download(url, target):
            shutil.copyfile(self.archive, target)
            return sha256(self.archive.read_bytes())
        with mock.patch.object(audit, 'download', download):
            report = audit.check(self.layer, fetch=True, only=['suifleet-node'])
        self.addCleanup(shutil.rmtree, report['fetch_dir'], True)
        return report

    def test_complete_aarch64_archive_passes(self):
        for kind in (2, 3):
            with self.subTest(e_type=kind):
                report = self.fetch({'LICENSE': b'node license\n', 'bin/node': self.elf(183, kind)})
                self.assertEqual(report['errors'], [])
        # A larger memory image (.bss) and a congruent page alignment are valid.
        for label, data in {'p_memsz above p_filesz': self.elf(183, memsz=4096),
                            'page aligned': self.elf(183, vaddr=0x400000 + 120, align=0x1000),
                            # Review finding: the last complete aligned instruction, not the last byte.
                            'entry at last aligned instruction': self.elf(183, entry=120 + 4),
                            'entry in an unaligned segment': self.elf(183, vaddr=122, align=2, entry=124, filesz=6)}.items():
            with self.subTest(label):
                report = self.fetch({'LICENSE': b'node license\n', 'bin/node': data})
                self.assertEqual(report['errors'], [])
                self.assertTrue(report['recipes']['suifleet-node']['fetched'][0]['verified'])

    def test_checksum_correct_archive_without_the_installed_executable_fails(self):
        report = self.fetch({'LICENSE': b'node license\n', 'bin/npm': b'#!/bin/sh\n'})
        self.assertFalse(report['ok'])
        self.assertTrue(any('do_install input bin/node is absent' in e for e in report['errors']), report['errors'])

    def test_installed_executable_must_be_an_aarch64_elf(self):
        cases = {'x86-64': self.elf(62), 'shell script': b'#!/bin/sh\n', 'truncated': b'\x7fELF',
                 '32-bit': b'\x7fELF\x01\x01\x01' + bytes(9) + (2).to_bytes(2, 'little') + (183).to_bytes(2, 'little'),
                 'relocatable': self.elf(183, kind=1),
                 # Review finding: only the first 20 bytes of an otherwise valid header.
                 'first 20 bytes': self.elf(183)[:20], 'header only': self.elf(183, size=64),
                 'no program headers': self.elf(183, phnum=0), 'bad e_version': self.elf(183, version=0),
                 'program headers past end': self.elf(183, phoff=4096),
                 'segment past end': self.elf(183, filesz=4096), 'no PT_LOAD': self.elf(183, p_type=6),
                 'empty PT_LOAD': self.elf(183, filesz=0),
                 # Review finding: a load segment whose file image exceeds its memory image.
                 'p_filesz above p_memsz': self.elf(183, memsz=4),
                 'alignment not a power of two': self.elf(183, align=3),
                 'offset and address not congruent': self.elf(183, vaddr=0x10001, align=0x1000),
                 # Combined review finding: e_entry must be in a file-backed executable PT_LOAD.
                 'ET_EXEC entry zero outside the segment': self.elf(183, entry=0),
                 'ET_DYN entry outside the segment': self.elf(183, kind=3, entry=0x10000),
                 'entry in a read-only segment': self.elf(183, flags=4),
                 'entry in a writable non-executable segment': self.elf(183, flags=6),
                 'entry past the file image (.bss)': self.elf(183, memsz=4096, entry=120 + 8),
                 # Review finding: AArch64 entry must be an aligned, complete instruction.
                 'entry at last file byte (unaligned)': self.elf(183, entry=120 + 7),
                 'entry unaligned inside the segment': self.elf(183, entry=120 + 2),
                 'entry instruction truncated by p_filesz': self.elf(183, filesz=6, entry=120 + 4),
                 'entry instruction straddles into .bss': self.elf(183, filesz=6, memsz=4096, entry=120 + 4)}
        for label, data in cases.items():
            with self.subTest(label):
                report = self.fetch({'LICENSE': b'node license\n', 'bin/node': data})
                self.assertTrue(any('not a 64-bit little-endian AArch64 ELF' in e for e in report['errors']), report['errors'])

    def test_do_install_inputs_must_be_the_audited_files(self):
        original = self.recipe.read_text()
        cases = {
            'extra input': original.replace('${S}/LICENSE ', '${S}/LICENSE ${S}/include/node.h '),
            'missing input': original.replace('    install -m 0644 ${S}/LICENSE ${D}${SUIFLEET_NODE_DIR}/LICENSE\n', ''),
            'no do_install': original.replace('do_install() {', 'do_other() {'),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertNotEqual(text, original)
                self.recipe.write_text(text)
                errors = audit.check(self.layer)['errors']
                self.assertTrue(any('Node do_install' in e for e in errors), errors)
        self.recipe.write_text(original)
        self.assertEqual(audit.check(self.layer)['errors'], [])

    def test_packaged_node_version_must_satisfy_the_codec_dependency(self):
        renamed = self.recipe.with_name('suifleet-node_22.22.3.bb')
        self.recipe.rename(renamed)
        inventory = self.layer / 'export-files.json'
        inventory.write_text(inventory.read_text().replace('suifleet-node_22.22.2.bb', 'suifleet-node_22.22.3.bb'))
        errors = audit.check(self.layer)['errors']
        self.assertTrue(any("PV '22.22.3' does not satisfy the codec dependency suifleet-node (= 22.22.2)" in e
                            for e in errors), errors)
        # Auditing only the codec does not load the Node recipe to compare.
        self.assertEqual(audit.check(self.layer, only=['suifleet-walrus-codec'])['errors'], [])

    def test_epoch_missing_recipe_and_missing_codec_pin_are_refused(self):
        original = self.recipe.read_text()
        self.recipe.write_text(original + 'PE = "1"\n')
        errors = audit.check(self.layer)['errors']
        self.assertTrue(any("with PE '1' does not satisfy" in e for e in errors), errors)
        self.recipe.unlink()
        errors = audit.check(self.layer)['errors']
        self.assertTrue(any("provides no 'suifleet-node' recipe" in e for e in errors), errors)
        self.recipe.write_text(original)
        codec = next(self.layer.glob('recipes-edge/suifleet-walrus-codec/*.bb'))
        codec.write_text(re.sub(r'^SUIFLEET_CODEC_NODE_VERSION = .*\n', '', codec.read_text(), flags=re.M))
        errors = audit.check(self.layer)['errors']
        self.assertTrue(any('lacks required SUIFLEET_CODEC_NODE_VERSION' in e for e in errors), errors)


    def test_package_version_must_come_from_pv_and_pe(self):
        # Review finding: PKGV/PKGE set the published package version that the
        # codec RDEPENDS resolves against, so any assignment form is refused.
        original = self.recipe.read_text()
        append = self.layer / 'recipes-devtools/suifleet-node/suifleet-node_%.bbappend'
        cases = {
            'PKGV': (original + 'PKGV = "99.0.0"\n', 'PKGV is assigned'),
            'PKGE': (original + 'PKGE = "1"\n', 'PKGE is assigned'),
            'weak PKGV': (original + 'PKGV ??= "99.0.0"\n', 'PKGV is assigned'),
            'PKGV append': (original + 'PKGV:append = "+x"\n', 'PKGV is assigned'),
            'package override': (original + 'PKGV:${PN} = "99.0.0"\n', 'PKGV is assigned'),
            'conditional override': (original + 'PKGE:aarch64 = "2"\n', 'PKGE is assigned'),
            'unparsed': (original + 'PKGV = 99\n', 'PKGV is assigned'),
            'inherit': (original + 'inherit gitpkgv\n', 'inherit gitpkgv'),
        }
        for label, (text, expected) in cases.items():
            with self.subTest(label):
                self.recipe.write_text(text)
                errors = audit.check(self.layer)['errors']
                self.assertTrue(any(expected in e for e in errors), errors)
        self.recipe.write_text(original)
        append.write_text('PKGV = "99.0.0"\n')
        inventory = self.layer / 'export-files.json'
        saved = inventory.read_text()
        files = json.loads(saved)
        files.append(append.relative_to(self.layer).as_posix())
        inventory.write_text(json.dumps(files))
        errors = audit.check(self.layer)['errors']
        self.assertTrue(any('PKGV is assigned' in e for e in errors), errors)
        append.unlink()
        inventory.write_text(saved)
        self.assertEqual(audit.check(self.layer)['errors'], [])


class RepositoryLayerTests(unittest.TestCase):
    # SF-295 published every layer source; any finding in the real layer is a regression.
    def test_repository_layer_has_no_source_findings(self):
        report = audit.check(ROOT / 'suifleet-edge/meta-suifleet')
        self.assertEqual(report['errors'], [])
        self.assertTrue(report['ok'])
        daemon = report['recipes']['suifleet']
        self.assertEqual(daemon['pv'], daemon['crate_version'])


if __name__ == '__main__':
    unittest.main()
