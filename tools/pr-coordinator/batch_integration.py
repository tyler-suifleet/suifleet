"""Durable, exact-candidate integration batches (no wallet or issue creation)."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

IDENTITY = 'Tyler Bush <tyler.jonathan.bush@gmail.com>'
TERMINAL = ('done', 'invalidated', 'failed')
SHA = re.compile(r'^[0-9a-f]{40}$')
PAIR_INCIDENT_HASHES = {
    'package-upgrade.json': '60062f8c4da6b48b678f2feaa96ef3e22c98722bf602b1326c3c3cbc4e180c0c',
    'upgrade-security.json': '0612fa3a06e7e696dae714c4c081fd5164569d29b74edb8adb04f0a8d2827d60',
}


class BatchInvalidated(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def pull_identity(value):
    """Pin reconciliation authority without volatile embedded repo metadata."""
    return (value.get('number'), value.get('state'), value.get('draft'),
            *((value.get(side, {}).get('sha'), value.get(side, {}).get('ref'),
               value.get(side, {}).get('repo', {}).get('full_name'))
              for side in ('head', 'base')))


def batch_error(batch):
    """Classify structured failures only; retain legacy diagnostics in storage."""
    try:
        value = json.loads(batch.get('error') or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def manifest_digest(project, batch_id, base, head, members):
    return hashlib.sha256(canonical(dict(project=project, id=batch_id, base=base,
                                         head=head, members=members)).encode()).hexdigest()


class BatchController:
    def __init__(self, coordinator, clock=time.time, installed=None):
        self.c, self.db, self.clock = coordinator, coordinator.store.db, clock
        self.installed = Path(installed) if installed is not None else Path(__file__).with_name('installed.json')
        self._installed_proofs = set()
        self.db.execute('''CREATE TABLE IF NOT EXISTS shared_harness_incidents (
            project TEXT,batch_id TEXT,owner_ticket TEXT,manifest TEXT,error_hash TEXT,
            receipt TEXT,created REAL,PRIMARY KEY(project,batch_id))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS integration_batches (
            project TEXT,id TEXT,status TEXT,base TEXT,head TEXT,branch TEXT,pr INTEGER,
            members TEXT,directory TEXT,error TEXT,created REAL,updated REAL,
            review_id INTEGER,publish_intent INTEGER DEFAULT 0,manifest_digest TEXT,
            PRIMARY KEY(project,id))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS batch_activation_commits (
            project TEXT,commit_id TEXT,batch_id TEXT,PRIMARY KEY(project,commit_id))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS batch_member_approvals (
            project TEXT,batch_id TEXT,pr INTEGER,head TEXT,account TEXT,body_hash TEXT,body TEXT,
            status TEXT,review_id INTEGER,review_node TEXT,created REAL,updated REAL,error TEXT,
            PRIMARY KEY(project,batch_id,pr))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS batch_funding_recoveries (
            project TEXT,batch_id TEXT,manifest TEXT,error_hash TEXT,receipt TEXT,created REAL,
            PRIMARY KEY(project,batch_id))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS batch_infrastructure_retries (
            project TEXT,batch_id TEXT,manifest TEXT,error_hash TEXT,receipt TEXT,created REAL,
            PRIMARY KEY(project,batch_id))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS integration_batch_conflicts (
            project TEXT,pr INTEGER,head TEXT,base TEXT,batch_id TEXT,context TEXT,
            PRIMARY KEY(project,pr,head,base))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS batch_historical_members (
            project TEXT,batch_id TEXT,pr INTEGER,receipt TEXT,created REAL,
            PRIMARY KEY(project,batch_id,pr))''')

    def exclude_included_sources(self, project, candidates, base):
        """Record ancestry only, never infer approval or ticket completion."""
        if not candidates:
            return []
        self.git(project, 'fetch', '--no-tags', 'origin', base,
                 *dict.fromkeys(m['head'] for m in candidates))
        self.db.execute('''CREATE TABLE IF NOT EXISTS batch_included_sources (
            project TEXT,pr INTEGER,head TEXT,base TEXT,receipt TEXT,created REAL,
            PRIMARY KEY(project,pr,head,base))''')
        available = []
        for member in candidates:
            try:
                self.git(project, 'merge-base', '--is-ancestor', member['head'], base)
            except subprocess.CalledProcessError as error:
                if error.returncode != 1:
                    raise  # Unknown ancestry must not authorize a new candidate.
                available.append(member)
                continue
            receipt = dict(head=member['head'], base=base, owner=member['owner'],
                           status='included-source-awaiting-reconciliation',
                           acceptance_complete=False)
            self.db.execute('INSERT OR IGNORE INTO batch_included_sources VALUES(?,?,?,?,?,?)',
                (project['id'], member['pr'], member['head'], base, canonical(receipt), self.clock()))
        return available

    @staticmethod
    def suspension(project):
        config = project.get('integration_batch', {})
        if not isinstance(config, dict) or config.get('enabled') is not True:
            return None  # A deliberate permanent disable is never overridden.
        required = config.get('suspend_until_installed')
        if required is None:
            return None
        reason = config.get('suspension_reason')
        if (not isinstance(required, str) or not SHA.fullmatch(required)
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 500):
            raise ValueError('batch recovery suspension requires an exact commit and bounded reason')
        return required

    def enabled(self, project):
        config = project.get('integration_batch', {})
        if not isinstance(config, dict) or config.get('enabled') is not True:
            return False
        required = self.suspension(project)
        if required is None:
            return True
        try:
            metadata = json.loads(self.installed.read_text())
            revision = metadata['revision']
            if type(metadata.get('protocol')) is not int or metadata['protocol'] != 1 or not isinstance(revision, str) or not SHA.fullmatch(revision):
                return False
            proof = (str(project['root']), required, revision)
            if proof not in self._installed_proofs:
                # The installed generation is authorized by the merged-only
                # activator. A branch tip, open PR or elapsed timer is no proof.
                self.git(project, '--no-replace-objects', 'merge-base', '--is-ancestor', required, revision)
                self._installed_proofs.add(proof)
            return True
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return False

    def pause_status(self, project, pr):
        if self.enabled(project):
            return None
        required = self.suspension(project)
        if required:
            # Only the exact repair source can use the single-PR bootstrap path.
            if pr.get('headRefOid') != required:
                return 'waiting-installed-repair'
            return None
        for batch in self.rows(project):
            if batch['status'] in TERMINAL:
                continue
            members = json.loads(batch['members'])
            if pr['number'] == batch.get('pr') or any(m['pr'] == pr['number'] for m in members):
                return 'disabled-with-active-batch'
        return None

    def settings(self, project):
        config = project.get('integration_batch', {})
        maximum, delay = config.get('max_members', 4), config.get('collection_seconds', 120)
        if not project.get('review_github_account'):
            raise ValueError('integration batching requires an independent review publication account')
        if type(maximum) is not int or not 1 <= maximum <= 8:
            raise ValueError('integration batch max_members must be 1..8')
        if type(delay) not in (int, float) or not 0 <= delay <= 1800:
            raise ValueError('integration batch collection_seconds must be 0..1800')
        return maximum, delay

    def rows(self, project):
        cursor = self.db.execute('SELECT * FROM integration_batches WHERE project=? ORDER BY created', (project['id'],))
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def reserved_members(self, project):
        """Keep source members reserved until their integration epoch ends."""
        return {member['pr'] for row in self.rows(project) if row['status'] not in TERMINAL
                for member in json.loads(row['members'])}

    def update(self, batch, **values):
        values['updated'] = self.clock()
        self.db.execute('UPDATE integration_batches SET ' + ','.join(key+'=?' for key in values)
                        + ' WHERE project=? AND id=?', (*values.values(), batch['project'], batch['id']))
        batch.update(values)

    def git(self, project, *args, cwd=None):
        # Only trusted local config may select a driver. Disable external drivers,
        # hooks, fsmonitor and all global/system filters before touching candidates.
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
               'GIT_TERMINAL_PROMPT': '0', 'GIT_AUTHOR_NAME': 'Tyler Bush',
               'GIT_AUTHOR_EMAIL': 'tyler.jonathan.bush@gmail.com',
               'GIT_COMMITTER_NAME': 'Tyler Bush', 'GIT_COMMITTER_EMAIL': 'tyler.jonathan.bush@gmail.com'}
        options = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.attributesFile=/dev/null',
                   '-c', 'core.fsmonitor=false', '-c', 'commit.gpgsign=false', '-c', 'merge.renormalize=false',
                   '-c', 'submodule.recurse=false']
        local = subprocess.run(['git', 'config', '--local', '--name-only', '--get-regexp',
                                r'^(filter\..*\.(clean|smudge|process|required)|merge\..*\.driver)$'],
                               cwd=project['root'], env=env, text=True, capture_output=True)
        if local.returncode not in (0, 1):
            raise RuntimeError('cannot inspect trusted Git driver configuration')
        for key in local.stdout.splitlines():
            value = 'false' if key.endswith(('.required', '.driver')) else ''
            options += ['-c', key+'='+value]
        result = subprocess.run(['git', *options, *args], cwd=cwd or project['root'], env=env,
                                text=True, capture_output=True, check=True, timeout=180)
        return result.stdout.strip()

    def main(self, project):
        line = self.git(project, 'ls-remote', 'origin', 'refs/heads/main').split()
        if len(line) != 2 or not SHA.fullmatch(line[0]):
            raise RuntimeError('cannot verify remote integration base')
        return line[0]

    def owner(self, project, member):
        return json.loads(self.c.ticket(project, 'show', member['owner']['key']))

    @staticmethod
    def outstanding_requests(pr, allow_coordinator=False, review_account=None):
        from coordinator import publication_hash
        latest = {}
        for review in pr.get('reviews', []):
            if review.get('state') in ('APPROVED','CHANGES_REQUESTED'):
                latest[(review.get('author') or {}).get('login', '')] = review
        outstanding = []
        for author, review in latest.items():
            if review['state'] != 'CHANGES_REQUESTED':
                continue
            known = pr.get('_receipts', {}).get(review.get('id')) == publication_hash(review)
            if (not allow_coordinator or not known or not author
                    or author.casefold() != str(review_account or '').casefold()):
                outstanding.append(review)
        return outstanding

    @staticmethod
    def stale_base_hold(evidence):
        """Recognize the existing isolated SF-146 hold for collection only.

        Never settle it or change funding/spend. A combined candidate still
        executes the protected harness, including its full historical hold.
        Unknown reservations remain ineligible for this routing exception.
        """
        held = evidence.get('funding', {}).get('held_mist')
        quarantine = evidence.get('quarantine')
        if type(held) is not int:
            return False
        if not quarantine:
            return held == 0
        if not isinstance(quarantine, dict) or set(quarantine) != {'sf146-run40095854988'}:
            return False
        record = quarantine['sf146-run40095854988']
        return (isinstance(record, dict)
            and evidence.get('operator') == '0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac'
            and held == 400_000_000
            and record.get('status') == 'unresolved'
            and type(record.get('reserved_mist')) is int
            and record['reserved_mist'] == held
            and record.get('source') == {
                'run': 40095854988, 'head': 'c50d39902848278b550717a29cd044e73485afd2',
                'base': 'b9e8053f4f034ca725686d19870cde6f3f756d63',
                'epoch': 1262, 'checkpoint': 398674879}
            and type(record.get('quarantined')) in (int, float) and record['quarantined'] > 0
            and all(record.get(key) is None for key in
                    ('resolved', 'digest', 'gas_mist', 'isolated_objects')))

    def annotate_stale_base(self, project, pr):
        """Read-only exception for a proven pre-submission stale-contract rejection.

        This authorizes collection only. Never clears a check, repair, claim,
        fixture, spend receipt or final candidate acceptance requirement.
        """
        from coordinator import failed_checks, testnet_failure, source_feedback
        pr.pop('_stale_base_contracts', None)
        failure = testnet_failure(pr)
        if (pr.get('_review_mode') != 'source' or not failure
                or failure.get('classification') != 'source'
                or failed_checks(pr) != [failure]
                or pr.get('_batch_failure') or pr.get('_batch_conflict') or pr.get('_batch_review_failure')):
            return
        active = self.db.execute("SELECT 1 FROM jobs WHERE project=? AND pr=? AND kind='fix' "
                                "AND status IN ('reserved','running','orphaned','quarantined','quota_wait','quota_launch')",
                                (project['id'], pr['number'])).fetchone()
        if active:
            return
        try:
            head, base = pr['headRefOid'], pr['baseRefOid']
            prefix = f"repos/{project['repository']}"
            read = lambda path: json.loads(self.c.gh(project, ['api', path]))
            comparison = self.c.immutable_read(project, f'{prefix}/compare/{base}...{head}')
            ancestor = comparison['merge_base_commit']['sha']
            if not all(SHA.fullmatch(x) for x in (head, base, ancestor)) or ancestor == base:
                return
            def contracts(revision):
                tree = self.c.immutable_read(project, f'{prefix}/git/trees/{revision}')
                entries = [e for e in tree['tree'] if e['path'] == 'contracts' and e['type'] == 'tree']
                if tree.get('truncated') or len(entries) != 1 or not SHA.fullmatch(entries[0]['sha']):
                    raise RuntimeError('contract tree unavailable')
                return entries[0]['sha']
            original, source, current = map(contracts, (ancestor, head, base))
            if source != original or current == original:
                return
            match = re.fullmatch(r'https://github\.com/' + re.escape(project['repository'])
                                 + r'/actions/runs/([1-9][0-9]*)', failure['detailsUrl'])
            if not match:
                return
            run_id = match[1]
            path = f'{prefix}/actions/runs/{run_id}'
            run = read(path)
            if (str(run.get('id')) != run_id or run.get('event') != 'workflow_run'
                    or run.get('head_branch') != 'main' or run.get('head_sha') != base
                    or run.get('name') != 'Automatic testnet' or run.get('path') != '.github/workflows/testnet.yml'
                    or run.get('repository', {}).get('full_name') != project['repository']
                    or run.get('head_repository', {}).get('full_name') != project['repository']
                    or run.get('status') != 'completed' or run.get('conclusion') != 'failure'
                    or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1):
                return
            with tempfile.TemporaryDirectory(prefix='stale-base-effects-') as directory:
                self.c.gh(project, ['run', 'download', run_id, '-R', project['repository'],
                                   '-n', 'testnet-effects-' + head, '-D', directory])
                files = list(Path(directory).iterdir())
                if (len(files) != 1 or files[0].name != 'package-upgrade.json' or files[0].is_symlink()
                        or not files[0].is_file() or files[0].stat().st_size > 1_000_000):
                    return
                raw = files[0].read_bytes()
                evidence = json.loads(raw)
            transactions = evidence.get('transactions', [])
            error = evidence.get('error', '')
            # Both diagnostics describe declarations added by newer main. The
            # immutable ancestor/source contract-tree equality above is still
            # required; this never permits a source contract deletion.
            diagnostics = set(re.findall(r'error\[([A-Z0-9]+)\]', error))
            stale_diagnostics = {
                'EUC01001': 'missing public declaration',
                'EUC01006': 'module missing',
            }
            stale_rejection = (bool(diagnostics) and diagnostics <= stale_diagnostics.keys()
                and all(f'error[{code}]: {stale_diagnostics[code]}' in error
                        for code in diagnostics))
            if (evidence.get('head') != head or evidence.get('base') != base
                    or evidence.get('network') != 'testnet' or evidence.get('chain') != '4c78adac'
                    or evidence.get('success') is not False or evidence.get('failure_class') != 'source'
                    or not self.stale_base_hold(evidence)
                    or evidence.get('reconciliations') != [] or evidence.get('cleanup_error')
                    or not transactions or transactions[-1].get('label') != 'upgrade to head'
                    or transactions[-1].get('status') != 'not_submitted' or transactions[-1].get('digest')
                    or not error.startswith('test-upgrade could not build a transaction:')
                    or not stale_rejection
                    or "this package requires changes to be compatible" not in error):
                return
            used = 0
            allowed = ['publish base', 'register device + issue cap (base)', 'add device to group (base)']
            # A reverified cached base needs no setup writes in this run. Keep
            # that zero-spend path distinct from the fully accounted fresh setup.
            cached = (len(transactions) == 1
                and isinstance(evidence.get('checks'), list)
                and any(isinstance(check, str) and re.fullmatch(
                    r'verified/reused base package 0x[0-9a-f]{64}; no identical package is republished',
                    check) for check in evidence['checks'])
                and type(evidence.get('allowance_used_mist')) is int
                and evidence['allowance_used_mist'] == 0
                and type(evidence.get('spent_mist')) is int and evidence['spent_mist'] == 0
                and type(evidence.get('start_balance_mist')) is int
                and evidence.get('start_balance_mist') == evidence.get('end_balance_mist'))
            if len(transactions) != 4 and not cached:
                return
            for transaction, label in zip(transactions[:-1], allowed):
                gas = transaction.get('gas_mist')
                if (transaction.get('label') != label or transaction.get('status') != 'success'
                        or not transaction.get('digest') or type(gas) is not int or gas < 0):
                    return
                used += gas
                if transaction.get('allowance_used_mist') != used:
                    return
            if (type(evidence.get('max_spend_mist')) is not int or not 0 <= used <= evidence['max_spend_mist']
                    or evidence.get('allowance_used_mist') != used
                    or transactions[-1].get('allowance_used_mist') != used or read(path) != run):
                return
            pr['_stale_base_contracts'] = dict(head=head, base=base, ancestor=ancestor,
                source_tree=source, base_tree=current, source_feedback=source_feedback(pr),
                status_hash=hashlib.sha256(json.dumps(pr['_testnet_status'], sort_keys=True).encode()).hexdigest(), run=int(run_id), attempt=run['run_attempt'],
                evidence_sha256=hashlib.sha256(raw).hexdigest(), allowance_used_mist=used,
                held_mist=evidence['funding']['held_mist'])
        except Exception:
            # Missing, ambiguous, changed or unavailable evidence is no exception.
            return

    def source(self, project, pr, owner):
        from coordinator import source_check_failures, verified_stale_base
        if pr.get('_stale_base_contracts') and not verified_stale_base(pr):
            return None
        # Concrete source failures need their existing owner before collection.
        # Pending checks may still be validated on the combined candidate.
        if source_check_failures(pr):
            return None
        if (not owner or owner.get('status') != 'review' or owner.get('publish_pending')
                or owner.get('expired') or pr.get('_batch_failure') or pr.get('_batch_conflict')
                or self.repair_context(project, pr) or pr.get('state') != 'OPEN'
                or self.outstanding_requests(pr, True, project.get('review_github_account')) or pr.get('isDraft')
                or pr.get('isCrossRepository') or pr.get('mergeable') != 'MERGEABLE'
                or any(not t.get('isResolved') for t in pr.get('threads', []))):
            return None
        active = self.db.execute("SELECT 1 FROM jobs WHERE project=? AND pr=? AND kind='fix' "
                                 "AND status IN ('reserved','running','orphaned','quarantined','quota_wait','quota_launch')",
                                 (project['id'], pr['number'])).fetchone()
        if active:
            return None
        try:
            receipt = self.c.source_receipt(project, pr, owner)
            if not receipt:
                return None
            self.c.assert_review_owner(project, owner)
        except Exception:
            return None
        if not receipt or not SHA.fullmatch(receipt.get('base', '')):
            return None
        from coordinator import source_feedback
        return dict(pr=pr['number'], head=pr['headRefOid'], reviewed_base=receipt['base'],
                    source_receipt=receipt, source_feedback=source_feedback(pr),
                    owner={key: owner.get(key) for key in ('key', 'agent', 'model', 'session', 'branch')}
                    | {'claim_hash': hashlib.sha256(owner['token'].encode()).hexdigest()},
                    acceptance=owner.get('spec', {}).get('acceptance', []), title=pr.get('title', ''),
                    stale_base_contracts=pr.get('_stale_base_contracts'))

    def verify_manifest(self, project, batch):
        if not batch.get('head'):
            return
        members = json.loads(batch['members'])
        expected = manifest_digest(project['id'], batch['id'], batch['base'], batch['head'], members)
        if expected != batch['manifest_digest'] or not members:
            raise RuntimeError('integration manifest digest mismatch')
        previous = batch['base']
        for member in members:
            if not all(SHA.fullmatch(member.get(k, '')) for k in ('head','commit','step_base')):
                raise RuntimeError('invalid integration manifest revision')
            if (member['step_base'] != previous
                    or self.git(project,'show','-s','--format=%P',member['commit']).split() != [previous,member['head']]
                    or self.git(project,'show','-s','--format=%an <%ae>|%cn <%ce>',member['commit']) != IDENTITY+'|'+IDENTITY):
                raise RuntimeError('integration manifest ancestry or identity mismatch')
            previous = member['commit']
        if previous != batch['head']:
            raise RuntimeError('integration manifest final revision mismatch')

    def validate_members(self, project, batch):
        from coordinator import source_feedback
        self.verify_manifest(project, batch)
        self.synthetic_owner(batch)
        for member in json.loads(batch['members']):
            pr = self.c.pr(project, member['pr'])
            owner = self.owner(project, member)
            if (pr['headRefOid'] != member['head'] or source_feedback(pr) != member['source_feedback']
                    or any(owner.get(key) != member['owner'][key] for key in ('key','agent','model','session','branch'))
                    or hashlib.sha256((owner.get('token') or '').encode()).hexdigest() != member['owner']['claim_hash']):
                raise BatchInvalidated('batch member ownership, head or feedback changed')
            fresh = self.source(project, pr, owner)
            if (not fresh or fresh['source_receipt'] != member['source_receipt']
                    or fresh.get('stale_base_contracts') != member.get('stale_base_contracts')):
                raise BatchInvalidated('batch source review or acceptance changed')
            # source() revalidates the non-expiring Review claim and dependencies.
        if self.main(project) != batch['base']:
            raise BatchInvalidated('integration base changed')

    def synthetic_owner(self, batch):
        members = json.loads(batch['members'])
        families = {m['owner']['agent'] for m in members}
        if len(families) != 1 or not families <= {'Codex', 'Claude'}:
            raise BatchInvalidated('integration members must have one author family')
        return dict(key='integration:'+batch['id'], agent=families.pop(), model='integration-controller',
                    status='review', publish_pending=False,
                    spec=dict(priority='P0', acceptance=[criterion for m in members for criterion in m['acceptance']],
                              depends_on=[], batch_members=[m['pr'] for m in members]),
                    evidence=canonical(self.marker(batch)))

    @staticmethod
    def marker(batch):
        return dict(id=batch['id'], manifest_digest=batch['manifest_digest'], members=json.loads(batch['members']))

    def candidate(self, project, batch):
        pr = self.c.pr(project, batch['pr'])
        if pr['headRefOid'] != batch['head'] or pr['baseRefOid'] != batch['base']:
            raise BatchInvalidated('integration candidate head/base changed')
        pr['_integration_batch'] = self.marker(batch)
        return pr

    def invalidate(self, project, batch, error):
        # Close only a positively identified, still-open coordinator candidate.
        if batch.get('pr'):
            pr = self.c.pr(project, batch['pr'])
            if pr['state'] == 'MERGED':
                raise RuntimeError('merged candidate requires publication reconciliation')
            if pr['state'] == 'OPEN' and pr.get('headRefName') == batch['branch']:
                self.c.gh(project, ['api', f"repos/{project['repository']}/pulls/{batch['pr']}",
                                    '--method', 'PATCH', '-f', 'state=closed'])
        self.update(batch, status='invalidated', error=str(error))

    def cleanup(self, project, batch):
        # Only the coordinator-owned disposable worktree is removed. Receipts and
        # Git objects remain available for review, activation and reconciliation.
        directory = Path(batch['directory'])
        expected = self.c.state/project['id']/'batches'/batch['id']
        if directory != expected:
            raise RuntimeError('unexpected integration worktree path')
        paths = self.git(project, 'worktree', 'list', '--porcelain').splitlines()
        if 'worktree '+str(directory) in paths:
            self.git(project, 'worktree', 'remove', '--force', str(directory))
        elif directory.exists():
            raise RuntimeError('unregistered candidate directory requires inspection')

    def reject_member(self, project, batch, member, reason):
        context = dict(candidate_base=batch['base'], batch_id=batch['id'],
                       other_members=[], reason=reason)
        self.db.execute('INSERT OR REPLACE INTO integration_batch_conflicts VALUES(?,?,?,?,?,?)',
            (project['id'],member['pr'],member['head'],batch['base'],batch['id'],canonical(context)))

    def recover_build(self, project, batch):
        # These states precede every remote publication intent. Require an absent
        # remote ref too, so an unexpected external write can never be overwritten.
        if (batch.get('head') or batch.get('pr') or batch.get('publish_intent')
                or self.git(project, 'ls-remote', 'origin', 'refs/heads/'+batch['branch'])):
            raise RuntimeError('build has publication evidence; manual inspection required')
        self.validate_members(project, batch)
        self.cleanup(project, batch)
        self.update(batch, status='building', error=None)
        self.build(project, batch)

    def build(self, project, batch):
        members = json.loads(batch['members'])
        accepted, previous = [], batch['base']
        try:
            self.git(project, 'fetch', '--no-tags', 'origin', batch['base'],
                     *dict.fromkeys([m['head'] for m in members]+[m['reviewed_base'] for m in members]))
            self.git(project, 'worktree', 'add', '--detach', batch['directory'], batch['base'])
            for member in members:
                # A stacked source can be reviewed against a preceding member.
                # Require that base in the already accepted candidate history;
                # a later, rejected or unrelated source cannot supply it.
                try:
                    self.git(project, 'merge-base', '--is-ancestor', member['reviewed_base'], previous)
                except subprocess.CalledProcessError as error:
                    if error.returncode != 1:
                        raise
                    self.reject_member(project, batch, member,
                        'reviewed base is absent from preceding candidate history')
                    continue
                try:
                    self.git(project, 'merge-base', '--is-ancestor', member['head'], previous)
                except subprocess.CalledProcessError as error:
                    if error.returncode != 1:
                        raise
                else:
                    # GitHub may briefly retain an open PR after an external
                    # integration. Do not turn an already-present head into an
                    # ambiguous build or manufacture a redundant merge step.
                    continue
                metadata = self.git(project, 'log', '--format=%an <%ae>|%cn <%ce>', batch['base']+'..'+member['head'])
                if not metadata or any(line != IDENTITY+'|'+IDENTITY for line in metadata.splitlines()):
                    self.reject_member(project, batch, member, 'commit author/committer must be Tyler Bush')
                    continue
                if 'co-authored-by:' in self.git(project, 'log', '--format=%B', batch['base']+'..'+member['head']).lower():
                    self.reject_member(project, batch, member, 'commit contains additional authorship attribution')
                    continue
                try:
                    self.git(project, 'merge', '--no-ff', '--no-edit', member['head'], '-m',
                             f"Merge PR #{member['pr']}: {member['title']}", '-m',
                             '- Include the independently source-reviewed revision in a combined integration candidate.\n'
                             '- Require exact-candidate CI, testnet and independent integration review before publication.',
                             cwd=batch['directory'])
                except subprocess.CalledProcessError:
                    self.git(project, 'merge', '--abort', cwd=batch['directory'])
                    context = dict(candidate_base=batch['base'], batch_id=batch['id'],
                                   other_members=[dict(pr=m['pr'],head=m['head']) for m in accepted])
                    self.db.execute('INSERT OR REPLACE INTO integration_batch_conflicts VALUES(?,?,?,?,?,?)',
                        (project['id'],member['pr'],member['head'],batch['base'],batch['id'],canonical(context)))
                    continue
                commit = self.git(project, 'rev-parse', 'HEAD', cwd=batch['directory'])
                parents = self.git(project, 'show', '-s', '--format=%P', 'HEAD', cwd=batch['directory']).split()
                if parents != [previous, member['head']]:
                    raise RuntimeError('candidate merge ancestry mismatch')
                accepted.append({**member, 'step_base': previous, 'commit': commit})
                previous = commit
            if not accepted:
                self.update(batch, status='failed', error='no conflict-free eligible batch members')
                return
            self.git(project, 'diff', '--check', batch['base'], 'HEAD', cwd=batch['directory'])
            digest = manifest_digest(project['id'], batch['id'], batch['base'], previous, accepted)
            self.update(batch, head=previous, members=canonical(accepted), manifest_digest=digest,
                        status='branch_publishing')
        except Exception:
            self.update(batch, status='build_uncertain', error='candidate build failed; retained for inspection')
            raise

    def publish_candidate(self, project, batch):
        self.validate_members(project, batch)
        if batch['status'] == 'branch_publishing':
            existing = self.git(project, 'ls-remote', 'origin', 'refs/heads/'+batch['branch']).split()
            if existing and existing[0] != batch['head']:
                raise RuntimeError('integration branch changed')
            if not existing:
                self.git(project, 'push', 'origin', batch['head']+':refs/heads/'+batch['branch'])
            self.update(batch, status='pr_creating')
            # Persist before crossing the ambiguous POST boundary. A restart in
            # pr_creating discovers/adopts a server-side PR, never blindly POSTs.
            body = ('Coordinator integration batch; no new implementation issue.\n\n'
                    + '\n'.join(f"- PR #{m['pr']} at `{m['head']}`" for m in json.loads(batch['members']))
                    + '\n\nManifest: `'+batch['manifest_digest']+'`\n'
                    'Combined CI/testnet and independent review are required before main publication.')
            response = json.loads(self.c.gh(project, ['api', f"repos/{project['repository']}/pulls", '--method', 'POST',
                        '-f', 'head='+batch['branch'], '-f', 'base=main', '-f', 'title=Integrate batch '+batch['id'],
                        '-f', 'body='+body]))
            self.update(batch, pr=response['number'], status='awaiting_ci', error=None)
        elif batch['status'] == 'pr_creating':
            matches = json.loads(self.c.gh(project, ['api', f"repos/{project['repository']}/pulls?state=open&head="
                                     + project['repository'].split('/')[0]+':'+batch['branch']]))
            matches = [p for p in matches if p['head']['sha'] == batch['head'] and p['base']['ref'] == 'main']
            if len(matches) == 1:
                self.update(batch, pr=matches[0]['number'], status='awaiting_ci', error=None)
            else:
                self.update(batch, error='ambiguous integration PR creation; manual inspection required')

    def approval_rows(self, batch):
        cursor = self.db.execute('SELECT * FROM batch_member_approvals WHERE project=? AND batch_id=? ORDER BY pr',
                                 (batch['project'],batch['id']))
        names = [item[0] for item in cursor.description]
        return [dict(zip(names,row)) for row in cursor.fetchall()]

    def review_project(self, project):
        return {**project, 'github_account': project['review_github_account']}

    def matching_approval(self, project, row):
        pages = json.loads(self.c.gh(self.review_project(project),
            ['api', f"repos/{project['repository']}/pulls/{row['pr']}/reviews?per_page=100", '--paginate', '--slurp']))
        records = [item for page in pages for item in page]
        matches = [r for r in records if r.get('state') == 'APPROVED' and r.get('commit_id') == row['head']
                   and (r.get('user') or {}).get('login','').casefold() == row['account'].casefold()
                   and r.get('body') == row['body']]
        if len(matches) != 1:
            raise RuntimeError('member approval publication is ambiguous; manual inspection required')
        return matches[0]

    def record_approval(self, row, response):
        from coordinator import publication_hash
        if hashlib.sha256(row['body'].encode()).hexdigest() != row['body_hash']:
            raise RuntimeError('member approval intent digest mismatch')
        if (response.get('state') != 'APPROVED' or response.get('commit_id') != row['head']
                or response.get('body') != row['body'] or not response.get('node_id')
                or type(response.get('id')) is not int
                or (response.get('user') or {}).get('login','').casefold() != row['account'].casefold()):
            raise RuntimeError('member approval response does not match publication intent')
        receipt = dict(body=row['body'],state='APPROVED',commit=dict(oid=row['head']))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT OR REPLACE INTO publications VALUES(?,?,?,?)',
                            (row['project'],row['pr'],response['node_id'],publication_hash(receipt)))
            self.db.execute("UPDATE batch_member_approvals SET status='applied',review_id=?,review_node=?,updated=?,error=NULL "
                            'WHERE project=? AND batch_id=? AND pr=?',
                            (response['id'],response['node_id'],self.clock(),row['project'],row['batch_id'],row['pr']))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK'); raise

    def adopt_approvals(self, project, batch):
        # Recover an uncertain external POST by positively identifying its exact
        # server record. Never repeat a POST whose outcome is unknown.
        for row in self.approval_rows(batch):
            if row['status'] != 'applied':
                self.record_approval(row, self.matching_approval(project,row))

    def approve_members(self, project, batch, review):
        account = json.loads(self.c.gh(self.review_project(project), ['api','user']))['login']
        if account.casefold() != project['review_github_account'].casefold():
            raise RuntimeError('configured reviewer account does not match authenticated identity')
        existing = {row['pr']:row for row in self.approval_rows(batch)}
        for member in json.loads(batch['members']):
            if member['pr'] in existing:
                self.record_approval(existing[member['pr']], self.matching_approval(project,existing[member['pr']]))
                continue
            source = json.loads(self.c.gh(project, ['api',f"repos/{project['repository']}/pulls/{member['pr']}"]))
            if ((source.get('user') or {}).get('login','').casefold() == account.casefold()
                    or source.get('state') != 'open' or source['head']['sha'] != member['head']
                    or (source['head'].get('repo') or {}).get('full_name') != project['repository']):
                raise RuntimeError('independent member approval target or identity changed')
            receipt = member['source_receipt']
            body = ('<!-- pr-coordinator -->\nIntegration-batch member acceptance publication.\n\n'
                    f"Source: PR #{member['pr']} at `{member['head']}`, independently source-reviewed by "
                    f"{receipt['family']} in job {receipt['job']} (evidence `{receipt['evidence_hash']}`).\n"
                    f"Combined candidate: PR #{batch['pr']} `{batch['head']}` against `{batch['base']}`; "
                    'ordinary/security CI and any required protected testnet checks passed for that combined candidate.\n'
                    f"Independent combined acceptance review: {review['family']} job {review['id']}.\n"
                    f"Batch manifest: `{batch['manifest_digest']}`.\n\n"
                    'This record publishes those existing independent review and combined-test receipts. '
                    'It does not claim an additional model review run or copy candidate check results onto the original source head.')
            row = dict(project=project['id'],batch_id=batch['id'],pr=member['pr'],head=member['head'],
                       account=account,body_hash=hashlib.sha256(body.encode()).hexdigest(),body=body,status='intent')
            self.db.execute('INSERT INTO batch_member_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['project'],row['batch_id'],row['pr'],row['head'],account,row['body_hash'],body,'intent',
                 None,None,self.clock(),self.clock(),None))
            try:
                response = json.loads(self.c.gh(self.review_project(project),
                    ['api', f"repos/{project['repository']}/pulls/{member['pr']}/reviews", '--method','POST',
                     '-f','commit_id='+member['head'],'-f','event=APPROVE','-f','body='+body]))
                self.record_approval(row,response)
            except Exception as error:
                self.db.execute("UPDATE batch_member_approvals SET status='uncertain',updated=?,error=? "
                    'WHERE project=? AND batch_id=? AND pr=?',
                    (self.clock(),type(error).__name__,project['id'],batch['id'],member['pr']))
                raise

    def publish_main(self, project, batch, candidate, owner, review):
        from coordinator import gate
        self.validate_members(project, batch)
        fresh = self.candidate(project, batch)
        if gate(fresh, owner, review) is not None:
            raise RuntimeError('candidate gate changed before publication')
        if batch.get('review_id') is not None and batch['review_id'] != review['id']:
            raise BatchInvalidated('integration review changed after member approval intent')
        self.update(batch, status='approving_members', review_id=review['id'])
        self.approve_members(project, batch, review)
        # Our publications are excluded only through their exact durable hashes.
        # Human requests, changed source/ownership and candidate changes still veto.
        self.validate_members(project, batch)
        for member in json.loads(batch['members']):
            # This uncached REST read includes the just-published approvals and
            # any human request arriving during publication, even if the shared
            # GraphQL PR snapshot has not refreshed yet.
            pages = json.loads(self.c.gh(project, ['api',
                f"repos/{project['repository']}/pulls/{member['pr']}/reviews?per_page=100", '--paginate','--slurp']))
            reviews = [dict(state=r.get('state'),author=dict(login=(r.get('user') or {}).get('login','')))
                       for page in pages for r in page]
            if self.outstanding_requests(dict(reviews=reviews)):
                raise BatchInvalidated('member change request remains outstanding')
        fresh = self.candidate(project, batch)
        if gate(fresh, owner, review) is not None:
            raise BatchInvalidated('combined acceptance changed after member approval publication')
        # All proof and the exact publication intent are durable before push.
        self.update(batch, status='publish_pending', publish_intent=1, review_id=review['id'])
        for member in json.loads(batch['members']):
            self.db.execute('INSERT OR REPLACE INTO batch_activation_commits VALUES(?,?,?)',
                            (project['id'], member['commit'], batch['id']))
        self.git(project, 'push', 'origin', batch['head']+':refs/heads/main')
        self.update(batch, status='published')

    def recognize_stacked_member(self, project, batch, member, pr, owner):
        """Retarget an already-published stacked head; never merge or close it."""
        members = json.loads(batch['members'])
        preceding = members[:members.index(member)]
        bases = {m['owner']['branch'] for m in preceding}
        if (pr['state'] != 'OPEN' or pr['headRefOid'] != member['head']
                or pr.get('baseRefName') not in bases or pr.get('isCrossRepository')
                or pr.get('isDraft') or batch.get('publish_intent') != 1
                or not batch.get('review_id')):
            return False
        approval = next((a for a in self.approval_rows(batch)
                         if a['pr'] == member['pr']), None)
        if (not approval or approval['status'] != 'applied'
                or approval['head'] != member['head'] or not approval['review_node']):
            raise RuntimeError('stacked member lacks published acceptance approval')
        # The manifest and candidate merge were verified by reconcile. Assert
        # the member is reachable from the actual integration branch as well.
        self.git(project, 'merge-base', '--is-ancestor', member['head'], 'origin/main')
        endpoint = f"repos/{project['repository']}/pulls/{member['pr']}"
        fresh = json.loads(self.c.gh(project, ['api', endpoint]))
        if (fresh.get('number') != member['pr'] or fresh.get('state') != 'open'
                or fresh.get('draft') is not False
                or fresh.get('head', {}).get('sha') != member['head']
                or fresh.get('base', {}).get('ref') not in bases
                or any(fresh.get(side, {}).get('repo', {}).get('full_name') != project['repository']
                       for side in ('head', 'base'))):
            raise RuntimeError('stacked member identity changed before recognition')
        # Recheck retained ownership immediately before the sole external write.
        current = self.owner(project, member)
        if (current.get('status') not in ('review', 'done')
                or hashlib.sha256((current.get('token') or '').encode()).hexdigest()
                   != member['owner']['claim_hash']):
            raise RuntimeError('stacked member claim changed before recognition')
        self.c.gh(project, ['api', endpoint, '--method', 'PATCH', '-f', 'base=main'])
        return True

    def reconcile_integrated_stacked_member(self, project, batch, candidate, member, pr):
        """Complete through the real merged candidate, never invent a source merge.

        A first member can retain a base from an earlier integrated batch. GitHub
        may refuse a no-diff retarget, so recognize its exact contribution using
        the sealed candidate and close the redundant source PR only afterwards.
        """
        branches = {m['owner']['branch'] for m in json.loads(batch['members'])}
        if (pr['headRefOid'] != member['head'] or pr.get('baseRefName') == 'main'
                or pr.get('baseRefName') in branches or pr['state'] not in ('OPEN', 'CLOSED')):
            return False
        if (pr.get('isDraft') or pr.get('isCrossRepository')
                or pr.get('headRefName') != member['owner']['branch']
                or batch.get('publish_intent') != 1 or not batch.get('review_id')):
            raise RuntimeError('integrated stacked member publication identity changed')
        approval = next((a for a in self.approval_rows(batch) if a['pr'] == member['pr']), None)
        if (not approval or approval['status'] != 'applied'
                or approval['head'] != member['head'] or not approval['review_node']):
            raise RuntimeError('integrated stacked member lacks published acceptance approval')
        endpoint = f"repos/{project['repository']}/pulls/{member['pr']}"
        fresh = json.loads(self.c.gh(project, ['api', endpoint]))
        if (fresh.get('number') != member['pr'] or fresh.get('state') != pr['state'].lower()
                or fresh.get('draft') is not False
                or fresh.get('head', {}).get('sha') != member['head']
                or fresh.get('head', {}).get('ref') != member['owner']['branch']
                or fresh.get('base', {}).get('ref') != pr.get('baseRefName')
                or fresh.get('base', {}).get('sha') != pr.get('baseRefOid')
                or not SHA.fullmatch(pr.get('baseRefOid', ''))
                or any(fresh.get(side, {}).get('repo', {}).get('full_name') != project['repository']
                       for side in ('head', 'base'))):
            raise RuntimeError('integrated stacked member remote identity changed')
        self.git(project, 'fetch', '--no-tags', 'origin', pr['baseRefName'])
        self.git(project, 'merge-base', '--is-ancestor', pr['baseRefOid'], 'origin/main')
        self.git(project, 'merge-base', '--is-ancestor', member['head'], batch['head'])
        current = self.owner(project, member)
        # Completion clears the token. A retry after completion must still match
        # every sealed owner identity field, but need not retain that token.
        if (current.get('status') not in ('review', 'done') or current.get('publish_pending')
                or any(current.get(k) != v for k, v in member['owner'].items() if k != 'claim_hash')
                or not ((current.get('status') == 'done' and current.get('token') is None)
                        or hashlib.sha256((current.get('token') or '').encode()).hexdigest()
                           == member['owner']['claim_hash'])):
            raise RuntimeError('integrated stacked member retained claim changed')
        if pull_identity(json.loads(self.c.gh(project, ['api', endpoint]))) != pull_identity(fresh):
            raise RuntimeError('integrated stacked member changed before completion')
        # The candidate really is MERGED and contains the exact reviewed source.
        # Existing completion verifies its main ancestry and closes this owner's
        # issue. Do not pass a synthetic MERGED source PR to that code.
        self.c.reconcile(project, candidate, current, None)
        if pull_identity(json.loads(self.c.gh(project, ['api', endpoint]))) != pull_identity(fresh):
            raise RuntimeError('integrated stacked source changed before closure')
        if pr['state'] == 'OPEN':
            self.c.gh(project, ['api', endpoint, '--method', 'PATCH', '-f', 'state=closed'])
        closed = json.loads(self.c.gh(project, ['api', endpoint]))
        if pull_identity(closed) != pull_identity({**fresh, 'state': 'closed'}):
            raise RuntimeError('integrated stacked source closure unconfirmed')
        self.db.execute('INSERT OR REPLACE INTO integrations(project,pr,head,commit_id,reconciled) VALUES(?,?,?,?,1)',
                        (project['id'], member['pr'], member['head'], batch['head']))
        return True

    def recognize_historical_member(self, project, batch, member, pr):
        """Record an integrated contribution, leaving its advanced PR untouched.

        Called only after manifest, candidate merge and main ancestry checks.
        This is not ticket completion or approval of the owner's newer work.
        """
        if pr['headRefOid'] == member['head']:
            return False
        if (pr['state'] != 'OPEN' or pr.get('isDraft') or pr.get('isCrossRepository')
                or pr.get('headRefName') != member['owner']['branch']
                or batch.get('publish_intent') != 1 or not batch.get('review_id')):
            raise RuntimeError('advanced member identity or publication changed')
        approval = next((a for a in self.approval_rows(batch) if a['pr'] == member['pr']), None)
        if (not approval or approval['status'] != 'applied'
                or approval['head'] != member['head'] or not approval['review_node']):
            raise RuntimeError('advanced member lacks published acceptance approval')
        endpoint = f"repos/{project['repository']}/pulls/{member['pr']}"
        fresh = json.loads(self.c.gh(project, ['api', endpoint]))
        if (fresh.get('number') != member['pr'] or fresh.get('state') != 'open'
                or fresh.get('draft') is not False
                or fresh.get('head', {}).get('sha') != pr['headRefOid']
                or fresh.get('head', {}).get('ref') != member['owner']['branch']
                or fresh.get('base', {}).get('ref') != pr.get('baseRefName')
                or any(fresh.get(side, {}).get('repo', {}).get('full_name') != project['repository']
                       for side in ('head', 'base'))):
            raise RuntimeError('advanced member remote identity changed')
        # Fetch only the owning branch; require the published contribution to be
        # retained by both real main and the newer owner head, never a rewrite.
        self.git(project, 'fetch', '--no-tags', 'origin', member['owner']['branch'])
        self.git(project, 'merge-base', '--is-ancestor', member['head'], 'origin/main')
        self.git(project, 'merge-base', '--is-ancestor', member['head'], pr['headRefOid'])
        current = self.owner(project, member)
        if (current.get('status') not in ('review', 'in_progress') or current.get('publish_pending')
                or any(current.get(k) != v for k, v in member['owner'].items() if k != 'claim_hash')
                or hashlib.sha256((current.get('token') or '').encode()).hexdigest()
                   != member['owner']['claim_hash']):
            raise RuntimeError('advanced member retained claim changed')
        # Detect a push or PR-state change during the Git/ownership reads. No
        # external write follows; a later push still needs its own fresh review.
        if json.loads(self.c.gh(project, ['api', endpoint])) != fresh:
            raise RuntimeError('advanced member changed during historical recognition')
        receipt = dict(manifest=batch['manifest_digest'], candidate=batch['head'],
                       published_head=member['head'], observed_head=pr['headRefOid'],
                       claim_hash=member['owner']['claim_hash'], approval=approval['review_node'])
        self.db.execute('INSERT OR IGNORE INTO batch_historical_members VALUES(?,?,?,?,?)',
                        (project['id'], batch['id'], member['pr'], canonical(receipt), self.clock()))
        return True

    def reconcile(self, project, batch):
        self.verify_manifest(project, batch)
        self.git(project, 'fetch', '--no-tags', 'origin', 'main')
        self.git(project, 'merge-base', '--is-ancestor', batch['head'], 'origin/main')
        self.update(batch, status='reconciling')
        candidate = self.c.pr(project, batch['pr'])
        if (candidate['state'] != 'MERGED' or candidate['headRefOid'] != batch['head']
                or (candidate.get('mergeCommit') or {}).get('oid') != batch['head']):
            self.update(batch, error='awaiting verified integration PR merge recognition')
            return
        for member in json.loads(batch['members']):
            pr = self.c.pr(project, member['pr'])
            if self.recognize_historical_member(project, batch, member, pr):
                continue
            if self.reconcile_integrated_stacked_member(project, batch, candidate, member, pr):
                continue
            owner = self.owner(project, member)
            if self.recognize_stacked_member(project, batch, member, pr, owner):
                pr = self.c.pr(project, member['pr'])
            if pr['state'] != 'MERGED' or pr['headRefOid'] != member['head'] or not pr.get('mergeCommit'):
                self.update(batch, error='awaiting verified member merge recognition')
                return
            owner = self.owner(project, member)
            if (owner.get('status') != 'done'
                    and hashlib.sha256((owner.get('token') or '').encode()).hexdigest() != member['owner']['claim_hash']):
                raise RuntimeError('member claim changed after publication; reconciliation retained')
            self.db.execute('INSERT OR IGNORE INTO integrations(project,pr,head,commit_id,reconciled) VALUES(?,?,?,?,0)',
                (project['id'],pr['number'],pr['headRefOid'],pr['mergeCommit']['oid']))
            self.c.reconcile(project, pr, owner, None)
        self.db.execute('INSERT OR IGNORE INTO integrations(project,pr,head,commit_id,reconciled) VALUES(?,?,?,?,1)',
            (project['id'],candidate['number'],candidate['headRefOid'],candidate['mergeCommit']['oid']))
        self.update(batch, status='done', error=None)

    @staticmethod
    def review_failure(batch):
        try:
            error = batch_error(batch)
            return error if error.get('kind') == 'candidate_review_changes' else None
        except (ValueError, TypeError, AttributeError):
            return None

    def close_review_rejected_candidate(self, project, batch):
        error = self.review_failure(batch)
        if not error or error.get('candidate_retired'):
            return
        pr = self.c.pr(project, batch['pr'])
        if (pr.get('state') == 'MERGED' or pr.get('headRefName') != batch['branch']
                or pr.get('headRefOid') != batch['head']):
            raise RuntimeError('rejected candidate identity/publication changed; retain evidence')
        if pr['state'] == 'OPEN':
            self.c.gh(project, ['api', f"repos/{project['repository']}/pulls/{batch['pr']}",
                                '--method', 'PATCH', '-f', 'state=closed'])
            observed = self.c.pr(project, batch['pr'])
            if (observed.get('state') != 'CLOSED' or observed.get('headRefName') != batch['branch']
                    or observed.get('headRefOid') != batch['head']):
                raise RuntimeError('rejected candidate closure not confirmed; retain evidence')
        elif pr['state'] != 'CLOSED':
            raise RuntimeError('rejected candidate state unavailable')
        error['candidate_retired'] = True
        self.update(batch, error=canonical(error))

    def reject_review(self, project, batch, candidate, review):
        from coordinator import feedback
        if (not review or review.get('head') != batch['head']
                or candidate['headRefOid'] != batch['head'] or candidate['baseRefOid'] != batch['base']
                or review.get('feedback') != feedback(candidate)
                or review.get('publication_done') != 1 or review.get('publication_pending')
                or review.get('status') != 'done' or review['result'].get('verdict') != 'changes_requested'
                or review.get('family') not in ('Codex', 'Claude')
                or review.get('family') == self.synthetic_owner(batch)['agent']
                or batch.get('publish_intent')):
            return False
        # Persist the rejected group and actual review before remote closure.
        # An ambiguous PATCH cannot lose the finding or authorize publication.
        error = dict(kind='candidate_review_changes', review_id=review['id'],
                     review_head=review['head'], review_feedback=review['feedback'],
                     result_hash=hashlib.sha256(canonical(review['result']).encode()).hexdigest(),
                     findings=review['result']['evidence'], candidate_retired=False)
        self.update(batch, status='failed', error=canonical(error))
        self.close_review_rejected_candidate(project, batch)
        return True

    def cleanup_terminal(self, project, batch):
        try:
            if self.review_failure(batch):
                self.close_review_rejected_candidate(project, batch)
            self.cleanup(project, batch)
        except Exception as error:
            # A retained cleanup/closure receipt must not stop unrelated work.
            print(f"{project['id']} batch {batch['id']}: terminal cleanup deferred ({type(error).__name__})", flush=True)

    def shared_harness_incident(self, project, batch, error):
        """Narrow operator-reported incident, authenticated before victim suppression.

        This routes evidence to existing SF-243/SF-255 ownership, never launches workers,
        retries a mutation, changes source receipts, or grants candidate acceptance.
        New signatures require a scoped tracker decision and reviewed code.
        """
        pair_incident = (batch['head'] == '58b0b0828925404219e65853b56431cb19cd1ce1'
                         and batch['base'] == 'd5702bc6be238f1887df00611ebf382b2bece755')
        owner = 'SF-255' if pair_incident else 'SF-243'
        run_id = 42031568923 if pair_incident else 41914017432
        if (project.get('repository') != 'tyler-suifleet/suifleet'
                or (not pair_incident and (batch['head'] != 'ebdca15ae1c244376701b323486a94e2ea002799'
                or batch['base'] != '4aa4344c0a0b0749348ed78544531ed28ac857d3'))
                or error.get('kind') != 'candidate_checks_failed'):
            return None
        checks = error.get('checks', [])
        url = f'https://github.com/tyler-suifleet/suifleet/actions/runs/{run_id}'
        if (len(checks) != 1 or checks[0].get('name') != 'testnet/package-upgrade'
                or checks[0].get('workflowName') != 'Automatic testnet'
                or checks[0].get('conclusion') != 'FAILURE' or checks[0].get('detailsUrl') != url):
            return None
        self.verify_manifest(project, batch)
        error_hash = hashlib.sha256(canonical(error).encode()).hexdigest()
        existing = self.db.execute('SELECT manifest,error_hash,receipt FROM shared_harness_incidents '
            'WHERE project=? AND batch_id=?', (project['id'], batch['id'])).fetchone()
        if existing:
            if tuple(existing[:2]) != (batch['manifest_digest'], error_hash):
                raise RuntimeError('shared harness incident identity changed')
            return json.loads(existing[2])
        path = 'repos/' + project['repository'] + f'/actions/runs/{run_id}'
        read = lambda: json.loads(self.c.gh(project, ['api', path]))
        try:
            run = read()
        except (RuntimeError, subprocess.CalledProcessError):
            return None  # No authenticated receipt: do not suppress owner repair.
        if (run.get('id') != run_id or run.get('status') != 'completed'
                or run.get('conclusion') != 'failure' or run.get('event') != 'workflow_run'
                or run.get('head_branch') != 'main' or run.get('path') != '.github/workflows/testnet.yml'
                or run.get('name') != 'Automatic testnet' or type(run.get('run_attempt')) is not int
                or run['run_attempt'] < 1 or run.get('repository', {}).get('full_name') != project['repository']
                or run.get('head_repository', {}).get('full_name') != project['repository']):
            raise RuntimeError('shared harness run provenance unavailable')
        with tempfile.TemporaryDirectory(prefix='shared-harness-effects-') as directory:
            try:
                self.c.gh(project, ['run', 'download', str(run_id), '-R', project['repository'],
                    '-n', 'testnet-effects-' + batch['head'], '-D', directory])
            except (RuntimeError, subprocess.CalledProcessError):
                return None
            evidence = {}
            hashes = {}
            for name in ('package-upgrade.json', 'upgrade-security.json'):
                file = Path(directory) / name
                if file.is_symlink() or not file.is_file() or file.stat().st_size > 2_000_000:
                    raise RuntimeError('shared harness effects unavailable')
                raw = file.read_bytes()
                evidence[name] = json.loads(raw)
                hashes[name] = hashlib.sha256(raw).hexdigest()
        package, security = evidence['package-upgrade.json'], evidence['upgrade-security.json']
        failures = [tx for tx in security.get('transactions', []) if tx.get('status') == 'failure'
                    and tx.get('label') == 'initialize cached passkey authority']
        diagnostic_hash = None
        if pair_incident:
            try:
                log = self.c.gh(project, ['run', 'view', str(run_id), '-R', project['repository'], '--log-failed'])
            except (RuntimeError, subprocess.CalledProcessError):
                return None
            if 'RuntimeError: unfinished native passkey fixture belongs to another package pair' not in log:
                raise RuntimeError('shared harness package-pair diagnostic unavailable')
            diagnostic_hash = hashlib.sha256(log.encode()).hexdigest()
        if (package.get('base') != batch['base'] or package.get('head') != batch['head']
                or package.get('chain_identifier', package.get('chain')) != '4c78adac'
                or security.get('chain_identifier', security.get('chain')) != '4c78adac'
                or security.get('head') != batch['head']
                or (not pair_incident and (len(failures) != 1
                    or failures[0].get('digest') != 'AEbuyHfJEsQjPt9GJq9gM5bMFraHa2VSyrVoUvQGtQkw'
                    or type(failures[0].get('gas_mist')) is not int or failures[0]['gas_mist'] <= 0))
                or (pair_incident and (hashes != PAIR_INCIDENT_HASHES
                    or security.get('native_passkey', {}).get('success')))):
            raise RuntimeError('shared harness failure receipt does not match pinned incident')
        try:
            final_run = read()
        except (RuntimeError, subprocess.CalledProcessError):
            return None
        if final_run != run:
            raise RuntimeError('shared harness run changed during authentication')
        receipt = dict(owner_ticket=owner, candidate_pr=batch['pr'], candidate_head=batch['head'],
            candidate_base=batch['base'], run=run['id'], attempt=run['run_attempt'], hashes=hashes,
            failure_digest=None if pair_incident else failures[0]['digest'], checks=checks,
            diagnostic_hash=diagnostic_hash,
            diagnostic=f'await scoped {owner} recovery; unchanged member repairs suppressed; all acceptance gates retained')
        self.db.execute('INSERT INTO shared_harness_incidents VALUES(?,?,?,?,?,?,?)',
            (project['id'], batch['id'], owner, batch['manifest_digest'], error_hash, canonical(receipt), self.clock()))
        self.db.commit()  # Durable attribution precedes scheduling decisions.
        return receipt

    def repair_context(self, project, pr):
        result = {}
        for batch in reversed(self.rows(project)):
            members = json.loads(batch['members'])
            if batch['status'] == 'failed' and any(m['pr'] == pr['number'] and m['head'] == pr['headRefOid'] for m in members):
                try:
                    shared_error = batch_error(batch)
                except (TypeError, ValueError):
                    shared_error = {}
                # This incident fences only the failed candidate's base. A new
                # main permits recollection with unchanged source heads and fresh
                # candidate acceptance; the durable receipt remains historical.
                incident = (self.shared_harness_incident(project, batch, shared_error)
                            if batch['base'] == pr.get('baseRefOid') else None)
                if incident:
                    result['_shared_acceptance_wait'] = incident
                    break
            if (batch['status'] == 'failed' and len(members) == 1
                    and members[0]['pr'] == pr['number'] and members[0]['head'] == pr['headRefOid']):
                try:
                    error = batch_error(batch)
                except (TypeError, ValueError):
                    continue
                if (error.get('kind') == 'candidate_checks_failed'
                        and batch['base'] == pr.get('baseRefOid')):
                    checks = error['checks']
                    # These rows were classified from the exact candidate/base
                    # by failed_checks before persistence. Provider failures cannot
                    # be repaired on an unchanged member's source branch. Retain
                    # the failed candidate/selection fence and all acceptance
                    # gates, but do not dispatch another owner repair for it.
                    if checks and all(check.get('name') == 'testnet/package-upgrade'
                                      and check.get('workflowName') == 'Automatic testnet'
                                      and check.get('classification') in ('funding', 'infrastructure')
                                      for check in checks):
                        break
                    result['_batch_failure'] = dict(candidate_pr=batch['pr'], candidate_head=batch['head'],
                                                   candidate_base=batch['base'], checks=checks)
                    break
                if (error.get('kind') == 'candidate_review_changes'
                        and error.get('review_head') == batch['head']
                        and type(error.get('review_id')) is int and error['review_id'] > 0
                        and isinstance(error.get('findings'), str) and error['findings']
                        and re.fullmatch(r'[0-9a-f]{64}', error.get('result_hash', ''))):
                    result['_batch_review_failure'] = dict(candidate_pr=batch['pr'], candidate_head=batch['head'],
                        candidate_base=batch['base'], source_head=members[0]['head'],
                        review_id=error['review_id'], result_hash=error['result_hash'], findings=error['findings'])
                    break
        row = self.db.execute('SELECT context FROM integration_batch_conflicts WHERE project=? AND pr=? AND head=? AND base=?',
            (project['id'],pr['number'],pr['headRefOid'],pr['baseRefOid'])).fetchone()
        if row:
            context = json.loads(row[0])
            # A composition failure may only involve unmerged siblings. Owners
            # must not import those stacks to repair an otherwise clean source.
            # Recheck against pinned main, including receipts from older runtimes;
            # after integration a new base will be tested by the next build.
            main_conflict = True
            if context.get('other_members'):
                # Use a disposable worktree for Git versions predating
                # merge-tree --write-tree. Never alter the owner's checkout.
                with tempfile.TemporaryDirectory(prefix='batch-conflict-') as directory:
                    probe = str(Path(directory)/'probe')
                    self.git(project, 'worktree', 'add', '--detach', probe, pr['baseRefOid'])
                    try:
                        try:
                            self.git(project, 'merge', '--no-commit', '--no-ff',
                                     pr['headRefOid'], cwd=probe)
                        except subprocess.CalledProcessError as error:
                            if error.returncode != 1:
                                raise
                            # Exit one alone is not proof of a merge conflict.
                            if not self.git(project, 'ls-files', '--unmerged', cwd=probe):
                                raise
                        else:
                            main_conflict = False
                    finally:
                        self.git(project, 'worktree', 'remove', '--force', probe)
            if main_conflict:
                result['_batch_conflict'] = context
        return result

    def select(self, project, candidates, base, maximum):
        failed = []
        for row in self.rows(project):
            if row['status'] != 'failed':
                continue
            try:
                kind = batch_error(row).get('kind')
                if (kind not in ('candidate_checks_failed', 'candidate_review_changes')
                        or kind == 'candidate_checks_failed' and row['base'] != base):
                    continue
            except (TypeError, ValueError):
                continue
            recovery = self.db.execute('SELECT manifest,error_hash FROM batch_funding_recoveries WHERE project=? AND batch_id=?',
                                       (project['id'],row['id'])).fetchone()
            recovery = recovery or self.db.execute('SELECT manifest,error_hash FROM batch_infrastructure_retries WHERE project=? AND batch_id=?',
                                       (project['id'],row['id'])).fetchone()
            original = json.loads(row['members'])
            exact_sources = all(any(all(candidate.get(k) == member.get(k) for k in
                ('pr','head','owner','source_receipt','source_feedback')) for candidate in candidates) for member in original)
            if (recovery and exact_sources and tuple(recovery) ==
                    (row['manifest_digest'],hashlib.sha256(row['error'].encode()).hexdigest())):
                continue  # Explicit durable authorization; the failed row is never reset.
            failed.append(tuple((m['pr'],m['head']) for m in json.loads(row['members'])))
        pool = self.exclude_included_sources(project,
            [m for m in candidates if ((m['pr'],m['head']),) not in failed], base)
        # One acceptance reviewer must be independent of every member author.
        # Split mixed-family pools instead of attributing a fictitious author.
        if pool:
            family = pool[0]['owner']['agent']
            pool = [m for m in pool if m['owner']['agent'] == family]
        selected = pool[:maximum]
        # A failed combination is never submitted again. Split deterministically;
        # singleton failures become explicit owner repair contexts instead.
        while selected and tuple((m['pr'],m['head']) for m in selected) in failed:
            selected = selected[:len(selected)//2]
        return selected

    @staticmethod
    def infrastructure_only(error):
        checks = error.get('checks', [])
        return (error.get('kind') == 'candidate_checks_failed' and bool(checks)
                and all(c.get('name') == 'testnet/package-upgrade'
                        and c.get('workflowName') == 'Automatic testnet'
                        and c.get('classification') == 'infrastructure' for c in checks))

    def infrastructure_receipt(self, project, batch, error):
        """Authenticate a narrow, zero-spend GitHub outage; never trust class alone."""
        from coordinator import preparation_job_logs
        checks = error['checks']
        match = re.fullmatch(r'https://github\.com/' + re.escape(project['repository'])
                             + r'/actions/runs/([0-9]+)', checks[0].get('detailsUrl', ''))
        if not match or len(checks) != 1:
            raise RuntimeError('infrastructure retry needs one authenticated run')
        path = f"repos/{project['repository']}/actions/runs/{match[1]}"
        read = lambda endpoint: json.loads(self.c.gh(project, ['api', endpoint]))
        run = read(path)
        if (run.get('event') != 'workflow_run' or run.get('head_branch') != 'main'
                or run.get('path') != '.github/workflows/testnet.yml'
                or run.get('head_repository', {}).get('full_name') != project['repository']
                or run.get('repository', {}).get('full_name') != project['repository']
                or run.get('status') != 'completed' or run.get('conclusion') != 'failure'
                or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1):
            raise RuntimeError('infrastructure run identity unavailable')
        jobs = read(path + f"/attempts/{run['run_attempt']}/jobs?per_page=100")
        entries = jobs.get('jobs', [])
        validation = [j for j in entries if j.get('name') == 'validate']
        if jobs.get('total_count') != len(entries) or len(validation) != 1:
            raise RuntimeError('incomplete infrastructure job evidence')
        job = validation[0]
        steps = job.get('steps', [])
        def step(name, conclusion):
            found = [s for s in steps if s.get('name') == name]
            return len(found) == 1 and found[0].get('conclusion') == conclusion
        if (job.get('conclusion') != 'failure'
                or not step('Recheck head and base immediately before spending', 'failure')
                or not step('Run bounded testnet acceptance', 'skipped')):
            raise RuntimeError('infrastructure failure is not proven before spending')
        logs = preparation_job_logs(lambda args: self.c.gh(project, args),
                                    f"repos/{project['repository']}/actions/jobs/{job['id']}/logs")
        # The exact candidate's failed status was independently authenticated by
        # failed_checks. Step identities prove no signing step ran; logs must
        # show the transient provider error, not a product/permission denial.
        if 'No server is currently available to service your request' not in logs:
            raise RuntimeError('not a recognized transient GitHub outage')
        if read(path) != run:
            raise RuntimeError('infrastructure run changed during verification')
        return dict(run=int(match[1]), attempt=run['run_attempt'], job=job['id'],
                    logs_sha256=hashlib.sha256(logs.encode()).hexdigest())

    def recover_infrastructure(self, project, candidates, base):
        """One retry per exact source set/base, after five minutes, under this daemon."""
        rows = self.rows(project)
        for batch in reversed(rows):
            if batch['status'] != 'failed' or batch['base'] != base:
                continue
            error = batch_error(batch)
            if not self.infrastructure_only(error):
                continue
            members = json.loads(batch['members'])
            pins = lambda ms: [(m['pr'], m['head']) for m in ms]
            related = [r for r in rows if r['base'] == base and pins(json.loads(r['members'])) == pins(members)]
            if related[-1]['id'] != batch['id'] or any(r['status'] not in TERMINAL or r.get('publish_intent') for r in related):
                continue
            if any(self.db.execute('SELECT 1 FROM batch_infrastructure_retries WHERE project=? AND batch_id=?',
                                   (project['id'], r['id'])).fetchone() for r in related):
                continue  # Exhausted: preserve failed evidence and selection fence.
            if self.clock() < batch['updated'] + 300 or not members:
                continue
            if not all(any(all(c.get(k) == m.get(k) for k in
                    ('pr','head','owner','source_receipt','source_feedback')) for c in candidates) for m in members):
                continue
            numbers = [batch['pr'], *[m['pr'] for m in members]]
            if any(self.db.execute("SELECT 1 FROM jobs WHERE project=? AND pr=? AND status IN "
                    "('reserved','running','orphaned','quarantined','quota_wait','quota_launch')",
                    (project['id'], n)).fetchone() for n in numbers):
                continue
            self.validate_members(project, batch)
            candidate = self.candidate(project, batch)
            from coordinator import failed_checks
            if (candidate.get('state') not in ('OPEN', 'CLOSED') or candidate.get('isDraft')
                    or candidate.get('headRefOid') != batch['head']
                    or candidate.get('baseRefOid') != base
                    or failed_checks(candidate) != error['checks']):
                continue
            try:
                receipt = self.infrastructure_receipt(project, batch, error)
            except (RuntimeError, subprocess.CalledProcessError, ValueError, KeyError):
                # Unavailable or nonqualifying provider evidence keeps this
                # failure fenced without stopping unrelated candidate progress.
                continue
            self.validate_members(project, batch)
            if self.main(project) != base or self.candidate(project, batch) != candidate:
                raise RuntimeError('infrastructure candidate changed during verification')
            self.db.execute('INSERT INTO batch_infrastructure_retries VALUES(?,?,?,?,?,?)',
                (project['id'], batch['id'], batch['manifest_digest'],
                 hashlib.sha256(batch['error'].encode()).hexdigest(), canonical(receipt), self.clock()))

    def fail_candidate(self, project, batch, failures):
        self.invalidate(project, batch, 'combined candidate checks failed')
        self.update(batch, status='failed', error=canonical(dict(kind='candidate_checks_failed', checks=failures)))

    def tick(self, project, prs, owners, execute=False):
        if not self.enabled(project):
            return set()
        maximum, delay = self.settings(project)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', project['id']):
            raise ValueError('invalid batch project namespace')
        if execute:
            for terminal in self.rows(project):
                if terminal['status'] in TERMINAL:
                    self.cleanup_terminal(project, terminal)
        candidates = []
        def priority(pr):
            owner = self.c.owner(project, pr, owners) or {}
            return (owner.get('spec',{}).get('priority','P9'),pr['number'])
        for pr in sorted(prs, key=priority):
            if pr.get('headRefName', '').startswith('integration/batch/'):
                continue
            source = self.source(project, pr, self.c.owner(project, pr, owners))
            if source:
                candidates.append(source)
                # source() already validates Review ownership without renewing a lease.
        held = {member['pr'] for member in candidates}
        active = [row for row in self.rows(project) if row['status'] not in TERMINAL]
        limit = project.get('integration_batch', {}).get('max_in_flight', 2)
        if type(limit) is not int or not 1 <= limit <= 4:
            raise ValueError('integration batch max_in_flight must be 1..4')
        reserved = [m['pr'] for row in active for m in json.loads(row['members'])]
        if len(reserved) != len(set(reserved)):
            raise RuntimeError('duplicate active integration member; inspect before proceeding')
        held.update(reserved)
        if not execute:
            return held
        # Preserve an ambiguous publication before admitting more work. A single
        # normal owner still serially publishes main; pending CI does not own it.
        uncertain = any(row.get('publish_intent') for row in active)
        available = [m for m in candidates if m['pr'] not in reserved]
        if len(active) < limit and available and not uncertain:
            base, batch_id = self.main(project), uuid.uuid4().hex
            self.recover_infrastructure(project, available, base)
            selected = self.select(project, available, base, maximum)
            if selected:
                directory = str(self.c.state/project['id']/'batches'/batch_id)
                Path(directory).parent.mkdir(parents=True, exist_ok=True)
                self.db.execute('INSERT INTO integration_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (project['id'],batch_id,'collecting',base,None,'integration/batch/'+batch_id,None,
                     canonical(selected),directory,None,self.clock(),self.clock(),None,0,None))
                active = [row for row in self.rows(project) if row['status'] not in TERMINAL]
        for batch in active:
            others = {m['pr'] for row in active if row['id'] != batch['id'] and row['status'] not in TERMINAL
                      for m in json.loads(row['members'])}
            pool = [m for m in candidates if m['pr'] not in others]
            # Recheck after each advance; a prior candidate may have published or
            # discovered an uncertain push during this same pass.
            publish_allowed = not any(row['id'] != batch['id'] and row.get('publish_intent')
                                      and row['status'] not in TERMINAL for row in self.rows(project))
            self.advance(project, batch, pool, maximum, delay, prs, held, publish_allowed)
        held.update(self.reserved_members(project))
        return held

    def advance(self, project, batch, candidates, maximum, delay, prs, held, publish_allowed=True):
        try:
            status = batch['status']
            if status in ('published', 'reconciling'):
                self.reconcile(project, batch)
            elif status == 'publish_pending':
                # Never repeat a main push whose external outcome is unknown.
                tip = self.main(project)
                self.git(project, 'fetch', '--no-tags', 'origin', 'main')
                try:
                    self.git(project, 'merge-base', '--is-ancestor', batch['head'], tip)
                except subprocess.CalledProcessError:
                    self.update(batch, error='ambiguous main publication; manual inspection required')
                else:
                    self.update(batch, status='published'); self.reconcile(project, batch)
            else:
                if status == 'collecting':
                    if self.main(project) != batch['base']:
                        raise BatchInvalidated('integration base changed during collection')
                    selected = self.select(project, candidates, batch['base'], maximum)
                    self.update(batch, members=canonical(selected))
                    if not selected:
                        self.update(batch, status='invalidated', error='no eligible members remain during collection')
                        return held
                    if len(selected) < maximum and self.clock()-batch['created'] < delay:
                        return held
                    self.validate_members(project, batch)
                    self.update(batch, status='building')
                    self.build(project, batch)
                    status = batch['status']
                if status in ('building', 'build_uncertain'):
                    self.recover_build(project, batch)
                    status = batch['status']
                if status in ('branch_publishing', 'pr_creating'):
                    self.publish_candidate(project, batch)
                    status = batch['status']
                if status in ('awaiting_ci','awaiting_review','ready','approving_members'):
                    self.adopt_approvals(project, batch)
                    from coordinator import checks_blocker, gate
                    self.validate_members(project, batch)
                    candidate = self.candidate(project, batch)
                    if candidate['state'] != 'OPEN' or candidate.get('isDraft'):
                        raise BatchInvalidated('integration candidate is not open and ready')
                    owner = self.synthetic_owner(batch)
                    review = self.c.store.review(project['id'], batch['pr'])
                    # Published, exact-candidate source findings are actionable
                    # before CI finishes. Neither a pending check nor a busy
                    # sibling candidate should leave an independent reviewer idle.
                    if self.reject_review(project, batch, candidate, review):
                        return held
                    if checks_blocker(candidate) is not None:
                        from coordinator import failed_checks, feedback
                        failures = failed_checks(candidate)
                        if failures:
                            self.fail_candidate(project, batch, failures)
                            return held
                        self.update(batch, status='awaiting_ci')
                        if (not review or review.get('head') != candidate['headRefOid']
                                or review.get('feedback') != feedback(candidate)):
                            self.c.launch(project, candidate, owner, 'review')
                        return held
                    if gate(candidate, owner, review) is not None:
                        self.update(batch, status='awaiting_review', error=None)
                        self.c.launch(project, candidate, owner, 'review')
                    else:
                        if not publish_allowed:
                            return held
                        self.update(batch, status='ready', error=None)
                        self.publish_main(project, batch, candidate, owner, review)
                        self.reconcile(project, batch)
        except Exception as error:
            # Publication/reconciliation uncertainty retains all reservations.
            if isinstance(error, BatchInvalidated):
                self.invalidate(project, batch, error)
            elif batch['status'] == 'failed' and self.review_failure(batch):
                # Preserve the durable review finding after uncertain closure.
                print(f"{project['id']} batch {batch['id']}: rejected candidate closure deferred ({type(error).__name__})", flush=True)
            else:
                # Transport/launch/local failures say nothing about candidate
                # validity. Preserve completed CI and testnet for the next tick.
                self.update(batch, error=type(error).__name__+': '+str(error)[:300])
        finally:
            if batch['status'] == 'failed' and self.review_failure(batch):
                # The caller schedules owner work from these same snapshots.
                # Route the finding in this pass, not after another full scan.
                for source in prs:
                    source.update(self.repair_context(project, source))
                # held was collected before rejection. Release the failed
                # group's stale candidates so the caller can launch repair now,
                # while preserving any live batch reservations.
                held.difference_update(member['pr'] for member in json.loads(batch['members']))
                held.update(self.reserved_members(project))
            if batch['status'] in TERMINAL:
                self.cleanup_terminal(project, batch)
        return held


# Kept in an installed runtime module so activation never imports candidate code.
LOCAL_COMMANDS = (
    ('python3', '-m', 'unittest', 'discover', '-s', 'tools/pr-coordinator/tests'),
    ('python3', '-m', 'unittest', 'discover', '-s', 'scripts/tests', '-p', 'test_tracking.py'),
    ('python3', 'scripts/bootstrap_tracking.py'),
)
LOCAL_DOCS = {'docs/PR_COORDINATION.md', 'docs/ATOMIC_TICKETING.md',
              'docs/RECOVERY_WATCHDOG.md'}


def infrastructure_scope(paths):
    return bool(paths) and all(
        path in LOCAL_DOCS or path.startswith('tools/pr-coordinator/')
        for path in paths)


TRACKING_FILES = {'docs/tracking/backlog.json', 'docs/tracking/github-state.json'}


def local_scope(git, repository, base, head, owner=None):
    """Permit only the current published infra ticket's append-only seed pair."""
    paths = git('diff', '--name-only', '--no-renames', base+'..'+head).splitlines()
    core = [p for p in paths if p not in TRACKING_FILES]
    if not infrastructure_scope(core):
        return None
    tracking = set(paths) & TRACKING_FILES
    if not tracking:
        return {'paths': paths, 'tracking': None}
    if tracking != TRACKING_FILES or not owner:
        return None
    try:
        before, after = [], []
        for ref, destination in [(base, before), (head, after)]:
            for path in ['docs/tracking/backlog.json','docs/tracking/github-state.json']:
                destination.append(json.loads(git('show', ref+':'+path)))
        for old, new in zip(before, after):
            if ({k:v for k,v in old.items() if k!='issues'} != {k:v for k,v in new.items() if k!='issues'}
                    or old.get('repository') != repository):
                return None
        old, new = before[0]['issues'], after[0]['issues']
        if not isinstance(old,list) or not isinstance(new,list) or new[:len(old)] != old or len(new)!=len(old)+1:
            return None
        spec = new[-1]; key = owner['key']
        if (spec != owner['spec'] or spec.get('key') != key or spec.get('area')!='infra'
                or any(v.get('key')==key for v in old)):
            return None
        old, new = before[1]['issues'], after[1]['issues']
        if (not isinstance(old,dict) or not isinstance(new,dict) or key in old
                or set(new)!=set(old)|{key} or any(new[k]!=v for k,v in old.items())):
            return None
        remote = new[key]; number = remote['number']
        if (type(number) is not int or number<=0 or remote['url']!=f'https://github.com/{repository}/issues/{number}'
                or remote['url']!=owner['remote']['url'] or remote['project_item_id']!=owner['remote']['item_id']
                or type(remote.get('id')) is not int or remote['id']<=0
                or not isinstance(remote.get('node_id'),str) or not remote['node_id']):
            return None
        proof = {'key':key,'spec':spec,'remote':{'url':remote['url'],'item_id':remote['project_item_id']}}
        return {'paths':paths,'tracking':proof}
    except (KeyError,TypeError,ValueError):
        return None


class LocalInfrastructure:
    """User-authorized local validation, distinct from independent approval."""
    def __init__(self, coordinator):
        self.c, self.b = coordinator, coordinator.batches
        self.db = coordinator.store.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS local_integrations (
            project TEXT, pr INTEGER, head TEXT, base TEXT, status TEXT,
            receipt TEXT, PRIMARY KEY(project,pr,head,base))""")

    def eligible(self, project, pr, owner):
        if project.get('local_infrastructure', {}).get('enabled') is not True:
            return False
        if (not owner or owner.get('spec', {}).get('area') != 'infra'
                or owner.get('status') != 'review' or owner.get('expired')
                or owner.get('publish_pending') or owner.get('branch') != pr.get('headRefName')
                or pr.get('state') != 'OPEN' or pr.get('isDraft') or pr.get('isCrossRepository')
                or pr.get('baseRefName') != 'main' or pr.get('mergeable') != 'MERGEABLE'
                or self.b.outstanding_requests(pr)
                or any(not t.get('isResolved') for t in pr.get('threads', []))):
            return False
        base, head = pr['baseRefOid'], pr['headRefOid']
        if not all(SHA.fullmatch(x) for x in (base, head)):
            return False
        self.b.git(project, 'fetch', '--no-tags', 'origin', base, head)
        return local_scope(lambda *args:self.b.git(project,*args), project['repository'], base, head, owner) is not None

    def fenced(self, project, pr, owner):
        self.c.assert_review_owner(project, owner)
        active = self.db.execute("SELECT 1 FROM jobs WHERE project=? AND pr=? AND "
            "status IN ('reserved','running','orphaned','quarantined','quota_wait','quota_launch')",
            (project['id'], pr['number'])).fetchone()
        if active or pr['number'] in self.b.reserved_members(project):
            raise RuntimeError('local integration has active or uncertain workers/batch')
        if owner.get('worktree') and self.b.git(project, 'status', '--porcelain', cwd=owner['worktree']):
            raise RuntimeError('implementation worktree is dirty')
        # The dispatcher directory is keyed by the immutable owning session.
        from coordinator import claim_processes, worker_stopped
        directory = self.c.state/'implementations'/owner['session']
        path = directory/'process.json'
        if not path.exists():
            raise RuntimeError('implementation process identity is unavailable')
        process = json.loads(path.read_text())
        if (not worker_stopped(process.get('pid'), process.get('started'))
                or claim_processes(owner['token'], process.get('started'))):
            raise RuntimeError('implementation worker/helpers have not stopped')

    def tick(self, project, pr, owner):
        """Return True when this PR belongs to the local lane (including holds)."""
        if not self.eligible(project, pr, owner):
            return False
        self.fenced(project, pr, owner)
        base, head = pr['baseRefOid'], pr['headRefOid']
        key = (project['id'], pr['number'], head, base)
        existing = self.db.execute('SELECT status,receipt FROM local_integrations WHERE project=? AND pr=? AND head=? AND base=?', key).fetchone()
        if existing:
            print(canonical(dict(project=project['id'], pr=pr['number'], local_infrastructure=existing[0],
                                 diagnostic=json.loads(existing[1]).get('diagnostic'))), flush=True)
            if existing[0] in ('publish_pending', 'published'):
                proof = json.loads(existing[1])
                self.b.git(project, 'fetch', '--no-tags', 'origin', 'main')
                self.b.git(project, 'merge-base', '--is-ancestor', proof['commit'], 'origin/main')
                self.db.execute('INSERT OR IGNORE INTO integrations(project,pr,head,commit_id,reconciled) VALUES(?,?,?,?,0)',
                                (project['id'], pr['number'], head, proof['commit']))
            return True  # Failed checks and uncertain publication are never blindly replayed.
        if self.db.execute("SELECT 1 FROM local_integrations WHERE project=? AND pr=? AND status='publish_pending'", (project['id'], pr['number'])).fetchone():
            raise RuntimeError('earlier local publication outcome remains uncertain')
        if self.b.main(project) != base:
            return True
        import tempfile
        directory = Path(tempfile.mkdtemp(prefix='local-infra-', dir=self.c.state))
        tree = directory/'source'
        receipt = dict(project=project['id'], pr=pr['number'], head=head, base=base,
                       authorization='user-local-infrastructure-2026-11-01', checks=[])
        self.db.execute('INSERT INTO local_integrations VALUES(?,?,?,?,?,?)', (*key, 'testing', canonical(receipt)))
        try:
            self.b.git(project, 'worktree', 'add', '--detach', str(tree), base)
            metadata = self.b.git(project, 'log', '--format=%an <%ae>|%cn <%ce>', base+'..'+head)
            if not metadata or any(x != IDENTITY+'|'+IDENTITY for x in metadata.splitlines()):
                raise RuntimeError('source authorship mismatch')
            if 'co-authored-by:' in self.b.git(project, 'log', '--format=%B', base+'..'+head).lower():
                raise RuntimeError('additional source authorship')
            self.b.git(project, 'merge', '--no-ff', head, '-m', f"Merge PR #{pr['number']}: local infrastructure", '-m',
                       '- Integrate infrastructure under user local-test authority.\n- Validate the exact combined tree in isolated state.', cwd=tree)
            receipt['commit'] = self.b.git(project, 'rev-parse', 'HEAD', cwd=tree)
            receipt['tree'] = self.b.git(project, 'rev-parse', 'HEAD^{tree}', cwd=tree)
            paths = self.b.git(project, 'diff', '--name-only', '--no-renames', base, receipt['commit']).splitlines()
            scope=local_scope(lambda *args:self.b.git(project,*args),project['repository'],base,receipt['commit'],owner)
            if scope is None:
                raise RuntimeError('combined scope is not infrastructure')
            if scope['tracking'] is not None:receipt['tracking']=scope['tracking']
            receipt['paths'] = paths
            self.b.git(project, 'diff', '--check', base, receipt['commit'])
            for index, command in enumerate(LOCAL_COMMANDS):
                log = directory/f'check-{index}.log'
                argv = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session',
                        '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
                        '--ro-bind', '/lib64', '/lib64', '--symlink', 'usr/bin', '/bin',
                        '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                        '--bind', str(tree), '/source', '--chdir', '/source',
                        '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
                        '--setenv', 'HOME', '/tmp', '--setenv', 'LANG', 'C.UTF-8',
                        '--setenv', 'PYTHONDONTWRITEBYTECODE', '1', *command]
                check = dict(command=list(command), exit=None, log=str(log))
                receipt['checks'].append(check)
                try:
                    with log.open('wb') as stream:
                        result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, timeout=900)
                    check['exit'] = result.returncode
                finally:
                    check['sha256'] = hashlib.sha256(log.read_bytes()).hexdigest()
                if result.returncode:
                    raise RuntimeError('local infrastructure checks failed; exact inputs held for repair')
            if (self.b.git(project, 'rev-parse', 'HEAD', cwd=tree) != receipt['commit']
                    or self.b.git(project, 'rev-parse', 'HEAD^{tree}', cwd=tree) != receipt['tree']
                    or self.b.git(project, 'status', '--porcelain', cwd=tree)):
                raise RuntimeError('tests modified the candidate')
            fresh = self.c.pr(project, pr['number'])
            current = json.loads(self.c.ticket(project, 'show', owner['key']))
            if (fresh['headRefOid'] != head or fresh['baseRefOid'] != base
                    or any(current.get(k) != owner.get(k) for k in ('token','session','branch','model','agent'))
                    or not self.eligible(project, fresh, current) or self.b.main(project) != base):
                raise RuntimeError('local integration inputs changed')
            from coordinator import source_feedback
            if current.get('spec') != owner.get('spec') or source_feedback(fresh) != source_feedback(pr):
                raise RuntimeError('local integration specification or feedback changed')
            self.fenced(project, fresh, current)
            self.db.execute('UPDATE local_integrations SET status=?,receipt=? WHERE project=? AND pr=? AND head=? AND base=?',
                            ('publish_pending', canonical(receipt), *key))
            self.db.execute('INSERT OR IGNORE INTO integrations(project,pr,head,commit_id,reconciled) VALUES(?,?,?,?,0)',
                            (project['id'], pr['number'], head, receipt['commit']))
            self.b.git(project, 'push', 'origin', receipt['commit']+':refs/heads/main')
            self.db.execute('UPDATE local_integrations SET status=? WHERE project=? AND pr=? AND head=? AND base=?', ('published', *key))
        except Exception as error:
            receipt['diagnostic'] = str(error) if type(error) is RuntimeError else type(error).__name__
            self.db.execute("UPDATE local_integrations SET status=CASE WHEN status='publish_pending' THEN status ELSE 'failed' END, receipt=? WHERE project=? AND pr=? AND head=? AND base=?",
                            (canonical(receipt), *key))
            raise
        finally:
            if tree.exists():
                self.c.cleanup(project, tree)
        return True


class SharedAcceptanceRepair:
    """Route a real shared funding incident once, never to every feature owner."""
    def __init__(self, coordinator):
        self.c, self.db = coordinator, coordinator.store.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS shared_acceptance_repairs (
            project TEXT, episode TEXT, owner_ticket TEXT, owner_head TEXT,
            base TEXT, job INTEGER, state TEXT, diagnostic TEXT, updated REAL,
            PRIMARY KEY(project,episode))''')
        self.db.execute('CREATE TABLE IF NOT EXISTS shared_acceptance_status (project TEXT PRIMARY KEY,owner_ticket TEXT,state TEXT,job INTEGER,diagnostic TEXT,updated REAL)')

    def record(self, project, episode, owner, head, base, job, state, diagnostic):
        self.db.execute('INSERT OR REPLACE INTO shared_acceptance_repairs VALUES(?,?,?,?,?,?,?,?,?)',
            (project['id'],episode,owner,head,base,job,state,diagnostic,self.c.batches.clock()))
        return dict(owner=owner,state=state,job=job,diagnostic=diagnostic)

    def tick(self, project):
        try:
            result=self.route(project)
        except Exception as error:
            self.db.execute('INSERT OR REPLACE INTO shared_acceptance_status VALUES(?,?,?,?,?,?)',
                (project['id'],project.get('shared_acceptance_repair',{}).get('ticket'),'held',None,
                 type(error).__name__,self.c.batches.clock()))
            raise
        if result:
            self.db.execute('INSERT OR REPLACE INTO shared_acceptance_status VALUES(?,?,?,?,?,?)',
                (project['id'],result['owner'],result['state'],result.get('job'),result.get('diagnostic',''),self.c.batches.clock()))
        return result

    def funding_receipt(self, project, batch, checks):
        """Authenticated read of the exact failed run; never execute artifact code."""
        urls = {c.get('detailsUrl') for c in checks}
        if len(urls) != 1:
            raise RuntimeError('ambiguous funding run')
        match = re.fullmatch(r'https://github.com/'+re.escape(project['repository'])+r'/actions/runs/([1-9][0-9]*)(?:/job/[1-9][0-9]*)?', urls.pop() or '')
        if not match:
            raise RuntimeError('funding run identity missing')
        run_id = match[1]
        run = json.loads(self.c.gh(project,['api',f"repos/{project['repository']}/actions/runs/{run_id}"]))
        if (str(run.get('id')) != run_id or run.get('head_branch') != 'main'
                or run.get('event') not in ('workflow_run','workflow_dispatch')
                or (run.get('head_repository') or {}).get('full_name') != project['repository']
                or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1
                or run.get('status') != 'completed' or run.get('conclusion') != 'failure'
                or run.get('name') != 'Automatic testnet'
                or run.get('path') != '.github/workflows/testnet.yml'
                or (run.get('repository') or {}).get('full_name') != project['repository']):
            raise RuntimeError('funding run is active or provenance changed')
        with tempfile.TemporaryDirectory(prefix='funding-effects-') as directory:
            self.c.gh(project,['run','download',run_id,'-R',project['repository'],
                               '-n','testnet-effects-'+batch['head'],'-D',directory])
            files = list(Path(directory).iterdir())
            # Preflight must precede every suite. Extra suite evidence could
            # contain submitted or uncertain work and cannot authorize replay.
            if len(files) != 1 or files[0].name != 'package-upgrade.json' or files[0].is_symlink():
                raise RuntimeError('funding effects missing or contain additional suite work')
            if not files[0].is_file() or files[0].stat().st_size > 1_000_000:
                raise RuntimeError('funding receipt too large or not a file')
            raw = files[0].read_bytes()
            evidence = json.loads(raw)
        funding, faucet = evidence.get('funding',{}), evidence.get('faucet',{})
        before = funding.get('address_mist',-1) + funding.get('coin_mist',-1)
        after, required = faucet.get('balance_after_mist'), funding.get('required_mist')
        if (evidence.get('network') != 'testnet' or evidence.get('failure_class') != 'funding'
                or evidence.get('toolchain',{}).get('compatibility',{}).get('chain') != '4c78adac'
                or evidence.get('success') is not False
                or evidence.get('transactions') != [] or evidence.get('allowance_used_mist') != 0
                or type(evidence.get('max_spend_mist')) is not int or evidence['max_spend_mist'] <= 0
                or type(funding.get('held_mist')) is not int or funding['held_mist'] != 0
                or type(required) is not int or required <= 0 or type(after) is not int or not 0 <= after < required
                or faucet.get('attempts') != 1 or faucet.get('cli_returncode') != 0
                or faucet.get('balance_before_mist') != before or before < 0
                or faucet.get('balance_change_mist') != after-before or after <= before
                or faucet.get('success') is not False):
            raise RuntimeError('not a zero-submission funding-only shortfall receipt')
        # No held uncertain reservation qualifies. Historical state is never
        # changed; a normal retry still restores its encrypted cache.
        fresh = json.loads(self.c.gh(project,['api',f"repos/{project['repository']}/actions/runs/{run_id}"]))
        if fresh != run:
            raise RuntimeError('funding run changed during artifact read')
        return dict(run=run_id,attempt=run.get('run_attempt'),sha256=hashlib.sha256(raw).hexdigest(),
                    evidence=evidence)

    def recover_funding(self, project, key, maximum):
        """One delayed fresh candidate per proven preflight, at most maximum retries.

        Runs use normal protected wallet serialization and the existing single
        faucet allowance. No operator wallet, rerun API or mutation replay here.
        """
        b = self.c.batches
        def status(state, diagnostic):
            return dict(owner=key,state=state,diagnostic=diagnostic)
        if not b.enabled(project):
            return status('held','funding recovery requires restored enabled batch mode')
        rows = b.rows(project)
        current_base = None
        for batch in reversed(rows):
            if batch['status'] != 'failed':
                continue
            error = batch_error(batch)
            checks = error.get('checks',[])
            members = json.loads(batch['members'])
            if (error.get('kind') != 'candidate_checks_failed' or not checks or len(members) != 1
                    or not all(c.get('name') == 'testnet/package-upgrade' and c.get('workflowName') == 'Automatic testnet'
                               and c.get('classification') == 'funding' for c in checks)):
                continue
            if self.db.execute('SELECT 1 FROM batch_funding_recoveries WHERE project=? AND batch_id=?',
                               (project['id'],batch['id'])).fetchone():
                continue
            # Old bases cannot be revived. A new main base already makes the
            # original selection eligible through normal batch collection.
            if current_base is None:
                current_base = b.main(project)
            if current_base != batch['base']:
                continue
            related = [r for r in rows if r['base'] == batch['base'] and
                       [(m['pr'],m['head']) for m in json.loads(r['members'])] ==
                       [(m['pr'],m['head']) for m in members]]
            if any(r['status'] not in TERMINAL or r.get('publish_intent') for r in related):
                return status('held','funding candidate active or publication uncertain')
            if related[-1]['id'] != batch['id']:
                continue
            attempts = sum(bool(self.db.execute('SELECT 1 FROM batch_funding_recoveries WHERE project=? AND batch_id=?',
                           (project['id'],r['id'])).fetchone()) for r in related)
            if attempts >= maximum:
                return status('blocked','funding retry bound exhausted; operator must verify dedicated balance/reserve before a reviewed recovery')
            next_check = batch['updated'] + 3600
            if b.clock() < next_check:
                return status('funding_wait',f'next check at {next_check:.0f}; resume only after exact funding receipt and ownership revalidation')
            for number in [batch['pr'], *[m['pr'] for m in members]]:
                if self.db.execute("SELECT 1 FROM jobs WHERE project=? AND pr=? AND status IN "
                        "('reserved','running','orphaned','quarantined','quota_wait','quota_launch')",
                        (project['id'],number)).fetchone():
                    return status('held','funding recovery worker active or uncertain')
            b.validate_members(project,batch)
            for member in members:
                pr = self.c.pr(project,member['pr'])
                self.c.local_infrastructure.fenced(project,pr,b.owner(project,member))
            candidate = b.candidate(project,batch)
            from coordinator import failed_checks
            if (candidate.get('state') != 'CLOSED' or candidate.get('headRefName') != batch['branch']
                    or failed_checks(candidate) != checks):
                raise RuntimeError('closed funding candidate/check identity changed')
            if self.db.execute("SELECT 1 FROM jobs WHERE project=? AND pr=? AND status IN "
                    "('reserved','running','orphaned','quarantined','quota_wait','quota_launch')",
                    (project['id'],batch['pr'])).fetchone():
                raise RuntimeError('funding candidate worker active or uncertain')
            receipt = self.funding_receipt(project,batch,checks)
            # Revalidate after authenticated provider reads; never reuse an
            # approval of an earlier source/base/claim snapshot.
            b.validate_members(project,batch)
            for member in members:
                self.c.local_infrastructure.fenced(project,self.c.pr(project,member['pr']),b.owner(project,member))
            if b.candidate(project,batch) != candidate:
                raise RuntimeError('funding candidate changed during receipt read')
            current = json.loads(self.c.ticket(project,'show',key))
            if current.get('status') != 'done' or current.get('publish_pending'):
                raise RuntimeError('completed shared owner changed during funding recovery')
            self.db.execute('INSERT INTO batch_funding_recoveries VALUES(?,?,?,?,?,?)',
                (project['id'],batch['id'],batch['manifest_digest'],hashlib.sha256(batch['error'].encode()).hexdigest(),
                 canonical(receipt),b.clock()))
            return status('awaiting_validation','funding-only retry authorized; fresh candidate requires all normal CI/testnet/review gates')
        return status('resolved','shared code owner Done; no current unrecovered funding candidate')

    def route(self, project):
        config=project.get('shared_acceptance_repair')
        if config is None: return None
        if (not isinstance(config,dict) or not isinstance(config.get('ticket'),str)
                or not re.fullmatch(r'[A-Z][A-Z0-9]*-\d+',config['ticket'])
                or type(config.get('max_attempts',3)) is not int
                or not 1 <= config.get('max_attempts',3) <= 5):
            raise ValueError('shared acceptance repair requires ticket and max_attempts1..5')
        key=config['ticket']; maximum=config.get('max_attempts',3)
        owner=json.loads(self.c.ticket(project,'show',key))
        if owner['status']=='done':
            if owner.get('publish_pending'):
                return dict(owner=key,state='held',diagnostic='completed shared ownership publication pending')
            return self.recover_funding(project, key, maximum)
        if owner.get('agent') not in ('Codex','Claude') or not owner.get('token'):
            return dict(owner=key,state='blocked',diagnostic='shared repair owner has no implementation claim')
        if owner.get('status')!='review' or owner.get('expired') or owner.get('publish_pending'):
            return dict(owner=key,state='blocked',diagnostic='shared owner requires published Review handoff or explicit recovery')
        # Select the real linked source; labels and a caller's arbitrary PR number
        # cannot redirect this authority to another source/claim.
        number=self.c.issue_number(project,owner)
        if not number: return dict(owner=key,state='blocked',diagnostic='owner issue unavailable')
        prs=json.loads(self.c.gh(project,['pr','list','-R',project['repository'],'--state','open',
                       '--head',owner['branch'],'--json','number,headRefName,headRefOid,baseRefName,isDraft,isCrossRepository,state']))
        if len(prs)!=1: return dict(owner=key,state='blocked',diagnostic='shared owner source is ambiguous or absent')
        pr=prs[0]
        if (not isinstance(pr,dict) or type(pr.get('number')) is not int
                or pr['number']<=0 or pr.get('headRefName')!=owner['branch']
                or not isinstance(pr.get('headRefOid'),str)
                or not re.fullmatch(r'[0-9a-f]{40}',pr['headRefOid'])
                or pr.get('baseRefName')!='main' or pr.get('isDraft') is not False
                or pr.get('isCrossRepository') is not False or pr.get('state')!='OPEN'):
            return dict(owner=key,state='blocked',diagnostic='shared owner source is not ready')
        head=pr['headRefOid']
        active=self.db.execute("SELECT id FROM jobs WHERE project=? AND pr=? AND status IN "
            "('reserved','running','orphaned','quarantined','quota_wait','quota_launch') ORDER BY id DESC LIMIT 1",
            (project['id'],pr['number'])).fetchone()
        if active: return dict(owner=key,state='owner_busy',job=active[0])
        # Do not interrupt an in-flight candidate or ambiguous publication. Only
        # already-failed exact-owner candidates establish a new repair episode.
        if pr['number'] in self.c.batches.reserved_members(project):
            return dict(owner=key,state='awaiting_validation',diagnostic='shared source has an active candidate')
        failure=None
        for batch in reversed(self.c.batches.rows(project)):
            if batch['status']!='failed': continue
            try:
                error=batch_error(batch); checks=error.get('checks',[])
                members=json.loads(batch['members'])
            except (ValueError,TypeError): continue
            if (error.get('kind')!='candidate_checks_failed' or not checks
                    or not all(c.get('name')=='testnet/package-upgrade' and c.get('workflowName')=='Automatic testnet'
                               and c.get('classification')=='funding' for c in checks)
                    or not any(m['pr']==pr['number'] and m['head']==head for m in members)):
                continue
            failure=(batch,checks);break
        if failure is None: return dict(owner=key,state='awaiting_validation',diagnostic='no failed candidate for current shared source')
        batch,checks=failure
        self.c.batches.verify_manifest(project,batch)
        episode=hashlib.sha256(canonical([key,head,batch['base'],'funding']).encode()).hexdigest()
        existing=self.db.execute('SELECT job,state,updated FROM shared_acceptance_repairs WHERE project=? AND episode=?',
                                 (project['id'],episode)).fetchone()
        if existing:
            # Even a failed/unknown launch is durable: never silently replay it.
            job,state,updated=existing
            if state=='held' and not job:
                # No Store reservation or process was created. Capacity may
                # recover; retry the guarded reservation after a bounded delay.
                if self.c.batches.clock()-updated < 60:
                    return dict(owner=key,state='held',diagnostic='waiting for repair capacity; no job was launched')
            else:
                if job:
                    row=self.db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()
                    state='awaiting_validation' if row and row[0]=='done' else 'repair_retained'
                return self.record(project,episode,key,head,batch['base'],job,state,'existing incident retained; no blind replay')
        count=self.db.execute('SELECT count(*) FROM shared_acceptance_repairs WHERE project=? AND owner_ticket=? AND owner_head=? AND episode!=?',
                              (project['id'],key,head,episode)).fetchone()[0]
        if count>=maximum: return dict(owner=key,state='blocked',diagnostic='shared repair attempts exhausted for unchanged source')
        # Cheap metadata only suppresses work. It never authorizes a launch.
        fresh=self.c.pr(project,pr['number'])
        identity=('number','headRefName','headRefOid','baseRefName','isDraft','isCrossRepository','state')
        if any(type(fresh.get(k)) is not type(pr[k]) or fresh.get(k)!=pr[k] for k in identity):
            return dict(owner=key,state='held',diagnostic='shared owner source changed during evidence hydration')
        pr=fresh
        self.c.batches.verify_manifest(project,batch)
        self.c.assert_review_owner(project,owner)
        self.c.local_infrastructure.fenced(project,pr,owner)
        context=dict(candidate_pr=batch['pr'],candidate_head=batch['head'],candidate_base=batch['base'],checks=checks,
                     shared_owner=key,episode=episode)
        snapshot={**pr,'_batch_failure':context}
        self.record(project,episode,key,head,batch['base'],None,'launch_pending','shared cost repair reserved before launch')
        try:
            self.c.launch(project,snapshot,owner,'fix')
            from coordinator import feedback
            row=self.db.execute("SELECT id,status FROM jobs WHERE project=? AND pr=? AND kind='fix' AND head=? AND feedback=?",
                                (project['id'],pr['number'],head,feedback(snapshot))).fetchone()
            return self.record(project,episode,key,head,batch['base'],row[0] if row else None,
                               ('repair_owned' if row[1] in ('reserved','running') else 'repair_retained') if row else 'held',
                               'one shared-owner repair; victim branches unchanged')
        except Exception:
            self.record(project,episode,key,head,batch['base'],None,'launch_uncertain','inspect retained launch; no automatic replay')
            raise
