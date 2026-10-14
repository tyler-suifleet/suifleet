from ticket_common import *
from ticket_github import Gh, set_option

class Coordinator:
    def __init__(self, path, clock=time.time, project_id="", legacy_path=None):
        self.project_id = project_id
        if project_id and not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", project_id):
            raise CoordinationError("Invalid project namespace")
        self.legacy_path = Path(legacy_path) if legacy_path else None
        self.path = Path(path)
        self.clock = clock
        new_parent = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if new_parent:
            os.chmod(self.path.parent, 0o700)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        # The DB and WAL live on the WSL Linux filesystem, not /mnt/c or a network share.
        self.db = sqlite3.connect(self.path, timeout=120, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (
                key TEXT PRIMARY KEY, project TEXT NOT NULL DEFAULT '', spec TEXT NOT NULL, remote TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'backlog', token TEXT, agent TEXT, model TEXT,
                session TEXT, heartbeat REAL, expires REAL, worktree TEXT, branch TEXT,
                evidence TEXT, revision INTEGER NOT NULL DEFAULT 0, published_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS unique_claim_token ON tickets(token) WHERE token IS NOT NULL;
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, repository TEXT UNIQUE NOT NULL, git_common_dir TEXT UNIQUE NOT NULL,
                root TEXT NOT NULL, config TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, ticket TEXT NOT NULL,
                actor TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
            );
        ''')
        version = self.db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if version and int(version[0]) != SCHEMA:
            raise CoordinationError('Unsupported coordinator database schema')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema', ?)", (str(SCHEMA),))
        os.chmod(self.path, 0o600)

    def qualified(self, key):
        if not self.project_id:
            return key
        if '::' in key:
            if not key.startswith(self.project_id + '::'):
                raise CoordinationError('Ticket belongs to a different project')
            return key
        return self.project_id + '::' + key

    def dependency_key(self, key):
        return key if '::' in key else self.qualified(key)

    def meta_key(self, key):
        return key + ':' + self.project_id if self.project_id else key

    def paths(self, paths, resources=()):
        prefix = 'repositories/' + self.project_id + '/' if self.project_id else ''
        result = [scope_path(prefix + scope_path(p)) for p in paths]
        for resource in resources:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', resource) or '..' in resource.split('/'):
                raise CoordinationError('Invalid shared resource name')
            result.append('resources/' + resource)
        return sorted(set(result))

    def ensure_legacy_retired(self):
        if self.legacy_path and self.legacy_path.exists():
            legacy = sqlite3.connect('file:' + str(self.legacy_path) + '?mode=ro', uri=True)
            try:
                marker = legacy.execute("SELECT value FROM metadata WHERE key='retired_to'").fetchone()
                imported = self.db.execute('SELECT value FROM metadata WHERE key=?', (self.meta_key('legacy_imported'),)).fetchone()
                if not marker or Path(marker[0]).resolve() != self.path.resolve() or not imported:
                    raise CoordinationError('Legacy coordinator is not retired into this database; run migrate before claiming')
            finally:
                legacy.close()

    def register(self, root, config):
        common = run_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        actual_remote = run_git(root, 'remote', 'get-url', 'origin')
        if actual_remote != config['git_remote']:
            raise CoordinationError('Configured Git remote differs from origin; do not change SSH identities implicitly')
        with self.transaction():
            old = self.db.execute('SELECT * FROM projects WHERE id=? OR repository=? OR git_common_dir=?',
                                  (self.project_id, config['repository'], common)).fetchone()
            if old and (old['id'], old['repository'], old['git_common_dir']) != (self.project_id, config['repository'], common):
                raise CoordinationError('Project is registered to another namespace/clone; use its canonical checkout or linked worktrees')
            canonical = str(Path(common).parent)
            self.db.execute('INSERT INTO projects VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config',
                            (self.project_id, config['repository'], common, canonical, json.dumps(config)))

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def event(self, key, actor, kind, detail):
        key = self.qualified(key)
        self.db.execute('INSERT INTO events(at,ticket,actor,kind,detail) VALUES (?,?,?,?,?)',
                        (self.clock(), key, actor, kind, detail))

    def import_catalog(self, config, snapshot):
        ordered_issues(config)
        config = json.loads(json.dumps(config))
        converted = {}
        for issue in config["issues"]:
            local_key = issue["key"]
            issue["key"] = self.qualified(local_key)
            issue["depends_on"] = [self.dependency_key(d) for d in issue["depends_on"]]
            issue["paths"] = self.paths(issue["paths"], issue.get("resources", []))
            remote = dict(snapshot[local_key])
            remote["depends_on"] = [self.dependency_key(d) for d in remote.get("depends_on", [])]
            converted[issue["key"]] = remote
        snapshot = converted
        if set(snapshot) != {i['key'] for i in config['issues']}:
            raise CoordinationError('Snapshot must contain every catalog ticket')
        with self.transaction():
            repo = self.db.execute("SELECT value FROM metadata WHERE key=?", (self.meta_key("repository"),)).fetchone()
            if repo and repo[0] != config['repository']:
                raise CoordinationError('Database belongs to another repository')
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (self.meta_key("repository"), config["repository"]))
            # Never silently discard claimed tickets when a worktree has an older catalog.
            existing = {r[0] for r in self.db.execute('SELECT key FROM tickets WHERE project=?', (self.project_id,))}
            if existing - set(snapshot):
                raise CoordinationError('Catalog is older/incomplete; refresh from the canonical checkout')
            for issue in config['issues']:
                for path in issue['paths']:
                    scope_path(path)
                if not issue['paths']:
                    raise CoordinationError('Empty ownership scope')
                key = issue['key']
                old = self.db.execute('SELECT * FROM tickets WHERE key=?', (key,)).fetchone()
                spec = dict(issue)
                if old and (old['token'] or old['status'] == 'done'):
                    # Catalog edits never drop an outstanding claim's resources.
                    previous = json.loads(old['spec'])
                    spec['paths'] = sorted(set(spec['paths']) | set(previous['paths']))
                    status = old['status']
                else:
                    remote = snapshot[key]
                    status = 'done' if remote['state'] == 'closed' and remote['status'] == 'Done' else 'backlog'
                self.db.execute('''INSERT INTO tickets(key,project,spec,remote,status) VALUES (?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET spec=excluded.spec, remote=excluded.remote, status=excluded.status''',
                    (key, self.project_id, json.dumps(spec), json.dumps(snapshot[key]), status))
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (self.meta_key("snapshot_at"), str(self.clock())))

    def row(self, key):
        key = self.qualified(key)
        row = self.db.execute('SELECT * FROM tickets WHERE key=?', (key,)).fetchone()
        if not row:
            raise CoordinationError(f'Unknown ticket: {key}')
        return row

    def view(self, row):
        item = dict(row)
        item['spec'] = json.loads(item['spec'])
        project = item['project']
        prefix = project + '::' if project else ''
        path_prefix = 'repositories/' + project + '/' if project else ''
        item['key'] = item['key'].removeprefix(prefix)
        item['spec']['key'] = item['spec']['key'].removeprefix(prefix)
        item['spec']['depends_on'] = [d.removeprefix(prefix) for d in item['spec']['depends_on']]
        paths = item['spec']['paths']
        item['spec']['resources'] = [p[len('resources/'):] for p in paths if p.startswith('resources/')]
        item['spec']['paths'] = [('.' if p == path_prefix.rstrip('/') else p.removeprefix(path_prefix)) for p in paths if not p.startswith('resources/')]
        item['remote'] = json.loads(item['remote'])
        item['expired'] = bool(item['expires'] is not None and item['expires'] <= self.clock())
        registration = self.db.execute('SELECT root FROM projects WHERE id=?', (project,)).fetchone()
        item['canonical_root'] = registration['root'] if registration else None
        item['publish_pending'] = item['published_revision'] < item['revision']
        return item

    def holds_scope(self, row):
        if row['status'] == 'done':
            return False
        remote = json.loads(row['remote'])
        return bool(row['token'] or (remote['state'] == 'open' and (
            remote['agent'] != 'Unassigned' or remote.get('assignees') or
            remote['status'] in {'In progress', 'Review', 'Blocked'})))

    def eligible(self, row, reserved, statuses):
        spec, remote = json.loads(row['spec']), json.loads(row['remote'])
        if row['revision'] > row['published_revision']:
            return 'GitHub ownership mirror pending; publish then refresh'
        if row['token'] or row['status'] != 'backlog':
            return 'already reserved or completed'
        if remote['state'] != 'open' or remote['status'] not in CLAIMABLE or remote['agent'] != 'Unassigned' or remote.get('assignees'):
            return 'reserved/blocked/closed in GitHub'
        if remote.get('external_blockers'):
            return 'external GitHub dependencies incomplete: ' + ', '.join(remote['external_blockers'])
        missing = [key for key in set(spec['depends_on']) | set(remote.get('depends_on', [])) if statuses.get(key) != 'done']
        if missing:
            return 'dependencies incomplete: ' + ', '.join(missing)
        # File paths are descriptive only; overlapping edits are resolved at
        # review/merge. Only shared physical/build resources are exclusive.
        for other in reserved:
            if conflicting_resources(spec['paths'], json.loads(other['spec'])['paths']):
                return f"resource conflicts with {other['key']} ({other['status']})"
        return None

    def claim(self, agent, model, session, ticket=None, ttl=900, resources=()):
        self.ensure_legacy_retired()
        if agent not in {'Codex', 'Claude', 'Other'} or not model.strip() or not session.strip() or not 60 <= ttl <= 3600:
            raise CoordinationError('Supply agent, actual model, session and a 60–3600 second lease')
        with self.transaction():
            updated = self.db.execute("SELECT value FROM metadata WHERE key=?", (self.meta_key("snapshot_at"),)).fetchone()
            if not updated or self.clock() - float(updated[0]) > MAX_SNAPSHOT_AGE:
                raise CoordinationError('GitHub snapshot stale/missing; run refresh before claiming')
            all_rows = list(self.db.execute('SELECT * FROM tickets'))
            rows = [r for r in all_rows if r['project'] == self.project_id]
            if any(r['session'] == session and r['status'] in {'in_progress', 'blocked'} for r in all_rows):
                raise CoordinationError('This session already owns an implementation; finish or release it first')
            statuses = {r['key']: ('done' if r['status'] == 'done' and r['revision'] == r['published_revision'] else r['status'] if r['status'] != 'done' else 'review') for r in all_rows}
            reserved = [r for r in all_rows if self.holds_scope(r)]
            candidates = [self.row(ticket)] if ticket else sorted(rows, key=lambda r: (json.loads(r['spec'])['priority'], r['key']))
            reasons = {}
            for original in candidates:
                candidate = dict(original)
                spec = json.loads(candidate['spec'])
                spec['paths'] = sorted(set(spec['paths']) | set(self.paths((), resources)))
                candidate['spec'] = json.dumps(spec)
                reason = self.eligible(candidate, reserved, statuses)
                if reason:
                    reasons[candidate['key']] = reason
                    continue
                token = uuid.uuid4().hex
                now = self.clock()
                self.db.execute('''UPDATE tickets SET spec=?,status='in_progress',token=?,agent=?,model=?,session=?,
                    heartbeat=?,expires=?,worktree=NULL,branch=NULL,evidence=NULL,revision=revision+1 WHERE key=?''',
                    (candidate['spec'], token, agent, model, session, now, now + ttl, candidate['key']))
                self.event(candidate['key'], session, 'claim', f'{agent}/{model}; token={token}; paths={spec["paths"]}')
                return self.view(self.row(candidate['key']))
            raise CoordinationError('No eligible ticket: ' + json.dumps(reasons, sort_keys=True))

    def owned(self, key, token, live=True):
        key = self.qualified(key)
        row = self.row(key)
        if not token or row['token'] != token:
            raise CoordinationError('Claim token no longer owns this ticket; stop editing')
        if live and row['expires'] is not None and row['expires'] <= self.clock():
            raise CoordinationError('Lease expired; stop editing and heartbeat before resuming')
        return row

    def heartbeat(self, key, token, ttl=900):
        key = self.qualified(key)
        if not 60 <= ttl <= 3600:
            raise CoordinationError('Lease must be 60–3600 seconds')
        with self.transaction():
            row = self.owned(key, token, live=False)
            if row['status'] not in {'in_progress', 'blocked'}:
                raise CoordinationError('Only implementation/blocked leases need heartbeats')
            self.db.execute('UPDATE tickets SET heartbeat=?, expires=? WHERE key=?', (self.clock(), self.clock() + ttl, key))
            return self.view(self.row(key))

    def assert_owner(self, key, token, resources=()):
        key = self.qualified(key)
        with self.transaction():
            row = self.owned(key, token)
            if row['status'] != 'in_progress':
                raise CoordinationError('Ticket is not in implementation; do not edit it')
            if row['revision'] != row['published_revision']:
                raise CoordinationError('Ownership mirror pending; publish before editing')
            remote = json.loads(row['remote'])
            if remote['state'] != 'open':
                raise CoordinationError('GitHub issue is closed; stop editing and coordinate review')
            if remote.get('external_blockers'):
                raise CoordinationError('External dependency incomplete; stop editing')
            for dependency in set(json.loads(row['spec'])['depends_on']) | set(remote.get('depends_on', [])):
                dep = self.db.execute('SELECT * FROM tickets WHERE key=?', (dependency,)).fetchone()
                if not dep:
                    raise CoordinationError('Dependency project/ticket is not initialized')
                if dep['status'] != 'done' or dep['revision'] != dep['published_revision']:
                    raise CoordinationError(f'Dependency no longer complete: {dependency}; stop editing')
            held = set(json.loads(row['spec'])['paths'])
            for resource in self.paths((), resources):
                if resource not in held:
                    raise CoordinationError(f'Resource not reserved: {resource[len("resources/"):]}')
            return self.view(row)

    def expand(self, key, token, resources):
        key = self.qualified(key)
        if not resources:
            raise CoordinationError('Supply additional resources')
        with self.transaction():
            row = self.owned(key, token)
            if row['status'] != 'in_progress':
                raise CoordinationError('Only implementation can reserve resources')
            spec = json.loads(row['spec'])
            expanded = set(spec['paths']) | set(self.paths((), resources))
            for other in self.db.execute('SELECT * FROM tickets WHERE key != ?', (key,)):
                if not self.holds_scope(other):
                    continue
                if conflicting_resources(expanded, json.loads(other['spec'])['paths']):
                    raise CoordinationError(f"Resource conflicts with {other['key']}")
            spec['paths'] = sorted(expanded)
            self.db.execute('UPDATE tickets SET spec=?, revision=revision+1 WHERE key=?', (json.dumps(spec), key))
            self.event(key, row['session'], 'scope', json.dumps(spec['paths']))
            return self.view(self.row(key))

    def attach_worktree(self, key, token, path, branch):
        key = self.qualified(key)
        with self.transaction():
            row = self.owned(key, token)
            if row['worktree'] or row['status'] != 'in_progress':
                raise CoordinationError('Claim already has a worktree or is not in progress')
            self.db.execute('UPDATE tickets SET worktree=?,branch=?,revision=revision+1 WHERE key=?', (str(path), branch, key))
            self.event(key, row['session'], 'worktree', str(path))

    def transition(self, key, token, status, evidence):
        key = self.qualified(key)
        if status not in {'review', 'blocked'} or not evidence.strip():
            raise CoordinationError('Review/blocked requires evidence or a blocker explanation')
        with self.transaction():
            row = self.owned(key, token)
            if row['status'] != 'in_progress':
                raise CoordinationError('Only an implementation can enter review/blocked')
            expires = None if status == 'review' else row['expires']
            self.db.execute('UPDATE tickets SET status=?,evidence=?,expires=?,revision=revision+1 WHERE key=?', (status, evidence, expires, key))
            self.event(key, row['session'], status, evidence)
            return self.view(self.row(key))

    def resume(self, key, token):
        key = self.qualified(key)
        with self.transaction():
            row = self.owned(key, token, live=False)
            if row['status'] not in {'review', 'blocked'}:
                raise CoordinationError('Only review/blocked can resume')
            other = self.db.execute("SELECT key FROM tickets WHERE key != ? AND session=? AND status IN ('in_progress','blocked')", (key, row['session'])).fetchone()
            if other:
                raise CoordinationError('This session already owns another implementation')
            self.db.execute("UPDATE tickets SET status='in_progress',heartbeat=?,expires=?,revision=revision+1 WHERE key=?", (self.clock(), self.clock() + 900, key))
            self.event(key, row['session'], 'resume', 'Reserved scope retained')
            return self.view(self.row(key))

    def release(self, key, token, reason, stopped):
        key = self.qualified(key)
        if not stopped or not reason.strip():
            raise CoordinationError('Release requires a reason and confirmation that editing has stopped')
        with self.transaction():
            row = self.owned(key, token, live=False)
            self.db.execute("UPDATE tickets SET status='backlog',token=NULL,expires=NULL,session=NULL,agent=NULL,model=NULL,revision=revision+1 WHERE key=?", (key,))
            self.event(key, row['session'], 'release', reason)
            return self.view(self.row(key))

    def recover(self, key, expected_token, reason, stopped):
        # No automatic expiration/reclaim: an old process could still write its checkout.
        if not stopped or not reason.strip():
            raise CoordinationError('Recovery requires explicit confirmation the old worker has stopped')
        return self.release(key, expected_token, 'Operator recovery: ' + reason, True)

    def complete(self, key, expected_token, reviewer, evidence):
        key = self.qualified(key)
        if not reviewer.strip() or not evidence.strip():
            raise CoordinationError('Completion requires reviewer and acceptance evidence')
        with self.transaction():
            row = self.owned(key, expected_token, live=False)
            if row['status'] != 'review':
                raise CoordinationError('Only reviewed work can complete')
            self.db.execute("UPDATE tickets SET status='done',token=NULL,expires=NULL,revision=revision+1 WHERE key=?", (key,))
            self.event(key, reviewer, 'complete', evidence)
            return self.view(self.row(key))

    def note(self, key, token, text):
        key = self.qualified(key)
        if not text.strip():
            raise CoordinationError('Empty note')
        with self.transaction():
            row = self.owned(key, token, live=False)
            self.event(key, row['session'], 'note', text)

    def publish(self, key, state, gh=None):
        key = self.qualified(key)
        gh = gh or Gh()
        # Serialize field writes with all claim mutations to prevent stale mirrors.
        with self.transaction():
            row = self.row(key)
            if row['revision'] == row['published_revision']:
                return
            project = state['project']
            fields = {f['name']: f for f in gh.run('project', 'field-list', str(project['number']), '--owner',
                project['owner']['login'], '--limit', '100', '--format', 'json', as_json=True)['fields']}
            item = json.loads(row['remote'])['item_id']
            status = {'in_progress': 'In progress', 'review': 'Review', 'blocked': 'Blocked', 'done': 'Done', 'backlog': 'Backlog'}[row['status']]
            for name, value in {'Status': status, 'Agent': row['agent'] or 'Unassigned'}.items():
                set_option(gh, project, item, fields[name], value)
            for name, value in {'Model': row['model'] or '', 'Work branch': row['branch'] or ''}.items():
                gh.graphql('''mutation($input: UpdateProjectV2ItemFieldValueInput!) {
                    updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
                }''', {'input': {'projectId': project['id'], 'itemId': item, 'fieldId': fields[name]['id'], 'value': {'text': value}}})
            self.db.execute('UPDATE tickets SET published_revision=revision WHERE key=?', (key,))


def prepare_worktree(coordinator, root, claim, requested=None, base='HEAD'):
    branch = f"work/{coordinator.project_id or 'ticket'}/{claim['key'].lower()}-{claim['token'][:8]}"
    path = Path(requested).resolve() if requested else Path(run_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).parent.parent / ((coordinator.project_id or 'ticket') + '-worktrees') / branch.replace('/', '-')
    if path.exists():
        raise CoordinationError(f'Worktree path already exists; reservation retained: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    run_git(root, 'worktree', 'add', '-b', branch, str(path), base)
    coordinator.attach_worktree(claim['key'], claim['token'], path, branch)
    return coordinator.view(coordinator.row(claim['key']))


def migrate_legacy(coordinator, legacy_path):
    legacy_path = Path(legacy_path)
    if not legacy_path.exists():
        return {'migrated': 0}
    legacy = sqlite3.connect(legacy_path, timeout=120, isolation_level=None)
    legacy.row_factory = sqlite3.Row
    try:
        legacy.execute('BEGIN IMMEDIATE')
        marker = legacy.execute("SELECT value FROM metadata WHERE key='retired_to'").fetchone()
        if marker:
            if Path(marker[0]).resolve() != coordinator.path.resolve():
                raise CoordinationError('Legacy database was retired elsewhere')
            imported = coordinator.db.execute('SELECT value FROM metadata WHERE key=?', (coordinator.meta_key('legacy_imported'),)).fetchone()
            if not imported:
                raise CoordinationError('Central migration record missing; restore the central database instead of reusing stale legacy state')
            legacy.execute('ROLLBACK')
            return {'already_migrated': True}
        schema = legacy.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if not schema or schema[0] != '1':
            raise CoordinationError('Unsupported legacy schema')
        rows = list(legacy.execute('SELECT * FROM tickets'))
        metadata = dict(legacy.execute('SELECT key,value FROM metadata'))
        # Backup through SQLite so committed WAL content is included; do not copy raw DB bytes.
        backup = coordinator.path.parent / ('legacy-' + coordinator.project_id + '-' + uuid.uuid4().hex[:8] + '.sqlite3')
        reader = sqlite3.connect('file:' + str(legacy_path) + '?mode=ro', uri=True)
        target = sqlite3.connect(backup)
        try:
            reader.backup(target)
        finally:
            target.close()
            reader.close()
        os.chmod(backup, 0o600)
        with coordinator.transaction():
            for source in rows:
                key = coordinator.qualified(source['key'])
                existing = coordinator.db.execute('SELECT token FROM tickets WHERE key=?', (key,)).fetchone()
                if existing and existing['token'] != source['token']:
                    raise CoordinationError('Central and legacy ownership differ; stop workers before resolving')
                values = dict(source)
                values['key'] = key
                values['project'] = coordinator.project_id
                spec = json.loads(values['spec'])
                spec['key'] = key
                spec['depends_on'] = [coordinator.dependency_key(d) for d in spec['depends_on']]
                spec['paths'] = coordinator.paths(spec['paths'])
                values['spec'] = json.dumps(spec)
                remote = json.loads(values['remote'])
                remote['depends_on'] = [coordinator.dependency_key(d) for d in remote.get('depends_on', [])]
                values['remote'] = json.dumps(remote)
                columns = list(values)
                coordinator.db.execute('INSERT OR REPLACE INTO tickets (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', list(values.values()))
                coordinator.event(key, 'migration', 'legacy-import', 'Ownership/lease/worktree/evidence preserved; original events retained in backup ' + str(backup))
            for key in ('repository', 'snapshot_at'):
                if key in metadata:
                    coordinator.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (coordinator.meta_key(key), metadata[key]))
            coordinator.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (coordinator.meta_key('legacy_imported'), str(legacy_path.resolve())))
        # Claims in the central tool check this marker. A crash before it is committed fails closed.
        legacy.execute("INSERT OR REPLACE INTO metadata VALUES ('schema','2')")
        legacy.execute("INSERT OR REPLACE INTO metadata VALUES ('retired_to',?)", (str(coordinator.path.resolve()),))
        legacy.execute('COMMIT')
        return {'migrated': len(rows), 'backup': str(backup), 'legacy_retired': True}
    except BaseException:
        if legacy.in_transaction:
            legacy.execute('ROLLBACK')
        raise
    finally:
        legacy.close()
