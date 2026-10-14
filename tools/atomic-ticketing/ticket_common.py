import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path.cwd()
VERSION = "0.2.0"
SCHEMA = 2
CLAIMABLE = {'Backlog', 'Ready', 'Todo'}
MAX_SNAPSHOT_AGE = 300

class CoordinationError(Exception):
    pass


def run_git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True)
    if result.returncode:
        raise CoordinationError(result.stderr.strip())
    return result.stdout.strip()


def scope_path(value):
    if not value or '\\' in value or value.startswith('/') or '..' in value.split('/'):
        raise CoordinationError(f'Invalid repository-relative scope: {value!r}')
    # Wildcards reserve the entire directory before the first wildcard, conservatively.
    parts = value.split('/')
    for index, part in enumerate(parts):
        if any(c in part for c in '*?['):
            parts = parts[:index]
            break
    normalized = str(PurePosixPath('/'.join(parts)))
    return normalized if normalized != '.' else '.'


def overlaps(left, right):
    left, right = scope_path(left), scope_path(right)
    return left == '.' or right == '.' or left == right or left.startswith(right + '/') or right.startswith(left + '/')


def conflicting_resources(left, right):
    """Shared hardware/build/port reservations are the only exclusive scope."""
    return any(overlaps(a, b) for a in left if a.startswith('resources/')
               for b in right if b.startswith('resources/'))


def database_path(root=None):
    state_home = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
    return state_home / 'atomic-ticketing' / 'coordination.sqlite3'


def ordered_issues(config):
    if config.get('schema_version') != 1 or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', config.get('repository', '')):
        raise CoordinationError('Invalid catalog schema/repository')
    issues = config['issues']
    keys = {issue['key'] for issue in issues}
    if len(keys) != len(issues):
        raise CoordinationError('Duplicate ticket keys')
    for issue in issues:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', issue['key']):
            raise CoordinationError('Invalid ticket key')
        if issue['priority'] not in {'P0', 'P1', 'P2'} or not issue.get('paths') or not issue.get('acceptance') or not issue.get('validation'):
            raise CoordinationError('Ticket needs priority, paths, acceptance and verification')
        if any(dep not in keys and '::' not in dep for dep in issue['depends_on']):
            raise CoordinationError('Unknown dependency')
    ordered, pending = [], list(issues)
    while pending:
        seen = {item['key'] for item in ordered}
        ready = [item for item in pending if {d for d in item['depends_on'] if '::' not in d} <= seen]
        if not ready:
            raise CoordinationError('Cyclic dependencies')
        ordered.extend(ready)
        pending = [item for item in pending if item not in ready]
    return ordered


def load_configuration(root):
    root = Path(run_git(root, 'rev-parse', '--show-toplevel'))
    config = json.loads((root / '.atomic-ticketing.json').read_text())
    if config.get('schema_version') != 1 or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', config.get('project_id', '')):
        raise CoordinationError('Invalid .atomic-ticketing.json schema/project_id')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', config.get('repository', '')):
        raise CoordinationError('Invalid repository')
    if not config.get('github', {}).get('account') or not config['github'].get('owner'):
        raise CoordinationError('Configure the GitHub API account and Project owner')
    if config.get('catalog', {}).get('kind') not in {'files', 'github'}:
        raise CoordinationError('Catalog kind must be files or github')
    for key in ('backlog', 'state'):
        if key in config['catalog']:
            scope_path(config['catalog'][key])
    return root, config
