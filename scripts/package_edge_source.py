#!/usr/bin/env python3
"""Build the immutable daemon source distribution fetched by the meta-suifleet recipe.

The archive is produced from committed Git objects only (never a working tree):

    git -c tar.umask=0002 archive --format=tar REVISION -- <paths> | gzip -n -6

Paths are the daemon build closure: the root LICENSE, the Cargo manifest and
lockfile, the crate sources and examples, every Cargo workspace member, every
local crate reached through `path` dependencies (transitively), every
file a manifest declares explicitly (`package.build` and each target `path`) and
each file named by a literal ``include!``/``include_str!``/``include_bytes!``
(in any delimiter) in those sources, or by a `#[path]` module attribute
(resolved transitively); an include or path form that is not one string literal
is refused. The closure is computed from the revision's
tree, so a missing workspace member or include target is an error instead of a
broken release. Ownership, timestamps (the commit time) and modes come from
``git archive``, with ``tar.umask`` pinned to git's default 0002 (which the
published archives used) so ambient Git configuration cannot change the
permission bits; ``gzip -n`` omits the name and
time, so rebuilding the same revision with the same git/gzip produces
byte-identical output.

Before anything is written, the tar stream is checked against the plan: it
must contain exactly the planned regular files (plus their directories), each
with the committed blob's bytes and exact mode (0664, or 0775 when executable). A committed
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
LICENSES = ('LICENSE',)


def roots(crate=CRATE, licenses=LICENSES):
    """Closure roots for the root crate directory `crate` (repository-relative)
    and the license files `licenses`; directory roots end with '/'."""
    return (*licenses, *(f'{crate}/{rel}' for rel in
                         ('Cargo.toml', 'Cargo.lock', 'config.example.toml', 'build.rs', 'src/', 'examples/')))


def required(crate=CRATE, licenses=LICENSES):
    return (*licenses, f'{crate}/Cargo.toml', f'{crate}/Cargo.lock')


# Every include macro name, however it is delimited or spaced (`include!(..)`,
# `include! { .. }`, `std::include_str![..]`).
INCLUDE_MACRO = re.compile(rb'(?<![A-Za-z0-9_])include(_str|_bytes)?\s*!')
# `use std::include_str as name;` renames a macro, so calls through the new
# name would not be seen; refused outside comments and string literals.
INCLUDE_RENAME = re.compile(rb'(?<![A-Za-z0-9_])include(?:_str|_bytes)?\s+as(?![A-Za-z0-9_\x80-\xff])')
# The one argument form followed: a single string literal (plain without
# escapes, or raw) in any macro delimiter, with an optional trailing comma.
INCLUDE_ARGUMENT = {
    opener: re.compile(rb'\s*' + re.escape(opener) + rb'\s*(?:"([^"\\]*)"|r(#*)"(.*?)"\2)\s*,?\s*' + re.escape(closer),
                       re.S)
    for opener, closer in ((b'(', b')'), (b'[', b']'), (b'{', b'}'))}
# An attribute opener (`#[`, `#![`, `# [`); its body is scanned bracket-balanced.
ATTRIBUTE = re.compile(rb'#\s*!?\s*\[')
# Built-in attribute names may be spelled as raw identifiers (`r#path`,
# `r#cfg_attr`), which rustc accepts identically, so both spellings match.
PATH_KEY = re.compile(rb'\s*(?:r#)?path\s*=')
PATH_VALUE = re.compile(rb'\s*(?:"([^"\\]*)"|r(#*)"(.*?)"\2)\s*$', re.S)
CFG_ATTR = re.compile(rb'\s*(?:r#)?cfg_attr\s*\(')
# A Rust identifier. Rust accepts Unicode (XID) identifiers; any non-ASCII
# UTF-8 byte is accepted here, a superset, so a module name is never missed.
IDENTIFIER = rb'[A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*'
# Rust's non-ASCII whitespace (Pattern_White_Space: U+0085, U+200E, U+200F,
# U+2028, U+2029) as UTF-8. The byte patterns here only model ASCII
# whitespace, so sources containing these are refused (literal_references).
UNICODE_WHITESPACE = re.compile(rb'\xc2\x85|\xe2\x80[\x8e\x8f\xa8\xa9]')
# An out-of-line module declaration (`mod name;`) with its leading attributes.
MODULE = re.compile(rb'((?:#\s*\[[^\]]*\]\s*)*)(?<![A-Za-z0-9_])(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+(?:r#)?(' + IDENTIFIER + rb')\s*;')
PATH_ATTR = re.compile(rb'#\s*\[\s*(?:r#)?path\s*=')
# An inline module block (`mod name {`). rustc resolves a #[path] inside one
# relative to the module's own directory, and a #[path] on one names a
# directory, so neither is modelled; both are refused (see literal_references).
# Blocks are found structurally in a code_mask()ed copy of the source.
INLINE_MODULE = re.compile(rb'(?<![A-Za-z0-9_])mod\s+(?:r#)?' + IDENTIFIER + rb'\s*\{')
RAW_STRING = re.compile(rb'r(#*)"')
ITEM_PREFIX = re.compile(rb'\s*(?:pub(?:\s*\([^)]*\))?\s+)?(?:unsafe\s+)?')
# Runtime identity/state names that must never be distributed.
FORBIDDEN_NAME = re.compile(
    r'(^|/)(device\.key|signer\.key|config\.toml|firmware\.toml|signer\.toml|'
    r'enrollment(\.[a-z]+)?|\.env|\.netrc|id_(rsa|ecdsa|ed25519)|[^/]*\.(pem|p12|pfx|key)|'
    r'[^/]*journal[^/]*|[^/]*\.sqlite3?|[^/]*\.db)$')
GZIP_LEVEL = '-6'


# git's default tar.umask, which the published archives were built with (0664
# files, 0775 executables and directories).
TAR_UMASK = 0o002


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
    # Cargo accepts spellings such as ./member/ for a literal member; git-tree
    # lookups, deduplication and archive roots need the canonical path.
    pending = [(normalize('', member, 'workspace member'), 'workspace member')
               for member in workspace_members(manifest)]
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
# Directories in which Cargo looks for a [[test]]/[[bench]] declared without a
# `path`. The other target kinds default under src/ or examples/, which the
# archive always contains whole.
DEFAULT_TARGET_DIRS = {'test': 'tests', 'bench': 'benches'}


def default_target_path(kind, entry, exists):
    """The file Cargo compiles for a [[test]]/[[bench]] declared without a
    `path`: `<dir>/<name>.rs` or `<dir>/<name>/main.rs`, relative to the
    manifest directory (the candidates Cargo infers from that directory).
    `exists(path)` sees the source layout. When neither exists the first
    candidate is returned, so the caller reports it absent exactly as for an
    explicit path; both existing is ambiguous to Cargo and raises ValueError,
    as does a missing or non-plain name."""
    name = entry.get('name')
    if type(name) is not str or not name or name in ('.', '..') or '/' in name or '\\' in name:
        raise ValueError(f'unsupported {kind} target without a path: expected a plain non-empty name, '
                         'from which Cargo infers its file')
    directory = DEFAULT_TARGET_DIRS[kind]
    candidates = [f'{directory}/{name}.rs', f'{directory}/{name}/main.rs']
    present = [path for path in candidates if exists(path)]
    if len(present) > 1:
        raise ValueError(f'{kind} target {name} matches both {candidates[0]} and {candidates[1]}; '
                         'Cargo cannot infer its file, so give it an explicit path')
    return present[0] if present else candidates[0]


def target_files(document, exists=lambda path: False):
    """Return the files a parsed manifest declares for its build.

    These are a string `package.build`, the `path` of every [lib], [[bin]],
    [[example]], [[test]] and [[bench]] target, and Cargo's inferred file for
    a [[test]] or [[bench]] declared by name only (see default_target_path;
    `exists` sees paths relative to the manifest directory), all relative to
    the manifest's directory. Cargo refuses a manifest whose declared target
    file is absent, so each one is a build input wherever it lives. Any other
    spelling of these keys (a non-string path, a build-script list) raises
    ValueError instead of being skipped."""
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
            elif name in DEFAULT_TARGET_DIRS:
                paths.append(default_target_path(name, entry, exists))
    return paths


def declared_files(manifest, read_manifest, exists):
    """Return the manifest-declared build files of the root crate and of every
    local crate (see local_crates), relative to the root crate directory.
    `exists(path)`, with `path` relative to the root crate directory, sees the
    source layout and resolves name-only [[test]]/[[bench]] targets.

    Raises MissingCrate for an absent local crate and ValueError for a target
    path that is absolute or leaves the root crate directory."""
    files = []
    for directory in ['', *local_crates(manifest, read_manifest)]:
        text = manifest if directory == '' else read_manifest(directory)
        def exists_here(raw, directory=directory):
            return exists(normalize(directory, raw, 'Cargo target file'))
        files += [normalize(directory, raw, 'Cargo target file') for raw in target_files(parse_toml(text), exists_here)]
    return sorted(set(files))


def _literal(match):
    """The text of a PATH_VALUE/INCLUDE_ARGUMENT string-literal match."""
    return match.group(1) if match.group(1) is not None else match.group(3)


def _bracketed(data, start, opener, closer):
    """Return the index just past the `closer` matching the `opener` before
    `start`, skipping string literals, or None when it is unbalanced."""
    depth, i = 1, start
    while i < len(data):
        c = data[i:i + 1]
        if c == b'"':
            i += 1
            while i < len(data) and data[i:i + 1] != b'"':
                i += 2 if data[i:i + 1] == b'\\' else 1
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _top_level_split(body):
    """Split an attribute argument list at its top-level commas."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(body):
        c = body[i:i + 1]
        if c == b'"':
            i += 1
            while i < len(body) and body[i:i + 1] != b'"':
                i += 2 if body[i:i + 1] == b'\\' else 1
        elif c in b'([{':
            depth += 1
        elif c in b')]}':
            depth -= 1
        elif c == b',' and depth == 0:
            parts.append(body[start:i])
            start = i + 1
        i += 1
    parts.append(body[start:])
    return parts


def _path_attributes(source_path, body):
    """Literal paths of a `path = ".."` attribute body, including one nested in
    `cfg_attr(predicate, ..)`. A path value that is not one plain or raw string
    literal raises ValueError instead of being skipped."""
    key = PATH_KEY.match(body)
    if key:
        value = PATH_VALUE.match(body, key.end())
        if not value:
            raise ValueError(f'{source_path}: unsupported #[path] attribute {body.decode(errors="replace").strip()!r}; '
                             'use a single string literal without escapes')
        return [_literal(value)]
    cfg = CFG_ATTR.match(body)
    if cfg:
        end = body.rfind(b')')
        paths = []
        for argument in _top_level_split(body[cfg.end():end])[1:]:
            paths += _path_attributes(source_path, argument)
        return paths
    return []


def literal_references(source_path, data):
    """Every file path a Rust source names through an include macro or a path
    attribute, as (path bytes, is_code) pairs. `include!` and #[path] targets
    are Rust code whatever their file name, so their own references must be
    followed too; `include_str!`/`include_bytes!` targets are data.

    Rust allows comments wherever whitespace may appear, including between a
    macro name and its `!`, inside an attribute and around the literal, so the
    authoritative scan runs on the source with comments blanked (string
    literals kept): each include-macro name must be followed by one string
    literal in any delimiter, otherwise ValueError, so an unhandled form
    cannot silently drop a build input. The raw text is scanned as well and
    whatever it resolves is added too (only ever more files, never an error),
    so references inside comments are still packaged as before. Rust's
    non-ASCII whitespace is refused anywhere in the source, since the
    patterns would not see through it."""
    unicode_space = UNICODE_WHITESPACE.search(data)
    if unicode_space:
        point = ord(unicode_space.group().decode())
        line = data.count(b'\n', 0, unicode_space.start()) + 1
        raise ValueError(f'{source_path}:{line}: Unicode whitespace U+{point:04X} is unsupported in Rust sources; '
                         'use ASCII whitespace')
    code = code_mask(data)
    blocks = inline_module_blocks(code)
    references = _scan_references(source_path, code_mask(data, strings=False), code, blocks, strict=True)
    for reference in _scan_references(source_path, data, code, blocks, strict=False):
        if reference not in references:
            references.append(reference)
    return references


def _scan_references(source_path, text, code, blocks, strict):
    """The literal references of one scan text (see literal_references). With
    `strict`, an unresolvable form raises ValueError; otherwise it is skipped."""
    references = []
    rename = INCLUDE_RENAME.search(code) if strict else None
    if rename:
        snippet = code[rename.start():rename.start() + 60].split(b'\n')[0].decode(errors='replace')
        raise ValueError(f'{source_path}: renaming an include macro is unsupported ({snippet!r}); '
                         'call include!/include_str!/include_bytes! by name')
    for match in INCLUDE_MACRO.finditer(text):
        rest = text[match.end():].lstrip()
        argument = INCLUDE_ARGUMENT.get(rest[:1])
        found = argument.match(text, match.end()) if argument else None
        if not found:
            if not strict:
                continue
            snippet = text[match.start():match.start() + 60].split(b'\n')[0].decode(errors='replace')
            raise ValueError(f'{source_path}: unsupported include form {snippet!r}; '
                             'use a single string literal without escapes')
        references.append((_literal(found), match.group(1) is None))
    for match in ATTRIBUTE.finditer(text):
        end = _bracketed(text, match.end(), b'[', b']')
        if end is None:
            continue
        try:
            paths = _path_attributes(source_path, text[match.end():end - 1])
        except ValueError:
            if strict:
                raise
            continue
        in_code = code[match.start():match.start() + 1] == b'#'
        if paths and in_code and (any(start < match.start() < stop for start, stop in blocks)
                                  or _annotates_inline_module(code, end)):
            # rustc resolves a #[path] inside `mod name { .. }` against that
            # module's directory (name/, or file_stem/name/ in a non-mod.rs
            # file), and a #[path] on an inline module names a directory.
            # Neither is modelled, so refuse rather than package the wrong file.
            if not strict:
                continue
            raise ValueError(f'{source_path}: #[path] attribute {paths[0].decode(errors="replace")!r} inside or on '
                             'an inline `mod name { .. }` block is unsupported; declare the #[path] module at file '
                             'level or move it to an out-of-line module file')
        references += [(path, True) for path in paths]
    return references


def code_mask(data, strings=True):
    """`data` with comments, string/byte/raw-string and char literals replaced
    by spaces, offsets unchanged, so braces and keywords can be matched
    structurally. Lifetimes and labels are left in place. With
    `strings=False` only comments are blanked and literals are kept."""
    out, i, n = bytearray(data), 0, len(data)

    def blank(start, stop):
        out[start:stop] = b' ' * (stop - start)

    def literal(start, stop):
        if strings:
            blank(start, stop)

    def ident(byte):
        return byte.isalnum() or byte == b'_' or (byte and byte[0] >= 0x80)

    while i < n:
        c = data[i:i + 1]
        if data.startswith(b'//', i):
            stop = data.find(b'\n', i)
            stop = n if stop < 0 else stop
            blank(i, stop)
            i = stop
        elif data.startswith(b'/*', i):
            depth, j = 1, i + 2
            while j < n and depth:
                if data.startswith(b'/*', j):
                    depth, j = depth + 1, j + 2
                elif data.startswith(b'*/', j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif c == b'"':
            j = i + 1
            while j < n and data[j:j + 1] != b'"':
                j += 2 if data[j:j + 1] == b'\\' else 1
            literal(i, min(j + 1, n))
            i = j + 1
        elif c == b'r' and not ident(data[i - 1:i] if i else b'') or (
                c == b'r' and data[i - 1:i] in (b'b', b'c') and not ident(data[i - 2:i - 1] if i > 1 else b'')):
            raw = RAW_STRING.match(data, i)
            if raw:
                stop = data.find(b'"' + raw.group(1), raw.end())
                stop = n if stop < 0 else stop + 1 + len(raw.group(1))
                literal(i, stop)
                i = stop
            else:
                i += 1
        elif c == b"'":
            if data[i + 1:i + 2] == b'\\':
                stop = data.find(b"'", i + 3)
                stop = n if stop < 0 else stop + 1
                literal(i, stop)
                i = stop
            else:
                lead = data[i + 1] if i + 1 < n else 0
                width = 1 if lead < 0x80 else 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
                if data[i + 1 + width:i + 2 + width] == b"'":
                    literal(i, i + 2 + width)
                    i += 2 + width
                else:
                    i += 1  # a lifetime or label
        else:
            i += 1
    return bytes(out)


def inline_module_blocks(code):
    """(start, stop) offsets of every inline `mod name { .. }` body in a
    code_mask()ed source; an unbalanced block extends to the end."""
    blocks = []
    for match in INLINE_MODULE.finditer(code):
        depth, j = 0, match.end() - 1
        while j < len(code):
            byte = code[j:j + 1]
            if byte == b'{':
                depth += 1
            elif byte == b'}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        blocks.append((match.end() - 1, j))
    return blocks


def _annotates_inline_module(code, end):
    """Whether the item after an attribute ending at `end`, past any further
    attributes and its visibility, is an inline module block."""
    i = end
    while True:
        while i < len(code) and code[i:i + 1].isspace():
            i += 1
        attribute = ATTRIBUTE.match(code, i)
        if not attribute:
            break
        i = _bracketed(code, attribute.end(), b'[', b']')
        if i is None:
            return False
    prefix = ITEM_PREFIX.match(code, i)
    return bool(INLINE_MODULE.match(code, prefix.end()))


def include_references(source_path, data):
    """Repository-relative (target, is_code) pairs for every literal reference
    in a Rust source; see literal_references."""
    base = PurePosixPath(source_path).parent
    targets = []
    for raw, code in literal_references(source_path, data):
        if not raw:
            raise ValueError(f'{source_path} references an empty path')
        # A trailing `/`, or a final `.` or `..` component, names a directory,
        # so the compiler fails with "Not a directory" even when the
        # normalized path is a regular file. PurePosixPath would silently
        # drop that syntax below; refuse it first.
        if raw.endswith(b'/') or raw.rsplit(b'/', 1)[-1] in (b'.', b'..'):
            raise ValueError(f'{source_path} references a directory, not a file: {raw.decode()}')
        # The compiler traverses every directory named in the literal path,
        # so `dir/../x` needs `dir` to exist even though it is cancelled. The
        # closure only packages the final target, so such a path would compile
        # in the repository and fail after extraction; refuse it. Leading `..`
        # only climbs the source file's own ancestors, which always exist.
        named = False
        for part in PurePosixPath(raw.decode()).parts:
            if part == '..' and named:
                raise ValueError(f'{source_path} references a path that cancels a directory with "..": {raw.decode()}')
            if part not in ('.', '..'):
                named = True
        parts = []
        for part in (base / raw.decode()).parts:
            if part == '..':
                if not parts:
                    raise ValueError(f'{source_path} includes a file outside the repository: {raw.decode()}')
                parts.pop()
            elif part != '.':
                parts.append(part)
        targets.append(('/'.join(parts), code))
    return targets


def include_targets(source_path, data):
    return [target for target, _ in include_references(source_path, data)]


def scanned_as_code(target, code):
    """Whether a reference target is itself scanned as Rust source: every
    include!/#[path] target whatever its suffix, and any .rs file (stricter)."""
    return code or target.endswith('.rs')


def implicit_modules(data):
    """Names of out-of-line modules (`mod name;`) not given a literal #[path].

    Comments are blanked first, since Rust allows them wherever whitespace may
    appear (`mod /* c */ name;`, `#[/* c */ path = ".."]`); string literals
    are kept, which only makes the refusal below stricter."""
    text = code_mask(data, strings=False)
    return [name.decode(errors='replace') for attributes, name in MODULE.findall(text) if not PATH_ATTR.search(attributes)]


def loose_sources(manifest, read_manifest, declared, exists, read, sources, crate=CRATE):
    """Rust sources outside the directories packaged whole, and their errors.

    `src/`, `examples/` and every local crate directory are packaged
    completely, so Rust's module resolution always finds its files there.
    Elsewhere only the files the manifest declares (plus the default
    `build.rs`) and their literal include/#[path] targets are packaged. Cargo
    would also compile a sibling `name.rs` or `name/mod.rs` for each implicit
    `mod name;` in such a file, which the closure does not model, so those
    declarations are refused instead of producing an archive that compiles in
    the repository and fails after extraction. This includes Rust files a
    literal reference reaches outside the crate directory (for example
    `#[path = "../../interfaces/helper.rs"]`), whose child modules would
    otherwise be silently omitted. Paths are repository-relative; `crate` is
    the root crate directory, `declared` is relative to it, `exists(path)`
    and `read(path)` (bytes) see the source layout and `sources` lists the
    files under the crate directory. The Rust files of the whole directories
    are followed too: a literal #[path] or include from src/ can reach a file
    elsewhere, which is then checked like a declared target.
    Returns (loose .rs files, error messages)."""
    whole = [f'{crate}/{d}' for d in ['src/', 'examples/', *(f'{d}/' for d in local_crates(manifest, read_manifest))]]

    def packaged_whole(path):
        return any(path.startswith(prefix) for prefix in whole)

    # Declared Cargo targets and the build script are compiler entry points
    # whatever their suffix, so they are scanned as Rust source too.
    pending = [path for path in (f'{crate}/{rel}' for rel in [*declared, 'build.rs']) if exists(path)]
    pending += sorted(path for path in sources if path.endswith('.rs') and packaged_whole(path))
    seen, followed, errors = set(), set(), []
    while pending:
        path = pending.pop()
        if path in seen or path in followed:
            continue
        data = read(path)
        if packaged_whole(path):
            # Its modules resolve inside the packaged directory; only its
            # literal references can leave it.
            followed.add(path)
            names = []
        else:
            seen.add(path)
            names = implicit_modules(data)
        if names:
            errors.append(f'{path} declares module(s) {", ".join(names)} without #[path]; outside {crate}/src/, '
                          'examples/ and local crates only declared and literally included files are packaged, '
                          'so give each module a #[path] attribute or move the target under src/')
        for target, code in include_references(path, data):
            if scanned_as_code(target, code) and exists(target):
                pending.append(target)
    return sorted(seen), errors


def closure(files, read, crate=CRATE, licenses=LICENSES):
    """Select the build closure from `files` ({path: (mode, kind, oid)}).

    `crate` is the root crate directory and `licenses` the license files, both
    repository-relative; a recipe passes its effective CARGO_SRC_DIR and
    LIC_FILES_CHKSUM paths, so a relocated crate yields its own closure."""
    selected = set()

    def add_root(root):
        if root.endswith('/'):
            selected.update(p for p in files if p.startswith(root))
        elif root in files:
            selected.add(root)

    for root in roots(crate, licenses):
        add_root(root)
    for name in required(crate, licenses):
        if name not in files:
            raise ValueError(f'source revision lacks required {name}')
    manifest = read(f'{crate}/Cargo.toml').decode()

    def read_manifest(directory):
        path = f'{crate}/{directory}/Cargo.toml'
        return read(path).decode() if path in files else None

    for directory in local_crates(manifest, read_manifest):
        add_root(f'{crate}/{directory}/')
    declared = declared_files(manifest, read_manifest, lambda rel: f'{crate}/{rel}' in files)
    for rel in declared:
        if f'{crate}/{rel}' not in files:
            raise ValueError(f'Cargo.toml declares build file {crate}/{rel}, absent at this revision')
        selected.add(f'{crate}/{rel}')
    # Every .rs file, and every manifest-declared target or build script
    # whatever its suffix: Cargo hands those to rustc as crate roots.
    pending = sorted({p for p in selected if p.endswith('.rs')} | {f'{crate}/{rel}' for rel in declared})
    scanned = set()
    while pending:
        path = pending.pop()
        if path in scanned:
            continue
        scanned.add(path)
        for target, code in include_references(path, read(path)):
            if target not in files:
                raise ValueError(f'{path} includes missing file {target}')
            selected.add(target)
            if scanned_as_code(target, code) and target not in scanned:
                pending.append(target)
    _, errors = loose_sources(manifest, read_manifest, declared, lambda path: path in files, read,
                              [p for p in files if p.startswith(f'{crate}/')])
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


def plan(repo, revision, paths=None, crate=CRATE, licenses=LICENSES):
    revision = git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    files = tree(repo, revision)
    cache = {}

    def read(path):
        if path not in cache:
            cache[path] = git(repo, 'cat-file', 'blob', files[path][2])
        return cache[path]

    needed = closure(files, read, crate, licenses)
    if paths is None:
        paths = needed
    else:
        # Reproduce a previously published file list. It may carry more than
        # the build closure, but never less: an archive missing a manifest,
        # workspace member or include target cannot build, and its provenance
        # would wrongly vouch for the reported Cargo inputs.
        paths = audit(set(paths), files, read)
        missing = sorted(set(needed) - set(paths))
        if missing:
            raise ValueError(f'file list omits {len(missing)} build-closure path(s): {", ".join(missing)}')
    license_md5 = hashlib.md5(read(licenses[0])).hexdigest() if licenses else None
    return {
        'source_commit': revision,
        'paths': paths,
        'crate_version': crate_version(read(f'{crate}/Cargo.toml').decode()),
        'lock_sha256': hashlib.sha256(read(f'{crate}/Cargo.lock')).hexdigest(),
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
                if member.mode != 0o777 & ~TAR_UMASK:
                    raise ValueError(f'git archive mode {member.mode:04o} for directory {name} is not {0o777 & ~TAR_UMASK:04o}')
                continue
            if not member.isfile() or name not in expected:
                raise ValueError(f'git archive emitted an unplanned member: {member.name}')
            if name in seen:
                raise ValueError(f'git archive emitted {name} twice')
            seen.add(name)
            mode, _, blob = files[name]
            if tar.extractfile(member).read() != git(repo, 'cat-file', 'blob', blob):
                raise ValueError(f'git archive bytes for {name} differ from the committed blob (export-subst?)')
            # Exact bits, not only the executable flag: the permissions are
            # part of the reproduced bytes.
            want = (0o777 if mode == '100755' else 0o666) & ~TAR_UMASK
            if member.mode != want:
                raise ValueError(f'git archive mode {member.mode:04o} for {name} is not {want:04o} (committed {mode})')
    missing = sorted(expected - seen)
    if missing:
        raise ValueError(f'git archive omitted planned files (export-ignore?): {", ".join(missing)}')


def build(repo, revision, output, paths=None, crate=CRATE, licenses=LICENSES):
    """Write the archive with exclusive creation; return its provenance."""
    info = plan(repo, revision, paths, crate, licenses)
    # tar.umask decides the archived permission bits; pin the published value
    # so an ambient user/system/GIT_CONFIG_* value cannot change the bytes.
    archive = subprocess.run(['git', '-C', str(repo), '-c', f'tar.umask={TAR_UMASK:04o}', 'archive', '--format=tar',
                              info['source_commit'], '--', *info['paths']],
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
    paths = [line for line in args.paths_from.read_text().splitlines() if line] if args.paths_from else None
    result = build(args.repo, args.revision, args.output, paths)
    result.pop('paths')
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
