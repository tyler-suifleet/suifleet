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


class BatchInvalidated(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def manifest_digest(project, batch_id, base, head, members):
    return hashlib.sha256(canonical(dict(project=project, id=batch_id, base=base,
                                         head=head, members=members)).encode()).hexdigest()


class BatchController:
    def __init__(self, coordinator, clock=time.time, installed=None):
        self.c, self.db, self.clock = coordinator, coordinator.store.db, clock
        self.installed = Path(installed) if installed is not None else Path(__file__).with_name('installed.json')
        self._installed_proofs = set()
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
        self.db.execute('''CREATE TABLE IF NOT EXISTS integration_batch_conflicts (
            project TEXT,pr INTEGER,head TEXT,base TEXT,batch_id TEXT,context TEXT,
            PRIMARY KEY(project,pr,head,base))''')

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

    def source(self, project, pr, owner):
        from coordinator import source_check_failures
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
                    acceptance=owner.get('spec', {}).get('acceptance', []), title=pr.get('title', ''))

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
            if not fresh or fresh['source_receipt'] != member['source_receipt']:
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
                # Old source reviews remain useful only when their base is in
                # the pinned integration history, never an unrelated/future base.
                self.git(project, 'merge-base', '--is-ancestor', member['reviewed_base'], batch['base'])
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
            error = json.loads(batch.get('error') or '{}')
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

    def repair_context(self, project, pr):
        result = {}
        for batch in reversed(self.rows(project)):
            members = json.loads(batch['members'])
            if (batch['status'] == 'failed' and len(members) == 1
                    and members[0]['pr'] == pr['number'] and members[0]['head'] == pr['headRefOid']):
                try:
                    error = json.loads(batch['error'] or '{}')
                except (TypeError, ValueError):
                    continue
                if (error.get('kind') == 'candidate_checks_failed'
                        and batch['base'] == pr.get('baseRefOid')):
                    checks = error['checks']
                    # These rows were classified from the exact candidate/base
                    # by failed_checks before persistence. Funding cannot be
                    # repaired on an unchanged member's source branch. Retain
                    # the failed candidate/selection fence and all acceptance
                    # gates, but do not dispatch another owner repair for it.
                    if checks and all(check.get('name') == 'testnet/package-upgrade'
                                      and check.get('workflowName') == 'Automatic testnet'
                                      and check.get('classification') == 'funding'
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
                kind = json.loads(row['error'] or '{}').get('kind')
                if (kind not in ('candidate_checks_failed', 'candidate_review_changes')
                        or kind == 'candidate_checks_failed' and row['base'] != base):
                    continue
            except (TypeError, ValueError):
                continue
            failed.append(tuple((m['pr'],m['head']) for m in json.loads(row['members'])))
        pool = [m for m in candidates if ((m['pr'],m['head']),) not in failed]
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
                    if not selected or (len(selected) < maximum and self.clock()-batch['created'] < delay):
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
        paths = self.b.git(project, 'diff', '--name-only', '--no-renames', base+'...'+head).splitlines()
        return infrastructure_scope(paths)

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
            if not infrastructure_scope(paths):
                raise RuntimeError('combined scope is not infrastructure')
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
