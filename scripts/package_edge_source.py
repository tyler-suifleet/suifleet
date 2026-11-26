#!/usr/bin/env python3
"""Build the immutable daemon source distribution fetched by the meta-suifleet recipe.

The archive is produced from committed Git objects only (never a working tree):

    git archive --format=tar REVISION -- <paths> | gzip -n -6

Paths are the daemon build closure: the root LICENSE, the Cargo manifest and
lockfile, the crate sources and examples, every Cargo workspace member, every
local crate reached through `path` dependencies (transitively), every
file a manifest declares explicitly (`package.build` and each target `path`) and
each file named by a literal ``include_str!``/``include_bytes!`` in those
sources, or by a `#[path]` module attribute (resolved transitively). The closure is computed from the revision's
tree, so a missing workspace member or include target is an error instead of a
broken release. Ownership, timestamps (the commit time) and modes come from
``git archive``; ``gzip -n`` omits the name and time, so rebuilding the same
revision with the same git/gzip produces byte-identical output.

Before anything is written, the tar stream is checked against the plan: it
must contain exactly the planned regular files (plus their directories), each
with the committed blob's bytes and executable bit. A committed
``export-ignore`` or ``export-subst`` attribute that drops or rewrites a build
input is therefore an error instead of a silently incomplete archive.

Runtime identities, keys, enrollment configuration and journals must never be
in the archive; files whose names look like runtime state are refused. Content
is not pattern-scanned here because the redaction test vectors deliberately
contain fake private keys: build only from reviewed main, which the repository
secret-scanning gates already cover (docs/SECURITY_CI.md).
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

CRATE = 'suifleet-edge'
ROOTS = (
    'LICENSE',
    f'{CRATE}/Cargo.toml',
    f'{CRATE}/Cargo.lock',
    f'{CRATE}/config.example.toml',
    f'{CRATE}/build.rs',
    f'{CRATE}/src/',
    f'{CRATE}/examples/',
)
REQUIRED = ('LICENSE', f'{CRATE}/Cargo.toml', f'{CRATE}/Cargo.lock')
INCLUDE = re.compile(rb'include_(?:str|bytes)!\(\s*"([^"\\]+)"\s*\)|#\[path\s*=\s*"([^"\\]+)"\s*\]')
# An out-of-line module declaration (`mod name;`) with its leading attributes.
MODULE = re.compile(rb'((?:#\[[^\]]*\]\s*)*)(?<![A-Za-z0-9_])(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+(?:r#)?([A-Za-z_][A-Za-z0-9_]*)\s*;')
PATH_ATTR = re.compile(rb'#\[path\s*=\s*"[^"\\]+"\s*\]')
# Runtime identity/state names that must never be distributed.
FORBIDDEN_NAME = re.compile(
    r'(^|/)(device\.key|signer\.key|config\.toml|firmware\.toml|signer\.toml|'
    r'enrollment(\.[a-z]+)?|\.env|\.netrc|id_(rsa|ecdsa|ed25519)|[^/]*\.(pem|p12|pfx|key)|'
    r'[^/]*journal[^/]*|[^/]*\.sqlite3?|[^/]*\.db)$')
GZIP_LEVEL = '-6'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def tree(repo, revision):
    """Map every path in the commit to (mode, type, object)."""
    entries = {}
    for record in git(repo, 'ls-tree', '-r', '-z', '--full-tree', revision).split(b'\0'):
        if not record:
            continue
        meta, path = record.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        entries[path.decode()] = (mode, kind, oid)
    return entries


class _Scalar(str):
    """A non-string TOML value (boolean, number, date), kept as its raw text."""


class _InlineTable(dict):
    """An inline table; TOML forbids extending it later."""


class _TableArray(list):
    """An array of tables (`[[name]]`)."""


TOML_BARE_KEY = re.compile(r'[A-Za-z0-9_-]+')
TOML_SCALAR = re.compile(r'[0-9A-Za-z_+.:-]+')
TOML_ESCAPES = {'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r', '"': '"', '\\': '\\'}


def parse_toml(text):
    """Parse a Cargo manifest into nested dicts, or raise ValueError.

    Python 3.10 (the dev container) has no tomllib, so this is a strict reader
    for the TOML subset Cargo manifests use: tables, arrays of tables, dotted
    and quoted keys (escapes decoded), single-line basic and literal strings,
    arrays, inline tables and raw scalars. Keys are compared after decoding, so
    `"members"` and `["workspace"]` mean what TOML says they mean.
    Multi-line strings and anything else outside the subset raise ValueError
    instead of being skipped, so a declaration is never silently missed."""
    i, n = 0, len(text)

    def fail(message):
        line = text.count('\n', 0, i) + 1
        raise ValueError(f'unsupported Cargo.toml syntax at line {line}: {message} near {text[i:i + 40]!r}')

    def blank():
        nonlocal i
        while i < n and text[i] in ' \t':
            i += 1

    def blank_lines():
        nonlocal i
        while i < n:
            if text[i] in ' \t\r\n':
                i += 1
            elif text[i] == '#':
                end = text.find('\n', i)
                i = n if end < 0 else end
            else:
                break

    def end_of_line():
        nonlocal i
        blank()
        if i < n and text[i] == '#':
            end = text.find('\n', i)
            i = n if end < 0 else end
        if text.startswith('\r\n', i):
            i += 2
        elif i < n and text[i] == '\n':
            i += 1
        elif i < n:
            fail('expected end of line')

    def string():
        nonlocal i
        if text.startswith(('"""', "'''"), i):
            fail('multi-line string')
        quote, out = text[i], []
        i += 1
        while True:
            if i >= n or text[i] in '\r\n':
                fail('unterminated string')
            c = text[i]
            if c == quote:
                i += 1
                return ''.join(out)
            if (ord(c) < 0x20 and c != '\t') or ord(c) == 0x7f:
                fail('control character in string')
            if c == '\\' and quote == '"':
                code = text[i + 1:i + 2]
                if code in TOML_ESCAPES:
                    out.append(TOML_ESCAPES[code])
                    i += 2
                    continue
                width = {'u': 4, 'U': 8}.get(code)
                digits = text[i + 2:i + 2 + width] if width else ''
                if not width or len(digits) != width or not re.fullmatch(r'[0-9A-Fa-f]+', digits):
                    fail('invalid escape')
                value = int(digits, 16)
                if value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                    fail('invalid unicode escape')
                out.append(chr(value))
                i += 2 + width
                continue
            out.append(c)
            i += 1

    def key():
        nonlocal i
        parts = []
        while True:
            blank()
            if i < n and text[i] in '"\'':
                parts.append(string())
            else:
                match = TOML_BARE_KEY.match(text, i)
                if not match:
                    fail('expected a key')
                parts.append(match.group(0))
                i = match.end()
            blank()
            if i < n and text[i] == '.':
                i += 1
                continue
            return parts

    def value():
        nonlocal i
        blank()
        if i < n and text[i] in '"\'':
            return string()
        if i < n and text[i] == '[':
            i += 1
            items = []
            while True:
                blank_lines()
                if i < n and text[i] == ']':
                    i += 1
                    return items
                items.append(value())
                blank_lines()
                if i < n and text[i] == ',':
                    i += 1
                elif not (i < n and text[i] == ']'):
                    fail('expected , or ] in array')
        if i < n and text[i] == '{':
            i += 1
            table = _InlineTable()
            blank()
            if i < n and text[i] == '}':
                i += 1
                return table
            while True:
                path = key()
                if not (i < n and text[i] == '='):
                    fail('expected =')
                i += 1
                assign(table, path, value())
                blank()
                if i < n and text[i] == ',':
                    i += 1
                elif i < n and text[i] == '}':
                    i += 1
                    return table
                else:
                    fail('expected , or } in inline table')
        match = TOML_SCALAR.match(text, i)
        if not match:
            fail('expected a value')
        i = match.end()
        return _Scalar(match.group(0))

    def descend(node, part, path):
        child = node.setdefault(part, {})
        if isinstance(child, _TableArray):
            child = child[-1]
        if not isinstance(child, dict) or isinstance(child, _InlineTable):
            fail(f'{".".join(path)} is not a table')
        return child

    def assign(table, path, item):
        node = table
        for depth, part in enumerate(path[:-1]):
            node = descend(node, part, path[:depth + 1])
        if path[-1] in node:
            fail(f'duplicate key {".".join(path)}')
        node[path[-1]] = item

    root, defined = {}, set()
    current = root
    while True:
        blank_lines()
        if i >= n:
            return root
        if text.startswith('[[', i):
            i += 2
            path = key()
            if not text.startswith(']]', i):
                fail('expected ]]')
            i += 2
            node = root
            for depth, part in enumerate(path[:-1]):
                node = descend(node, part, path[:depth + 1])
            tables = node.setdefault(path[-1], _TableArray())
            if not isinstance(tables, _TableArray):
                fail(f'{".".join(path)} is not an array of tables')
            tables.append({})
            current = tables[-1]
        elif text[i] == '[':
            i += 1
            path = key()
            if not (i < n and text[i] == ']'):
                fail('expected ]')
            i += 1
            node = root
            for depth, part in enumerate(path[:-1]):
                node = descend(node, part, path[:depth + 1])
            existing = node.get(path[-1])
            if existing is None:
                node[path[-1]] = current = {}
            elif type(existing) is dict and id(existing) not in defined:
                current = existing
            else:
                fail(f'duplicate table {".".join(path)}')
            defined.add(id(current))
        else:
            path = key()
            if not (i < n and text[i] == '='):
                fail('expected =')
            i += 1
            assign(current, path, value())
        end_of_line()


def workspace_members(manifest):
    """Return `[workspace] members` from a Cargo manifest (literal paths only).

    The whole manifest is parsed (see parse_toml), so every TOML spelling of
    workspace.members is found; syntax outside the supported subset, or a
    members value that is not an array of strings, raises ValueError. A
    member is never silently omitted from the archive or the fetched-layout
    check."""
    workspace = parse_toml(manifest).get('workspace')
    if workspace is None:
        return []
    if not isinstance(workspace, dict):
        raise ValueError('unsupported workspace declaration: not a table')
    members = workspace.get('members', [])
    if type(members) is not list or any(type(m) is not str for m in members):
        raise ValueError('unsupported [workspace] members value: expected an array of strings')
    for member in members:
        if not member:
            raise ValueError('empty [workspace] member')
        if any(c in member for c in '*?[') or '..' in PurePosixPath(member).parts or member.startswith('/'):
            raise ValueError(f'unsupported workspace member pattern: {member}')
    return members


DEPENDENCY_TABLES = ('dependencies', 'dev-dependencies', 'dev_dependencies',
                     'build-dependencies', 'build_dependencies')


def path_dependencies(document):
    """Return the literal `path` of every local dependency a parsed manifest declares.

    Covers [dependencies], [dev-dependencies], [build-dependencies] (and the
    underscore spellings Cargo still accepts), their [target.*] forms,
    [workspace.dependencies], [patch.*] and [replace]. A dependency entry
    whose `path` is not a string raises ValueError."""
    groups = [document.get(name) for name in DEPENDENCY_TABLES]
    targets = document.get('target', {})
    if not isinstance(targets, dict):
        raise ValueError('unsupported [target] declaration: not a table')
    for target in targets.values():
        if not isinstance(target, dict):
            raise ValueError('unsupported [target.*] declaration: not a table')
        groups += [target.get(name) for name in DEPENDENCY_TABLES]
    workspace = document.get('workspace', {})
    if isinstance(workspace, dict):
        groups.append(workspace.get('dependencies'))
    patch = document.get('patch', {})
    if not isinstance(patch, dict):
        raise ValueError('unsupported [patch] declaration: not a table')
    groups += list(patch.values())
    groups.append(document.get('replace'))
    paths = []
    for group in groups:
        if group is None:
            continue
        if not isinstance(group, dict):
            raise ValueError('unsupported dependency table: not a table')
        for name, spec in group.items():
            if isinstance(spec, dict) and 'path' in spec:
                if type(spec['path']) is not str or not spec['path']:
                    raise ValueError(f'unsupported path for dependency {name}: expected a non-empty string')
                paths.append(spec['path'])
    return paths


class MissingCrate(ValueError):
    """A workspace member or local path dependency without a Cargo.toml."""
    def __init__(self, what, directory):
        super().__init__(f'{what} {directory} has no Cargo.toml at this revision')
        self.what, self.directory = what, directory


def normalize(base, raw, what='local dependency'):
    """`raw` (relative to the crate directory `base`) as a path relative to the
    root crate; ValueError for absolute paths, backslashes or an escape."""
    if raw.startswith('/') or '\\' in raw:
        raise ValueError(f'unsupported {what} path: {raw}')
    parts = [p for p in PurePosixPath(base).parts if p != '.']
    for part in PurePosixPath(raw).parts:
        if part == '..':
            if not parts:
                raise ValueError(f'{what} {raw} is outside the {CRATE} source layout')
            parts.pop()
        elif part != '.':
            parts.append(part)
    return '/'.join(parts)


def local_crates(manifest, read_manifest):
    """Return every local crate directory the root manifest's build needs.

    Directories are POSIX paths relative to the root crate: the workspace
    members plus the transitive closure of local `path` dependencies (Cargo
    needs those even when they are not listed as members). `read_manifest(dir)`
    returns that directory's Cargo.toml text, or None when it is absent, which
    raises MissingCrate (a ValueError). A path dependency that leaves the root crate directory
    is refused: the archive layout and fetched-layout check cover only it."""
    found = []
    pending = [(member, 'workspace member') for member in workspace_members(manifest)]
    pending += [(normalize('', raw), 'path dependency') for raw in path_dependencies(parse_toml(manifest))]
    seen = {''}
    while pending:
        directory, what = pending.pop(0)
        if directory in seen:
            continue
        seen.add(directory)
        text = read_manifest(directory)
        if text is None:
            raise MissingCrate(what, directory)
        found.append(directory)
        pending += [(normalize(directory, raw), 'path dependency') for raw in path_dependencies(parse_toml(text))]
    return found


TARGET_TABLES = ('lib', 'bin', 'example', 'test', 'bench')


def target_files(document):
    """Return the files a parsed manifest declares explicitly for its build.

    These are a string `package.build` and the `path` of every [lib], [[bin]],
    [[example]], [[test]] and [[bench]] target, relative to the manifest's
    directory. Cargo refuses a manifest whose declared target file is absent,
    so each one is a build input wherever it lives. Any other spelling of
    these keys (a non-string path, a build-script list) raises ValueError
    instead of being skipped."""
    paths = []
    package = document.get('package', {})
    if not isinstance(package, dict):
        raise ValueError('unsupported [package] declaration: not a table')
    build = package.get('build')
    if type(build) is str and build:
        paths.append(build)
    elif build is not None and not (isinstance(build, _Scalar) and build in ('true', 'false')):
        raise ValueError('unsupported package.build value: expected a non-empty string or a boolean')
    for name in TARGET_TABLES:
        entries = document.get(name)
        if entries is None:
            continue
        if name == 'lib':
            entries = [entries]
        elif type(entries) not in (list, _TableArray):
            raise ValueError(f'unsupported [[{name}]] declaration: expected an array of tables')
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f'unsupported {name} target declaration: not a table')
            if 'path' in entry:
                if type(entry['path']) is not str or not entry['path']:
                    raise ValueError(f'unsupported {name} target path: expected a non-empty string')
                paths.append(entry['path'])
    return paths


def declared_files(manifest, read_manifest):
    """Return the manifest-declared build files of the root crate and of every
    local crate (see local_crates), relative to the root crate directory.

    Raises MissingCrate for an absent local crate and ValueError for a target
    path that is absolute or leaves the root crate directory."""
    files = []
    for directory in ['', *local_crates(manifest, read_manifest)]:
        text = manifest if directory == '' else read_manifest(directory)
        files += [normalize(directory, raw, 'Cargo target file') for raw in target_files(parse_toml(text))]
    return sorted(set(files))


def include_targets(source_path, data):
    base = PurePosixPath(source_path).parent
    targets = []
    for include, module in INCLUDE.findall(data):
        raw = include or module
        parts = []
        for part in (base / raw.decode()).parts:
            if part == '..':
                if not parts:
                    raise ValueError(f'{source_path} includes a file outside the repository: {raw.decode()}')
                parts.pop()
            elif part != '.':
                parts.append(part)
        targets.append('/'.join(parts))
    return targets


def implicit_modules(data):
    """Names of out-of-line modules (`mod name;`) not given a literal #[path].

    Comments are not stripped, so a commented-out declaration also counts;
    that only makes the refusal below stricter."""
    return [name.decode() for attributes, name in MODULE.findall(data) if not PATH_ATTR.search(attributes)]


def loose_sources(manifest, read_manifest, declared, exists, read):
    """Rust sources outside the directories packaged whole, and their errors.

    `src/`, `examples/` and every local crate directory are packaged
    completely, so Rust's module resolution always finds its files there.
    Elsewhere only the files the manifest declares (plus the default
    `build.rs`) and their literal include/#[path] targets are packaged. Cargo
    would also compile a sibling `name.rs` or `name/mod.rs` for each implicit
    `mod name;` in such a file, which the closure does not model, so those
    declarations are refused instead of producing an archive that compiles in
    the repository and fails after extraction. Paths are relative to the root
    crate; `exists(rel)` and `read(rel)` (bytes) see the source layout.
    Returns (loose .rs files, error messages)."""
    whole = ['src/', 'examples/', *(f'{d}/' for d in local_crates(manifest, read_manifest))]
    pending = [rel for rel in [*declared, 'build.rs'] if rel.endswith('.rs') and exists(rel)]
    seen, errors = set(), []
    while pending:
        rel = pending.pop()
        if rel in seen or any(rel.startswith(prefix) for prefix in whole):
            continue
        seen.add(rel)
        data = read(rel)
        names = implicit_modules(data)
        if names:
            errors.append(f'{CRATE}/{rel} declares module(s) {", ".join(names)} without #[path]; outside src/, '
                          'examples/ and local crates only declared and literally included files are packaged, '
                          'so give each module a #[path] attribute or move the target under src/')
        for target in include_targets(f'{CRATE}/{rel}', data):
            if target.startswith(f'{CRATE}/') and target.endswith('.rs') and exists(target[len(CRATE) + 1:]):
                pending.append(target[len(CRATE) + 1:])
    return sorted(seen), errors


def closure(files, read):
    """Select the build closure from `files` ({path: (mode, kind, oid)})."""
    selected = set()

    def add_root(root):
        if root.endswith('/'):
            selected.update(p for p in files if p.startswith(root))
        elif root in files:
            selected.add(root)

    for root in ROOTS:
        add_root(root)
    for name in REQUIRED:
        if name not in files:
            raise ValueError(f'source revision lacks required {name}')
    manifest = read(f'{CRATE}/Cargo.toml').decode()

    def read_manifest(directory):
        path = f'{CRATE}/{directory}/Cargo.toml'
        return read(path).decode() if path in files else None

    for directory in local_crates(manifest, read_manifest):
        add_root(f'{CRATE}/{directory}/')
    for rel in declared_files(manifest, read_manifest):
        if f'{CRATE}/{rel}' not in files:
            raise ValueError(f'Cargo.toml declares build file {CRATE}/{rel}, absent at this revision')
        selected.add(f'{CRATE}/{rel}')
    pending = sorted(p for p in selected if p.endswith('.rs'))
    while pending:
        path = pending.pop()
        for target in include_targets(path, read(path)):
            if target not in files:
                raise ValueError(f'{path} includes missing file {target}')
            if target not in selected:
                selected.add(target)
                if target.endswith('.rs'):
                    pending.append(target)
    _, errors = loose_sources(manifest, read_manifest, declared_files(manifest, read_manifest),
                              lambda rel: f'{CRATE}/{rel}' in files, lambda rel: read(f'{CRATE}/{rel}'))
    if errors:
        raise ValueError(errors[0])
    return audit(selected, files, read)


def audit(paths, files, read):
    """Refuse absent or non-regular files and runtime-state names."""
    for path in sorted(paths):
        if path not in files:
            raise ValueError(f'path absent from revision: {path}')
        mode, kind, _ = files[path]
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError(f'source distribution requires regular tracked files: {path} ({mode} {kind})')
        if FORBIDDEN_NAME.search(path):
            raise ValueError(f'refusing runtime identity/state-like file: {path}')
    return sorted(paths)


def crate_version(manifest):
    version = parse_toml(manifest).get('package', {}).get('version')
    if type(version) is not str:
        raise ValueError('Cargo.toml has no literal [package] version')
    return version


def plan(repo, revision, paths=None):
    revision = git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    files = tree(repo, revision)
    cache = {}

    def read(path):
        if path not in cache:
            cache[path] = git(repo, 'cat-file', 'blob', files[path][2])
        return cache[path]

    if paths is None:
        paths = closure(files, read)
    else:
        # Reproduce a previously published file list; it gets the same audit.
        paths = audit(set(paths), files, read)
    license_md5 = hashlib.md5(read('LICENSE')).hexdigest()
    return {
        'source_commit': revision,
        'paths': paths,
        'crate_version': crate_version(read(f'{CRATE}/Cargo.toml').decode()),
        'lock_sha256': hashlib.sha256(read(f'{CRATE}/Cargo.lock')).hexdigest(),
        'license_md5': license_md5,
    }


def verify_archive(repo, revision, paths, data):
    """Refuse a tar stream that differs from the planned committed files."""
    files = tree(repo, revision)
    expected = set(paths)
    directories = {p.as_posix() for path in paths for p in PurePosixPath(path).parents if p.as_posix() != '.'}
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as tar:
        for member in tar.getmembers():
            name = member.name.rstrip('/')
            if member.isdir() and name in directories:
                continue
            if not member.isfile() or name not in expected:
                raise ValueError(f'git archive emitted an unplanned member: {member.name}')
            if name in seen:
                raise ValueError(f'git archive emitted {name} twice')
            seen.add(name)
            mode, _, blob = files[name]
            if tar.extractfile(member).read() != git(repo, 'cat-file', 'blob', blob):
                raise ValueError(f'git archive bytes for {name} differ from the committed blob (export-subst?)')
            if bool(member.mode & 0o111) != (mode == '100755'):
                raise ValueError(f'git archive mode for {name} differs from the committed mode {mode}')
    missing = sorted(expected - seen)
    if missing:
        raise ValueError(f'git archive omitted planned files (export-ignore?): {", ".join(missing)}')


def build(repo, revision, output, paths=None):
    """Write the archive with exclusive creation; return its provenance."""
    info = plan(repo, revision, paths)
    archive = subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', info['source_commit'], '--', *info['paths']],
                             stdout=subprocess.PIPE)
    if archive.returncode != 0:
        raise RuntimeError('git archive failed')
    verify_archive(repo, info['source_commit'], info['paths'], archive.stdout)
    compressed = subprocess.run(['gzip', '-n', GZIP_LEVEL], input=archive.stdout, stdout=subprocess.PIPE, check=True).stdout
    with Path(output).open('xb') as target:
        target.write(compressed)
    info.update(sha256=hashlib.sha256(compressed).hexdigest(), files=len(info['paths']),
                filename=Path(output).name,
                tools={'git': git(repo, '--version').decode().strip(),
                       'gzip': subprocess.check_output(['gzip', '--version']).decode().splitlines()[0]})
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--revision', required=True, help='reviewed, committed source revision')
    parser.add_argument('--output', type=Path, help='new archive path, conventionally suifleet-edge-<commit>.tar.gz')
    parser.add_argument('--list', action='store_true', help='print the selected paths without writing an archive')
    parser.add_argument('--paths-from', type=Path, help='reproduce an existing archive from its newline-separated file list')
    args = parser.parse_args()
    if args.list:
        print('\n'.join(plan(args.repo, args.revision)['paths']))
        return
    if not args.output:
        parser.error('--output is required unless --list is given')
    paths = args.paths_from.read_text().split() if args.paths_from else None
    result = build(args.repo, args.revision, args.output, paths)
    result.pop('paths')
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
