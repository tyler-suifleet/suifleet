"""Offline tests for the edge source distribution builder and layer source audit."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
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
        listing = ['LICENSE', 'suifleet-edge/Cargo.toml', 'suifleet-edge/Cargo.lock']
        info = packager.build(self.repo.root, 'HEAD', self.out / 'listed.tar.gz', listing)
        self.assertEqual(info['files'], 3)
        with self.assertRaisesRegex(ValueError, 'absent from revision'):
            packager.build(self.repo.root, 'HEAD', self.out / 'bad.tar.gz', ['nope'])


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

    def test_fetch_reports_checksum_and_layout_defects(self):
        self.write_recipe(sha='f' * 64)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertTrue(any('differs from pin' in e for e in report['errors']))
        # The published-archive defect: a workspace member left out of the file list.
        broken = self.archive.with_name('broken.tar.gz')
        packager.build(self.repo.root, self.rev, broken,
                       [p for p in self.info['paths'] if 'windows-runtime' not in p])
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
        self.write_recipe(sha=sha256(archive.read_bytes()))
        self.archive = archive
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        expected = 'suifleet-edge/tools/entry.inc references suifleet-edge/tools/message.txt, absent from the source layout'
        self.assertTrue(any(expected in e for e in report['errors']), report['errors'])
        # Committed and packaged, the entry point and its data audit clean.
        self.repo.write('suifleet-edge/Cargo.toml', manifest)
        self.repo.write('suifleet-edge/tools/entry.inc', entry)
        self.repo.write('suifleet-edge/tools/message.txt', 'hello\n')
        rev = self.repo.commit()
        complete = self.archive.with_name(f'suifleet-edge-{rev}.tar.gz')
        info = packager.build(self.repo.root, rev, complete)
        self.assertIn('suifleet-edge/tools/message.txt', info['paths'])
        self.archive = complete
        self.write_recipe(url=self.url.replace(f'suifleet-edge-{self.rev}', f'suifleet-edge-{rev}'),
                          sha=info['sha256'], srcrev=rev)
        with mock.patch.object(audit, 'download', self.fake_download):
            report = audit.check(self.layer, repo=self.repo.root, fetch=True)
        self.addCleanup(shutil.rmtree, report['fetch_dir'])
        self.assertEqual(report['errors'], [])

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
