from ticket_common import *
from ticket_core import Coordinator, prepare_worktree, migrate_legacy
from ticket_github import Gh, fetch_catalog, setup_project

def parser():
    p = argparse.ArgumentParser(description='Atomic tickets across local repositories. Never creates issues.')
    p.add_argument('--root', type=Path, default=Path.cwd())
    p.add_argument('--project', help='Select a registered project from any directory')
    p.add_argument('--database', type=Path, help='Testing/explicit deployment only; workers must share the same database')
    p.add_argument('--version', action='version', version=VERSION)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('register','init','refresh','migrate','projects'):
        sub.add_parser(name)
    listing = sub.add_parser('list')
    listing.add_argument('--all', action='store_true', help='Show every project')
    setup = sub.add_parser('setup-project')
    setup.add_argument('--create-if-missing', action='store_true')
    claim = sub.add_parser('claim')
    claim.add_argument('--agent', choices=['Codex','Claude','Other'], required=True)
    claim.add_argument('--model', required=True)
    claim.add_argument('--session', required=True)
    claim.add_argument('--ticket')
    claim.add_argument('--path', action='append', default=[])
    claim.add_argument('--resource', action='append', default=[])
    claim.add_argument('--worktree', type=Path)
    claim.add_argument('--base', default='HEAD')
    claim.add_argument('--local-only', action='store_true')
    for name in ('show','publish'):
        command = sub.add_parser(name)
        command.add_argument('ticket')
    for name in ('heartbeat','assert','expand','review','block','resume','release','note'):
        command = sub.add_parser(name)
        command.add_argument('ticket')
        command.add_argument('--token', required=True)
        if name in {'assert','expand'}:
            command.add_argument('--path', action='append', default=[])
        if name in {'assert','expand'}:
            command.add_argument('--resource', action='append', default=[])
        if name in {'review','block','release','note'}:
            command.add_argument('--text', required=True)
        if name == 'release':
            command.add_argument('--confirm-stopped', action='store_true')
        if name in {'review','block','resume','release'}:
            command.add_argument('--local-only', action='store_true')
    recover = sub.add_parser('recover')
    recover.add_argument('ticket')
    recover.add_argument('--expect-token', required=True)
    recover.add_argument('--text', required=True)
    recover.add_argument('--confirm-worker-stopped', action='store_true')
    recover.add_argument('--local-only', action='store_true')
    complete = sub.add_parser('complete')
    complete.add_argument('ticket')
    complete.add_argument('--expect-token', required=True)
    complete.add_argument('--reviewer', required=True)
    complete.add_argument('--text', required=True)
    complete.add_argument('--merged-commit', required=True)
    complete.add_argument('--integration-ref', default='main')
    complete.add_argument('--local-only', action='store_true')
    return p


def summary(coordinator, row):
    item = coordinator.view(row)
    return {key: item[key] for key in ['project','key','status','agent','model','session','expired','worktree','branch','publish_pending']} | {'paths': item['spec']['paths'], 'resources': item['spec']['resources']}


def main():
    args = parser().parse_args()
    db_path = args.database or database_path()
    if args.command == 'projects':
        coordinator = Coordinator(db_path)
        try:
            print(json.dumps([{'project_id': r['id'], 'repository': r['repository'], 'root': r['root']} for r in coordinator.db.execute('SELECT * FROM projects ORDER BY id')], indent=2))
        finally:
            coordinator.close()
        return
    if args.project:
        reader = Coordinator(db_path)
        try:
            registered = reader.db.execute('SELECT * FROM projects WHERE id=?', (args.project,)).fetchone()
            if not registered:
                raise CoordinationError('Project is not registered; run register from its checkout')
            root, config = load_configuration(Path(registered['root']))
            if config['project_id'] != args.project:
                raise CoordinationError('Registered and committed project IDs differ')
        finally:
            reader.close()
    else:
        root, config = load_configuration(args.root)
    common = Path(run_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    candidate_legacy = common / 'suifleet-coordinator.sqlite3'
    legacy = candidate_legacy if config.get('legacy_coordinator') or candidate_legacy.exists() else None
    coordinator = Coordinator(db_path, project_id=config['project_id'], legacy_path=legacy)
    coordinator.register(root, config)
    try:
        result = None
        gh = None
        if args.command in {'init','refresh','setup-project'}:
            gh = Gh(config['github']['account'])
        if args.command == 'register':
            result = {'registered': config['project_id'], 'database': str(db_path), 'repository': config['repository']}
        elif args.command == 'setup-project':
            result = setup_project(root, config, gh, args.create_if_missing)
            coordinator.register(root, config)
        elif args.command == 'migrate':
            if not legacy:
                raise CoordinationError('No legacy coordinator configured')
            result = migrate_legacy(coordinator, legacy)
        elif args.command in {'init','refresh'}:
            catalog, state, snapshot = fetch_catalog(root, config, gh)
            coordinator.import_catalog(catalog, snapshot)
            with coordinator.transaction():
                coordinator.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (coordinator.meta_key('github_state'), json.dumps(state)))
            result = {'project': config['project_id'], 'database': str(db_path), 'tickets': len(catalog['issues'])}
        elif args.command == 'list':
            rows = coordinator.db.execute('SELECT * FROM tickets ORDER BY project,key') if args.all else coordinator.db.execute('SELECT * FROM tickets WHERE project=? ORDER BY key', (config['project_id'],))
            result = [summary(coordinator, row) for row in rows]
        elif args.command == 'show':
            result = coordinator.view(coordinator.row(args.ticket))
            result['events'] = [dict(r) for r in coordinator.db.execute('SELECT * FROM events WHERE ticket=? ORDER BY id', (coordinator.qualified(args.ticket),))]
        elif args.command == 'claim':
            result = coordinator.claim(args.agent, args.model, args.session, args.ticket, args.path, resources=args.resource)
            print(json.dumps({'project': config['project_id'], 'reserved': result['key'], 'token': result['token'], 'database': str(db_path)}), file=sys.stderr, flush=True)
            result = prepare_worktree(coordinator, root, result, args.worktree, args.base)
        elif args.command == 'heartbeat':
            result = coordinator.heartbeat(args.ticket, args.token)
        elif args.command == 'assert':
            result = coordinator.assert_owner(args.ticket, args.token, args.path, args.resource)
        elif args.command == 'expand':
            result = coordinator.expand(args.ticket, args.token, args.path, args.resource)
        elif args.command in {'review','block'}:
            result = coordinator.transition(args.ticket, args.token, 'review' if args.command == 'review' else 'blocked', args.text)
        elif args.command == 'resume':
            result = coordinator.resume(args.ticket, args.token)
        elif args.command == 'release':
            result = coordinator.release(args.ticket, args.token, args.text, args.confirm_stopped)
        elif args.command == 'recover':
            result = coordinator.recover(args.ticket, args.expect_token, args.text, args.confirm_worker_stopped)
        elif args.command == 'complete':
            commit = run_git(root, 'rev-parse', '--verify', args.merged_commit + '^{commit}')
            run_git(root, 'merge-base', '--is-ancestor', commit, args.integration_ref)
            result = coordinator.complete(args.ticket, args.expect_token, args.reviewer, args.text + '\nMerged commit: ' + commit)
        elif args.command == 'note':
            coordinator.note(args.ticket, args.token, args.text)
            result = {'noted': args.ticket}
        if args.command in {'claim','expand','review','block','resume','release','recover','complete','publish'} and not getattr(args,'local_only',False):
            saved = coordinator.db.execute('SELECT value FROM metadata WHERE key=?', (coordinator.meta_key('github_state'),)).fetchone()
            if not saved:
                raise CoordinationError('No GitHub publication snapshot; refresh before publishing')
            publication = json.loads(saved[0])
            if publication['project']['owner']['login'] != config['github']['owner'] or (config['github'].get('project_number') and publication['project']['number'] != config['github']['project_number']):
                raise CoordinationError('Project configuration changed; refresh before publishing ownership')
            gh = Gh(config['github']['account'])
            key = result['key'] if args.command == 'claim' else args.ticket
            coordinator.publish(key, publication, gh)
            result = coordinator.view(coordinator.row(key))
        print(json.dumps(result, indent=2))
    finally:
        coordinator.close()
