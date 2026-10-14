from ticket_common import *

class Gh:
    def __init__(self, account=None):
        self.account = account
        self.env = dict(os.environ, GH_HOST='github.com', GH_PROMPT_DISABLED='1')
        if account:
            env = dict(self.env)
            env.pop('GH_TOKEN', None)
            env.pop('GITHUB_TOKEN', None)
            result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', account],
                                    text=True, capture_output=True, env=env)
            legacy_lookup = bool(result.returncode)
            if legacy_lookup:
                # Older gh stores one top-level token instead of a users map. Capture it,
                # then verify its actual login with that exact token before using repo APIs.
                result = subprocess.run(['gh','auth','token','--hostname','github.com'],
                                        text=True,capture_output=True,env=env)
            if result.returncode or not result.stdout.strip():
                raise CoordinationError(f'GitHub API account {account!r} is not connected; authenticate it with repo/project access')
            self.env.pop('GITHUB_TOKEN', None)
            self.env['GH_TOKEN'] = result.stdout.strip()
            if legacy_lookup:
                verified = subprocess.run(['gh','api','user','--jq','.login'],
                                          text=True,capture_output=True,env=self.env)
                if verified.returncode or verified.stdout.strip().lower() != account.lower():
                    raise CoordinationError(f'GitHub API account {account!r} is not connected; legacy credentials belong to another account')

    def run(self, *args, payload=None, as_json=False):
        result = subprocess.run(['gh', *args], input=json.dumps(payload) if payload is not None else None,
                                text=True, capture_output=True, env=self.env)
        if result.returncode:
            raise CoordinationError(f"GitHub command {' '.join(args[:3])} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if as_json else result.stdout.strip()

    def api(self, endpoint, method=None, payload=None, paginate=False):
        args = ['api', endpoint]
        if endpoint != 'graphql':
            args += ['-H', 'X-GitHub-Api-Version: 2026-03-10']
        if method:
            args += ['--method', method]
        if payload is not None:
            args += ['--input', '-']
        if paginate:
            args += ['--paginate', '--slurp']
        response = self.run(*args, payload=payload, as_json=True)
        return [item for page in response for item in page] if paginate else response

    def graphql(self, query, variables):
        response = self.api('graphql', payload={'query': query, 'variables': variables})
        if response.get('errors'):
            raise CoordinationError('GitHub GraphQL operation failed: ' + str(response['errors']))
        return response['data']


def set_option(gh, project, item, field, value):
    options = {option['name']: option['id'] for option in field.get('options', [])}
    if value not in options:
        raise CoordinationError(f"Project field {field['name']} lacks option {value}")
    gh.graphql('''mutation($input: UpdateProjectV2ItemFieldValueInput!) {
        updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
    }''', {'input': {'projectId': project['id'], 'itemId': item, 'fieldId': field['id'],
                     'value': {'singleSelectOptionId': options[value]}}})


def live_snapshot(config, state, gh=None):
    gh = gh or Gh()
    items = gh.run('project', 'item-list', str(state['project']['number']), '--owner',
                   state['project']['owner']['login'], '--limit', '1000', '--format', 'json', as_json=True)
    if items['totalCount'] > len(items['items']):
        raise CoordinationError('Incomplete Project snapshot; refusing claims')
    issues = gh.api(f"repos/{config['repository']}/issues?state=all&per_page=100", paginate=True)
    by_number = {item['number']: item for item in issues if 'pull_request' not in item}
    by_url = {item['content'].get('url'): item for item in items['items'] if item.get('content')}
    snapshot = {}
    by_issue_id = {item['id']: key for key, item in state['issues'].items()}
    for issue in config['issues']:
        published = state['issues'].get(issue['key'])
        if not published or published['number'] not in by_number or published['url'] not in by_url:
            raise CoordinationError(f"Missing published issue/Project item: {issue['key']}")
        live = by_number[published['number']]
        item = by_url[published['url']]
        dependencies = gh.api(f"repos/{config['repository']}/issues/{published['number']}/dependencies/blocked_by?per_page=100", paginate=True)
        snapshot[issue['key']] = {
            'state': live['state'], 'status': item.get('status', ''),
            'agent': item.get('agent', ''), 'assignees': [a['login'] for a in live.get('assignees', [])],
            'url': published['url'], 'item_id': item['id'],
            'depends_on': [by_issue_id[d['id']] for d in dependencies if d['id'] in by_issue_id],
            'external_blockers': [d['html_url'] for d in dependencies if d['id'] not in by_issue_id and d['state'] != 'closed'],
        }
    return snapshot


def resolve_github_project(config, gh):
    github = config['github']
    number = github.get('project_number')
    if not number:
        result = gh.run('project', 'list', '--owner', github['owner'], '--limit', '1000', '--format', 'json', as_json=True)
        if result['totalCount'] > len(result['projects']):
            raise CoordinationError('Incomplete Project discovery')
        matches = [p for p in result['projects'] if p['title'] == github.get('project_title')]
        if len(matches) != 1:
            raise CoordinationError('Configure the existing Project number or use setup-project --create-if-missing after API authentication')
        number = matches[0]['number']
    return gh.run('project', 'view', str(number), '--owner', github['owner'], '--format', 'json', as_json=True)


def github_catalog(config, gh, project):
    result = gh.run('project', 'item-list', str(project['number']), '--owner', config['github']['owner'],
                    '--limit', '1000', '--format', 'json', as_json=True)
    if result['totalCount'] > len(result['items']):
        raise CoordinationError('Incomplete Project issue snapshot')
    live = gh.api(f"repos/{config['repository']}/issues?state=all&per_page=100", paginate=True)
    by_url = {i['html_url']: i for i in live if 'pull_request' not in i}
    issues, published = [], {}
    for item in result['items']:
        url = item.get('content', {}).get('url')
        if url not in by_url:
            continue
        source = by_url[url]
        key = 'GH-' + str(source['number'])
        body = source.get('body') or ''
        override = config.get('ticket_overrides', {}).get(key, {})
        # Unspecified ownership stays conservative: reserve the entire repository.
        paths = override.get('paths', ['.'])
        priority = item.get('priority')
        if priority not in {'P0', 'P1', 'P2'}:
            priority = next((l['name'].split(':', 1)[1] for l in source.get('labels', []) if l['name'] in {'priority:P0', 'priority:P1', 'priority:P2'}), 'P1')
        issues.append({'key': key, 'title': source['title'], 'priority': priority,
                       'paths': paths, 'resources': override.get('resources', []),
                       'depends_on': override.get('depends_on', []), 'acceptance': [body or source['title']],
                       'validation': 'Verify the existing issue acceptance; record exact evidence and limitations.'})
        published[key] = {'number': source['number'], 'id': source['id'], 'url': url}
    return {'schema_version': 1, 'repository': config['repository'], 'issues': issues}, {'project': project, 'issues': published}


def fetch_catalog(root, config, gh):
    project = resolve_github_project(config, gh)
    if config['catalog']['kind'] == 'files':
        catalog = json.loads((root / config['catalog']['backlog']).read_text())
        state = json.loads((root / config['catalog']['state']).read_text())
        if catalog['repository'] != config['repository'] or state['repository'] != config['repository']:
            raise CoordinationError('Catalog/publication repository does not match project config')
        if project['id'] != state['project']['id']:
            raise CoordinationError('Configured Project differs from catalog publication records')
        state['project'] = project
    else:
        catalog, state = github_catalog(config, gh, project)
    for issue in catalog['issues']:
        resources = set(issue.get('resources', []))
        for rule in config.get('resource_rules', []):
            if any(overlaps(path, rule_path) for path in issue['paths'] for rule_path in rule['paths']):
                resources.update(rule['resources'])
        issue['resources'] = sorted(resources)
    return catalog, state, live_snapshot(catalog, state, gh)


def setup_project(root, config, gh, create=False):
    github = config['github']
    if github.get('project_number'):
        project = resolve_github_project(config, gh)
        new = False
    else:
        projects = gh.run('project', 'list', '--owner', github['owner'], '--limit', '1000', '--format', 'json', as_json=True)
        if projects['totalCount'] > len(projects['projects']):
            raise CoordinationError('Incomplete Project discovery')
        matches = [p for p in projects['projects'] if p['title'] == github['project_title']]
        if len(matches) > 1:
            raise CoordinationError('Ambiguous Project title; configure a number explicitly')
        if not matches and not create:
            raise CoordinationError('No matching Project; creation requires --create-if-missing')
        new = not matches
        project = matches[0] if matches else gh.run('project', 'create', '--owner', github['owner'], '--title', github['project_title'], '--format', 'json', as_json=True)
    number = str(project['number'])
    if new:
        gh.run('project', 'edit', number, '--owner', github['owner'], '--visibility', 'PRIVATE')
    gh.run('project', 'link', number, '--owner', github['owner'], '--repo', config['repository'])
    fields = gh.run('project', 'field-list', number, '--owner', github['owner'], '--limit', '100', '--format', 'json', as_json=True)['fields']
    if new:
        status = next(f for f in fields if f['name'] == 'Status')
        gh.graphql('''mutation($input: UpdateProjectV2FieldInput!) {
            updateProjectV2Field(input: $input) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
        }''', {'input': {'fieldId': status['id'], 'singleSelectOptions': [
            {'name': name, 'color': color, 'description': 'Workflow: ' + name}
            for name, color in [('Backlog','GRAY'),('Ready','BLUE'),('In progress','YELLOW'),('Review','PURPLE'),('Blocked','RED'),('Done','GREEN')]]}})
    for name, options in {'Priority': ['P0','P1','P2'], 'Agent': ['Unassigned','Codex','Claude','Other'], 'Model': None, 'Work branch': None}.items():
        if not any(f['name'] == name for f in fields):
            args = ['project','field-create',number,'--owner',github['owner'],'--name',name,'--data-type','SINGLE_SELECT' if options else 'TEXT','--format','json']
            if options:
                args += ['--single-select-options', ','.join(options)]
            gh.run(*args)
    fields = {f['name']: f for f in gh.run('project','field-list',number,'--owner',github['owner'],'--limit','100','--format','json',as_json=True)['fields']}
    required = {'Status': {'Backlog','In progress','Review','Blocked','Done'}, 'Agent': {'Unassigned','Codex','Claude','Other'}}
    for name, values in required.items():
        if not values <= {o['name'] for o in fields[name].get('options',[])}:
            raise CoordinationError('Existing Project workflow differs; preserve it and configure compatible fields explicitly before claiming')
    items = gh.run('project','item-list',number,'--owner',github['owner'],'--limit','1000','--format','json',as_json=True)
    if items['totalCount'] > len(items['items']):
        raise CoordinationError('Incomplete Project ownership initialization')
    for item in items['items']:
        content = item.get('content',{})
        if content.get('repository') != config['repository']:
            continue
        for name, property_name, value in [('Agent','agent','Unassigned'), ('Status','status','Backlog')]:
            if not item.get(property_name):
                set_option(gh,project,item['id'],fields[name],value)
    config['github']['project_number'] = project['number']
    (root / '.atomic-ticketing.json').write_text(json.dumps(config, indent=2) + '\n')
    return project
