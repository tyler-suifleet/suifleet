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
    # The codec's pre-parse local artifacts are being replaced by SF-295's
    # published archive; every other finding in the real layer is a regression.
    KNOWN_UNTIL_SF295 = ('suifleet-walrus-codec_1.0.bb: FILESEXTRAPATHS points outside the layer',
                         'suifleet-walrus-codec_1.0.bb: file:// source is not in the layer')

    def test_repository_layer_has_no_unexpected_source_findings(self):
        report = audit.check(ROOT / 'suifleet-edge/meta-suifleet')
        unexpected = [e for e in report['errors'] if not any(k in e for k in self.KNOWN_UNTIL_SF295)]
        self.assertEqual(unexpected, [])
        daemon = report['recipes']['suifleet']
        self.assertEqual(daemon['pv'], daemon['crate_version'])


if __name__ == '__main__':
    unittest.main()
