"""Security gates must reject incomplete scans without exposing scanner payloads."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load('security_audit')
installer = load('install_security_scanner')
SENSITIVE = 'scanner-payload-must-stay-private'


def trivy():
    return {'SchemaVersion': 2, 'Results': [
        {'Target': target, 'Type': kind, 'Packages': [{'Name': 'fixture', 'Version': '1'}]}
        for target, kind in audit.REQUIRED_TARGETS.items()]}


def sarif(score='7.5', level='warning'):
    rule = {'id': 'py/fixture', 'defaultConfiguration': {'level': level}}
    if score is not None:
        rule['properties'] = {'security-severity': score}
    return {'version': '2.1.0', 'runs': [{
        'tool': {'driver': {'rules': [rule]}},
        'invocations': [{'executionSuccessful': True}],
        'results': [{'ruleId': 'py/fixture', 'ruleIndex': 0,
                     'message': {'text': SENSITIVE}}]}]}


class SecurityGateTests(unittest.TestCase):
    def run_gate(self, scanner, report=None, raw=None, missing=False):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'scan.json'
            output = Path(directory) / 'sanitized.json'
            if not missing:
                source.write_text(raw if raw is not None else json.dumps(report))
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = audit.main([scanner, str(source), '--output', str(output)])
            result = json.loads(output.read_text()) if output.exists() else None
            emitted = stdout.getvalue() + stderr.getvalue() + (output.read_text() if output.exists() else '')
            self.assertNotIn(SENSITIVE, emitted)
            return code, result

    def test_missing_and_malformed_reports_fail_closed(self):
        for scanner in ['trivy', 'sarif']:
            for kwargs in [{'missing': True}, {'raw': '{' + SENSITIVE}, {'report': {}}, {'report': []}]:
                with self.subTest(scanner=scanner, kwargs=kwargs):
                    self.assertEqual(self.run_gate(scanner, **kwargs)[0], 2)

    def test_clean_dependency_and_dockerfile_coverage_passes(self):
        self.assertEqual(set(audit.REQUIRED_TARGETS), {
            'dapp/pnpm-lock.yaml', 'suifleet-edge/Cargo.lock',
            'suifleet-indexer/Cargo.lock', 'Dockerfile', 'docker/release.Dockerfile',
            'docker/linux-x86.Dockerfile', 'suifleet-edge/packaging/tunneling/localproxy.Dockerfile'})
        self.assertEqual(self.run_gate('trivy', trivy()), (0, {
            'scanner': 'trivy', 'blocking_findings': 0, 'findings': []}))

    def test_each_required_target_must_be_scanned_with_correct_parser(self):
        for target in audit.REQUIRED_TARGETS:
            for mutation in ['missing', 'wrong-parser', 'scan-error']:
                report = trivy()
                result = next(row for row in report['Results'] if row['Target'] == target)
                if mutation == 'missing':
                    report['Results'].remove(result)
                elif mutation == 'wrong-parser':
                    result['Type'] = 'unrecognized'
                else:
                    result['Error'] = SENSITIVE
                with self.subTest(target=target, mutation=mutation):
                    self.assertEqual(self.run_gate('trivy', report)[0], 2)

    def test_empty_lockfile_package_inventory_is_not_success(self):
        for target, kind in audit.REQUIRED_TARGETS.items():
            if kind not in ('pnpm', 'cargo'):
                continue
            report = trivy()
            next(row for row in report['Results'] if row['Target'] == target)['Packages'] = []
            self.assertEqual(self.run_gate('trivy', report)[0], 2)

    def test_all_trivy_finding_classes_gate_high_critical_and_unknown(self):
        for field, key in [('Vulnerabilities', 'VulnerabilityID'), ('Secrets', 'RuleID'), ('Misconfigurations', 'ID')]:
            for severity in ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL', 'UNKNOWN']:
                report = trivy()
                report['Results'][0][field] = [{key: 'TEST-001', 'Severity': severity,
                    'Match': SENSITIVE, 'Code': {'Lines': [{'Content': SENSITIVE}]},
                    'Description': SENSITIVE, 'Title': SENSITIVE}]
                with self.subTest(field=field, severity=severity):
                    code, result = self.run_gate('trivy', report)
                    blocking = severity in ('HIGH', 'CRITICAL', 'UNKNOWN')
                    self.assertEqual(code, int(blocking))
                    self.assertEqual(result['blocking_findings'], int(blocking))
                    self.assertEqual(set(result['findings'][0]), {'scanner', 'id', 'severity', 'path'})

    def test_unknown_trivy_severity_or_invalid_rule_id_is_malformed(self):
        for finding in [{'RuleID': 'fixture', 'Severity': 'NORMAL'},
                        {'RuleID': 'bad\n::error::' + SENSITIVE, 'Severity': 'HIGH'}]:
            report = trivy()
            report['Results'][0]['Secrets'] = [finding]
            self.assertEqual(self.run_gate('trivy', report)[0], 2)

    def test_configuration_exceptions_do_not_hide_failures(self):
        for status, expected in [('PASS', 0), ('FAIL', 1), ('EXCEPTION', 1), ('unexpected', 2)]:
            report = trivy()
            report['Results'][0]['Misconfigurations'] = [{'ID': 'AVD-001', 'Severity': 'HIGH', 'Status': status}]
            self.assertEqual(self.run_gate('trivy', report)[0], expected)

    def test_secret_paths_cannot_inject_annotations_or_escape_repository(self):
        for target in ['../../outside', '/private/file', 'path\n::error::injected']:
            report = trivy()
            report['Results'].append({'Target': target, 'Type': 'text', 'Secrets': [
                {'RuleID': 'fixture', 'Severity': 'HIGH', 'Match': SENSITIVE}]})
            code, result = self.run_gate('trivy', report)
            self.assertEqual(code, 1)
            self.assertEqual(result['findings'][0]['path'], '<redacted path>')

    def test_sarif_thresholds_and_error_without_score(self):
        for score, level, expected in [('9', 'warning', 1), ('7', 'note', 1),
                ('6.9', 'warning', 0), ('0', 'error', 1), (None, 'error', 1),
                (None, 'warning', 0)]:
            self.assertEqual(self.run_gate('sarif', sarif(score, level))[0], expected)

    def test_sarif_indices_and_identity_are_validated(self):
        for index in [-1, 1, True, '0', 0.5]:
            report = sarif()
            report['runs'][0]['results'][0]['ruleIndex'] = index
            self.assertEqual(self.run_gate('sarif', report)[0], 2)
        report = sarif()
        report['runs'][0]['results'][0]['ruleId'] = 'different/rule'
        self.assertEqual(self.run_gate('sarif', report)[0], 2)

    def test_sarif_rule_id_and_nested_rule_reference_resolve(self):
        for nested in [False, True]:
            report = sarif()
            report['runs'][0]['results'] = [
                {'rule': {'id': 'py/fixture', 'index': 0}} if nested else {'ruleId': 'py/fixture'}]
            self.assertEqual(self.run_gate('sarif', report)[0], 1)

    def test_sarif_extension_rule_metadata_controls_threshold(self):
        for score, expected in [('9.5', 1), ('4.5', 0)]:
            report = sarif(score)
            run = report['runs'][0]
            run['tool']['extensions'] = [run['tool']['driver']]
            run['tool']['driver'] = {'rules': []}
            run['results'][0]['rule'] = {'index': 0, 'toolComponent': {'index': 0}}
            self.assertEqual(self.run_gate('sarif', report)[0], expected)

    def test_sarif_taxonomy_components_need_no_rules(self):
        report = sarif()
        report['runs'][0]['tool']['extensions'] = [{'name': 'taxonomy', 'taxa': []}]
        self.assertEqual(self.run_gate('sarif', report)[0], 1)
        report['runs'][0]['results'] = []
        self.assertEqual(self.run_gate('sarif', report)[0], 0)
        report['runs'][0]['tool']['extensions'][0]['rules'] = None
        self.assertEqual(self.run_gate('sarif', report)[0], 2)

    def test_sarif_extension_references_reject_invalid_or_conflicting_identity(self):
        for reference in [None, {'index': -1}, {'index': 1}, {'index': True},
                          {'index': '0'}, {'index': 0, 'name': 'wrong'},
                          {'index': 0, 'guid': 'wrong'}, {'guid': 'missing'}]:
            report = sarif()
            run = report['runs'][0]
            run['tool']['extensions'] = [run['tool']['driver']]
            run['tool']['driver'] = {'rules': []}
            run['results'][0]['rule'] = {'index': 0, 'toolComponent': reference}
            self.assertEqual(self.run_gate('sarif', report)[0], 2)
        for reference in [{'id': 'different/rule'}, {'index': 1}]:
            report = sarif()
            report['runs'][0]['results'][0]['rule'] = reference
            self.assertEqual(self.run_gate('sarif', report)[0], 2)

    def test_sarif_component_guid_lookup_and_consistency(self):
        report = sarif()
        run = report['runs'][0]
        component = dict(run['tool']['driver'], guid='fixture-guid', name='fixture-pack')
        run['tool']['extensions'] = [component]
        run['tool']['driver'] = {'rules': []}
        run['results'][0]['rule'] = {'index': 0, 'toolComponent': {
            'guid': 'fixture-guid', 'name': 'fixture-pack'}}
        self.assertEqual(self.run_gate('sarif', report)[0], 1)
        run['tool']['extensions'].append(component)
        self.assertEqual(self.run_gate('sarif', report)[0], 2)

    def test_sarif_reports_safe_locations_without_source_excerpts(self):
        report = sarif()
        report['runs'][0]['results'][0]['locations'] = [
            {'physicalLocation': {'artifactLocation': {'uri': '.github/workflows/ci.yml'},
             'region': {'startLine': 12, 'snippet': {'text': SENSITIVE}}}},
            {'physicalLocation': {'artifactLocation': {'uri': '/private/' + SENSITIVE}}}]
        code, result = self.run_gate('sarif', report)
        self.assertEqual(code, 1)
        self.assertEqual(result['findings'][0]['locations'], [
            {'path': '.github/workflows/ci.yml', 'line': 12}, {'path': '<redacted path>'}])

    def test_report_errors_publish_only_constant_diagnostics(self):
        code, result = self.run_gate('sarif', {'version': '2.1.0', 'runs': [], 'payload': SENSITIVE})
        self.assertEqual(code, 2)
        self.assertEqual(result, {'scanner': 'sarif', 'status': 'error', 'reason': 'invalid or empty SARIF report'})

    def test_invalid_sarif_scores_fail_closed(self):
        for score in [True, 'nan', 'Infinity', '-Infinity', '-0.1', '10.1', 'invalid', {}, []]:
            with self.subTest(score=score):
                self.assertEqual(self.run_gate('sarif', sarif(score))[0], 2)

    def test_sarif_execution_must_be_proven_successful(self):
        for invocations in [None, [], [{'executionSuccessful': False}], [{}]]:
            report = sarif()
            report['runs'][0]['invocations'] = invocations
            self.assertEqual(self.run_gate('sarif', report)[0], 2)

    def test_empty_sarif_runs_missing_results_and_unknown_levels_fail(self):
        report = sarif()
        report['runs'] = []
        self.assertEqual(self.run_gate('sarif', report)[0], 2)
        report = sarif()
        del report['runs'][0]['results']
        self.assertEqual(self.run_gate('sarif', report)[0], 2)
        report = sarif()
        report['runs'][0]['results'][0]['level'] = 'unknown'
        self.assertEqual(self.run_gate('sarif', report)[0], 2)


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as bundle:
        for name, content, kind, link in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = link
            if kind == tarfile.REGTYPE:
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))
            else:
                bundle.addfile(member)
    return stream.getvalue()


class ScannerInstallerTests(unittest.TestCase):
    def test_checksum_mismatch_prevents_parsing_or_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'scanner'
            with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(b'untrusted')) as network, \
                    patch.object(installer.tarfile, 'open') as parse:
                with self.assertRaises(ValueError):
                    installer.install(destination)
            parse.assert_not_called()
            network.assert_called_once_with(installer.URL, timeout=120)
            self.assertFalse(destination.exists())

    def test_extracts_only_verified_binary_without_archive_traversal(self):
        payload = archive([('../escaped', b'bad', tarfile.REGTYPE, ''),
                           ('trivy', b'fixture-binary', tarfile.REGTYPE, '')])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'bin' / 'trivy'
            with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(payload)), \
                    patch.object(installer, 'SHA256', hashlib.sha256(payload).hexdigest()):
                installer.install(destination)
            self.assertEqual(destination.read_bytes(), b'fixture-binary')
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertFalse((Path(directory) / 'escaped').exists())

    def test_symlink_hardlink_and_directory_binary_entries_rejected(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE]:
            payload = archive([('trivy', b'', kind, '../../outside')])
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / 'scanner'
                with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(payload)), \
                        patch.object(installer, 'SHA256', hashlib.sha256(payload).hexdigest()):
                    with self.assertRaises(ValueError):
                        installer.install(destination)
                self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
