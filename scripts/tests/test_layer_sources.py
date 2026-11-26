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

    def test_workspace_members_in_every_supported_string_form_are_packaged(self):
        # A literal-string member was silently dropped by the old reader: the
        # archive declared windows-runtime but carried none of its files.
        for members in ("['windows-runtime']", '[\n  "windows-runtime", # the runtime\n]'):
            with self.subTest(members):
                self.repo.write('suifleet-edge/Cargo.toml',
                                CARGO_TOML.replace('members = ["windows-runtime"]', f'members = {members}'))
                self.repo.commit()
                paths = packager.plan(self.repo.root, 'HEAD')['paths']
                self.assertIn('suifleet-edge/windows-runtime/Cargo.toml', paths)
                self.assertIn('suifleet-edge/windows-runtime/src/lib.rs', paths)
        # Forms the strict reader does not model are refused, never skipped.
        for workspace in ('[workspace]\nmembers = ["windows\\u002druntime"]',
                          '[workspace]\nmembers = ["""windows-runtime"""]',
                          '[workspace]\n"members" = ["windows-runtime"]',
                          '[workspace]\nmembers = "windows-runtime"',
                          'workspace.members = ["windows-runtime"]',
                          'workspace = { members = ["windows-runtime"] }'):
            with self.subTest(workspace):
                self.repo.write('suifleet-edge/Cargo.toml',
                                CARGO_TOML.replace('[workspace]\nmembers = ["windows-runtime"]', workspace))
                self.repo.commit()
                with self.assertRaisesRegex(ValueError, 'unsupported'):
                    packager.plan(self.repo.root, 'HEAD')

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
    return f'''SUMMARY = "fixture"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5={md5(LICENSE)}"
SRC_URI = " \\
    {archive_url};subdir=source;name=edge \\
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
        (self.layer / 'conf/layer.conf').write_text('LAYERSERIES_COMPAT_meta-suifleet = "scarthgap wrynose"\n')
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

    def test_git_sources_need_https_and_full_srcrev(self):
        git = 'git://github.com/example/suifleet.git;protocol=https;nobranch=1'
        self.write_recipe(url=git, srcrev='main')
        self.assertTrue(any('40-hex SRCREV' in e for e in self.errors()))
        self.write_recipe(url=git.replace('protocol=https', 'protocol=ssh'))
        self.assertTrue(any('protocol=https' in e for e in self.errors()))

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
