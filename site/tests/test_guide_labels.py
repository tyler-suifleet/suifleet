"""Current written instructions and recorder selectors must follow console labels."""
import json
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'site'))
import build


class GuideLabelsTest(unittest.TestCase):
    def test_guide_routes_and_sidebar_actions_use_current_labels(self):
        layout = (ROOT / 'dapp/src/components/ConsoleNavigation.tsx').read_text()
        nav = dict(re.findall(r'href: "([^"]+)",\s*label: "([^"]+)"', layout))
        steps = json.loads((ROOT / 'site/guide/steps.json').read_text())['steps']
        for step in steps:
            route = '/' + step['console_route'].strip('/')
            self.assertIn(route, nav)
            self.assertEqual(step['console_label'], nav[route], step['id'])
            for action in step['actions']:
                for label in re.findall(r'Choose (.*?) in the sidebar', action):
                    self.assertIn(label, nav.values(), step['id'])
        recorder = (ROOT / 'site/guide/record_guide.cjs').read_text()
        for label in re.findall(r'h\.sidebar\("([^"]+)"\)', recorder):
            self.assertIn(label, nav.values())
        budget_label = nav['/budget']
        self.assertIn(f'name: "{budget_label}", exact: true', recorder)
        self.assertIn(f'title="{budget_label}"', (ROOT / 'dapp/src/app/budget/page.tsx').read_text())

    def test_storage_creation_action_matches_button(self):
        steps = json.loads((ROOT / 'site/guide/steps.json').read_text())['steps']
        fund = next(step for step in steps if step['id'] == 'fund')
        action = next(action for action in fund['actions'] if 'Create storage budget' in action)
        self.assertIn('Storage funding', action)
        self.assertIn('>Create storage budget</button>', (ROOT / 'dapp/src/components/FleetBudget.tsx').read_text())
        self.assertIn('title="Storage funding"', (ROOT / 'dapp/src/app/budget/page.tsx').read_text())

    def test_current_guide_sources_have_no_obsolete_budget_terms(self):
        for name in ('steps.json', 'lessons.json', 'narration.json', 'record_guide.cjs'):
            text = (ROOT / 'site/guide' / name).read_text()
            for obsolete in ('network budget', 'fleet budget'):
                self.assertNotIn(obsolete, text.casefold(), name)

    def test_current_dapp_surfaces_have_no_obsolete_budget_terms(self):
        for directory in ('app', 'components'):
            for path in sorted((ROOT / 'dapp/src' / directory).rglob('*')):
                if path.suffix not in ('.ts', '.tsx'):
                    continue
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    # Developer comments are not customer-facing copy.
                    if line.lstrip().startswith(('//', '/*', '*', '{/*')):
                        continue
                    with self.subTest(path=str(path.relative_to(ROOT)), line=number):
                        self.assertNotRegex(line.casefold(), r'\b(?:network|fleet) budget\b')

    def test_recorded_snapshot_remains_digest_checked(self):
        manifest = build.load_json(ROOT / 'site/media/guide/videos.json')
        original = build.sha256
        snapshot = ROOT / 'site/guide/recorded/lessons.json'
        with mock.patch.object(build, 'sha256', side_effect=lambda path: '0' * 64 if path == snapshot else original(path)):
            with self.assertRaisesRegex(build.BuildError, 'source/artwork'):
                build.videos_by_step()
        with self.assertRaisesRegex(build.BuildError, 'Invalid guide script snapshot'):
            build.recorded_guide_source({'script_snapshot': '../untrusted'}, 'site/guide/lessons.json')
        self.assertEqual(manifest['tutorial_production']['script_snapshot'], 'site/guide/recorded')
