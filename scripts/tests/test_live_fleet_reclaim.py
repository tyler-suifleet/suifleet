"""Offline fixture-evolution checks for the SF-129 Testnet reclaim run."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_fleet_budget import record_publication
from live_fleet_reclaim import check_origins, fixture_step

# The committed SF-129 receipt: SF-082 base, then the first reclaim upgrade.
RECEIPT = json.loads((Path(__file__).resolve().parents[2] / 'docs/validation/sf129-testnet.json').read_text())
BASE, V2 = RECEIPT['fixture']['base'], RECEIPT['fixture']['head']
V3 = '0x' + '3' * 64
BASE_ORIGINS = {name: origin for name, origin in RECEIPT['origins'].items() if origin == BASE}
V2_ORIGINS = dict(RECEIPT['origins'])


class FixtureStepTests(unittest.TestCase):
    def test_changed_source_after_a_successful_upgrade_upgrades_from_the_latest_version(self):
        # The cap already names version 2 built from older source: upgrade from it, not from the base.
        self.assertEqual(fixture_step(BASE, V2, '2', lambda package: False), ('upgrade', V2))

    def test_unchanged_source_reuses_the_latest_version_without_republishing(self):
        seen = []
        self.assertEqual(fixture_step(BASE, V2, '2', lambda package: seen.append(package) or True), ('reuse', V2))
        self.assertEqual(seen, [V2])

    def test_first_run_upgrades_from_the_base(self):
        self.assertEqual(fixture_step(BASE, BASE, '1', lambda package: False), ('upgrade', BASE))

    def test_inconsistent_cap_is_refused(self):
        for package, version in ((BASE, '2'), (V2, '1'), (V2, '0'), (V2, 'x')):
            with self.assertRaises(AssertionError):
                fixture_step(BASE, package, version, lambda package: False)


class PublicationTests(unittest.TestCase):
    def test_upgrade_from_version_2_records_its_latest_version_beside_the_original_id(self):
        cap = RECEIPT['fixture']['cap']
        with tempfile.TemporaryDirectory() as directory:
            record_publication(Path(directory), '4c78adac', BASE, cap, published_at=V2, version='2')
            text = (Path(directory) / 'Published.toml').read_text()
        self.assertIn(f'published-at = "{V2}"', text)
        self.assertIn(f'original-id = "{BASE}"', text)
        self.assertIn('version = 2\n', text)


class OriginTests(unittest.TestCase):
    def introduced(self, package):
        return {BASE: BASE_ORIGINS, V2: V2_ORIGINS}[package]

    def test_version_3_keeps_base_and_version_2_origins(self):
        head = dict(V2_ORIGINS, **{'fleet_data::CompactHead': V3})
        self.assertEqual(check_origins(V3, BASE, head, BASE_ORIGINS, self.introduced, previous=V2_ORIGINS), head)

    def test_reused_version_3_is_checked_against_each_introducing_package(self):
        head = dict(V2_ORIGINS, **{'fleet_data::CompactHead': V3})
        introduced = lambda package: {**{BASE: BASE_ORIGINS, V2: V2_ORIGINS}, V3: head}[package]
        check_origins(V3, BASE, head, BASE_ORIGINS, introduced)

    def test_first_upgrade_gives_new_events_the_new_origin(self):
        check_origins(V2, BASE, V2_ORIGINS, BASE_ORIGINS, self.introduced, previous=BASE_ORIGINS)

    def test_reclaim_events_moved_to_the_new_version_are_refused(self):
        head = dict(V2_ORIGINS, **{'fleet_data::AnchorsPruned': V3})
        with self.assertRaisesRegex(AssertionError, 'AnchorsPruned changed origin'):
            check_origins(V3, BASE, head, BASE_ORIGINS, self.introduced, previous=V2_ORIGINS)

    def test_base_type_moved_is_refused(self):
        head = dict(V2_ORIGINS, **{'fleet_data::Anchor': V2})
        with self.assertRaisesRegex(AssertionError, 'Anchor lost its base origin'):
            check_origins(V3, BASE, head, BASE_ORIGINS, self.introduced, previous=V2_ORIGINS)

    def test_new_type_with_a_foreign_origin_is_refused(self):
        head = dict(V2_ORIGINS, **{'fleet_data::CompactHead': V2})
        with self.assertRaisesRegex(AssertionError, 'CompactHead is new in this upgrade'):
            check_origins(V3, BASE, head, BASE_ORIGINS, self.introduced, previous=V2_ORIGINS)
        with self.assertRaisesRegex(AssertionError, 'did not introduce it'):
            check_origins(V3, BASE, head, BASE_ORIGINS, self.introduced)

    def test_missing_reclaim_event_is_refused(self):
        head = {name: origin for name, origin in V2_ORIGINS.items() if name != 'fleet_data::ExpiredBlobsBurned'}
        with self.assertRaisesRegex(AssertionError, 'ExpiredBlobsBurned is missing'):
            check_origins(V2, BASE, head, BASE_ORIGINS, self.introduced)


if __name__ == '__main__':
    unittest.main()
