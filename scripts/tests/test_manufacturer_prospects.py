"""Reject corrupted research artifacts even when Python optimization is enabled."""
import csv
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ProspectValidationTests(unittest.TestCase):
    def test_validation_survives_optimization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'scripts').mkdir()
            script = root / 'scripts/check_manufacturer_prospects.py'
            shutil.copyfile(ROOT / 'scripts/check_manufacturer_prospects.py', script)
            directory = root / 'docs/business/prospects'
            shutil.copytree(ROOT / 'docs/business/prospects', directory)
            csv_path = directory / 'manufacturers.csv'
            original_csv = csv_path.read_text()
            report = directory / 'REPORT.md'
            original_report = report.read_text()
            sources = directory / 'source-checks.json'
            original_sources = sources.read_text()

            def run(optimized):
                return subprocess.run(
                    [sys.executable, *(['-O'] if optimized else []), str(script)],
                    capture_output=True, text=True,
                )

            for optimized in (False, True):
                with self.subTest(optimized=optimized, case='pristine'):
                    self.assertEqual(run(optimized).returncode, 0)
                for case in ('budget', 'http', 'missing', 'duplicate', 'report', 'sources'):
                    with self.subTest(optimized=optimized, case=case):
                        try:
                            rows = list(csv.DictReader(io.StringIO(original_csv)))
                            if case == 'budget':
                                rows[0]['budget'] = 'Invented budget'
                            elif case == 'http':
                                rows[0]['primary_url'] = 'http://example.com'
                            elif case == 'missing':
                                rows.pop()
                            elif case == 'duplicate':
                                rows[1]['company'] = rows[0]['company']
                            elif case == 'report':
                                report.write_text(original_report + '\nstale')
                            elif case == 'sources':
                                sources.write_text(original_sources.replace('2026-10-21', '2000-01-01'))
                            with csv_path.open('w', newline='') as stream:
                                writer = csv.DictWriter(stream, fieldnames=rows[0].keys(), lineterminator='\n')
                                writer.writeheader()
                                writer.writerows(rows)
                            result = run(optimized)
                            self.assertNotEqual(result.returncode, 0, result.stdout)
                            self.assertIn('ValueError', result.stderr)
                        finally:
                            csv_path.write_text(original_csv)
                            report.write_text(original_report)
                            sources.write_text(original_sources)
