"""Real Git regression for open stacked PRs already included in main."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_batch_integration import Fixture


class IncludedSourcesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = Fixture(Path(temp.name))

    def candidates(self):
        f = self.f
        return [f.controller.source(f.project, p, o)
                for p, o in zip(f.source_prs.values(), f.owners)]

    def include_first(self):
        f = self.f
        f.git('push', '-q', 'origin', f.heads[0]+':refs/heads/main')
        return f.heads[0]

    def test_open_included_source_never_creates_empty_batch(self):
        f = self.f
        self.include_first()
        prs = [copy.deepcopy(f.source_prs[1])]  # GitHub still reports OPEN.
        owners = copy.deepcopy(f.owners)
        for _ in range(3):
            self.assertEqual(f.controller.tick(f.project, prs, f.owners, True), {1})
        self.assertEqual(f.controller.rows(f.project), [])
        self.assertEqual(f.owners, owners)
        self.assertEqual(prs[0]['state'], 'OPEN')
        self.assertEqual(f.reconciled, [])
        self.assertEqual(f.events, [])
        receipt = json.loads(f.db.execute('SELECT receipt FROM batch_included_sources').fetchone()[0])
        self.assertFalse(receipt['acceptance_complete'])
        self.assertEqual(receipt['head'], f.heads[0])
        self.assertEqual(f.db.execute('SELECT count(*) FROM batch_included_sources').fetchone()[0], 1)

    def test_unrelated_source_keeps_progressing(self):
        f = self.f
        self.include_first()
        f.controller.tick(f.project, list(f.source_prs.values()), f.owners, True)
        row = f.row()
        self.assertEqual(row['status'], 'awaiting_ci')
        self.assertEqual([m['pr'] for m in json.loads(row['members'])], [2])
        self.assertEqual(f.reconciled, [])

    def test_new_descendant_is_eligible_despite_old_receipt(self):
        f = self.f
        base = self.include_first()
        candidates = self.candidates()
        self.assertEqual([m['pr'] for m in f.controller.select(f.project, candidates, base, 2)], [2])
        f.git('checkout', '-q', 'work/1')
        (f.repo/'new-work').write_text('still needs acceptance\n')
        f.git('add', 'new-work'); f.git('commit', '-qm', 'new source')
        head = f.git('rev-parse', 'HEAD')
        f.git('push', '-q', 'origin', 'work/1')
        candidates[0]['head'] = head
        self.assertEqual([m['pr'] for m in f.controller.select(f.project, candidates, base, 2)], [1, 2])

    def test_unknown_ancestry_fails_closed(self):
        f = self.f
        original = f.controller.git
        def git(project, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                raise subprocess.CalledProcessError(128, 'git')
            return original(project, *args, **kwargs)
        with patch.object(f.controller, 'git', side_effect=git):
            with self.assertRaises(subprocess.CalledProcessError):
                f.controller.select(f.project, self.candidates(), f.base, 2)
        self.assertEqual(f.controller.rows(f.project), [])
        self.assertEqual(f.reconciled, [])
