"""SF-309 offline guard: advertised Yocto series have pinned, measured profiles.

LAYERSERIES_COMPAT may name only series with committed, fully pinned kas
profiles whose builds are recorded in docs/YOCTO_LEGACY_SERIES.md (older
series) or docs/YOCTO_PACKAGING.md. Blocked series must stay out of it.
No BitBake runs here; this does not replace the recorded builds.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
KAS = ROOT / 'kas'
LAYERS = {
    'meta-suifleet': ROOT / 'suifleet-edge/meta-suifleet/conf/layer.conf',
    'suifleet-integration': ROOT / 'suifleet-edge/meta-suifleet-integration/conf/layer.conf',
}
# Series -> (package profile, QEMU runtime profile).
PROFILES = {
    'kirkstone': ('packaging-kirkstone.yml', 'packaging-kirkstone-qemu.yml'),
    'scarthgap': ('packaging.yml', 'packaging-qemu.yml'),
    'wrynose': ('packaging-wrynose.yml', 'packaging-wrynose-qemu.yml'),
}
# Measured blockers (docs/YOCTO_LEGACY_SERIES.md): never advertise these.
BLOCKED = {'dunfell'}
LEGACY_DOC = ROOT / 'docs/YOCTO_LEGACY_SERIES.md'


def compat(path, collection):
    match = re.search(rf'^LAYERSERIES_COMPAT_{re.escape(collection)} = "([^"]*)"$',
                      path.read_text(), re.M)
    assert match, f'{path}: no LAYERSERIES_COMPAT_{collection}'
    return match.group(1).split()


def remote_repos(text):
    """(name, url, commit, branch) for every repo block with a url."""
    repos = []
    section = text.split('\nrepos:\n', 1)[1].split('\nlocal_conf_header:', 1)[0]
    for block in re.split(r'\n(?=  [A-Za-z0-9_-]+:\n)', '\n' + section):
        name = re.match(r'\s*([A-Za-z0-9_-]+):', block)
        url = re.search(r'^    url: (\S+)$', block, re.M)
        if not (name and url):
            continue
        commit = re.search(r'^    commit: (\S+)$', block, re.M)
        branch = re.search(r'^    branch: (\S+)$', block, re.M)
        repos.append((name.group(1), url.group(1), commit and commit.group(1),
                      branch and branch.group(1)))
    return repos


class SeriesPolicyTests(unittest.TestCase):
    def test_both_layers_advertise_exactly_the_profiled_series(self):
        for collection, path in LAYERS.items():
            self.assertEqual(sorted(compat(path, collection)), sorted(PROFILES), collection)

    def test_blocked_series_are_not_advertised_or_profiled(self):
        for collection, path in LAYERS.items():
            self.assertFalse(BLOCKED & set(compat(path, collection)), collection)
        for series in BLOCKED:
            self.assertEqual(list(KAS.glob(f'*{series}*')), [], series)

    def test_every_profile_pins_full_public_revisions(self):
        for series, (package, qemu) in PROFILES.items():
            text = (KAS / package).read_text()
            repos = remote_repos(text)
            self.assertTrue(repos, package)
            for name, url, commit, _branch in repos:
                self.assertTrue(url.startswith('https://'), (package, name))
                self.assertRegex(commit or '', '^[0-9a-f]{40}$', (package, name))
            self.assertIn(f'    - {package}', (KAS / qemu).read_text(), qemu)

    def test_kirkstone_uses_the_rust_mixin_not_core_rust(self):
        repos = {name: (url, commit, branch) for name, url, commit, branch in
                 remote_repos((KAS / 'packaging-kirkstone.yml').read_text())}
        url, _commit, branch = repos['meta-lts-mixins']
        self.assertEqual(url, 'https://git.yoctoproject.org/meta-lts-mixins')
        self.assertEqual(branch, 'kirkstone/rust')
        # Scarthgap's mixin must not leak into the older series.
        self.assertNotIn('scarthgap/rust', (KAS / 'packaging-kirkstone.yml').read_text())

    def test_runtime_profiles_install_the_measured_packages(self):
        for series, (_package, qemu) in PROFILES.items():
            text = (KAS / qemu).read_text()
            for package in ('suifleet', 'suifleet-walrus-codec', 'suifleet-test-component'):
                self.assertIn(f' {package} ', text, (qemu, package))

    def test_legacy_record_covers_each_older_series_and_blocker(self):
        record = LEGACY_DOC.read_text()
        self.assertIn('kas/packaging-kirkstone.yml', record)
        self.assertIn('kas/packaging-kirkstone-qemu.yml', record)
        for series in BLOCKED:
            self.assertIn(series.capitalize(), record)
        self.assertIn('SO_PEERPIDFD', record)


if __name__ == '__main__':
    unittest.main()
