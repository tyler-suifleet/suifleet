#!/usr/bin/env python3
"""Offline stand-in for a model CLI used by the SF-116 redirect tests.

Implementation/resume prompts idle like a long-running worker. A repair-first
prompt commits a narrow fix in its clean checkout, pushes it fast-forward to the
claimed branch and writes a structured result. Nothing here reaches a network.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

prompt = sys.stdin.read()
log = os.environ.get('FAKE_CLI_LOG')
mode = 'repair' if 'CI REPAIR-FIRST' in prompt else 'implement'
if log:
    with open(log, 'a') as out:
        out.write(json.dumps({'mode': mode, 'cwd': os.getcwd(), 'token': os.environ.get('PR_COORDINATOR_CLAIM_TOKEN'),
                              'resumed': 'CI repair-first redirect' in prompt}) + '\n')
if mode == 'implement' and os.environ.get('FAKE_MUTATE'):
    # A legacy worker that obeys shared-resource locks: each mutation runs only
    # while it holds the exclusive lease. A begin without an end in the log
    # means the worker was interrupted mid-mutation.
    ticket = [sys.executable, str(Path(__file__).with_name('fake_ticket.py')), '--db', os.environ['FAKE_TICKET_DB'],
              '--project', 'suifleet']
    resource = os.environ['FAKE_MUTATE']
    deadline = time.time() + float(os.environ.get('FAKE_CLI_SLEEP', '300'))
    while time.time() < deadline:
        taken = subprocess.run([*ticket, 'lock', resource, '--agent', 'Codex', '--model', 'fake-model', '--session',
                                'dispatch-owner', '--purpose', 'SF-099 shared mutation', '--ttl', '60'],
                               capture_output=True, text=True)
        with open(log, 'a') as out:
            out.write(json.dumps({'mutation': 'begin' if taken.returncode == 0 else 'refused'}) + '\n')
        if taken.returncode == 0:
            time.sleep(float(os.environ.get('FAKE_MUTATION_SECONDS', '0.8')))
            with open(log, 'a') as out:
                out.write(json.dumps({'mutation': 'end'}) + '\n')
            subprocess.run([*ticket, 'unlock', resource, '--lock-token', json.loads(taken.stdout)['token']], check=True,
                           capture_output=True)
        time.sleep(float(os.environ.get('FAKE_MUTATION_GAP', '0.3')))
    sys.exit(0)
if mode == 'implement':
    time.sleep(float(os.environ.get('FAKE_CLI_SLEEP', '300')))
    sys.exit(0)
result = Path(re.search(r'^Result file: (.+)$', prompt, re.M)[1])
time.sleep(float(os.environ.get('FAKE_REPAIR_DELAY', '0')))  # Lets a test observe the live repair.
branch = re.search(r'git push origin HEAD:refs/heads/(\S+)', prompt)[1]
behaviour = os.environ.get('FAKE_REPAIR', 'fix')
if behaviour == 'blocked':
    result.write_text(json.dumps({'verdict': 'blocked', 'model': 'fake-model', 'evidence': 'cannot reproduce offline'}))
    sys.exit(0)
target = Path(os.environ.get('FAKE_REPAIR_FILE', 'fix.txt'))
target.write_text('repaired\n')
identity = {'GIT_AUTHOR_NAME': 'Tyler Bush', 'GIT_AUTHOR_EMAIL': 'tyler.jonathan.bush@gmail.com',
            'GIT_COMMITTER_NAME': 'Tyler Bush', 'GIT_COMMITTER_EMAIL': 'tyler.jonathan.bush@gmail.com'}
env = {**os.environ, **identity}
subprocess.run(['git', 'add', str(target)], check=True, env=env)
subprocess.run(['git', 'commit', '-q', '-m', 'Repair failed check', '-m', '- Fix the failing fixture.'], check=True, env=env)
subprocess.run(['git', 'push', '-q', 'origin', f'HEAD:refs/heads/{branch}'], check=True, env=env)
head = subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
result.write_text(json.dumps({'verdict': 'fixed', 'model': 'fake-model', 'evidence': f'fixed and pushed {head}; offline test passed'}))
