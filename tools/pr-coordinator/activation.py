#!/usr/bin/env python3
"""Fail-closed local runtime activation. Private configuration is never installed."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

FILES = ('coordinator.py', 'dispatcher.py', 'worker.py')
COMPATIBILITY = {'activation_protocol': 1, 'durable_store_compatibility': 1}
PREFIX = 'tools/pr-coordinator/'
SERVICES = ('pr-coordinator.service', 'ticket-dispatcher.service')


def run(argv, cwd=None, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, timeout=120).stdout.strip()


def atomic(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync(path.parent)


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def switch(link, target):
    temporary = link.with_name(link.name + '.next')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)
    sync(link.parent)


def ready(state, role):
    """Called only after a parent acquires its lock and opens its durable store."""
    pid = os.getpid()
    started = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    atomic(state / (role + '-ready.json'), dict(pid=pid, started=started,
           runtime=str(Path(__file__).resolve().parent)))


class Systemd:
    def __init__(self, state):
        self.state = state

    def active(self):
        result = []
        for unit in SERVICES:
            properties = dict(line.split('=', 1) for line in run(
                ['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '-p', 'KillMode']).splitlines())
            if properties.get('KillMode') != 'process':
                raise RuntimeError('parent unit must preserve workers with KillMode=process')
            if properties.get('ActiveState') == 'active':
                result.append(unit)
            elif properties.get('ActiveState') not in ('inactive', 'failed'):
                raise RuntimeError('parent service is transitioning; retry later')
        if SERVICES[1] in result and SERVICES[0] not in result:
            raise RuntimeError('dispatcher is active without its coordinator dependency; repair parent state first')
        return result

    def stop(self, units):
        if units:
            run(['systemctl', '--user', 'stop', *units])

    def start(self, units, generation):
        for unit in units:
            role = 'dispatcher' if unit == SERVICES[1] else 'coordinator'
            (self.state / (role + '-ready.json')).unlink(missing_ok=True)
        if units:
            run(['systemctl', '--user', 'start', *units])
        deadline = time.monotonic() + 30
        while units and time.monotonic() < deadline:
            healthy = True
            for unit in units:
                role = 'dispatcher' if unit == SERVICES[1] else 'coordinator'
                try:
                    receipt = json.loads((self.state / (role + '-ready.json')).read_text())
                    pid = int(run(['systemctl', '--user', 'show', unit, '-p', 'MainPID', '--value']))
                    started = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
                    healthy &= (receipt == dict(pid=pid, started=started, runtime=str(generation.resolve())))
                except (OSError, ValueError, KeyError):
                    healthy = False
            if healthy:
                return
            time.sleep(0.25)
        if units:
            raise RuntimeError('parent startup readiness timed out')


class Activator:
    def __init__(self, settings, library, state, services=None):
        self.settings, self.library, self.state = settings, library, state
        self.root = Path(settings['root'])
        import re
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', settings['repository']):
            raise ValueError('activation repository must be an explicit GitHub owner/repository')
        self.services = services or Systemd(state)
        self.link = library / 'current'
        self.journal = state / 'activation.json'

    def git(self, *args):
        return run(['git', *args], cwd=self.root)

    def tip(self):
        # Fetch exact configured repository, not mutable origin configuration or checkout files.
        url = 'https://github.com/' + self.settings['repository'] + '.git'
        token = run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', self.settings['github_account']])
        run(['git', '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
             'fetch', '--no-tags', url, 'refs/heads/main'], cwd=self.root,
            env={**os.environ, 'GH_TOKEN': token, 'GIT_TERMINAL_PROMPT': '0'})
        sha = self.git('rev-parse', 'FETCH_HEAD^{commit}')
        return sha

    def gh(self, endpoint):
        token = run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', self.settings['github_account']])
        return json.loads(run(['gh', 'api', endpoint], env={**os.environ, 'GH_TOKEN': token}))

    def verify(self, old, target):
        self.git('merge-base', '--is-ancestor', old, target)
        changed = self.git('rev-list', '--first-parent', '--reverse', old + '..' + target).splitlines()
        with sqlite3.connect(f'file:{self.state / "reviews.sqlite3"}?mode=ro', uri=True) as db:
            for commit in changed:
                paths = self.git('diff-tree', '--no-commit-id', '--name-only', '-r', commit + '^1', commit).splitlines()
                if not set(paths).intersection(PREFIX + name for name in (*FILES, 'runtime-compatibility.json')):
                    continue
                row = db.execute('SELECT pr,head,base,review_id FROM activation_merges WHERE project=? AND commit_id=?',
                                 (self.settings['project'], commit)).fetchone()
                if not row:
                    raise RuntimeError('runtime change lacks reviewed integration receipt: ' + commit)
                pr, head, base, review_id = row
                parents = self.git('show', '-s', '--format=%P', commit).split()
                if parents != [base, head]:
                    raise RuntimeError('integration ancestry does not match receipt')
                review = db.execute("SELECT head,status,publication_done,result FROM jobs WHERE id=? AND kind='review'", (review_id,)).fetchone()
                if not review or review[:3] != (head, 'done', 1):
                    raise RuntimeError('published independent review unavailable')
                result = json.loads(review[3])
                if result.get('verdict') != 'approve' or result.get('acceptance_complete') is not True:
                    raise RuntimeError('integration review is incomplete')
                source = self.gh(f'repos/{self.settings["repository"]}/pulls/{pr}')
                if (not source.get('merged_at') or source.get('merge_commit_sha') != commit
                        or source['base']['ref'] != 'main' or source['base']['repo']['full_name'] != self.settings['repository']
                        or source['head']['sha'] != head
                        or (source['head'].get('repo') or {}).get('full_name') != self.settings['repository']):
                    raise RuntimeError('remote merged source does not match receipt')

    def verify_bootstrap(self, target):
        # Initial installation predates activation_merges. Use the existing
        # integration + independently published review + immutable snapshot,
        # then verify its actual merged PR remotely. Subsequent updates require
        # the stricter pre-push activation receipt instead.
        commit = self.git('log', '--first-parent', '-1', '--format=%H', target, '--', PREFIX)
        parents = self.git('show', '-s', '--format=%P', commit).split()
        if len(parents) != 2:
            raise RuntimeError('bootstrap runtime lacks a reviewed merge')
        base, head = parents
        with sqlite3.connect(f'file:{self.state / "reviews.sqlite3"}?mode=ro', uri=True) as db:
            integration = db.execute('SELECT pr FROM integrations WHERE project=? AND commit_id=? AND head=?',
                                     (self.settings['project'], commit, head)).fetchone()
            if not integration:
                raise RuntimeError('bootstrap merge has no integration evidence')
            rows = db.execute("SELECT family,result,directory FROM jobs WHERE project=? AND pr=? AND head=? AND kind='review' AND status='done' AND publication_done=1 ORDER BY id DESC",
                              (self.settings['project'], integration[0], head)).fetchall()
            approved = False
            for family, raw, directory in rows:
                result = json.loads(raw)
                snapshot = json.loads((Path(directory)/'snapshot.json').read_text())
                if (result.get('verdict') == 'approve' and result.get('acceptance_complete') is True
                        and snapshot['headRefOid'] == head and snapshot['baseRefOid'] == base
                        and family in ('Codex', 'Claude') and snapshot['ticket']['agent'] != family):
                    approved = True
                    break
            if not approved:
                raise RuntimeError('bootstrap lacks independent exact-head/base published approval')
        source = self.gh(f'repos/{self.settings["repository"]}/pulls/{integration[0]}')
        if (not source.get('merged_at') or source.get('merge_commit_sha') != commit
                or source['base']['ref'] != 'main' or source['base']['repo']['full_name'] != self.settings['repository']
                or source['head']['sha'] != head
                or (source['head'].get('repo') or {}).get('full_name') != self.settings['repository']):
            raise RuntimeError('bootstrap remote merge evidence does not match')

    def stage(self, target):
        generations = self.library / 'generations'
        generations.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.stage-', dir=generations))
        try:
            compatibility = json.loads(self.git('show', target + ':' + PREFIX + 'runtime-compatibility.json'))
            if compatibility != COMPATIBILITY:
                raise RuntimeError('runtime protocol/store migration requires explicit bootstrap')
            hashes = {}
            for name in FILES:
                mode = self.git('ls-tree', target, PREFIX + name).split()[0]
                if mode != '100644':
                    raise RuntimeError('runtime source must be a regular non-executable Python file')
                data = subprocess.run(['git', 'show', target + ':' + PREFIX + name], cwd=self.root,
                                      check=True, capture_output=True, timeout=120).stdout
                compile(data, name, 'exec')
                (staging / name).write_bytes(data)
                hashes[name] = hashlib.sha256(data).hexdigest()
            # The supervisor protocol is fixed at bootstrap; candidate parents must
            # use it, not silently remove readiness or change persistent store rules.
            (staging / 'activation.py').write_bytes(Path(__file__).read_bytes())
            for name in ('coordinator.py', 'dispatcher.py'):
                run([sys.executable, str(staging / name), '--help'],
                    env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                         'HOME': str(staging), 'PYTHONDONTWRITEBYTECODE': '1'})
            atomic(staging / 'installed.json', dict(revision=target, hashes=hashes, protocol=1))
            for path in staging.iterdir():
                if path.is_file():
                    with path.open('rb') as stream:
                        os.fsync(stream.fileno())
            sync(staging)
            destination = generations / (target + '-' + staging.name.removeprefix('.stage-'))
            os.rename(staging, destination)
            sync(generations)
            return destination
        except BaseException:
            shutil.rmtree(staging)
            raise

    def validate(self, target):
        # Run the reviewed revision's offline suite in a disposable source tree,
        # never in a dirty checkout and never against the installed databases.
        with tempfile.TemporaryDirectory(prefix='runtime-validation-') as directory:
            root = Path(directory)
            paths = self.git('ls-tree', '-r', '--name-only', target, PREFIX,
                             'docs/tracking/acceptance-policy.json').splitlines()
            for name in paths:
                path = Path(name)
                if path.is_absolute() or '..' in path.parts:
                    raise RuntimeError('invalid validation source path')
                mode = self.git('ls-tree', target, name).split()[0]
                if mode not in ('100644', '100755'):
                    raise RuntimeError('validation source must contain regular files')
                content = subprocess.run(['git', 'show', target + ':' + name], cwd=self.root,
                                         check=True, capture_output=True, timeout=120).stdout
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            run([sys.executable, '-m', 'unittest', 'discover', '-s', PREFIX + 'tests'], cwd=root,
                env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(root/'home'),
                     'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'})

    def status(self):
        installed = json.loads((self.link / 'installed.json').read_text())
        receipt = json.loads(self.journal.read_text()) if self.journal.exists() else {}
        return dict(installed=installed['revision'], merged=receipt.get('target', installed['revision']), activation=receipt)

    def recover(self, receipt):
        # Durable intent survives supervisor termination at any replacement/start step.
        units = receipt['units']
        self.services.stop(units)
        switch(self.link, receipt['previous'])
        self.services.start(units, Path(receipt['previous']))
        atomic(self.journal, {**receipt, 'phase': 'rolled_back', 'error': 'startup/replacement failed',
                             'action': 'Inspect activation failure before retrying this revision'})

    def activate(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / 'activation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.journal.exists():
                receipt = json.loads(self.journal.read_text())
                if receipt['phase'] == 'pending':
                    self.recover(receipt)
            old = json.loads((self.link / 'installed.json').read_text())['revision']
            target = self.tip()
            if old == target:
                return self.status()
            if self.journal.exists():
                receipt = json.loads(self.journal.read_text())
                if receipt.get('target') == target and receipt['phase'] == 'unchanged':
                    return self.status()
                if receipt.get('target') == target and receipt['phase'] in ('rolled_back',):
                    raise RuntimeError('revision failed activation; inspect receipt and explicitly clear it to retry')
            try:
                self.verify(old, target)
                generation = self.stage(target)
                self.validate(target)
                if self.tip() != target:
                    raise RuntimeError('main moved during validation; retry with fresh source')
                if json.loads((generation/'installed.json').read_text())['hashes'] == json.loads((self.link/'installed.json').read_text())['hashes']:
                    atomic(self.journal, dict(phase='unchanged', target=target))
                    return self.status()
                units = self.services.active()
                previous = str(self.link.resolve())
                receipt = dict(phase='pending', previous=previous, target=target,
                               generation=str(generation), units=units)
                atomic(self.journal, receipt)
                try:
                    self.services.stop(units)
                    switch(self.link, generation)
                    self.services.start(units, generation)
                    atomic(self.journal, {**receipt, 'phase': 'installed'})
                except BaseException:
                    self.recover(receipt)
                    raise
            except Exception as error:
                if not self.journal.exists() or json.loads(self.journal.read_text()).get('phase') not in ('pending', 'rolled_back'):
                    atomic(self.journal, dict(phase='failed', target=target, error=type(error).__name__, detail=str(error) if isinstance(error, RuntimeError) else 'Subprocess or source validation failed',
                                             action='Inspect activation service journal; fix provenance/validation before retry'))
                raise
            return self.status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=Path.home()/'.config/pr-coordinator/activation.json')
    parser.add_argument('--library', type=Path, default=Path.home()/'.local/lib/pr-coordinator')
    parser.add_argument('--state', type=Path, default=Path.home()/'.local/state/pr-coordinator')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    engine = Activator(json.loads(args.settings.read_text()), args.library, args.state)
    try:
        print(json.dumps(engine.status() if args.status else engine.activate(), sort_keys=True))
    except Exception as error:
        # No subprocess stderr/argv or private configuration in journal output.
        print('Local activation failed: ' + type(error).__name__ + '; inspect private activation.json and provenance', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
