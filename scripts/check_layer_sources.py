#!/usr/bin/env python3
"""Audit meta-suifleet source pins and, optionally, fetch them anonymously.

Static checks (always):
  * no AUTOREV; git sources use https and a 40-hex SRCREV;
  * https sources carry a SHA-256 and are immutable: no query strings or
    credentials, no branch/tag archives, no raw/codeload URL at a non-commit
    ref, and no expiring release-asset redirect hosts;
  * every crate:// entry has a SHA-256;
  * file:// sources exist inside the layer (no FILESEXTRAPATHS outside it);
  * no runtime identity/state files in the layer or its export list;
  * coupled SuiFleet pins agree: PV equals the crate version, the codec source
    revision equals the daemon SRCREV and a commit named in a source archive
    filename equals SRCREV.

--repo checks SRCREV in a Git checkout: Cargo.lock digest, crate version,
LICENSE md5 and the generated crate include (names, versions and checksums).

--fetch downloads every non-crate source with no credentials into a new
directory, verifies its SHA-256 before unpacking, unpacks it as BitBake would
(subdir=/destsuffix=), copies layer file:// entries and checks the resulting
S: license checksums, the daemon lockfile/version/crate include/workspace
members/include targets, and the codec's exact pinned file set and provenance.
--cargo additionally runs `cargo metadata --offline --locked --no-deps`.

The JSON report maps each recipe to its version, source revision, sources,
checksums and licenses. Parsing here is a source audit, not a BitBake build.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

HEX40 = re.compile(r'^[0-9a-f]{40}$')
HEX64 = re.compile(r'^[0-9a-f]{64}$')
ASSIGN = re.compile(r'^(?:export\s+)?([A-Za-z0-9_\-${}.:/+]+?)(?:\[([^\]]+)\])?\s*(\?\?=|\?=|:=|\+=|=\+|\.=|=\.|=)\s*"(.*)"\s*$', re.S)
REF = re.compile(r'\$\{([A-Za-z0-9_\-]+)\}')
# The layer's Scarthgap/Wrynose unpack-root idiom; no other inline Python is evaluated.
UNPACKDIR_EXPR = re.compile(r"""\$\{@d\.getVar\(['"]UNPACKDIR['"]\)\s+or\s+d\.getVar\(['"]WORKDIR['"]\)\}""")
TARBALL = re.compile(r'\.(tar(\.(gz|xz|bz2|zst))?|tgz|tbz2|txz)$')
MUTABLE_URL = (
    (re.compile(r'^https://github\.com/[^/]+/[^/]+/archive/(?!([0-9a-f]{40})(\.tar\.gz|\.zip)$)'), 'GitHub archive of a branch/tag'),
    (re.compile(r'^https://codeload\.github\.com/[^/]+/[^/]+/[^/]+/(?![0-9a-f]{40}$)'), 'codeload archive of a branch/tag'),
    (re.compile(r'^https://raw\.githubusercontent\.com/[^/]+/[^/]+/(?![0-9a-f]{40}/)'), 'raw file at a branch/tag'),
    (re.compile(r'/releases/latest(/|$)'), 'latest-release redirect'),
    (re.compile(r'^https://(objects|release-assets|github-releases)\.githubusercontent\.com/'), 'expiring release-asset URL'),
)
FORBIDDEN_NAME = re.compile(
    r'(^|/)(device\.key|signer\.key|config\.toml|firmware\.toml|signer\.toml|enrollment(\.[a-z]+)?|'
    r'\.env|\.netrc|id_(rsa|ecdsa|ed25519)|[^/]*\.(pem|p12|pfx|key)|[^/]*journal[^/]*|[^/]*\.sqlite3?|[^/]*\.db)$')


def load_packager():
    spec = importlib.util.spec_from_file_location('package_edge_source', Path(__file__).with_name('package_edge_source.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Recipe:
    """A deliberately small BitBake reader: assignments, flags and require."""

    def __init__(self, path, layer):
        self.path, self.layer = Path(path), Path(layer)
        stem = self.path.name.rsplit('.', 1)[0]
        pn, _, pv = stem.partition('_')
        self.vars = {'PN': pn, 'BPN': pn, 'PV': pv or '1.0'}
        self.flags = {}
        self.text = ''
        self.read(self.path)
        self.vars.setdefault('BP', '${BPN}-${PV}')

    def read(self, path):
        text = path.read_text()
        self.text += text
        lines, block = [], False
        for line in text.replace('\\\n', ' ').splitlines():
            stripped = line.strip()
            if block:
                block = stripped != '}'
                continue
            if stripped.endswith('{') and ('()' in stripped or stripped.startswith('python')):
                block = True
                continue
            lines.append(stripped)
        for line in lines:
            if not line or line.startswith('#'):
                continue
            words = line.split()
            if words[0] in ('require', 'include'):
                target = self.path.parent / self.expand(words[1])
                if not target.is_file() and words[0] == 'require':
                    raise ValueError(f'{self.path.name}: required file missing: {words[1]}')
                if target.is_file():
                    self.read(target)
                continue
            match = ASSIGN.match(line)
            if not match:
                continue
            name, flag, op, value = match.groups()
            if flag:
                self.flags.setdefault(name, {})[flag] = value
                continue
            base, _, override = name.partition(':')
            if override and override.split(':')[0] not in ('append', 'prepend', 'remove'):
                continue
            old = self.vars.get(base, '')
            if override.startswith('append') or op in ('+=', '.='):
                self.vars[base] = old + (' ' if op == '+=' else '') + value
            elif override.startswith('prepend') or op in ('=+', '=.'):
                self.vars[base] = value + (' ' if op == '=+' else '') + old
            elif override.startswith('remove'):
                continue
            elif op in ('?=', '??='):
                self.vars.setdefault(base, value)
            else:
                self.vars[base] = value

    def expand(self, value, depth=0):
        if depth > 20:
            raise ValueError(f'{self.path.name}: recursive variable expansion')
        if 'UNPACKDIR' in self.vars:
            value = UNPACKDIR_EXPR.sub('${UNPACKDIR}', value)
        expanded = REF.sub(lambda m: self.vars[m.group(1)] if m.group(1) in self.vars else m.group(0), value)
        return expanded if expanded == value else self.expand(expanded, depth + 1)

    def get(self, name, default=''):
        return self.expand(self.vars.get(name, default))

    def sources(self):
        for entry in self.get('SRC_URI').split():
            url, *params = entry.split(';')
            yield url, dict(p.split('=', 1) for p in params if '=' in p)


def checksum_for(recipe, params, default_name=None):
    if 'sha256sum' in params:
        return params['sha256sum']
    name = params.get('name') or default_name
    flags = recipe.flags.get('SRC_URI', {})
    return recipe.expand(flags.get(f'{name}.sha256sum' if name else 'sha256sum', ''))


def runtime_state_errors(layer):
    errors = []
    for path in sorted(Path(layer).rglob('*')):
        rel = path.relative_to(layer).as_posix()
        if path.is_file() and FORBIDDEN_NAME.search(rel):
            errors.append(f'runtime identity/state-like file in layer: {rel}')
    export = Path(layer) / 'export-files.json'
    if export.is_file():
        for rel in json.loads(export.read_text()):
            if FORBIDDEN_NAME.search(rel):
                errors.append(f'export-files.json lists runtime identity/state-like file: {rel}')
            elif not (Path(layer) / rel).is_file():
                errors.append(f'export-files.json lists a missing file: {rel}')
    return errors


def file_source(recipe, name):
    base = recipe.path.parent
    for directory in (recipe.get('BP'), recipe.get('BPN'), 'files', '.'):
        candidate = (base / directory / name).resolve()
        if candidate.is_file():
            return candidate
    return None


def audit_recipe(recipe):
    errors, sources = [], []
    where = recipe.path.relative_to(recipe.layer).as_posix()
    if 'AUTOREV' in recipe.text:
        errors.append(f'{where}: AUTOREV is forbidden')
    extra = recipe.get('FILESEXTRAPATHS')
    for directory in filter(None, extra.replace(':', ' ').split()):
        resolved = Path(directory).resolve() if Path(directory).is_absolute() else None
        if '${' in directory or resolved is None or recipe.layer.resolve() not in (resolved, *resolved.parents):
            errors.append(f'{where}: FILESEXTRAPATHS points outside the layer: {directory}')
    srcrev = recipe.get('SRCREV')
    for url, params in recipe.sources():
        scheme = url.split('://', 1)[0]
        entry = {'url': url, 'type': scheme}
        parsed = urllib.parse.urlsplit(url.replace('git://', 'https://', 1) if scheme in ('git', 'gitsm') else url)
        if parsed.username or parsed.password or '@' in parsed.netloc:
            errors.append(f'{where}: credentials in source URL {url}')
        if scheme in ('git', 'gitsm'):
            name = params.get('name')
            rev = recipe.get(f'SRCREV_{name}') if name else srcrev
            entry['revision'] = rev
            if params.get('protocol', 'git') != 'https':
                errors.append(f'{where}: git source must use protocol=https: {url}')
            if not HEX40.match(rev or ''):
                errors.append(f'{where}: git source needs a 40-hex SRCREV, got {rev!r}')
        elif scheme in ('https', 'http', 'ftp'):
            sha = checksum_for(recipe, params)
            entry['sha256'] = sha
            if scheme != 'https':
                errors.append(f'{where}: source must use https: {url}')
            if not HEX64.match(sha):
                errors.append(f'{where}: https source lacks a SHA-256 pin: {url}')
            if parsed.query or parsed.fragment:
                errors.append(f'{where}: query/fragment (signed or expiring URL) in {url}')
            for pattern, label in MUTABLE_URL:
                if pattern.search(url):
                    errors.append(f'{where}: mutable or expiring source ({label}): {url}')
            commits = re.findall(r'(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])', PurePosixPath(parsed.path).name)
            if commits and HEX40.match(srcrev or '') and commits[-1] != srcrev:
                errors.append(f'{where}: archive {PurePosixPath(parsed.path).name} names commit {commits[-1]}, SRCREV is {srcrev}')
        elif scheme == 'crate':
            parts = url.split('/')
            sha = checksum_for(recipe, params, f'{parts[-2]}-{parts[-1]}')
            if not HEX64.match(sha):
                errors.append(f'{where}: crate {parts[-2]} {parts[-1]} lacks a SHA-256')
            continue
        elif scheme == 'file':
            name = url[len('file://'):]
            if name.startswith('/') or '..' in PurePosixPath(name).parts:
                errors.append(f'{where}: file:// source escapes the layer: {name}')
            elif file_source(recipe, recipe.expand(name)) is None:
                errors.append(f'{where}: file:// source is not in the layer: {name}')
            continue
        else:
            errors.append(f'{where}: unsupported fetcher {scheme}: {url}')
        sources.append(entry)
    crate_version = recipe.get('SUIFLEET_CRATE_VERSION')
    if crate_version and recipe.get('PV') != crate_version:
        errors.append(f'{where}: PV {recipe.get("PV")!r} differs from SUIFLEET_CRATE_VERSION {crate_version!r}')
    return errors, sources


def lock_packages(lock):
    """Return {(name, version): checksum} for registry packages in Cargo.lock."""
    packages = {}
    for block in lock.split('[[package]]')[1:]:
        fields = dict(re.findall(r'^(\w+) = "([^"]*)"', block, re.M))
        if fields.get('source', '').startswith('registry+'):
            packages[(fields['name'], fields['version'])] = fields.get('checksum', '')
        elif fields.get('source'):
            packages[(fields['name'], fields['version'])] = 'unsupported:' + fields['source']
    return packages


def crate_include(recipe):
    flags = recipe.flags.get('SRC_URI', {})
    entries = {}
    for url, params in recipe.sources():
        if url.startswith('crate://'):
            name, version = url.split('/')[-2:]
            entries[(name, version)] = recipe.expand(flags.get(f'{name}-{version}.sha256sum', ''))
    return entries


def license_md5s(recipe):
    for entry in recipe.get('LIC_FILES_CHKSUM').split():
        url, *params = entry.split(';')
        options = dict(p.split('=', 1) for p in params if '=' in p)
        if set(options) - {'md5'}:
            raise ValueError(f'{recipe.path.name}: unsupported LIC_FILES_CHKSUM options {sorted(options)}')
        yield url[len('file://'):], options.get('md5', '')


def daemon_errors(recipe, read, exists=None):
    """Check coupled daemon pins with `read(relpath)` returning bytes or None."""
    errors = []
    src = recipe.get('CARGO_SRC_DIR', 'suifleet-edge')
    lock = read(f'{src}/Cargo.lock')
    manifest = read(f'{src}/Cargo.toml')
    if lock is None or manifest is None:
        return [f'{recipe.path.name}: source lacks {src}/Cargo.lock or Cargo.toml']
    if hashlib.sha256(lock).hexdigest() != recipe.get('SUIFLEET_LOCK_SHA256'):
        errors.append(f'{recipe.path.name}: Cargo.lock sha256 {hashlib.sha256(lock).hexdigest()} differs from SUIFLEET_LOCK_SHA256')
    packager = load_packager()
    version = packager.crate_version(manifest.decode())
    if version != recipe.get('SUIFLEET_CRATE_VERSION'):
        errors.append(f'{recipe.path.name}: Cargo.toml version {version} differs from SUIFLEET_CRATE_VERSION')
    locked, included = lock_packages(lock.decode()), crate_include(recipe)
    if any(v.startswith('unsupported:') for v in locked.values()):
        errors.append(f'{recipe.path.name}: Cargo.lock has non-registry sources the crate include cannot pin')
    missing = sorted(set(locked) - set(included))
    extra = sorted(set(included) - set(locked))
    wrong = sorted(k for k in set(locked) & set(included) if locked[k] != included[k])
    for label, items in (('missing from', missing), ('not in Cargo.lock but in', extra), ('checksum differs in', wrong)):
        if items:
            errors.append(f'{recipe.path.name}: {len(items)} crates {label} suifleet-crates.inc, e.g. {items[:3]}; regenerate with bitbake -c update_crates')
    for rel, md5 in license_md5s(recipe):
        data = read(rel)
        if data is None or hashlib.md5(data).hexdigest() != md5:
            errors.append(f'{recipe.path.name}: license file {rel} does not match md5 {md5}')
    if exists is not None:
        for member in packager.workspace_members(manifest.decode()):
            if not exists(f'{src}/{member}/Cargo.toml'):
                errors.append(f'{recipe.path.name}: workspace member {src}/{member} is missing from the source layout')
    return errors


def codec_errors(recipe, s_dir):
    errors, pins = [], {k: recipe.expand(v) for k, v in recipe.flags.get('SUIFLEET_CODEC_SHA256', {}).items()}
    names = sorted(p.name for p in Path(s_dir).iterdir()) if Path(s_dir).is_dir() else []
    if names != sorted(pins):
        return [f'{recipe.path.name}: unpacked codec has {names}, recipe pins {sorted(pins)}']
    for name, sha in pins.items():
        if hashlib.sha256((Path(s_dir) / name).read_bytes()).hexdigest() != sha:
            errors.append(f'{recipe.path.name}: codec file {name} differs from its pin')
    provenance = json.loads((Path(s_dir) / 'provenance.json').read_text())
    manifest = json.loads((Path(s_dir) / 'manifest.json').read_text())
    if provenance.get('source_revision') != recipe.get('SUIFLEET_CODEC_SOURCE_REV'):
        errors.append(f'{recipe.path.name}: codec provenance revision {provenance.get("source_revision")} differs from SUIFLEET_CODEC_SOURCE_REV')
    if ' & '.join(provenance.get('licenses') or []) != recipe.get('LICENSE'):
        errors.append(f'{recipe.path.name}: codec provenance licenses differ from LICENSE')
    if manifest.get('node') != recipe.get('SUIFLEET_CODEC_NODE_VERSION'):
        errors.append(f'{recipe.path.name}: codec manifest Node version differs from SUIFLEET_CODEC_NODE_VERSION')
    for rel, md5 in license_md5s(recipe):
        path = Path(s_dir) / rel
        if not path.is_file() or hashlib.md5(path.read_bytes()).hexdigest() != md5:
            errors.append(f'{recipe.path.name}: license file {rel} does not match md5 {md5}')
    return errors


def anonymous_env(home):
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'https_proxy', 'HTTPS_PROXY', 'no_proxy', 'NO_PROXY')}
    env.update(HOME=str(home), GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false', SSH_ASKPASS='/bin/false',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    return env


def download(url, target):
    # urllib sends no Authorization header and never reads ~/.netrc.
    request = urllib.request.Request(url, headers={'User-Agent': 'meta-suifleet-source-audit'})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=120) as response, open(target, 'xb') as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def safe_extract(archive, destination):
    destination = Path(destination).resolve()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if destination not in (target, *target.parents):
                raise ValueError(f'{archive.name}: member escapes the unpack directory: {member.name}')
            if member.issym() or member.islnk():
                base = target.parent if member.issym() else destination
                link = (base / member.linkname).resolve()
                if member.linkname.startswith('/') or destination not in (link, *link.parents):
                    raise ValueError(f'{archive.name}: link escapes the unpack directory: {member.name}')
            elif not (member.isfile() or member.isdir()):
                raise ValueError(f'{archive.name}: unsupported member type: {member.name}')
        links = []
        for member in tar.getmembers():
            target = destination / member.name
            if member.issym() or member.islnk():
                links.append((member, target))
            elif member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, open(target, 'wb') as out:
                    shutil.copyfileobj(source, out)
                target.chmod(member.mode & 0o755 | 0o600)
        for member, target in links:
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                target.symlink_to(member.linkname)
            else:
                os.link(destination / member.linkname, target)


def fetch_recipe(recipe, workdir, cargo=None):
    errors, fetched = [], []
    unpack = Path(workdir) / recipe.get('PN')
    downloads = Path(workdir) / 'downloads'
    unpack.mkdir(parents=True)
    downloads.mkdir(exist_ok=True)
    recipe.vars.update(UNPACKDIR=str(unpack), WORKDIR=str(unpack))
    for url, params in recipe.sources():
        scheme = url.split('://', 1)[0]
        if scheme == 'crate':
            continue
        if scheme == 'file':
            source = file_source(recipe, recipe.expand(url[len('file://'):]))
            if source:
                destination = unpack / params.get('subdir', '') / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            continue
        if scheme in ('git', 'gitsm'):
            rev = recipe.get(f'SRCREV_{params["name"]}') if 'name' in params else recipe.get('SRCREV')
            destination = unpack / params.get('destsuffix', 'git')
            remote = 'https://' + url.split('://', 1)[1]
            env = anonymous_env(Path(workdir))
            for command in (['init', '-q', str(destination)], ['-C', str(destination), 'fetch', '-q', '--depth', '1', remote, rev],
                            ['-C', str(destination), 'checkout', '-q', '--detach', 'FETCH_HEAD']):
                subprocess.run(['git', '-c', 'credential.helper=', *command], env=env, check=True)
            actual = subprocess.check_output(['git', '-C', str(destination), 'rev-parse', 'HEAD'], env=env, text=True).strip()
            fetched.append({'url': url, 'revision': actual, 'verified': actual == rev})
            if actual != rev:
                errors.append(f'{recipe.path.name}: fetched {actual}, SRCREV is {rev}')
            continue
        name = PurePosixPath(urllib.parse.urlsplit(url).path).name
        target = downloads / name
        expected = checksum_for(recipe, params)
        actual = download(url, target)
        fetched.append({'url': url, 'sha256': actual, 'verified': actual == expected, 'bytes': target.stat().st_size})
        if actual != expected:
            errors.append(f'{recipe.path.name}: {name} sha256 {actual} differs from pin {expected}')
            continue
        destination = unpack / params.get('subdir', '')
        destination.mkdir(parents=True, exist_ok=True)
        if TARBALL.search(name) and params.get('unpack', '1') == '1':
            safe_extract(target, destination)
        else:
            shutil.copyfile(target, destination / name)
    s_dir = Path(recipe.get('S'))
    if '${' in str(s_dir) or unpack.resolve() not in (s_dir.resolve(), *s_dir.resolve().parents):
        return errors + [f'{recipe.path.name}: cannot resolve S inside the unpack directory: {s_dir}'], fetched
    if not s_dir.is_dir():
        return errors + [f'{recipe.path.name}: S does not exist after unpack: {s_dir.relative_to(unpack)}'], fetched
    if recipe.get('SUIFLEET_LOCK_SHA256'):
        def read(rel):
            path = s_dir / rel
            return path.read_bytes() if path.is_file() else None
        errors += daemon_errors(recipe, read, lambda rel: (s_dir / rel).is_file())
        packager = load_packager()
        files = {p.relative_to(s_dir).as_posix(): ('100644', 'blob', None) for p in s_dir.rglob('*') if p.is_file()}
        for rs in sorted(p for p in files if p.endswith('.rs')):
            try:
                for target in packager.include_targets(rs, (s_dir / rs).read_bytes()):
                    if target not in files:
                        errors.append(f'{recipe.path.name}: {rs} references {target}, absent from the source layout')
            except ValueError as error:
                errors.append(f'{recipe.path.name}: {error}')
        if cargo:
            result = subprocess.run([*cargo.split(), 'metadata', '--offline', '--locked', '--no-deps', '--format-version', '1'],
                                    cwd=s_dir / recipe.get('CARGO_SRC_DIR', 'suifleet-edge'), capture_output=True, text=True,
                                    env={**anonymous_env(Path(workdir)), 'CARGO_HOME': str(Path(workdir) / 'cargo-home'),
                                         'RUSTUP_HOME': os.environ.get('RUSTUP_HOME', str(Path.home() / '.rustup'))})
            if result.returncode:
                errors.append(f'{recipe.path.name}: cargo metadata failed: {result.stderr.strip().splitlines()[:3]}')
    elif recipe.flags.get('SUIFLEET_CODEC_SHA256'):
        errors += codec_errors(recipe, s_dir)
    else:
        for rel, md5 in license_md5s(recipe):
            path = s_dir / rel
            if not path.is_file() or hashlib.md5(path.read_bytes()).hexdigest() != md5:
                errors.append(f'{recipe.path.name}: license file {rel} does not match md5 {md5}')
    return errors, fetched


def check(layer, repo=None, fetch=False, cargo=None, only=None):
    layer = Path(layer).resolve()
    report = {'layer': str(layer), 'errors': runtime_state_errors(layer), 'recipes': {}}
    conf = (layer / 'conf' / 'layer.conf').read_text()
    if 'AUTOREV' in conf:
        report['errors'].append('conf/layer.conf: AUTOREV is forbidden')
    series = re.search(r'^LAYERSERIES_COMPAT_\S+\s*=\s*"([^"]*)"', conf, re.M)
    report['series'] = series.group(1).split() if series else []
    recipes = {}
    for path in sorted(layer.glob('recipes-*/*/*.bb')) + sorted(layer.glob('recipes-*/*/*.bbappend')):
        recipe = Recipe(path, layer)
        if only and recipe.get('PN') not in only:
            continue
        recipes[recipe.get('PN')] = recipe
        errors, sources = audit_recipe(recipe)
        report['errors'] += errors
        report['recipes'][recipe.get('PN')] = {
            'file': path.relative_to(layer).as_posix(), 'pv': recipe.get('PV'), 'license': recipe.get('LICENSE'),
            'license_files': dict(license_md5s(recipe)), 'sources': sources,
            'source_revision': recipe.get('SRCREV') or recipe.get('SUIFLEET_CODEC_SOURCE_REV') or None,
            'crate_version': recipe.get('SUIFLEET_CRATE_VERSION') or None,
            'lock_sha256': recipe.get('SUIFLEET_LOCK_SHA256') or None,
            'crates': len(crate_include(recipe)) or None,
        }
    daemon = next((r for r in recipes.values() if r.get('SUIFLEET_LOCK_SHA256')), None)
    codec = next((r for r in recipes.values() if r.get('SUIFLEET_CODEC_SOURCE_REV')), None)
    if daemon and codec and daemon.get('SRCREV') != codec.get('SUIFLEET_CODEC_SOURCE_REV'):
        report['errors'].append(f'codec SUIFLEET_CODEC_SOURCE_REV {codec.get("SUIFLEET_CODEC_SOURCE_REV")} differs from daemon SRCREV {daemon.get("SRCREV")}')
    if repo and daemon:
        rev = daemon.get('SRCREV')

        def read(rel):
            result = subprocess.run(['git', '-C', str(repo), 'show', f'{rev}:{rel}'], capture_output=True)
            return result.stdout if result.returncode == 0 else None
        if read('LICENSE') is None and subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', f'{rev}^{{commit}}'],
                                                       capture_output=True).returncode:
            report['errors'].append(f'SRCREV {rev} is not in {repo}')
        else:
            report['errors'] += daemon_errors(daemon, read, lambda rel: read(rel) is not None)
    if fetch:
        workdir = Path(tempfile.mkdtemp(prefix='meta-suifleet-fetch-'))
        report['fetch_dir'] = str(workdir)
        for name, recipe in recipes.items():
            try:
                errors, fetched = fetch_recipe(recipe, workdir, cargo)
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                errors, fetched = [f'{recipe.path.name}: fetch/unpack failed: {error}'], []
            report['errors'] += errors
            report['recipes'][name]['fetched'] = fetched
    report['ok'] = not report['errors']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument('--layer', type=Path, default=root / 'suifleet-edge' / 'meta-suifleet')
    parser.add_argument('--repo', type=Path, help='Git checkout containing the daemon SRCREV')
    parser.add_argument('--fetch', action='store_true', help='anonymously fetch, verify and unpack every source')
    parser.add_argument('--cargo', help='cargo command for the workspace check, e.g. "cargo +1.94.0"')
    parser.add_argument('--recipe', action='append', help='limit to these PNs (repeatable)')
    args = parser.parse_args()
    report = check(args.layer, args.repo, args.fetch, args.cargo, args.recipe)
    print(json.dumps(report, indent=2, sort_keys=True))
    sys.exit(0 if report['ok'] else 1)


if __name__ == '__main__':
    main()
