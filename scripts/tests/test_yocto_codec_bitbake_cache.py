"""SF-307: a cached missing-input skip of the codec recipe refreshes with real BitBake.

Runs the committed suifleet-walrus-codec recipe in a minimal standalone BitBake
project (no OpenEmbedded-Core) and keeps one persistent parse cache. It parses
with the local codec inputs absent, stages them without touching any metadata
or the cache, then asks for the recipe again.

Set SUIFLEET_BITBAKE_DIRS to colon-separated BitBake checkouts (the directory
containing bin/bitbake), for example the kas-pinned Kirkstone, Scarthgap and
Wrynose trees. The tests are skipped when none is given.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).parents[2]
RECIPE = ROOT/'suifleet-edge/meta-suifleet/recipes-edge/suifleet-walrus-codec/suifleet-walrus-codec_1.0.bb'
INPUTS = ['walrus-device-codec.cjs', 'walrus_wasm_bg.wasm', 'manifest.json',
          'THIRD_PARTY_LICENSES.txt', 'provenance.json']
BITBAKES = [Path(p) for p in os.environ.get('SUIFLEET_BITBAKE_DIRS', '').split(':') if p]

BITBAKE_CONF = '''\
TMPDIR = "${TOPDIR}/tmp"
CACHE = "${TOPDIR}/cache"
PERSISTENT_DIR = "${TOPDIR}/persistent"
THISDIR = "${@os.path.dirname(d.getVar('FILE'))}"
PN = "${@bb.parse.vars_from_file(d.getVar('FILE', False),d)[0] or 'defaultpkgname'}"
PV = "${@bb.parse.vars_from_file(d.getVar('FILE', False),d)[1] or '1.0'}"
PR = "r0"
PF = "${PN}-${PV}-${PR}"
PROVIDES = "${PN}"
WORKDIR = "${TMPDIR}/work/${PF}"
T = "${WORKDIR}/temp"
STAMP = "${TMPDIR}/stamps/${PF}"
D = "${WORKDIR}/image"
libdir = "/usr/lib"
HOST_ARCH = "aarch64"
HOST_SYS = "aarch64-poky-linux"
TCLIBC = "glibc"
FILESPATH = "${TOPDIR}/empty:${TOPDIR}/codec-files"
DL_DIR = "${TOPDIR}/downloads"
BB_NUMBER_THREADS = "1"
BB_NUMBER_PARSE_THREADS = "1"
BB_SIGNATURE_HANDLER = "noop"
BB_BASEHASH_IGNORE_VARS = "TMPDIR TOPDIR FILE BB_CURRENTTASK"
export PATH
'''

LAYER_CONF = '''\
BBFILES += "${LAYERDIR}/recipes/*.bb ${LAYERDIR}/recipes/*.bbappend"
BBFILE_COLLECTIONS += "codectest"
BBFILE_PATTERN_codectest = "^${LAYERDIR}/recipes/"
BBFILE_PRIORITY_codectest = "5"
LAYERSERIES_CORENAMES = "codectest"
LAYERSERIES_COMPAT_codectest = "codectest"
'''

BASE_CLASS = '''\
python do_fetch() {
}
addtask fetch
python do_unpack() {
}
addtask unpack after do_fetch
python do_populate_lic() {
}
addtask populate_lic after do_unpack
python do_configure() {
}
addtask configure after do_unpack
python do_compile() {
}
addtask compile after do_configure
python do_install() {
}
addtask install after do_compile
python do_build() {
}
addtask build after do_install
'''

# Points the committed recipe at local inputs, as a downstream override would.
BBAPPEND = 'SRC_URI = "%s"\n' % ' '.join('file://%s;subdir=codec' % name for name in INPUTS)

# The guard without recorded parse dependencies (control for the cache test).
UNRECORDED_GUARD = ("    missing = [e[len('file://'):].split(';')[0] for e in local\n"
                    "               if not bb.utils.which(d.getVar('FILESPATH'), e[len('file://'):].split(';')[0])]\n")
# The earlier raw-text guard: FILESPATH only, no URL decoding or fetcher lookup.
RAW_TEXT_GUARD = ("    missing = []\n"
                  "    for e in local:\n"
                  "        name = e[len('file://'):].split(';')[0]\n"
                  "        found, searched = bb.utils.which(d.getVar('FILESPATH'), name, history=True)\n"
                  "        for path in searched:\n"
                  "            bb.parse.mark_dependency(d, path)\n"
                  "        if not found:\n"
                  "            missing.append(name)\n")


def replaced_guard(guard):
    text = RECIPE.read_text()
    loop = re.search(r'^    # Resolve local inputs.*?^                missing\.append\([^\n]*\n', text, re.M | re.S)
    if not loop:
        raise AssertionError('recipe no longer has the fetcher-resolution loop this control replaces')
    return text[:loop.start()] + guard + text[loop.end():]


def unrecorded_recipe():
    return replaced_guard(UNRECORDED_GUARD)


def raw_text_recipe():
    return replaced_guard(RAW_TEXT_GUARD)


def bbappend(entries):
    return 'SRC_URI = "%s"\n' % ' '.join('file://%s;subdir=codec' % entry for entry in entries)


def encoded_absolute(top):
    """Absolute file:// inputs under a directory whose space is percent-encoded."""
    return bbappend('%s/abs%%20dir/%s' % (top, name) for name in INPUTS)


def bitbake_version(bitbake):
    text = (bitbake/'lib/bb/__init__.py').read_text()
    return tuple(int(x) for x in re.search(r'^__version__ = "(\d+)\.(\d+)', text, re.M).groups())


class Project:
    def __init__(self, bitbake, top, recipe_text, append=BBAPPEND):
        self.bitbake = bitbake
        self.top = top
        (top/'conf').mkdir()
        (top/'classes').mkdir()
        (top/'recipes').mkdir()
        (top/'empty').mkdir()
        (top/'codec-files').mkdir()
        (top/'downloads').mkdir()
        (top/'abs dir').mkdir()
        (top/'conf/bblayers.conf').write_text('BBPATH = "%s"\nBBLAYERS = "%s"\n' % (top, top))
        (top/'conf/layer.conf').write_text(LAYER_CONF)
        (top/'conf/bitbake.conf').write_text(BITBAKE_CONF)
        (top/'classes/base.bbclass').write_text(BASE_CLASS)
        (top/'recipes'/RECIPE.name).write_text(recipe_text)
        (top/'recipes/suifleet-walrus-codec_%.bbappend').write_text(append(top) if callable(append) else append)
        # Provider for the codec's runtime dependency; its content is irrelevant here.
        (top/'recipes/suifleet-node_22.22.2.bb').write_text('LICENSE = "MIT"\n')
        self.metadata = {p: p.read_bytes() for p in top.rglob('*') if p.is_file()}

    def run(self, *args):
        env = {'PATH': '%s:%s' % (self.bitbake/'bin', os.environ.get('PATH', '/usr/bin:/bin')),
               'HOME': str(self.top), 'LC_ALL': 'C.UTF-8', 'LANG': 'C.UTF-8',
               'PYTHONDONTWRITEBYTECODE': '1', 'BBPATH': str(self.top)}
        return subprocess.run([str(self.bitbake/'bin/bitbake'), *args], cwd=self.top, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)

    def request(self):
        """bitbake -e for the recipe: resolves it through the parse cache."""
        return self.run('-e', 'suifleet-walrus-codec')

    def cache_files(self):
        return sorted((self.top/'cache').glob('bb_cache.dat*'))

    def stage_inputs(self, directory='codec-files'):
        for name in INPUTS:
            (self.top/directory/name).write_text('staged %s\n' % name)

    def metadata_unchanged(self):
        current = {p: p.read_bytes() for p in self.metadata}
        return current == self.metadata


@unittest.skipUnless(BITBAKES, 'set SUIFLEET_BITBAKE_DIRS to real BitBake checkouts')
class CachedCodecSkipTests(unittest.TestCase):
    def cycle(self, bitbake, recipe_text, append=BBAPPEND, directory='codec-files'):
        """Parse with absent inputs, stage them, request again; return both results."""
        with tempfile.TemporaryDirectory(prefix='sf307-bitbake-') as tmp:
            project = Project(bitbake, Path(tmp), recipe_text, append)
            first = project.request()
            self.assertNotEqual(first.returncode, 0, first.stdout[-4000:])
            self.assertIn('optional Walrus codec inputs not found', first.stdout, first.stdout[-4000:])
            for name in INPUTS:
                self.assertIn(name, first.stdout)
            cache = project.cache_files()
            self.assertTrue(cache, 'the first parse must leave a persistent parse cache')
            stamps = {p: p.stat().st_mtime_ns for p in cache}
            project.stage_inputs(directory)
            self.assertTrue(project.metadata_unchanged())
            self.assertEqual(project.cache_files(), cache, 'the cache was not cleared')
            self.assertEqual({p: p.stat().st_mtime_ns for p in cache}, stamps)
            return first, project.request()

    def test_staged_inputs_make_a_cached_skip_eligible(self):
        for bitbake in BITBAKES:
            with self.subTest(bitbake=str(bitbake)):
                _, second = self.cycle(bitbake, RECIPE.read_text())
                self.assertEqual(second.returncode, 0, second.stdout[-4000:])
                self.assertNotIn('was skipped', second.stdout)
                self.assertRegex(second.stdout, r'(?m)^PN="suifleet-walrus-codec"$')
                self.assertRegex(second.stdout, r'(?m)^SRC_URI="file://walrus-device-codec\.cjs;subdir=codec ')

    # Control: proves the harness really reuses the cache. Without the
    # recorded parse dependencies, the stale skip survives the staged inputs.
    def test_unrecorded_guard_keeps_the_stale_skip(self):
        for bitbake in BITBAKES:
            with self.subTest(bitbake=str(bitbake)):
                _, second = self.cycle(bitbake, unrecorded_recipe())
                self.assertNotEqual(second.returncode, 0, second.stdout[-4000:])
                self.assertIn('optional Walrus codec inputs not found', second.stdout)


    def assert_kept(self, result):
        self.assertEqual(result.returncode, 0, result.stdout[-4000:])
        self.assertNotIn('was skipped', result.stdout)
        self.assertRegex(result.stdout, r'(?m)^PN="suifleet-walrus-codec"$')

    def assert_skipped(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout[-4000:])
        self.assertIn('optional Walrus codec inputs not found', result.stdout, result.stdout[-4000:])

    # The fetcher decodes URI escapes, so a percent-encoded absolute file:// URI
    # names the decoded file. The guard must agree and keep the recipe.
    def test_present_percent_encoded_absolute_inputs_are_kept(self):
        for bitbake in BITBAKES:
            with self.subTest(bitbake=str(bitbake)):
                with tempfile.TemporaryDirectory(prefix='sf307-bitbake-') as tmp:
                    project = Project(bitbake, Path(tmp), RECIPE.read_text(), encoded_absolute)
                    project.stage_inputs('abs dir')
                    self.assert_kept(project.request())
                    for name in INPUTS:
                        (project.top/'abs dir'/name).unlink()
                    # Absent decoded files still skip, naming the URI as written.
                    with tempfile.TemporaryDirectory(prefix='sf307-bitbake-') as tmp2:
                        absent = Project(bitbake, Path(tmp2), RECIPE.read_text(), encoded_absolute).request()
                    self.assert_skipped(absent)
                    self.assertIn('abs%20dir/walrus-device-codec.cjs', absent.stdout)

    def test_encoded_absolute_cached_skip_refreshes_when_inputs_appear(self):
        for bitbake in BITBAKES:
            with self.subTest(bitbake=str(bitbake)):
                _, second = self.cycle(bitbake, RECIPE.read_text(), encoded_absolute, 'abs dir')
                self.assert_kept(second)

    # BitBake 2.0 (Kirkstone) falls back to DL_DIR for a relative file:// input
    # that FILESPATH lacks; later releases removed that fallback, so there a
    # DL_DIR-only file is genuinely missing to the fetcher and the skip stands.
    def test_dl_dir_inputs_follow_the_local_fetcher(self):
        for bitbake in BITBAKES:
            with self.subTest(bitbake=str(bitbake)):
                with tempfile.TemporaryDirectory(prefix='sf307-bitbake-') as tmp:
                    project = Project(bitbake, Path(tmp), RECIPE.read_text())
                    project.stage_inputs('downloads')
                    result = project.request()
                if bitbake_version(bitbake) < (2, 2):
                    self.assert_kept(result)
                else:
                    self.assert_skipped(result)

    def test_dl_dir_cached_skip_refreshes_when_inputs_appear(self):
        for bitbake in BITBAKES:
            if bitbake_version(bitbake) >= (2, 2):
                continue
            with self.subTest(bitbake=str(bitbake)):
                _, second = self.cycle(bitbake, RECIPE.read_text(), BBAPPEND, 'downloads')
                self.assert_kept(second)

    # Control: the earlier raw-text guard skipped these present inputs.
    def test_raw_text_guard_skips_present_supported_inputs(self):
        for bitbake in BITBAKES:
            with self.subTest(bitbake=str(bitbake)):
                with tempfile.TemporaryDirectory(prefix='sf307-bitbake-') as tmp:
                    project = Project(bitbake, Path(tmp), raw_text_recipe(), encoded_absolute)
                    project.stage_inputs('abs dir')
                    self.assert_skipped(project.request())
                if bitbake_version(bitbake) < (2, 2):
                    with tempfile.TemporaryDirectory(prefix='sf307-bitbake-') as tmp:
                        project = Project(bitbake, Path(tmp), raw_text_recipe())
                        project.stage_inputs('downloads')
                        self.assert_skipped(project.request())


if __name__ == '__main__':
    unittest.main()
