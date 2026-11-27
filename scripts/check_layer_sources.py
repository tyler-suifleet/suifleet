#!/usr/bin/env python3
"""Audit meta-suifleet source pins and, optionally, fetch them anonymously.

Static checks (always):
  * no AUTOREV; git sources use https and a 40-hex SRCREV;
  * https sources carry a SHA-256 and are immutable: no query strings or
    credentials, no branch/tag archives, no raw/codeload URL at a non-commit
    ref, and no expiring release-asset redirect hosts;
  * every crate:// entry has a SHA-256;
  * file:// sources resolve, after symlinks, to regular files inside the layer
    (no FILESEXTRAPATHS outside it, no symlink leaving the layer);
  * source-affecting variables (SRC_URI, SRCREV*, PV, S, LIC_FILES_CHKSUM,
    SUIFLEET_* and everything they reference) take no conditional overrides:
    only a plain :append/:prepend is accepted, so the audited assignment is the
    one BitBake uses for every machine, distro and class. As in BitBake, those
    appends/prepends apply after every assignment wherever they appear, and
    ??= is a weak default used only when nothing else assigns the variable
    (the last ??= wins, while ?= keeps the first assignment);
  * flags (for example SRC_URI[name.sha256sum]) follow the same =, :=, ?=,
    +=, =+, .= and =. semantics; ??= on a flag is rejected on source-affecting
    variables because its meaning differs between supported BitBake releases;
  * double- and single-quoted assignments are read; an assignment-shaped line
    the reader cannot interpret is reported when it names a source-affecting
    variable, never silently skipped;
  * require/include targets must be files inside the layer (after symlinks);
    out-of-layer, unexpandable or missing targets are reported and never read,
    since BitBake would otherwise find them through BBPATH in another layer;
  * each .bbappend (name_%.bbappend or exact name) is parsed after its recipe
    as one metadata set, as BitBake does, and a bbappend that matches no
    recipe in the layer is rejected;
  * no runtime identity/state files in the layer, its export list or (with
    --fetch) any fetched and unpacked source;
  * coupled SuiFleet pins agree: PV equals the crate version, the codec source
    revision equals the daemon SRCREV and a commit named in a source archive
    filename equals SRCREV.

--repo checks SRCREV in a Git checkout: Cargo.lock digest, crate version,
LICENSE md5 and the generated crate include (names, versions and checksums).

--fetch skips recipes with static findings. For the rest it first requires
the unpack directory to be a new plain PN name inside the scratch directory and
confines every subdir=/destsuffix= destination and S to it (plain relative
paths, resolved through symlinks), then downloads every
non-crate source with no credentials into a new directory, verifies its
SHA-256 before unpacking, re-checks each destination before writing, unpacks
it as BitBake would, creates loose files exclusively without following links
(file:// entries and unpack=0 downloads) and checks the resulting
S: license checksums, the daemon lockfile/version/crate include, workspace
members and transitive local path-dependency crates, include targets, and the codec's exact pinned file set and provenance.
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
ASSIGN = re.compile(r'^(?:export\s+)?([A-Za-z0-9_\-${}.:/+]+?)(?:\[([^\]]+)\])?\s*(\?\?=|\?=|:=|\+=|=\+|\.=|=\.|=)\s*(["\'])(.*)\4\s*$', re.S)
# Anything shaped like an assignment; lines it matches but ASSIGN cannot read are
# recorded as unparsed so that source-affecting ones are reported, not dropped.
ASSIGN_LIKE = re.compile(r'^(?:export\s+)?([A-Za-z0-9_\-${}.:/+]+?)(?:\[[^\]]*\])?\s*(\?\?=|\?=|:=|\+=|=\+|\.=|=\.|=)')
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

    def __init__(self, path, layer, appends=()):
        self.path, self.layer = Path(path), Path(layer)
        self.appends = [Path(a) for a in appends]
        stem = self.path.name.rsplit('.', 1)[0]
        pn, _, pv = stem.partition('_')
        self.vars = {'PN': pn, 'BPN': pn, 'PV': pv or '1.0'}
        self.flags = {}
        self.overrides = []
        self.unparsed = []
        self.include_errors = []
        self.text = ''
        # BitBake semantics: ??= is a weak default used only when nothing else
        # assigns the variable, and :append/:prepend apply at expansion time,
        # after every assignment, whatever their position in the metadata.
        self.weak, self.deferred = {}, {}
        # As in BitBake, each matching .bbappend is parsed after the recipe as part
        # of the same metadata, so its assignments and appends combine with it.
        for source in (self.path, *self.appends):
            self.read(source)
        # THISDIR is only meaningful while its file is parsed (an immediate :=
        # assignment). A deferred reference would expand to whichever file
        # BitBake parsed last, so leave it unresolved for the audit to reject.
        self.vars.pop('THISDIR', None)
        for name in set(self.weak) | set(self.deferred):
            self.vars[name] = self.raw(name)
        self.weak, self.deferred = {}, {}
        self.vars.setdefault('BP', '${BPN}-${PV}')

    def raw(self, name):
        """The unexpanded value BitBake's getVar would expand for name."""
        value = self.vars.get(name, self.weak.get(name))
        if value is None and name not in self.deferred:
            return None
        value = value or ''
        for kind, text in self.deferred.get(name, ()):
            value = value + text if kind == 'append' else text + value
        return value

    def read(self, path):
        # BitBake sets THISDIR to the directory of the file being parsed,
        # including a required/included file, and restores it afterwards.
        outer = self.vars.get('THISDIR')
        self.vars['THISDIR'] = str(path.resolve().parent)
        try:
            self._read(path)
        finally:
            if outer is None:
                self.vars.pop('THISDIR', None)
            else:
                self.vars['THISDIR'] = outer

    def _read(self, path):
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
                # BitBake searches the including file's directory first and then
                # BBPATH, which reaches other layers. A standalone layer's audited
                # metadata must therefore come from a file found inside the layer:
                # an out-of-layer, unresolved or missing target is a finding, and
                # its contents are never read.
                named = self.expand(words[1])
                target = (path.parent / named).resolve()
                where = f'{path.resolve().relative_to(self.layer.resolve()).as_posix()}: {words[0]} {words[1]}'
                if '${' in named:
                    self.include_errors.append(f'{where} cannot be resolved inside the layer')
                elif self.layer.resolve() not in target.parents:
                    self.include_errors.append(f'{where} resolves outside the layer: {target}')
                elif not target.is_file():
                    self.include_errors.append(
                        f'{where} is not a file in the layer; BitBake would search BBPATH outside it'
                        if words[0] == 'include' else f'{where}: required file missing')
                else:
                    self.read(target)
                continue
            match = ASSIGN.match(line)
            if not match:
                loose = ASSIGN_LIKE.match(line)
                if loose:
                    self.unparsed.append((loose.group(1).partition(':')[0], path.name))
                continue
            name, flag, op, _, value = match.groups()
            base, _, override = name.partition(':')
            if (override not in ('', 'append', 'prepend') or (override and (flag or op in ('?=', '??=')))
                    or (flag and op == '??=')):
                # Conditional or removing overrides are not evaluated here;
                # audit_recipe rejects them on source-affecting variables.
                label = override + (f'[{flag}]' if flag else '') + (f' {op}' if op in ('?=', '??=') else '')
                self.overrides.append((base, label, path.name))
                continue
            if flag:
                # BitBake applies the same operators to flags as to variables
                # (ast.DataNode.eval): ?= keeps an already set flag, := expands
                # now, and the append/prepend forms join with the current
                # unexpanded flag. ??= on a flag differs between supported
                # BitBake releases, so it is rejected above.
                flags = self.flags.setdefault(name, {})
                old = flags.get(flag, '')
                if op == '?=':
                    flags.setdefault(flag, value)
                elif op == ':=':
                    flags[flag] = self.expand(value)
                elif op in ('+=', '.='):
                    flags[flag] = old + (' ' if op == '+=' else '') + value
                elif op in ('=+', '=.'):
                    flags[flag] = value + (' ' if op == '=+' else '') + old
                else:
                    flags[flag] = value
                continue
            if override:
                # The operator only joins the text: SRC_URI:append += "x" adds " x";
                # := expands the text now and still applies it at the end.
                value = self.expand(value) if op == ':=' else value
                text = (' ' + value if op == '+=' else value + ' ' if op == '=+' else value)
                self.deferred.setdefault(base, []).append((override, text))
                continue
            old = self.vars.get(base, '')
            if op in ('+=', '.='):
                self.vars[base] = old + (' ' if op == '+=' else '') + value
            elif op in ('=+', '=.'):
                self.vars[base] = value + (' ' if op == '=+' else '') + old
            elif op == '??=':
                # Each weak default replaces the previous one; ?= keeps the first.
                self.weak[base] = value
            elif op == '?=':
                self.vars.setdefault(base, value)
            elif op == ':=':
                self.vars[base] = self.expand(value)
            else:
                self.vars[base] = value

    def expand(self, value, depth=0):
        if depth > 20:
            raise ValueError(f'{self.path.name}: recursive variable expansion')
        if 'UNPACKDIR' in self.vars:
            value = UNPACKDIR_EXPR.sub('${UNPACKDIR}', value)
        def lookup(match):
            found = self.raw(match.group(1))
            return match.group(0) if found is None else found
        expanded = REF.sub(lookup, value)
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
        if path.is_symlink() and Path(layer).resolve() not in path.resolve().parents:
            errors.append(f'symlink leaves the layer: {rel}')
    export = Path(layer) / 'export-files.json'
    if export.is_file():
        for rel in json.loads(export.read_text()):
            if FORBIDDEN_NAME.search(rel):
                errors.append(f'export-files.json lists runtime identity/state-like file: {rel}')
            elif not (Path(layer) / rel).is_file():
                errors.append(f'export-files.json lists a missing file: {rel}')
    return errors


SOURCE_ROOTS = ('SRC_URI', 'SRCREV', 'SRCREV_FORMAT', 'PN', 'BPN', 'BP', 'PV', 'PE', 'S', 'LICENSE', 'LIC_FILES_CHKSUM',
                'FILESEXTRAPATHS', 'FILESPATH', 'CARGO_SRC_DIR', 'UNPACKDIR', 'WORKDIR')


def source_variables(recipe):
    """Source-affecting variables and every variable they reference."""
    pending = [v for v in recipe.vars if v in SOURCE_ROOTS or v.startswith(('SRCREV_', 'SUIFLEET_'))]
    pending += list(SOURCE_ROOTS) + [b for b, *_ in recipe.overrides + recipe.unparsed if b.startswith(('SRCREV_', 'SUIFLEET_'))]
    for variable, flags in recipe.flags.items():
        if variable == 'SRC_URI' or variable.startswith('SUIFLEET_'):
            for value in flags.values():
                pending += REF.findall(value)
    seen = set()
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending += REF.findall(recipe.vars.get(name, ''))
    return seen


def override_errors(recipe, where):
    names = source_variables(recipe)
    errors = [f'{where}: conditional override {base}:{override} ({file}) on a source-affecting variable is not supported; '
              'set the source pin unconditionally' for base, override, file in recipe.overrides if base in names]
    errors += [f'{where}: unparsed assignment to source-affecting variable {base} ({file}); '
               'use a plain double- or single-quoted assignment' for base, file in recipe.unparsed if base in names]
    return errors


def extra_paths(recipe):
    """FILESEXTRAPATHS entries, in the order BitBake's base_set_filespath uses."""
    return [entry.strip() for entry in recipe.get('FILESEXTRAPATHS').split(':')
            if entry.strip() and entry.strip() != '__default']


def search_path(recipe):
    """The local fetcher's FILESPATH: FILESEXTRAPATHS, then BP, BPN and files.

    Only absolute FILESEXTRAPATHS entries are searched; audit_recipe reports
    every other entry. An explicit FILESPATH is rejected by audit_recipe."""
    base = recipe.path.parent
    extra = [Path(entry) for entry in extra_paths(recipe) if '${' not in entry and Path(entry).is_absolute()]
    return extra + [base / recipe.get('BP'), base / recipe.get('BPN'), base / 'files']


def file_source(recipe, name):
    """Return the layer file BitBake would use for file://name, or None.

    Raises ValueError when the match (after following symlinks) leaves the layer,
    or when any search directory has an override subdirectory holding the same
    name. BitBake's base_set_filespath tries every FILESOVERRIDES subdirectory
    of every search directory before the unqualified directories, so such a
    file replaces this one for some MACHINE/DISTRO/arch. The audit does not
    model per-configuration overrides, so it rejects these candidates."""
    layer = recipe.layer.resolve()
    directories = search_path(recipe)
    searched = {directory.resolve() for directory in directories}
    for directory in directories:
        if not directory.is_dir():
            continue
        for sub in sorted(directory.iterdir()):
            if not sub.is_dir() or sub.resolve() in searched:
                continue
            candidate = sub / name
            if candidate.is_file() or candidate.is_symlink():
                raise ValueError(f'file:// source {name} has an override-specific candidate {candidate} '
                                 f'that BitBake selects when FILESOVERRIDES contains {sub.name!r}; '
                                 'machine/distro-specific local files are not supported')
    for directory in directories:
        candidate = directory / name
        if candidate.is_file() or candidate.is_symlink():
            resolved = candidate.resolve()
            if layer not in resolved.parents or not resolved.is_file():
                raise ValueError(f'file:// source {name} resolves outside the layer or to a non-file: {resolved}')
            return resolved
    return None


def fetched_state_errors(recipe, unpack):
    errors = []
    for path in sorted(Path(unpack).rglob('*')):
        rel = path.relative_to(unpack).as_posix()
        if '.git' not in PurePosixPath(rel).parts and (path.is_file() or path.is_symlink()) and FORBIDDEN_NAME.search(rel):
            errors.append(f'{recipe.path.name}: runtime identity/state-like file in fetched source: {rel}')
    return errors


def audit_recipe(recipe):
    errors, sources = [], []
    where = recipe.path.relative_to(recipe.layer).as_posix()
    if 'AUTOREV' in recipe.text:
        errors.append(f'{where}: AUTOREV is forbidden')
    errors += override_errors(recipe, where)
    errors += recipe.include_errors
    if 'FILESPATH' in recipe.vars:
        # The audit models BitBake's default FILESPATH (FILESEXTRAPATHS, then
        # BP, BPN and files beside the recipe). An explicit search path could
        # select a different, possibly out-of-layer, file.
        errors.append(f'{where}: explicit FILESPATH is not supported; add in-layer directories with FILESEXTRAPATHS')
    for directory in extra_paths(recipe):
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
            if scheme == 'gitsm':
                # gitsm also fetches every submodule named by .gitmodules at the
                # pinned gitlinks. The audit verifies only the top-level commit,
                # so it cannot vouch for those inputs; refuse instead of passing.
                errors.append(f'{where}: gitsm sources are not supported (submodule inputs cannot be verified); '
                              f'publish a checksum-pinned archive or list each repository as a pinned git source: {url}')
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
            else:
                try:
                    if file_source(recipe, recipe.expand(name)) is None:
                        errors.append(f'{where}: file:// source is not in the layer: {name}')
                except ValueError as error:
                    errors.append(f'{where}: {error}')
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


def daemon_errors(recipe, read, exists=None, listing=None):
    """Check coupled daemon pins with `read(relpath)` returning bytes or None.

    With `exists`, the layout is also audited; `listing(directory)` must then
    return every file path under that directory of the source layout."""
    errors = []
    src = recipe.get('CARGO_SRC_DIR', 'suifleet-edge')
    lock = read(f'{src}/Cargo.lock')
    manifest = read(f'{src}/Cargo.toml')
    if lock is None or manifest is None:
        return [f'{recipe.path.name}: source lacks {src}/Cargo.lock or Cargo.toml']
    if hashlib.sha256(lock).hexdigest() != recipe.get('SUIFLEET_LOCK_SHA256'):
        errors.append(f'{recipe.path.name}: Cargo.lock sha256 {hashlib.sha256(lock).hexdigest()} differs from SUIFLEET_LOCK_SHA256')
    packager = load_packager()
    try:
        version = packager.crate_version(manifest.decode())
        packager.workspace_members(manifest.decode())
    except ValueError as error:
        errors.append(f'{recipe.path.name}: {src}/Cargo.toml: {error}')
        version = None
    if version is not None and version != recipe.get('SUIFLEET_CRATE_VERSION'):
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
    if exists is not None and version is not None:
        def read_manifest(directory):
            path = f'{src}/{directory}/Cargo.toml'
            return read(path).decode() if exists(path) else None
        try:
            declared = packager.declared_files(manifest.decode(), read_manifest)
            for rel in declared:
                if not exists(f'{src}/{rel}'):
                    errors.append(f'{recipe.path.name}: Cargo.toml declares build file {src}/{rel}, '
                                  'missing from the source layout')
            _, loose = packager.loose_sources(manifest.decode(), read_manifest, declared, exists, read,
                                              [p for p in listing(src) if p.startswith(f'{src}/')], crate=src)
            errors += [f'{recipe.path.name}: {error}' for error in loose]
        except packager.MissingCrate as error:
            errors.append(f'{recipe.path.name}: {error.what} {src}/{error.directory} is missing from the source layout')
        except ValueError as error:
            errors.append(f'{recipe.path.name}: {src}: unsupported local crate layout: {error}')
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


def confined(root, rel, what):
    """root/rel, resolved through any existing symlinks; ValueError unless it stays inside root."""
    root = Path(root).resolve()
    if '${' in rel or rel.startswith('/') or '..' in PurePosixPath(rel).parts:
        raise ValueError(f'{what} {rel!r} is not a plain relative path')
    path = (root / rel).resolve()
    if root not in (path, *path.parents):
        raise ValueError(f'{what} {rel!r} resolves outside the unpack directory: {path}')
    return path


def write_new(root, rel, source):
    """Copy source to a new file root/rel: the parent must resolve inside root and
    the final name must not exist, so nothing is written through a link."""
    parent = confined(root, PurePosixPath(rel).parent.as_posix(), 'unpack destination')
    name = PurePosixPath(rel).name
    if name in ('', '.', '..'):
        raise ValueError(f'unpack destination {rel!r} has no file name')
    parent.mkdir(parents=True, exist_ok=True)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        except FileExistsError:
            raise ValueError(f'unpack destination {rel!r} already exists; refusing to write through or over it') from None
    finally:
        os.close(directory)
    with os.fdopen(fd, 'wb') as out, open(source, 'rb') as data:
        shutil.copyfileobj(data, out)


RESERVED_WORKDIR_NAMES = ('downloads', 'cargo-home')


def unpack_root(workdir, pn):
    """The recipe's new unpack directory: a single plain name directly inside workdir."""
    workdir = Path(workdir).resolve()
    if (pn in ('', '.', '..', *RESERVED_WORKDIR_NAMES) or '/' in pn or '\\' in pn or '${' in pn
            or pn != pn.strip() or '\0' in pn):
        raise ValueError(f'PN {pn!r} is not a usable unpack directory name')
    unpack = workdir / pn
    if unpack.is_symlink() or unpack.exists():
        raise ValueError(f'unpack directory for PN {pn!r} already exists')
    return unpack


def unpack_plan(recipe, unpack):
    """Validate every metadata-derived destination before anything is written."""
    plan = []
    for url, params in recipe.sources():
        scheme = url.split('://', 1)[0]
        if scheme == 'crate':
            continue
        if scheme == 'file':
            source = file_source(recipe, recipe.expand(url[len('file://'):]))
            rel = PurePosixPath(params.get('subdir', ''), source.name).as_posix() if source else None
            plan.append((url, params, scheme, rel and (confined(unpack, rel, 'file:// subdir'), rel)))
        elif scheme == 'gitsm':
            raise ValueError(f'gitsm submodule inputs cannot be verified: {url}')
        elif scheme == 'git':
            plan.append((url, params, scheme, confined(unpack, params.get('destsuffix', 'git'), 'destsuffix')))
        else:
            name = PurePosixPath(urllib.parse.urlsplit(url).path).name
            if name in ('', '.', '..'):
                raise ValueError(f'source URL has no file name: {url}')
            plan.append((url, params, scheme, confined(unpack, params.get('subdir', '') or '.', 'subdir')))
    s_dir = recipe.get('S')
    if not s_dir.startswith(str(unpack) + '/') and s_dir != str(unpack):
        raise ValueError(f'cannot resolve S inside the unpack directory: {s_dir}')
    return plan, confined(unpack, os.path.relpath(s_dir, unpack), 'S')


def fetch_recipe(recipe, workdir, cargo=None):
    errors, fetched = [], []
    downloads = Path(workdir).resolve() / 'downloads'
    try:
        unpack = unpack_root(workdir, recipe.get('PN'))
        recipe.vars.update(UNPACKDIR=str(unpack), WORKDIR=str(unpack))
        plan, s_dir = unpack_plan(recipe, unpack)
    except ValueError as error:
        return [f'{recipe.path.name}: {error}; nothing fetched'], fetched
    unpack.mkdir()
    downloads.mkdir(exist_ok=True)
    for url, params, scheme, destination in plan:
        if scheme == 'file':
            if destination:
                try:
                    write_new(unpack, destination[1], file_source(recipe, recipe.expand(url[len('file://'):])))
                except ValueError as error:
                    errors.append(f'{recipe.path.name}: {error}')
            continue
        # Re-resolve: an earlier archive may have created a symlink on the path.
        destination = confined(unpack, os.path.relpath(destination, unpack.resolve()), 'unpack destination')
        if scheme == 'git':
            rev = recipe.get(f'SRCREV_{params["name"]}') if 'name' in params else recipe.get('SRCREV')
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
        destination.mkdir(parents=True, exist_ok=True)
        if TARBALL.search(name) and params.get('unpack', '1') == '1':
            safe_extract(target, destination)
        else:
            try:
                write_new(unpack, os.path.relpath(destination / name, unpack.resolve()), target)
            except ValueError as error:
                errors.append(f'{recipe.path.name}: {error}')
    errors += fetched_state_errors(recipe, unpack)
    try:
        s_dir = confined(unpack, os.path.relpath(s_dir, unpack.resolve()), 'S')
    except ValueError as error:
        return errors + [f'{recipe.path.name}: {error}'], fetched
    if not s_dir.is_dir():
        return errors + [f'{recipe.path.name}: S does not exist after unpack: {s_dir.relative_to(unpack)}'], fetched
    if recipe.get('SUIFLEET_LOCK_SHA256'):
        def read(rel):
            path = s_dir / rel
            return path.read_bytes() if path.is_file() else None
        errors += daemon_errors(recipe, read, lambda rel: (s_dir / rel).is_file(),
                                lambda directory: [p.relative_to(s_dir).as_posix()
                                                   for p in (s_dir / directory).rglob('*') if p.is_file()])
        packager = load_packager()
        files = {p.relative_to(s_dir).as_posix(): ('100644', 'blob', None) for p in s_dir.rglob('*') if p.is_file()}
        # Every .rs file is scanned, and so is every manifest-declared target
        # or build script and every include!/#[path] target whatever its
        # suffix, because Rust compiles each of them as code.
        roots = {p for p in files if p.endswith('.rs')}
        src = recipe.get('CARGO_SRC_DIR', 'suifleet-edge')
        try:
            manifest = (s_dir / src / 'Cargo.toml').read_bytes().decode()

            def read_manifest(directory):
                path = s_dir / src / directory / 'Cargo.toml'
                return path.read_bytes().decode() if path.is_file() else None
            roots |= {f'{src}/{rel}' for rel in packager.declared_files(manifest, read_manifest)
                      if f'{src}/{rel}' in files}
        except (OSError, ValueError, packager.MissingCrate):
            pass  # daemon_errors above already reports an unreadable manifest or layout
        pending, scanned = sorted(roots), set()
        while pending:
            rs = pending.pop()
            if rs in scanned:
                continue
            scanned.add(rs)
            try:
                for target, code in packager.include_references(rs, (s_dir / rs).read_bytes()):
                    if target not in files:
                        errors.append(f'{recipe.path.name}: {rs} references {target}, absent from the source layout')
                    elif packager.scanned_as_code(target, code) and target not in scanned:
                        pending.append(target)
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


def append_matches(append_name, recipe_name):
    """BitBake's rule: name.bbappend applies to name.bb; '%' matches any suffix."""
    target = append_name[:-len('.bbappend')] + '.bb'
    if '%' in target:
        return recipe_name.startswith(target[:target.index('%')])
    return target == recipe_name


def check(layer, repo=None, fetch=False, cargo=None, only=None):
    layer = Path(layer).resolve()
    report = {'layer': str(layer), 'errors': runtime_state_errors(layer), 'recipes': {}}
    conf = (layer / 'conf' / 'layer.conf').read_text()
    if 'AUTOREV' in conf:
        report['errors'].append('conf/layer.conf: AUTOREV is forbidden')
    series = re.search(r'^LAYERSERIES_COMPAT_\S+\s*=\s*"([^"]*)"', conf, re.M)
    report['series'] = series.group(1).split() if series else []
    recipes, static_errors = {}, {}
    bbappends = sorted(layer.glob('recipes-*/*/*.bbappend'))
    bbfiles = sorted(layer.glob('recipes-*/*/*.bb'))
    used = set()
    for path in bbfiles:
        appends = [a for a in bbappends if append_matches(a.name, path.name)]
        used.update(appends)
        recipe = Recipe(path, layer, appends)
        pn = recipe.get('PN')
        if only and pn not in only:
            continue
        if pn in recipes:
            # Several versions of one PN are all selectable (PREFERRED_VERSION),
            # but the report, cross-checks and unpack directory are keyed by
            # PN. Refuse the ambiguity rather than audit only one of them.
            error = (f'{path.relative_to(layer).as_posix()}: PN {pn!r} is also provided by '
                     f'{report["recipes"][pn]["file"]}; the audit supports one recipe per PN')
            report['errors'].append(error)
            static_errors[pn].append(error)
            report['recipes'][pn].setdefault('duplicates', []).append(path.relative_to(layer).as_posix())
            continue
        recipes[pn] = recipe
        errors, sources = audit_recipe(recipe)
        static_errors[pn] = errors
        report['errors'] += errors
        report['recipes'][pn] = {
            'file': path.relative_to(layer).as_posix(),
            'appends': [a.relative_to(layer).as_posix() for a in appends], 'pv': recipe.get('PV'), 'license': recipe.get('LICENSE'),
            'license_files': dict(license_md5s(recipe)), 'sources': sources,
            'source_revision': recipe.get('SRCREV') or recipe.get('SUIFLEET_CODEC_SOURCE_REV') or None,
            'crate_version': recipe.get('SUIFLEET_CRATE_VERSION') or None,
            'lock_sha256': recipe.get('SUIFLEET_LOCK_SHA256') or None,
            'crates': len(crate_include(recipe)) or None,
        }
    for path in bbappends:
        if path not in used:
            # A dangling append targets a recipe outside this layer, whose
            # effective sources this audit cannot see.
            report['errors'].append(f'{path.relative_to(layer).as_posix()}: bbappend matches no recipe in the layer; '
                                    'its effect on sources cannot be audited')
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
            def listing(directory):
                result = subprocess.run(['git', '-C', str(repo), 'ls-tree', '-r', '-z', '--name-only', rev, '--',
                                         f'{directory}/'], capture_output=True, check=True)
                return [p for p in result.stdout.decode().split('\0') if p]
            report['errors'] += daemon_errors(daemon, read, lambda rel: read(rel) is not None, listing)
    if fetch:
        workdir = Path(tempfile.mkdtemp(prefix='meta-suifleet-fetch-'))
        report['fetch_dir'] = str(workdir)
        for name, recipe in recipes.items():
            if static_errors[name]:
                report['recipes'][name]['fetched'] = None
                report['recipes'][name]['fetch_skipped'] = 'static source findings'
                continue
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
