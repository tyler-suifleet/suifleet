#!/usr/bin/env python3
"""Fail-closed local runtime activation. Private configuration is never installed."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

FILES = ('coordinator.py', 'dispatcher.py', 'worker.py', 'batch_integration.py', 'dashboard.py')
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
             'fetch', '--no-tags', '--no-write-fetch-head', url,
             '+refs/heads/main:refs/pr-coordinator/activation-main'], cwd=self.root,
            env={**os.environ, 'GH_TOKEN': token, 'GIT_TERMINAL_PROMPT': '0'})
        sha = self.git('rev-parse', 'refs/pr-coordinator/activation-main^{commit}')
        return sha

    def gh(self, endpoint):
        token = run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', self.settings['github_account']])
        return json.loads(run(['gh', 'api', endpoint], env={**os.environ, 'GH_TOKEN': token}))

    def verify(self, old, target):
        self.git('merge-base', '--is-ancestor', old, target)
        changed = self.git('rev-list', '--first-parent', '--reverse', old + '..' + target).splitlines()
        with sqlite3.connect(f'file:{self.state / "reviews.sqlite3"}?mode=ro', uri=True) as db:
            verified_batches = {}
            for commit in changed:
                paths = self.git('diff-tree', '--no-commit-id', '--name-only', '-r', commit + '^1', commit).splitlines()
                if not any(path.startswith(PREFIX) for path in paths):
                    continue
                row = db.execute('SELECT pr,head,base,review_id FROM activation_merges WHERE project=? AND commit_id=?',
                                 (self.settings['project'], commit)).fetchone()
                if not row:
                    self.verify_batch(db, commit, target, verified_batches)
                    continue
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

    def verify_batch(self, db, commit, target, verified):
        """Verify the entire reviewed batch, never impersonate an individual merge.

        Each first-parent step belongs to a pinned ordered manifest. Approval and
        checks apply to the final combined tree, so even activation at an earlier
        step is refused until that final tree is present in the verified target.
        The caller opened this database read-only; this path never creates proofs.
        """
        project = self.settings['project']
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'integration_batches', 'batch_activation_commits'} <= tables:
            raise RuntimeError('runtime change lacks reviewed integration receipt: ' + commit)
        link = db.execute('SELECT batch_id FROM batch_activation_commits WHERE project=? AND commit_id=?',
                          (project, commit)).fetchone()
        if not link:
            raise RuntimeError('runtime change lacks reviewed integration receipt: ' + commit)
        batch_id = link[0]
        if batch_id in verified:
            if commit not in verified[batch_id]:
                raise RuntimeError('runtime change is not a member of the verified batch')
            return
        try:
            row = db.execute('SELECT status,base,head,pr,members,review_id,publish_intent,manifest_digest '
                             'FROM integration_batches WHERE project=? AND id=?', (project, batch_id)).fetchone()
            if not row:
                raise RuntimeError('batch integration receipt is missing')
            status, base, head, number, raw_members, review_id, intent, digest = row
            if status not in ('published', 'reconciling', 'done') or intent != 1:
                raise RuntimeError('batch publication is not verified')
            if (not all(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value)
                        for value in (base, head)) or type(number) is not int or number <= 0
                    or type(review_id) is not int or review_id <= 0):
                raise RuntimeError('invalid batch integration identity')
            members = json.loads(raw_members)
            if not isinstance(members, list) or not 1 <= len(members) <= 16:
                raise RuntimeError('invalid batch member count')
            payload = dict(project=project, id=batch_id, base=base, head=head, members=members)
            if digest != self.batch_hash(payload):
                raise RuntimeError('batch manifest digest does not match')
            self.git('merge-base', '--is-ancestor', head, target)
            previous, commits, pulls, heads = base, set(), set(), set()
            for member in members:
                step, source = member['commit'], member['head']
                if (not all(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value)
                            for value in (step, source, member['reviewed_base']))
                        or type(member['pr']) is not int or member['pr'] <= 0
                        or step in commits or member['pr'] in pulls or source in heads):
                    raise RuntimeError('invalid or duplicate batch member')
                if member['step_base'] != previous or self.git('show', '-s', '--format=%P', step).split() != [previous, source]:
                    raise RuntimeError('batch integration ancestry does not match manifest')
                mapped = db.execute('SELECT batch_id FROM batch_activation_commits WHERE project=? AND commit_id=?',
                                    (project, step)).fetchone()
                if not mapped or mapped[0] != batch_id:
                    raise RuntimeError('batch step lacks an activation receipt')
                self.git('merge-base', '--is-ancestor', member['reviewed_base'], base)
                self.verify_batch_source(db, project, member)
                previous = step
                commits.add(step); pulls.add(member['pr']); heads.add(source)
            if previous != head or commit not in commits:
                raise RuntimeError('batch final head does not match merge chain')
            expected = 'Tyler Bush <tyler.jonathan.bush@gmail.com>'
            metadata = self.git('log', '--format=%an <%ae>|%cn <%ce>', base + '..' + head).splitlines()
            if not metadata or any(value != expected + '|' + expected for value in metadata):
                raise RuntimeError('batch commit authorship violates configured user identity')
            if 'co-authored-by:' in self.git('log', '--format=%B', base + '..' + head).lower():
                raise RuntimeError('batch commit contains additional authorship attribution')
            review = self.batch_review(db, project, number, head, review_id)
            result, snapshot, family = review
            if result.get('verdict') != 'approve' or result.get('acceptance_complete') is not True:
                raise RuntimeError('batch integration approval is incomplete')
            proof = snapshot.get('_integration_batch') or {}
            if (snapshot.get('headRefOid') != head or snapshot.get('baseRefOid') != base
                    or proof.get('id') != batch_id or proof.get('manifest_digest') != digest
                    or proof.get('members') != members):
                raise RuntimeError('batch integration approval does not bind the manifest')
            from coordinator import checks_blocker, feedback
            required = bool(self.git('ls-tree', '--name-only', base, '.github/workflows/testnet.yml'))
            if snapshot.get('_testnet_required') is not required or checks_blocker(snapshot) is not None:
                raise RuntimeError('batch combined checks are incomplete')
            stored_feedback = db.execute('SELECT feedback FROM jobs WHERE id=?', (review_id,)).fetchone()[0]
            if stored_feedback != feedback(snapshot):
                raise RuntimeError('batch review feedback does not match inspected snapshot')
            for member in members:
                self.verify_batch_member_approval(db, project, batch_id, member, number, head, base,
                                                  family, review_id, digest)
            remote = self.gh(f'repos/{self.settings["repository"]}/pulls/{number}')
            if (not remote.get('merged_at') or remote.get('merge_commit_sha') != head
                    or remote['base']['ref'] != 'main'
                    or remote['base']['repo']['full_name'] != self.settings['repository']
                    or remote['head']['sha'] != head
                    or (remote['head'].get('repo') or {}).get('full_name') != self.settings['repository']):
                raise RuntimeError('remote merged batch does not match receipt')
        except (KeyError, TypeError, ValueError, sqlite3.Error, OSError) as error:
            raise RuntimeError('invalid batch integration receipt') from error
        verified[batch_id] = commits

    def verify_batch_member_approval(self, db, project, batch_id, member, number, head, base,
                                     family, review_id, digest):
        """Verify the formal publication of existing review/combined-test proof.

        The account is attested by the coordinator's configured reviewer route.
        Independently verify its actual same-repository GitHub review rather than
        trusting a locally inserted status or copying combined checks onto H_i.
        """
        row = db.execute('SELECT head,account,body_hash,body,status,review_id,review_node '
                         'FROM batch_member_approvals WHERE project=? AND batch_id=? AND pr=?',
                         (project, batch_id, member['pr'])).fetchone()
        if not row:
            raise RuntimeError('batch member approval receipt is missing')
        source_head, account, body_hash, body, status, remote_id, node = row
        if (source_head != member['head'] or status != 'applied' or type(remote_id) is not int or remote_id <= 0
                or not isinstance(account, str) or not account
                or not isinstance(node, str) or not node or not isinstance(body, str)
                or len(body) > 16384 or hashlib.sha256(body.encode()).hexdigest() != body_hash):
            raise RuntimeError('batch member approval receipt is incomplete or changed')
        proof = member['source_receipt']
        expected_lines = (
            f"Source: PR #{member['pr']} at `{member['head']}`, independently source-reviewed by "
            f"{proof['family']} in job {proof['job']} (evidence `{proof['evidence_hash']}`).",
            f"Combined candidate: PR #{number} `{head}` against `{base}`; "
            'ordinary/security CI and any required protected testnet checks passed for that combined candidate.',
            f'Independent combined acceptance review: {family} job {review_id}.',
            f'Batch manifest: `{digest}`.',
        )
        if not set(expected_lines) <= set(body.splitlines()):
            raise RuntimeError('batch member approval does not link source and combined evidence')
        remote = self.gh(f'repos/{self.settings["repository"]}/pulls/{member["pr"]}/reviews/{remote_id}')
        if (remote.get('state') != 'APPROVED' or remote.get('id') != remote_id
                or remote.get('node_id') != node or remote.get('commit_id') != source_head
                or (remote.get('user') or {}).get('login', '').casefold() != account.casefold()
                or remote.get('body') != body):
            raise RuntimeError('remote batch member approval does not match receipt')

    @staticmethod
    def batch_hash(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def batch_review(self, db, project, number, head, review_id):
        row = db.execute('SELECT project,pr,head,family,kind,status,publication_done,result,directory '
                         'FROM jobs WHERE id=?', (review_id,)).fetchone()
        if not row or row[:3] != (project, number, head) or row[4:7] != ('review', 'done', 1):
            raise RuntimeError('published batch review is unavailable')
        result, snapshot, family = json.loads(row[7]), json.loads((Path(row[8])/'snapshot.json').read_text()), row[3]
        author = snapshot['ticket']['agent']
        if family not in ('Codex', 'Claude') or author not in ('Codex', 'Claude') or family == author:
            raise RuntimeError('batch review is not independent')
        return result, snapshot, family

    def verify_batch_source(self, db, project, member):
        proof = member['source_receipt']
        if type(proof['job']) is not int or proof['job'] <= 0:
            raise RuntimeError('invalid source review identity')
        result, snapshot, family = self.batch_review(db, project, member['pr'], member['head'], proof['job'])
        from coordinator import source_feedback, feedback
        stored_feedback = db.execute('SELECT feedback FROM jobs WHERE id=?', (proof['job'],)).fetchone()[0]
        if (stored_feedback != feedback(snapshot) or result.get('source_complete') is not True or result.get('acceptance_ready') is not True
                or result.get('verdict') not in ('approve', 'blocked')
                or proof['family'] != family or member['owner']['agent'] == family
                or member['owner']['agent'] != snapshot['ticket']['agent']
                or member['owner']['key'] != snapshot['ticket']['key']
                or proof['head'] != member['head'] or proof['base'] != member['reviewed_base']
                or snapshot['headRefOid'] != member['head'] or snapshot['baseRefOid'] != proof['base']
                or proof['source_feedback'] != source_feedback(snapshot)
                or member['source_feedback'] != proof['source_feedback']
                or proof['evidence_hash'] != self.batch_hash(result)):
            raise RuntimeError('batch source review proof is incomplete or changed')

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
            upgrade = self.state/'activation-supervisor-upgrade.json'
            if upgrade.exists() and json.loads(upgrade.read_text()).get('phase') == 'pending':
                raise RuntimeError('supervisor upgrade is pending; run explicit upgrade recovery first')
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
