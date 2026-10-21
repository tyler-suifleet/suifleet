#!/usr/bin/env python3
"""Validate SF-105 CSV and render/check its deterministic human-readable report.

Offline by design: URL coverage is structural, not a claim of present availability.
"""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'docs/business/prospects'


def load():
    with (DIRECTORY / 'manufacturers.csv').open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 50, 'Expected exactly 50 records'
    assert len({r['company'].casefold().strip() for r in rows}) == 50, 'Duplicate company'
    assert {r['sector'] for r in rows} == {'industrial', 'energy', 'commercial'}
    assert rows == sorted(rows, key=lambda r: (-int(r['score']), r['company'].casefold()))
    for rank, row in enumerate(rows, 1):
        assert all(v and v.strip() for v in row.values()), f'Empty field: {row["company"]}'
        assert row['rank'] == str(rank)
        assert row['checked_on'] == '2026-10-21'
        assert row['source_basis'] in {'Vendor documentation', 'Supplier case study; historical relationship claim'}
        assert row['role'].startswith('manufacturer'), row['company']
        for key in ('fleet_size', 'budget', 'buyer_interest'):
            assert row[key] == 'Unknown', f'Unsubstantiated commercial claim: {key}'
        for key in ('primary_url', 'ota_url'):
            if row[key] == 'Unknown':
                assert key == 'ota_url'
                continue
            url = urlsplit(row[key])
            assert url.scheme == 'https' and url.hostname and not url.username and not url.password
        linux, boundary, friction = (int(row[k]) for k in ('linux_evidence', 'application_boundary', 'friction'))
        assert all(v in range(3) for v in (linux, boundary, friction))
        assert int(row['score']) == 2 * linux + boundary - friction
    return rows


def render(rows):
    counts = Counter(r['sector'] for r in rows)
    lines = ['# Manufacturer prospect research — ranked records', '',
             'Research date: 2026-10-21. Generated from [manufacturers.csv](manufacturers.csv); '
             'see [methodology and discovery plan](README.md). No outreach occurred.', '',
             f'50 unique companies: {counts["industrial"]} industrial, {counts["energy"]} energy, '
             f'{counts["commercial"]} connected commercial equipment. Rankings are research hypotheses, not sales qualification.', '',
             'Score = 2 × Linux evidence + application boundary − friction; ties use company name. '
             'All fleet sizes, budgets and buying interest are unknown. Documentation does not prove current stock or deployment.', '']
    for r in rows:
        lines += [f'## {r["rank"]}. {r["company"]} — {r["product"]}', '',
                  f'**{r["sector"]}; {r["role"]}. Score {r["score"]} '
                  f'(Linux {r["linux_evidence"]}, boundary {r["application_boundary"]}, friction {r["friction"]}).', '',
                  f'**Documented:** {r["technical_fact"]} [Primary source]({r["primary_url"]}). '
                  f'Source basis: {r["source_basis"]}.', '',
                  '**OTA evidence:** ' + r['ota_evidence'] +
                  (f' [Source]({r["ota_url"]}).' if r['ota_url'] != 'Unknown' else ''), '',
                  f'**Inferred pilot:** {r["pilot_fit_inferred"]} '
                  f'**Inferred migration/sales/safety friction:** {r["migration_friction_inferred"]}', '',
                  f'**Geography:** {r["geography"]}. **Availability caveat:** {r["availability_caveat"]}', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='Regenerate REPORT.md after editing the CSV')
    args = parser.parse_args()
    rows = load()
    sources = json.loads((DIRECTORY / 'source-checks.json').read_text(encoding='utf-8'))
    urls = {r[k] for r in rows for k in ('primary_url', 'ota_url') if r[k] != 'Unknown'}
    checks = sources['checks']
    assert sources['checked_on'] == '2026-10-21'
    assert len(checks) == len(urls) and {c['url'] for c in checks} == urls, 'Stale source coverage'
    assert all(c['status'] in (200, 403, 'unavailable') for c in checks), 'Unresolved source URL failure'
    report = DIRECTORY / 'REPORT.md'
    expected = render(rows)
    if args.write:
        report.write_text(expected, encoding='utf-8')
    else:
        assert report.read_text(encoding='utf-8') == expected, 'Stale report; rerun with --write'
    print('PASS: 50 unique manufacturers; 3 sectors; required fields/primary HTTPS sources; scores/ranks; unknown commercial facts; exact CSV/report consistency')


if __name__ == '__main__':
    main()
