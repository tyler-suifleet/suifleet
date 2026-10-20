"""Negative guards for roadmap ownership, evidence and executable contracts."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import greengrass_roadmap as roadmap

ROOT = Path(__file__).resolve().parents[2]


class RoadmapTests(unittest.TestCase):
    def test_repository(self):
        self.assertEqual(roadmap.check(ROOT), 76)

    def reject(self, file, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in ('interfaces/compat', 'docs/tracking', 'docs/validation'):
                shutil.copytree(ROOT / directory, root / directory)
            path = root / file
            data = json.loads(path.read_text())
            mutate(data)
            path.write_text(json.dumps(data))
            with self.assertRaises(AssertionError):
                roadmap.check(root)

    def test_missing_scope(self):
        self.reject('interfaces/compat/remaining-roadmap.json', lambda d: d['scopes'].pop())

    def test_missing_publication(self):
        self.reject('docs/tracking/github-state.json', lambda d: d['issues']['SF-069'].update(project_item_id=None))

    def test_dependency_drift(self):
        self.reject('interfaces/compat/remaining-roadmap.json', lambda d: d['scopes'][0].update(depends_on=[]))

    def test_no_runtime_promotion(self):
        self.reject('interfaces/compat/remaining-roadmap.json', lambda d: d['scopes'][0].update(runtime_execution='passed'))

    def test_missing_runtime_receipt(self):
        def mutate(d):
            next(s for s in d['scopes'] if s['id'] == 'video').pop('runtime_evidence')
        self.reject('interfaces/compat/remaining-roadmap.json', mutate)

    def test_wrong_runtime_receipt_owner(self):
        self.reject('docs/validation/sf072-video.json', lambda d: d.update(ticket='SF-069'))

    def test_runtime_receipt_requires_limits(self):
        self.reject('docs/validation/sf072-video.json', lambda d: d.update(limits=[]))

    def test_no_parity_claim(self):
        self.reject('interfaces/compat/remaining-roadmap.json', lambda d: d.update(parity_claim=True))

    def test_missing_platform_owner(self):
        def mutate(d):
            next(p for p in d['platform_phases'] if p['platform'] == 'darwin')['tickets'] = ['SF-064']
        self.reject('interfaces/compat/managed-components.json', mutate)

    def test_missing_catalog_owner(self):
        def mutate(d):
            next(p for p in d['components'] if p['name'] == 'aws.greengrass.LambdaManager')['tickets'] = ['SF-064']
        self.reject('interfaces/compat/managed-components.json', mutate)

    def test_incorrect_fixture(self):
        self.reject('interfaces/compat/remaining-roadmap-cases.json', lambda d: d['cases'][0].update(expected={'status': 'ok'}))

    def test_authorization_and_version_precede_work(self):
        for scope in roadmap.SCOPES:
            self.assertEqual(roadmap.evaluate(scope, {'version': 1}), {'status': 'denied'})
            for version in (True, 1.0, 0, 2, '1', None):
                self.assertEqual(roadmap.evaluate(scope, {'version': version, 'authorized': True}), {'status': 'unsupported-version'})

    def test_model_does_not_mutate_request(self):
        fixtures = json.loads((ROOT / 'interfaces/compat/remaining-roadmap-cases.json').read_text())
        for case in fixtures['cases']:
            request = copy.deepcopy(case['request'])
            roadmap.evaluate(case['scope'], request)
            self.assertEqual(request, case['request'])


if __name__ == '__main__':
    unittest.main()
