#!/usr/bin/env python3
"""Fail closed on security reports; publish only allowlisted, credential-free metadata."""
import argparse
import json
import math
from pathlib import Path, PurePosixPath
import re
import sys

SEVERITIES = {'UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'}
class ReportError(ValueError):
    """A constant, source-independent diagnostic safe for public logs."""


REQUIRED_TARGETS = {'dapp/pnpm-lock.yaml': 'pnpm', 'suifleet-edge/Cargo.lock': 'cargo',
                    'suifleet-indexer/Cargo.lock': 'cargo', 'Dockerfile': 'dockerfile',
                    'docker/release.Dockerfile': 'dockerfile'}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,200}', value):
        raise ReportError('invalid security rule identifier')
    return value


def safe_path(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_./+@ -]{1,300}', value)
            or PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts):
        return '<redacted path>'
    return value


def trivy_findings(report):
    if not isinstance(report, dict) or report.get('SchemaVersion') != 2 or not isinstance(report.get('Results'), list):
        raise ReportError('invalid or missing Trivy report')
    coverage = {}
    findings = []
    for result in report['Results']:
        if not isinstance(result, dict) or result.get('Error'):
            raise ReportError('Trivy target scan failed')
        target = result.get('Target')
        if target in REQUIRED_TARGETS and result.get('Type') == REQUIRED_TARGETS[target]:
            if result['Type'] in ('pnpm', 'cargo') and not result.get('Packages'):
                raise ReportError('dependency scanner reported no packages')
            coverage[target] = True
        for field, key in [('Vulnerabilities', 'VulnerabilityID'), ('Secrets', 'RuleID'), ('Misconfigurations', 'ID')]:
            entries = result.get(field, [])
            if not isinstance(entries, list):
                raise ReportError('invalid Trivy finding list')
            for item in entries:
                if not isinstance(item, dict) or item.get('Severity') not in SEVERITIES:
                    raise ReportError('invalid Trivy finding severity')
                if field == 'Misconfigurations' and item.get('Status', 'FAIL') != 'FAIL':
                    if item['Status'] not in ('PASS', 'EXCEPTION'):
                        raise ReportError('invalid configuration scan status')
                    # Exceptions must not hide findings in this no-suppression policy.
                    if item['Status'] == 'PASS':
                        continue
                findings.append({'scanner': field.lower(), 'id': identifier(item.get(key)),
                                 'severity': item['Severity'], 'path': safe_path(target)})
    if set(coverage) != set(REQUIRED_TARGETS):
        raise ReportError('Trivy report lacks required lockfile or Dockerfile coverage')
    return findings


def sarif_findings(report):
    if (not isinstance(report, dict) or report.get('version') != '2.1.0'
            or not isinstance(report.get('runs'), list) or not report['runs']):
        raise ReportError('invalid or empty SARIF report')
    findings = []
    for run in report['runs']:
        invocations = run.get('invocations')
        if not isinstance(invocations, list) or not invocations or any(i.get('executionSuccessful') is not True for i in invocations):
            raise ReportError('CodeQL analysis execution failed or is unproven')
        rules = run['tool']['driver']['rules']
        results = run['results']
        if not isinstance(rules, list) or not isinstance(results, list):
            raise ReportError('invalid SARIF rules or results')
        by_id = {identifier(rule['id']): rule for rule in rules}
        for result in results:
            if not isinstance(result, dict):
                raise ReportError('invalid SARIF result')
            rule_id = result.get('ruleId', result.get('rule', {}).get('id'))
            index = result.get('ruleIndex', result.get('rule', {}).get('index'))
            if index is not None:
                if type(index) is not int or not 0 <= index < len(rules):
                    # Schema-only diagnostics: never emit identifiers, messages or source.
                    print(json.dumps({'sarif_schema': {
                        'driver_rule_count': len(rules),
                        'extension_count': len(run['tool'].get('extensions', [])),
                        'nested_rule_present': isinstance(result.get('rule'), dict),
                        'component_reference_present': isinstance(result.get('rule', {}).get('toolComponent'), dict),
                        'index_is_integer': type(index) is int,
                    }}), file=sys.stderr)
                    raise ReportError('invalid SARIF rule index')
                rule = rules[index]
                if rule_id is not None and rule_id != rule['id']:
                    raise ReportError('SARIF rule identity mismatch')
                rule_id = rule['id']
            elif rule_id in by_id:
                rule = by_id[rule_id]
            else:
                raise ReportError('SARIF result lacks a matching rule')
            level = result.get('level', rule.get('defaultConfiguration', {}).get('level', 'warning'))
            if level not in ('none', 'note', 'warning', 'error'):
                raise ReportError('invalid SARIF result level')
            raw = rule.get('properties', {}).get('security-severity')
            if raw is not None:
                if isinstance(raw, bool):
                    raise ReportError('invalid SARIF security score')
                score = float(raw)
                if not math.isfinite(score) or not 0 <= score <= 10:
                    raise ReportError('invalid SARIF security score')
                severity = 'CRITICAL' if score >= 9 else 'HIGH' if score >= 7 else 'MEDIUM' if score >= 4 else 'LOW'
            else:
                severity = 'HIGH' if level == 'error' else 'MEDIUM' if level == 'warning' else 'LOW'
            if level == 'error' and severity in ('LOW', 'MEDIUM'):
                severity = 'HIGH'
            locations = []
            for location in result.get('locations', []):
                physical = location.get('physicalLocation', {})
                item = {'path': safe_path(physical.get('artifactLocation', {}).get('uri'))}
                line = physical.get('region', {}).get('startLine')
                if type(line) is int and line > 0:
                    item['line'] = line
                locations.append(item)
            findings.append({'scanner': 'codeql', 'id': identifier(rule_id), 'severity': severity,
                             'locations': locations})
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scanner', choices=['trivy', 'sarif'])
    parser.add_argument('reports', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        findings = []
        for path in args.reports:
            report = json.loads(path.read_text())
            findings.extend((trivy_findings if args.scanner == 'trivy' else sarif_findings)(report))
        blocked = sum(item['severity'] in ('HIGH', 'CRITICAL', 'UNKNOWN') for item in findings)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'scanner': args.scanner, 'blocking_findings': blocked,
                                          'findings': findings}, indent=2) + '\n')
        print(f'Security scan: {len(findings)} findings; {blocked} blocking. Sanitized details saved.')
        return 1 if blocked else 0
    except ReportError as error:
        # Every ReportError is a fixed literal above, never an input payload.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'scanner': args.scanner, 'status': 'error', 'reason': str(error)}) + '\n')
        print('Security scan failed: ' + str(error) + '.', file=sys.stderr)
        return 2
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        # Do not echo scanner messages, source excerpts, secret matches or JSON payloads.
        print('Security scan failed: missing, malformed or incomplete report.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
