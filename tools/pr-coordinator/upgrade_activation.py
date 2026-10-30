#!/usr/bin/env python3
"""Explicit, reviewed-main-only upgrade of the pinned activation supervisor."""
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import subprocess
import tempfile

PREFIX = 'tools/pr-coordinator/'
SHA = re.compile(r'[0-9a-f]{40}')


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError('cannot load activation supervisor')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replace_bytes(path, data, mode=0o644):
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def authority_locks(state):
    # Parents must be stopped first. Release before service readiness waits:
    # the new parents must acquire these same authority locks to become ready.
    with contextlib.ExitStack() as stack:
        for name in ('daemon.lock', 'dispatcher.lock'):
            stream = stack.enter_context((state/name).open('a'))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def read_settings(project_id, home):
    if not isinstance(project_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', project_id):
        raise RuntimeError('invalid activation project')
    config = json.loads((home/'.config/pr-coordinator/config.json').read_text())
    projects = [p for p in config['projects'] if p['id'] == project_id]
    if len(projects) != 1:
        raise RuntimeError('activation project is not configured exactly once')
    project = projects[0]
    expected = dict(project=project_id, root=project['root'], repository=project['repository'],
                    github_account=project['github_account'])
    actual = json.loads((home/'.config/pr-coordinator/activation.json').read_text())
    if actual != expected or not Path(expected['root']).is_absolute():
        raise RuntimeError('installed activation settings do not match configured project')
    return expected


def verify_source(source, target, engine):
    """Authenticate every file before importing any new supervisor implementation."""
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if head != target:
        raise RuntimeError('supervisor upgrade source is not fetched main')
    paths = engine.git('ls-tree', '-r', '--name-only', target, PREFIX).splitlines()
    required = {PREFIX+name for name in ('activation.py', 'upgrade_activation.py', 'install.py',
                'coordinator.py', 'dispatcher.py', 'worker.py', 'batch_integration.py', 'dashboard.py', 'runtime-compatibility.json')}
    if not required <= set(paths):
        raise RuntimeError('reviewed main lacks required supervisor upgrade files')
    expected_names = set()
    verified = {}
    for name in paths:
        relative = Path(name).relative_to(PREFIX)
        expected_names.add(relative.as_posix())
        path = source/relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('supervisor source must contain regular committed files')
        mode = engine.git('ls-tree', target, name).split()[0]
        if mode not in ('100644', '100755'):
            raise RuntimeError('unsupported committed supervisor source mode')
        expected = subprocess.check_output(['git', 'show', target+':'+name], cwd=engine.root)
        if path.read_bytes() != expected:
            raise RuntimeError('supervisor source differs from committed main')
        verified[relative] = expected
    for path in source.rglob('*'):
        if path.is_symlink():
            raise RuntimeError('supervisor source contains an untrusted symlink')
        if path.is_file() and path.relative_to(source).as_posix() not in expected_names:
            if path.suffix == '.pyc' and '__pycache__' in path.relative_to(source).parts:
                continue
            raise RuntimeError('supervisor source contains an uncommitted extra file')
    return verified


@contextlib.contextmanager
def helper_scope(directory):
    # Dynamic verifier imports must come from the corresponding trusted generation,
    # never an already imported or mutable worktree copy of coordinator helpers.
    names = ('coordinator', 'batch_integration', 'activation')
    saved = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    previous = sys.path[:]
    sys.path.insert(0, str(directory))
    try:
        yield
    finally:
        sys.path[:] = previous
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


def checked_generation(link, library):
    if not link.is_symlink():
        raise RuntimeError('activation must already be bootstrapped')
    previous = link.resolve(strict=True)
    if not previous.is_relative_to((library/'generations').resolve()):
        raise RuntimeError('installed generation is outside the activation library')
    installed = json.loads((previous/'installed.json').read_text())
    if not isinstance(installed.get('revision'), str) or not SHA.fullmatch(installed['revision']):
        raise RuntimeError('invalid installed activation revision')
    for name, digest in installed['hashes'].items():
        if Path(name).name != name or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise RuntimeError('invalid installed runtime hash')
        path = previous/name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError('installed runtime differs from its receipt')
    return previous, installed['revision']


def rollback(module, engine, supervisor, receipt, journal):
    backup = Path(receipt['backup'])
    if (not isinstance(receipt['units'], list)
            or len(set(receipt['units'])) != len(receipt['units'])
            or not set(receipt['units']) <= set(module.SERVICES)
            or not re.fullmatch(r'[0-9a-f]{64}', receipt['previous_supervisor_hash'])
            or backup != engine.state/'activation-supervisor-backups'/(receipt['previous_supervisor_hash']+'.py')
            or backup.is_symlink()):
        raise RuntimeError('invalid supervisor rollback receipt')
    data = backup.read_bytes()
    if hashlib.sha256(data).hexdigest() != receipt['previous_supervisor_hash']:
        raise RuntimeError('supervisor rollback backup does not match receipt')
    dashboard_data = None
    if receipt.get('dashboard_backup') is not None:
        dashboard_backup = Path(receipt['dashboard_backup'])
        dashboard_hash = receipt['previous_dashboard_hash']
        if (not re.fullmatch(r'[0-9a-f]{64}', dashboard_hash)
                or dashboard_backup != engine.state/'activation-supervisor-backups'/(dashboard_hash+'.dashboard.py')
                or dashboard_backup.is_symlink()):
            raise RuntimeError('invalid dashboard rollback receipt')
        dashboard_data = dashboard_backup.read_bytes()
        if hashlib.sha256(dashboard_data).hexdigest() != dashboard_hash:
            raise RuntimeError('dashboard rollback backup does not match receipt')
    previous = Path(receipt['previous']).resolve(strict=True)
    if not previous.is_dir() or not previous.is_relative_to((engine.library/'generations').resolve()):
        raise RuntimeError('supervisor rollback generation is unavailable')
    engine.services.stop(receipt['units'])
    with authority_locks(engine.state):
        module.switch(engine.link, previous)
        replace_bytes(supervisor, data, receipt['previous_supervisor_mode'])
        dashboard = engine.library/'dashboard.py'
        if dashboard_data is None:
            dashboard.unlink(missing_ok=True)
        else:
            replace_bytes(dashboard, dashboard_data, receipt['previous_dashboard_mode'])
    engine.services.start(receipt['units'], previous)
    failed = {**receipt, 'phase': 'rolled_back', 'error': 'supervisor upgrade failed; inspect before retry'}
    module.atomic(journal, failed)
    module.atomic(engine.journal, {**failed, 'action': 'Inspect supervisor upgrade before retrying this revision'})


def upgrade(project_id, source, home):
    source, home = Path(source).resolve(), Path(home).resolve()
    settings = read_settings(project_id, home)
    library, state = home/'.local/lib/pr-coordinator', home/'.local/state/pr-coordinator'
    supervisor = library/'activation.py'
    dashboard = library/'dashboard.py'
    if supervisor.is_symlink() or not supervisor.is_file() or not state.is_dir():
        raise RuntimeError('existing trusted activation supervisor is required')
    with (state/'activation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old_module = load_module(supervisor, '_installed_activation_supervisor')
        old_engine = old_module.Activator(settings, library, state)
        journal = state/'activation-supervisor-upgrade.json'
        if journal.exists():
            prior = json.loads(journal.read_text())
            if prior.get('phase') == 'pending':
                # Recovery restores the verified saved supervisor and old generation,
                # then stops. Never silently retry a possibly interrupted upgrade.
                rollback(old_module, old_engine, supervisor, prior, journal)
                raise RuntimeError('interrupted supervisor upgrade rolled back; inspect before retry')
        if old_engine.journal.exists() and json.loads(old_engine.journal.read_text()).get('phase') == 'pending':
            raise RuntimeError('normal activation is pending; recover it before upgrading supervisor')
        previous, old_revision = checked_generation(old_engine.link, library)
        old_bytes = supervisor.read_bytes()
        old_mode = stat.S_IMODE(supervisor.stat().st_mode)
        if dashboard.is_symlink() or (dashboard.exists() and not dashboard.is_file()):
            raise RuntimeError('installed dashboard must be a regular file')
        old_dashboard = dashboard.read_bytes() if dashboard.exists() else None
        old_dashboard_mode = stat.S_IMODE(dashboard.stat().st_mode) if dashboard.exists() else None
        target = old_engine.tip()
        if journal.exists():
            prior = json.loads(journal.read_text())
            if prior.get('phase') == 'rolled_back' and prior.get('target') == target:
                raise RuntimeError('revision failed supervisor upgrade; inspect receipt before retry')
        verified = verify_source(source, target, old_engine)
        # This is the decisive authorization boundary: new code cannot authorize
        # its own installation. The installed supervisor must accept all changes.
        with helper_scope(previous):
            old_engine.verify(old_revision, target)
        with tempfile.TemporaryDirectory(prefix='verified-supervisor-', dir=state) as temporary:
            verified_source = Path(temporary)
            for relative, data in verified.items():
                path = verified_source/relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            new_module = load_module(verified_source/'activation.py', '_reviewed_activation_supervisor')
            new_engine = new_module.Activator(settings, library, state, services=old_engine.services)
            with helper_scope(verified_source):
                new_engine.verify(old_revision, target)
            new_engine.validate(target)
            generation = new_engine.stage(target)
            if old_engine.tip() != target:
                raise RuntimeError('main moved during supervisor upgrade validation')
            new_bytes = verified[Path('activation.py')]
            if previous == generation and old_bytes == new_bytes and old_dashboard == verified[Path('dashboard.py')]:
                return dict(installed=target, supervisor='unchanged')
            units = old_engine.services.active()  # Validates KillMode=process before stop.
            backups = state/'activation-supervisor-backups'
            backups.mkdir(mode=0o700, exist_ok=True)
            digest = hashlib.sha256(old_bytes).hexdigest()
            backup = backups/(digest+'.py')
            if backup.exists() and backup.read_bytes() != old_bytes:
                raise RuntimeError('existing supervisor backup differs from its hash')
            if not backup.exists():
                replace_bytes(backup, old_bytes, 0o600)
            dashboard_hash = hashlib.sha256(old_dashboard).hexdigest() if old_dashboard is not None else None
            dashboard_backup = backups/(dashboard_hash+'.dashboard.py') if dashboard_hash else None
            if dashboard_backup:
                if dashboard_backup.exists() and dashboard_backup.read_bytes() != old_dashboard:
                    raise RuntimeError('existing dashboard backup differs from its hash')
                if not dashboard_backup.exists():
                    replace_bytes(dashboard_backup, old_dashboard, 0o600)
            receipt = dict(phase='pending', previous=str(previous), generation=str(generation),
                           previous_revision=old_revision, target=target, units=units, backup=str(backup),
                           previous_supervisor_hash=digest, previous_supervisor_mode=old_mode,
                           dashboard_backup=str(dashboard_backup) if dashboard_backup else None,
                           previous_dashboard_hash=dashboard_hash, previous_dashboard_mode=old_dashboard_mode,
                           supervisor_upgrade=True)
            new_module.atomic(journal, receipt)
            try:
                old_engine.services.stop(units)
                with authority_locks(state):
                    current, revision = checked_generation(old_engine.link, library)
                    if current != previous or revision != old_revision or supervisor.read_bytes() != old_bytes:
                        raise RuntimeError('installed runtime changed before supervisor replacement')
                    if (dashboard.read_bytes() if dashboard.exists() else None) != old_dashboard:
                        raise RuntimeError('installed dashboard changed before replacement')
                    if old_engine.tip() != target:
                        raise RuntimeError('main moved before supervisor replacement')
                    new_module.switch(new_engine.link, generation)
                    replace_bytes(supervisor, new_bytes)
                    replace_bytes(dashboard, verified[Path('dashboard.py')])
                new_engine.services.start(units, generation)
                installed = {**receipt, 'phase': 'installed'}
                new_module.atomic(new_engine.journal, installed)
                new_module.atomic(journal, installed)
            except BaseException:
                rollback(old_module, old_engine, supervisor, receipt, journal)
                raise
            return dict(installed=target, supervisor='upgraded', activation=installed)
