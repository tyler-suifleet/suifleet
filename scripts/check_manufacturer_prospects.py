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


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load():
    with (DIRECTORY / 'manufacturers.csv').open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    require(len(rows) == 50, 'Expected exactly 50 records')
    require(len({r['company'].casefold().strip() for r in rows}) == 50, 'Duplicate company')
    require({r['sector'] for r in rows} == {'industrial', 'energy', 'commercial'}, 'Expected all three research sectors')
    require(rows == sorted(rows, key=lambda r: (-int(r['score']), r['company'].casefold())), 'Records must be sorted by score and company')
    for rank, row in enumerate(rows, 1):
        require(all(v and v.strip() for v in row.values()), f'Empty field: {row["company"]}')
        require(row['rank'] == str(rank), 'Rank does not match sorted position')
        require(row['checked_on'] == '2026-10-21', 'Unexpected research date')
        require(row['source_basis'] in {'Vendor documentation', 'Supplier case study; historical relationship claim'}, 'Unsupported source basis')
        require(row['role'].startswith('manufacturer'), row['company'])
        for key in ('fleet_size', 'budget', 'buyer_interest'):
            require(row[key] == 'Unknown', f'Unsubstantiated commercial claim: {key}')
        for key in ('primary_url', 'ota_url'):
            if row[key] == 'Unknown':
                require(key == 'ota_url', 'Primary source is required')
                continue
            url = urlsplit(row[key])
            require(url.scheme == 'https' and url.hostname and not url.username and not url.password, 'Sources must be HTTPS without embedded credentials')
        linux, boundary, friction = (int(row[k]) for k in ('linux_evidence', 'application_boundary', 'friction'))
        require(all(v in range(3) for v in (linux, boundary, friction)), 'Scoring inputs must be between zero and two')
        require(int(row['score']) == 2 * linux + boundary - friction, 'Score does not match ranking formula')
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
    require(sources['checked_on'] == '2026-10-21', 'Unexpected source-check date')
    require(len(checks) == len(urls) and {c['url'] for c in checks} == urls, 'Stale source coverage')
    require(all(c['status'] in (200, 403, 'unavailable') for c in checks), 'Unresolved source URL failure')
    report = DIRECTORY / 'REPORT.md'
    expected = render(rows)
    if args.write:
        report.write_text(expected, encoding='utf-8')
    else:
        require(report.read_text(encoding='utf-8') == expected, 'Stale report; rerun with --write')
    print('PASS: 50 unique manufacturers; 3 sectors; required fields/primary HTTPS sources; scores/ranks; unknown commercial facts; exact CSV/report consistency')


if __name__ == '__main__':
    main()
