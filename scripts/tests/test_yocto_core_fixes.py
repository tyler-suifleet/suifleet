"""Offline checks for meta-suifleet's opt-in OE-Core fix class (SF-308).

The class must stay opt-in (no layer-wide inclusion, so yocto-check-layer
signatures are unchanged), act only on its named recipes/series/variants, and
remain the single definition shared by the integration layer's bbappends.
Real BitBake builds are recorded in docs/YOCTO_STANDALONE_VALIDATION.md.
"""
import json
from pathlib import Path
import re
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
LAYER = ROOT / 'suifleet-edge/meta-suifleet'
INTEGRATION = ROOT / 'suifleet-edge/meta-suifleet-integration'
CLASS = LAYER / 'classes/suifleet-core-fixes.bbclass'
LIBCAP_TOOLS = (' CC="${CC}" AR="${AR}" RANLIB="${RANLIB}" OBJCOPY="${OBJCOPY}"'
                ' BUILD_CC="${BUILD_CC}" BUILD_LDFLAGS="${BUILD_LDFLAGS}"')


def code_lines(path):
    return [line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


class FakeData:
    """Records the datastore mutations the anonymous function performs."""

    def __init__(self, **values):
        self.values = values
        self.changes = []

    def getVar(self, name):
        return self.values.get(name)

    def setVar(self, name, value):
        self.changes.append(('set', name, value))

    def appendVar(self, name, value):
        self.changes.append(('append', name, value))


def anonymous_changes(bpn, series, classoverride='class-target', pn=None):
    text = CLASS.read_text()
    match = re.search(r'^python __anonymous\(\) \{\n(.*?)^\}', text, re.M | re.S)
    body = textwrap.dedent(match.group(1))
    d = FakeData(BPN=bpn, PN=pn or bpn, LAYERSERIES_CORENAMES=series,
                 CLASSOVERRIDE=classoverride)
    exec(compile(body, str(CLASS), 'exec'), {'d': d})
    return d.changes


class CoreFixClassTests(unittest.TestCase):
    def test_layer_never_enables_the_class_itself(self):
        layer_conf = (LAYER / 'conf/layer.conf').read_text()
        self.assertNotRegex(layer_conf, r'\b(INHERIT|USER_CLASSES|require|include)\b')
        metadata = [p for p in LAYER.rglob('*')
                    if p.suffix in {'.bb', '.bbclass', '.conf', '.inc'} and p != CLASS]
        self.assertTrue(metadata)
        for path in metadata:
            self.assertNotIn('suifleet-core-fixes', '\n'.join(code_lines(path)), path)
        self.assertEqual(list(LAYER.rglob('*.bbappend')), [])

    def test_class_is_exported_with_the_software_layer(self):
        names = json.loads((LAYER / 'export-files.json').read_text())
        self.assertIn('classes/suifleet-core-fixes.bbclass', names)

    def test_class_contains_no_board_access_or_qa_policy(self):
        text = '\n'.join(code_lines(CLASS))
        for forbidden in ('ssh', 'sudo', 'passwd', 'network', 'MACHINE', 'IMAGE_',
                          'INSANE_SKIP', 'ERROR_QA', 'WARN_QA', 'EXTRA_IMAGE_FEATURES'):
            self.assertNotIn(forbidden.lower(), text.lower(), forbidden)

    def test_openssl_install_is_serialized_for_every_variant_and_series(self):
        for series in ('scarthgap', 'wrynose'):
            for override, pn in (('class-target', 'openssl'), ('class-native', 'openssl-native'),
                                 ('class-nativesdk', 'nativesdk-openssl')):
                self.assertEqual(anonymous_changes('openssl', series, override, pn),
                                 [('set', 'PARALLEL_MAKEINST', '-j1')])

    def test_libcap_install_toolchain_only_on_wrynose(self):
        self.assertEqual(anonymous_changes('libcap', 'wrynose'),
                         [('append', 'EXTRA_OEMAKE', LIBCAP_TOOLS)])
        self.assertEqual(anonymous_changes('libcap', 'wrynose', 'class-native', 'libcap-native'),
                         [('append', 'EXTRA_OEMAKE', LIBCAP_TOOLS)])
        self.assertEqual(anonymous_changes('libcap', 'scarthgap'), [])

    def test_xz_interpreter_only_for_wrynose_target(self):
        self.assertEqual(anonymous_changes('xz', 'wrynose'), [
            ('append', 'RDEPENDS:xz', ' bash'),
            ('append', 'do_install', '\n    suifleet_xz_interpreter\n'),
        ])
        self.assertEqual(anonymous_changes('xz', 'wrynose', 'class-native', 'xz-native'), [])
        self.assertEqual(anonymous_changes('xz', 'wrynose', 'class-nativesdk', 'nativesdk-xz'), [])
        self.assertEqual(anonymous_changes('xz', 'scarthgap'), [])
        self.assertIn("sed -i '1s|^#!/bin/bash$|#!${bindir}/bash|' ${D}${bindir}/xzmore",
                      CLASS.read_text())

    def test_unrelated_recipes_are_untouched(self):
        for bpn in ('glibc', 'systemd', 'suifleet', 'openssl3', 'xz-utils', 'libcap-ng', ''):
            for series in ('scarthgap', 'wrynose', ''):
                self.assertEqual(anonymous_changes(bpn, series), [], (bpn, series))

    def test_integration_layer_reuses_the_single_definition(self):
        for recipe in ('openssl', 'libcap', 'xz'):
            append = INTEGRATION / f'recipes-support/{recipe}/{recipe}_%.bbappend'
            self.assertEqual(code_lines(append), ['inherit suifleet-core-fixes'], append)
        # The glibc layout guard is local cache hygiene, not a standalone fix.
        glibc = (INTEGRATION / 'recipes-core/glibc/glibc_%.bbappend').read_text()
        self.assertIn('bbfatal', glibc)
        self.assertNotIn('glibc', '\n'.join(code_lines(CLASS)))


if __name__ == '__main__':
    unittest.main()
