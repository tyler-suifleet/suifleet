#!/usr/bin/env python3
"""Audit meta-suifleet source pins and, optionally, fetch them anonymously.

Static checks (always):
  * no AUTOREV; git sources use https and a 40-hex SRCREV;
  * https sources carry a SHA-256 and are immutable: no query strings or
    credentials, no branch/tag archives, no raw/codeload URL at a non-commit
    ref, and no expiring release-asset redirect hosts;
  * every crate:// entry has a SHA-256;
  * file:// sources resolve, after symlinks, to regular files inside the layer
    (no FILESEXTRAPATHS outside it, no symlink leaving the layer) and use
    only the subdir and unpack=0/1 parameters (an explicit localpath would
    make BitBake read a different file);
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
  * metadata BitBake runs after parsing is refused, since it can rewrite
    SRC_URI after the reader has read it: anonymous Python functions,
    addhandler event handlers and inherit/inherit_defer of an in-layer or
    unexpandable class, in the recipe, its includes and bbappends. The one
    exception is an anonymous function built only from the reviewed skip
    guard forms (a positive grammar: `import os`, platform checks of literal
    datastore reads raising SkipRecipe and the exact reviewed missing-local-
    input block; see GUARD_*): skipping a recipe changes whether it builds,
    not its pins. Accepted guards are listed per recipe as skip_guards in the
    report;
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
(file:// entries keep their URL directories, like BitBake; unpack=0
downloads; archive formats it cannot extract are refused) and checks the resulting
S: license checksums, the daemon lockfile/version/crate include, workspace
members and transitive local path-dependency crates, include targets, and the codec's exact pinned file set and provenance.
--cargo additionally runs `cargo metadata --offline --locked --no-deps`.

The JSON report maps each recipe to its version, source revision, sources,
checksums and licenses. Parsing here is a source audit, not a BitBake build.
"""
import argparse
import ast
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
import textwrap
import urllib.parse
import urllib.request

HEX40 = re.compile(r'^[0-9a-f]{40}$')
HEX64 = re.compile(r'^[0-9a-f]{64}$')
ASSIGN = re.compile(r'^(?:export\s+)?([A-Za-z0-9_\-${}.:/+]+?)(?:\[([^\]]+)\])?\s*(\?\?=|\?=|:=|\+=|=\+|\.=|=\.|=)\s*(["\'])(.*)\4\s*$', re.S)
# Anything shaped like an assignment; lines it matches but ASSIGN cannot read are
# recorded as unparsed so that source-affecting ones are reported, not dropped.
ASSIGN_LIKE = re.compile(r'^(?:export\s+)?([A-Za-z0-9_\-${}.:/+]+?)(\[[^\]]*\])?\s*(\?\?=|\?=|:=|\+=|=\+|\.=|=\.|=)')
REF = re.compile(r'\$\{([A-Za-z0-9_\-]+)\}')
# The layer's Scarthgap/Wrynose unpack-root idiom; no other inline Python is evaluated.
UNPACKDIR_EXPR = re.compile(r"""\$\{@d\.getVar\(['"]UNPACKDIR['"]\)\s+or\s+d\.getVar\(['"]WORKDIR['"]\)\}""")
# Any other inline Python expansion. BitBake evaluates it whenever a value
# containing it is expanded (by a getVar, a := assignment, signature
# generation or a class's anonymous Python, transitively through references),
# and the code can write the datastore, so audited metadata must not carry it.
INLINE_PYTHON = re.compile(r'\$\{@')
# BitBake's anonymous Python forms: python () {, python __anonymous () {, with
# optional fakeroot and spacing. They run at the end of parsing (ast.finalize).
# BitBake directives the reader does not model. unset removes a variable or
# flag (SRC_URI, SRCREV, a checksum), include_all reads the named file from
# every BBPATH layer, and addfragments/addpylib load further metadata or code.
UNMODELLED_DIRECTIVES = frozenset({'unset', 'include_all', 'addfragments', 'addpylib'})
ANONYMOUS_PYTHON = re.compile(r'^(?:fakeroot\s+)?python(?:\s+__anonymous)?\s*\(\s*\)\s*\{$')
# Anonymous Python the audit accepts without running it: a parse-time skip
# guard. Skipping a recipe changes whether it is built, never which sources it
# pins. Rather than trying to prove arbitrary Python read-only, the audit
# accepts only the reviewed guard forms of the shipped recipes (SF-307's
# suifleet-node and suifleet-walrus-codec), described by a positive grammar.
# The body must be a sequence of these statements and nothing else:
# * `import os`;
# * a platform skip, `if COND: raise bb.parse.SkipRecipe(MESSAGE)` with no
#   else, where COND compares a literal datastore read (d.getVar(NAME[,
#   EXPAND]) or d.getVarFlag(NAME, FLAG[, EXPAND]), see GUARD_D_METHODS) with
#   a string constant using ==, !=, in or not in, optionally combined with
#   and/or/not, and MESSAGE is a string constant, optionally `%` one literal
#   read or a tuple of them;
# * the reviewed missing-local-input block (GUARD_LOCAL_INPUT): it resolves
#   SRC_URI's file:// entries with bb.fetch2.Fetch(..., localonly=True), calls
#   that fetcher's local method's search path, records each candidate with
#   bb.parse.mark_dependency and skips when an input is missing. It must match
#   the reviewed statements exactly (AST equality; comments and the skip
#   message text are not compared), so every receiver and call is fixed.
# No other statement, call, name, attribute or binding is accepted, so the
# datastore is only ever read or handed to those fixed BitBake calls. A read
# expands its value, which could run inline Python reached through references;
# the audit refuses inline Python anywhere in the recipe, its includes and
# bbappends (INLINE_PYTHON), so a guard's reads only reach that metadata and
# the base layers the build already trusts. A new guard form must extend this
# grammar deliberately, with review.
# Each read method with its number of literal string arguments.
GUARD_D_METHODS = {'getVar': 1, 'getVarFlag': 2}
GUARD_COMPARE_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
GUARD_LOCAL_INPUT = '''\
local = [entry for entry in (d.getVar('SRC_URI') or '').split() if entry.startswith('file://')]
missing = []
if local:
    fetcher = bb.fetch2.Fetch(local, d, cache=False, localonly=True)
    for entry in local:
        ud = fetcher.ud[entry]
        search = getattr(ud.method, 'localfile_searchpaths', None) or ud.method.localpaths
        searched = search(ud, d)
        for path in searched:
            bb.parse.mark_dependency(d, path)
        if not searched or not os.path.exists(searched[-1]):
            missing.append(entry[len('file://'):].split(';')[0])
if missing:
    raise bb.parse.SkipRecipe('' % ', '.join(missing))
'''


def is_skip_recipe(node):
    """Whether node is the name bb.parse.SkipRecipe."""
    return (isinstance(node, ast.Attribute) and node.attr == 'SkipRecipe'
            and isinstance(node.value, ast.Attribute) and node.value.attr == 'parse'
            and isinstance(node.value.value, ast.Name) and node.value.value.id == 'bb')


def normalized(statements):
    """AST dump of statements with each SkipRecipe message constant blanked."""
    module = ast.Module(body=list(statements), type_ignores=[])
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and is_skip_recipe(node.func):
            for message in ast.walk(node):
                if isinstance(message, ast.Constant) and isinstance(message.value, str):
                    message.value = ''
    return ast.dump(module, annotate_fields=True, include_attributes=False)


def literal_read_error(call):
    """None if call is d.getVar/d.getVarFlag with literal arguments (see
    GUARD_D_METHODS): the names (and flag) as strings, then optionally expand
    as a bool, given positionally or as expand=."""
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name) and call.func.value.id == 'd'
            and call.func.attr in GUARD_D_METHODS):
        return 'only literal d.getVar/d.getVarFlag reads are allowed'
    method = call.func.attr
    names = GUARD_D_METHODS[method]
    args, keywords = call.args, call.keywords
    if any(isinstance(a, ast.Starred) for a in args) or any(k.arg != 'expand' for k in keywords):
        return f'd.{method} must be called with literal arguments'
    if len(args) < names or len(args) + len(keywords) > names + 1:
        return f'd.{method} must be called with literal arguments'
    if not all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args[:names]):
        return f'd.{method} must read a literal variable name'
    extra = args[names:] + [k.value for k in keywords]
    if not all(isinstance(a, ast.Constant) and isinstance(a.value, bool) for a in extra):
        return f'd.{method} expand argument must be a literal True or False'
    return None


def string_constant(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def skip_condition_error(node):
    """None if node is a platform-skip condition (see the grammar above)."""
    if isinstance(node, ast.BoolOp):
        return next(filter(None, map(skip_condition_error, node.values)), None)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return skip_condition_error(node.operand)
    if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], GUARD_COMPARE_OPS)
            and string_constant(node.comparators[0])):
        return literal_read_error(node.left)
    return 'a skip condition must compare a literal datastore read with a string constant'


def skip_message_error(node):
    """None if node is a string constant, optionally % literal read(s)."""
    if string_constant(node):
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and string_constant(node.left):
        values = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        return next(filter(None, map(literal_read_error, values)), None)
    return 'a skip message must be a string constant, optionally formatted with literal datastore reads'


def platform_skip_error(statement):
    """None if statement is `if COND: raise bb.parse.SkipRecipe(MESSAGE)`."""
    if not (isinstance(statement, ast.If) and not statement.orelse and len(statement.body) == 1):
        return 'not a reviewed skip-guard statement'
    raised = statement.body[0]
    if not (isinstance(raised, ast.Raise) and raised.cause is None and isinstance(raised.exc, ast.Call)
            and is_skip_recipe(raised.exc.func) and len(raised.exc.args) == 1 and not raised.exc.keywords):
        return 'a skip condition must only raise bb.parse.SkipRecipe(message)'
    return skip_condition_error(statement.test) or skip_message_error(raised.exc.args[0])


def skip_guard_error(body):
    """None if the anonymous Python body consists only of the reviewed skip
    guard forms (see the grammar above), else the first reason it does not."""
    text = textwrap.dedent('\n'.join(body) + '\n')
    try:
        statements = ast.parse(text).body
    except SyntaxError as error:
        return f'Python body does not parse ({error.msg})'
    local_input = ast.parse(GUARD_LOCAL_INPUT).body
    template = normalized(local_input)
    if not statements:
        return 'empty body is not a skip guard'
    index = 0
    while index < len(statements):
        statement = statements[index]
        window = statements[index:index + len(local_input)]
        if (isinstance(statement, ast.Import) and len(statement.names) == 1
                and statement.names[0].name == 'os' and statement.names[0].asname is None):
            index += 1
        elif len(window) == len(local_input) and normalized(window) == template:
            index += len(local_input)
        else:
            reason = platform_skip_error(statement)
            if reason:
                return f'line {statement.lineno}: {reason}'
            index += 1
    return None


TARBALL = re.compile(r'\.(tar(\.(gz|xz|bz2|zst))?|tgz|tbz2|txz)$')
# Every suffix BitBake's FetchMethod.unpack runs an unpack command for (tar and
# its compressed forms, bare gz/bz2/xz/lz/lzma/zst/Z, zip/jar, rpm/srpm,
# deb/ipk, 7z) plus squashfs, matched case-insensitively so near misses are
# refused rather than copied. A source whose local name matches is extracted
# by BitBake unless unpack=0, so the audit must not copy it as a loose file.
BITBAKE_UNPACKED = re.compile(r'\.(tar|tgz|tbz2?|txz|tlz|tzst|gz|bz2|xz|lz|lzma|zst|z|zip|jar|rpm|srpm|deb|ipk|7z|squashfs)$', re.I)


NOTHING = re.compile(r'(?!)')


def unsupported_unpack(name, params, extracts=TARBALL):
    """An error message when BitBake would unpack name (unpack is not 0) with a
    method the audit does not reproduce (anything extracts does not match)."""
    if params.get('unpack', '1') != '0' and BITBAKE_UNPACKED.search(name) and not extracts.search(name):
        return (f'{name} is an archive BitBake unpacks, in a format the audit does not '
                'extract; use a supported tarball or unpack=0')
    return None
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
        # Metadata BitBake runs after parsing (anonymous Python, event handlers,
        # in-layer classes). It can rewrite SRC_URI and the other pins, and
        # this reader does not execute it, so audit_recipe refuses it.
        self.parse_time = []
        # Anonymous Python accepted as a read-only parse-time skip guard
        # (skip_guard_error); reported, never executed.
        self.guards = []
        # Inline Python expansions (${@...}) other than the UNPACKDIR idiom, as
        # file:line. BitBake runs them on expansion; audit_recipe refuses them.
        self.inline_python = []
        # Directives this reader does not model (unset, include_all, ...). They
        # can remove or replace source pins, so audit_recipe refuses them.
        self.unmodelled = []
        # Assignments whose variable name contains a ${...} reference. BitBake
        # expands such keys when it finalizes the datastore (SRC_${X} can
        # become SRC_URI and replace it), which this reader does not model, so
        # audit_recipe refuses them. Overrides such as RDEPENDS:${PN} keep a
        # literal variable name and are not affected.
        self.expanded_keys = []
        # Variables an include/require operand referenced when it was expanded,
        # with everything they referenced at that point. They select which
        # metadata (and so which sources) is read, so they are source-affecting:
        # a machine/distro override of one could select a different file.
        self.include_refs = set()
        # For each variable (or the variable owning a flag/append) assigned
        # with :=, every variable the value referenced when it was expanded.
        # The stored value is already expanded, but BitBake's immediate
        # expansion sees the active machine/distro overrides, so the result
        # still depends on those variables and inherits their provenance.
        self.immediate_refs = {}
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

    def anonymous_python(self, path, header, body):
        """Accept a parse-time availability guard; refuse any other anonymous
        Python, which BitBake runs at the end of parsing and which could
        rewrite the pins this reader has already read."""
        reason = skip_guard_error(body)
        where = f'{self.rel(path)}: anonymous Python function ({header})'
        if reason is None:
            self.guards.append(where)
        else:
            self.parse_time.append(f'{where}, not a read-only skip guard: {reason}')

    def rel(self, path):
        return path.resolve().relative_to(self.layer.resolve()).as_posix()

    def layer_class(self, name):
        """Whether BitBake could load class name from this layer (classes,
        classes-recipe or classes-global), whose metadata this reader skips."""
        return any((self.layer / d / f'{name}.bbclass').exists()
                   for d in ('classes', 'classes-recipe', 'classes-global'))

    def _read(self, path):
        text = path.read_text()
        self.text += text
        # Each logical line (continuations joined, as BitBake reads them),
        # numbered by its first physical line; values are never reported.
        number = 1
        for logical in re.split(r'(?<!\\)\n', text):
            if INLINE_PYTHON.search(UNPACKDIR_EXPR.sub('', logical.replace('\\\n', ''))):
                self.inline_python.append(f'{self.rel(path)}:{number}')
            number += logical.count('\n') + 1
        lines, block, anonymous = [], False, None
        for line in text.replace('\\\n', ' ').splitlines():
            stripped = line.strip()
            if block:
                block = stripped != '}'
                if anonymous is not None:
                    if block:
                        anonymous[1].append(line)
                    else:
                        self.anonymous_python(path, *anonymous)
                        anonymous = None
                continue
            if stripped.endswith('{') and ('()' in stripped or stripped.startswith('python')):
                if ANONYMOUS_PYTHON.match(stripped):
                    anonymous = (stripped.rstrip('{').strip(), [])
                block = True
                continue
            lines.append(stripped)
        if anonymous is not None:
            # An unterminated function: BitBake would not parse it either.
            self.parse_time.append(f'{self.rel(path)}: anonymous Python function ({anonymous[0]}) '
                                   'without a closing brace')
        for line in lines:
            if not line or line.startswith('#'):
                continue
            words = line.split()
            if words[0] == 'addhandler':
                self.parse_time.append(f'{self.rel(path)}: event handler ({line})')
                continue
            if words[0] in ('inherit', 'inherit_defer'):
                for name in words[1:]:
                    if '${' in name or self.layer_class(name):
                        self.parse_time.append(f'{self.rel(path)}: {words[0]} of in-layer or unresolved class {name}')
                continue
            if words[0] in UNMODELLED_DIRECTIVES:
                self.unmodelled.append(f'{self.rel(path)}: {line}')
                continue
            if words[0] in ('require', 'include') and len(words) > 1:
                # BitBake expands the whole operand and includes every
                # space-separated file name in it, in order. It searches the
                # including file's directory first and then BBPATH, which
                # reaches other layers. A standalone layer's audited metadata
                # must therefore come from files found inside the layer: an
                # out-of-layer, unresolved or missing target is a finding, and
                # its contents are never read.
                operand = line.split(None, 1)[1]
                where = f'{self.rel(path)}: {words[0]} {operand}'
                self.include_refs |= self.references(operand)
                expanded = self.expand(operand)
                if '${' in expanded:
                    self.include_errors.append(f'{where} cannot be resolved inside the layer')
                    continue
                for named in expanded.split():
                    target = (path.parent / named).resolve()
                    if self.layer.resolve() not in target.parents:
                        self.include_errors.append(f'{where} resolves outside the layer: {target}')
                    elif not target.is_file():
                        self.include_errors.append(
                            f'{where}: {named} is not a file in the layer; BitBake would search BBPATH outside it'
                            if words[0] == 'include' else f'{where}: required file missing: {named}')
                    else:
                        self.read(target)
                continue
            match = ASSIGN.match(line)
            loose = match or ASSIGN_LIKE.match(line)
            if loose and '${' in loose.group(1).partition(':')[0]:
                # Record only the variable name: the assigned value (and a
                # flag name) may carry credentials or signed URLs that the
                # report must never copy.
                shape = ASSIGN_LIKE.match(line)
                flag = '[flag]' if shape.group(2) is not None else ''
                self.expanded_keys.append(f'{self.rel(path)}: {shape.group(1)}{flag} {shape.group(3)} <value omitted>')
                continue
            if not match:
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
                    flags[flag] = self.immediate(name, value)
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
                value = self.immediate(base, value) if op == ':=' else value
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
                self.vars[base] = self.immediate(base, value)
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

    def immediate(self, name, value):
        """Expand value now for a := assignment to name, recording which
        variables the expansion depended on."""
        self.immediate_refs.setdefault(name, set()).update(self.references(value))
        return self.expand(value)

    def references(self, value):
        """Every variable value references, directly, through the current
        unexpanded values of the variables it references, or through what
        those variables referenced when they were immediately expanded."""
        seen, pending = set(), REF.findall(value)
        while pending:
            name = pending.pop()
            if name not in seen:
                seen.add(name)
                pending += REF.findall(self.raw(name) or '')
                pending += sorted(self.immediate_refs.get(name, ()))
        return seen

    def get(self, name, default=''):
        return self.expand(self.vars.get(name, default))

    def sources(self):
        for entry in self.get('SRC_URI').split():
            url, *params = entry.split(';')
            yield url, dict(p.split('=', 1) for p in params if '=' in p)


# Git SRC_URI parameters whose fetch/unpack semantics the audit reproduces.
# Anything else (subpath, nocheckout, bareclone, rev, tag, usehead, lfs, ...)
# changes what BitBake fetches or the unpacked layout, so it is refused rather
# than verified against a layout the recipe would not get.
GIT_PARAMS = frozenset({'protocol', 'branch', 'nobranch', 'name', 'destsuffix'})


def unmodelled_git_params(params):
    return sorted(set(params) - GIT_PARAMS)


# HTTPS SRC_URI parameters the audit reproduces: the checksum name or inline
# pin, the unpack subdir, unpack=0/1 and downloadfilename (the local file name
# BitBake stores and unpacks by suffix). Anything else (striplevel, md5sum,
# localpath, ...) could change the fetched file or unpacked layout, so it is
# refused rather than verified against a layout the recipe would not get.
HTTPS_PARAMS = frozenset({'name', 'sha256sum', 'subdir', 'unpack', 'downloadfilename'})


def unmodelled_https_params(params):
    unmodelled = set(params) - HTTPS_PARAMS
    if params.get('unpack', '1') not in ('0', '1'):
        unmodelled.add(f'unpack={params["unpack"]}')
    # BitBake uses a downloadfilename value literally (it never percent-decodes
    # parameters), so an escape would name a file whose suffix differs from the
    # decoded name. Refuse the ambiguous form instead of guessing either way.
    if '%' in params.get('downloadfilename', ''):
        unmodelled.add('downloadfilename with a percent escape')
    return sorted(unmodelled)


# file:// SRC_URI parameters the audit reproduces: the unpack subdir and
# unpack=0/1. Anything else (localpath, downloadfilename, striplevel, ...) can
# make BitBake select or place a different file than the layer-relative one the
# audit checks (FetchData.setup_localpath honours an explicit localpath), so it
# is refused rather than verified against a source the recipe would not use.
FILE_PARAMS = frozenset({'subdir', 'unpack'})


def unmodelled_file_params(params):
    unmodelled = set(params) - FILE_PARAMS
    if params.get('unpack', '1') not in ('0', '1'):
        unmodelled.add(f'unpack={params["unpack"]}')
    return sorted(unmodelled)


def download_name(url, params):
    """The local file name BitBake's wget fetcher stores and unpacks: downloadfilename
    when set, taken literally like BitBake's parameter parser (its suffix selects
    the unpack method), otherwise the percent-decoded URL basename (BitBake's
    decodeurl unquotes the path only)."""
    if 'downloadfilename' in params:
        name = params['downloadfilename']
    else:
        name = urllib.parse.unquote(PurePosixPath(urllib.parse.urlsplit(url).path).name)
    if name in ('', '.', '..') or '/' in name or '\\' in name or '${' in name or '\0' in name:
        raise ValueError(f'source has no usable local file name {name!r}: {redact_url(url)}')
    return name


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
    # Variables that chose an included file decide which source metadata exists.
    pending += sorted(recipe.include_refs)
    for variable, flags in recipe.flags.items():
        if variable == 'SRC_URI' or variable.startswith('SUIFLEET_'):
            # Also walk the variable itself, which carries the provenance of
            # any flag assigned with := (see Recipe.immediate).
            pending.append(variable)
            for value in flags.values():
                pending += REF.findall(value)
    seen = set()
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending += REF.findall(recipe.vars.get(name, ''))
            # A := value is stored expanded; keep what it was expanded from.
            pending += sorted(recipe.immediate_refs.get(name, ()))
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
    errors += [f'{where}: {item} is a directive the audit does not model; it can remove or replace '
               'source pins and is not supported in audited recipes, includes or appends' for item in recipe.unmodelled]
    errors += [f'{where}: {item} assigns a variable whose name BitBake expands (key expansion); it can '
               'replace source pins and is not supported in audited recipes, includes or appends'
               for item in recipe.expanded_keys]
    errors += [f'{where}: {item} runs after parsing and can change source pins the audit '
               'reads; it is not supported in audited recipes, includes or appends' for item in recipe.parse_time]
    errors += [f'{where}: {item} uses inline Python (${{@...}}), which BitBake runs whenever the value is '
               'expanded and which can change source pins; only the UNPACKDIR idiom is supported'
               for item in recipe.inline_python]
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
        shown = redact_url(url)
        entry = {'url': shown, 'type': scheme}
        parsed = urllib.parse.urlsplit(url.replace('git://', 'https://', 1) if scheme in ('git', 'gitsm') else url)
        if parsed.username or parsed.password or '@' in parsed.netloc:
            errors.append(f'{where}: credentials in source URL {shown}')
        if scheme in ('git', 'gitsm'):
            name = params.get('name')
            rev = recipe.get(f'SRCREV_{name}') if name else srcrev
            entry['revision'] = rev
            if params.get('protocol', 'git') != 'https':
                errors.append(f'{where}: git source must use protocol=https: {shown}')
            if not HEX40.match(rev or ''):
                errors.append(f'{where}: git source needs a 40-hex SRCREV, got {rev!r}')
            # BitBake checks that SRCREV is on the named branch unless
            # nobranch=1. Require one of the two explicitly (newer BitBake
            # rejects a missing branch) so the --fetch check can model it.
            if params.get('nobranch') != '1' and not params.get('branch'):
                errors.append(f'{where}: git source needs branch=NAME (SRCREV must be on it) or nobranch=1: {shown}')
            if ',' in params.get('branch', '') or ',' in params.get('name', ''):
                errors.append(f'{where}: git source with several names or branches is not supported: {shown}')
            if unmodelled_git_params(params):
                errors.append(f'{where}: git source parameters the audit does not model: '
                              f'{", ".join(unmodelled_git_params(params))}: {shown}')
            if scheme == 'gitsm':
                # gitsm also fetches every submodule named by .gitmodules at the
                # pinned gitlinks. The audit verifies only the top-level commit,
                # so it cannot vouch for those inputs; refuse instead of passing.
                errors.append(f'{where}: gitsm sources are not supported (submodule inputs cannot be verified); '
                              f'publish a checksum-pinned archive or list each repository as a pinned git source: {shown}')
        elif scheme in ('https', 'http', 'ftp'):
            sha = checksum_for(recipe, params)
            entry['sha256'] = sha
            if scheme != 'https':
                errors.append(f'{where}: source must use https: {shown}')
            if not HEX64.match(sha):
                errors.append(f'{where}: https source lacks a SHA-256 pin: {shown}')
            if parsed.query or parsed.fragment:
                errors.append(f'{where}: query/fragment (signed or expiring URL) in {shown}')
            for pattern, label in MUTABLE_URL:
                if pattern.search(url):
                    errors.append(f'{where}: mutable or expiring source ({label}): {shown}')
            if unmodelled_https_params(params):
                errors.append(f'{where}: https source parameters the audit does not model: '
                              f'{", ".join(unmodelled_https_params(params))}: {shown}')
            try:
                local = download_name(url, params)
                entry['file'] = local
                if unsupported_unpack(local, params):
                    errors.append(f'{where}: {unsupported_unpack(local, params)}: {shown}')
            except ValueError as error:
                errors.append(f'{where}: {error}')
                local = None
            for archive in dict.fromkeys(filter(None, (PurePosixPath(parsed.path).name, local))):
                commits = re.findall(r'(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])', archive)
                if commits and HEX40.match(srcrev or '') and commits[-1] != srcrev:
                    errors.append(f'{where}: archive {archive} names commit {commits[-1]}, SRCREV is {srcrev}')
        elif scheme == 'crate':
            parts = url.split('/')
            sha = checksum_for(recipe, params, f'{parts[-2]}-{parts[-1]}')
            if not HEX64.match(sha):
                errors.append(f'{where}: crate {parts[-2]} {parts[-1]} lacks a SHA-256')
            continue
        elif scheme == 'file':
            name = url[len('file://'):]
            if unmodelled_file_params(params):
                errors.append(f'{where}: file:// source parameters the audit does not model: '
                              f'{", ".join(unmodelled_file_params(params))}: {shown}')
            if name.startswith('/') or '..' in PurePosixPath(name).parts:
                errors.append(f'{where}: file:// source escapes the layer: {name}')
            else:
                try:
                    if file_source(recipe, recipe.expand(name)) is None:
                        errors.append(f'{where}: file:// source is not in the layer: {name}')
                except ValueError as error:
                    errors.append(f'{where}: {error}')
                # Local files are only copied by the audit, never extracted.
                problem = unsupported_unpack(PurePosixPath(recipe.expand(name)).name, params, extracts=NOTHING)
                if problem:
                    errors.append(f'{where}: file:// source {problem}')
            continue
        else:
            errors.append(f'{where}: unsupported fetcher {scheme}: {shown}')
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


URL_TEXT = re.compile(r'\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\'"<>]+')


def redact_url(url):
    """The URL without userinfo, query or fragment, for diagnostics.

    Rejected credentials or signed/expiring URL parameters must not reach a
    saved report or CI log, so every URL shown is passed through here."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return '<unparseable URL redacted>'
    netloc = parts.netloc
    if '@' in netloc:
        netloc = '<redacted>@' + netloc.rsplit('@', 1)[1]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path,
                                    '<redacted>' if parts.query else '', '<redacted>' if parts.fragment else ''))


def redact(value):
    """Redact every URL inside a report value (strings, lists, dicts)."""
    if isinstance(value, str):
        return URL_TEXT.sub(lambda m: redact_url(m.group(0)), value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def git_remote(url):
    return 'https://' + url.split('://', 1)[1]


def git_branch_error(repo, remote, branch, rev, env):
    """Model BitBake's branch check: SRCREV must be reachable from the named
    remote branch. Returns an error string, or None when it is."""
    if not branch or branch.startswith('-') or '..' in branch or branch.endswith('/'):
        return f'git branch {branch!r} is not a valid branch name'
    git = ['git', '-c', 'credential.helper=', '-C', str(repo)]
    ref = f'refs/heads/{branch}'
    listed = subprocess.run([*git, 'ls-remote', '--heads', remote, ref], env=env, capture_output=True, text=True)
    if listed.returncode != 0:
        return f'could not list branch {branch!r} on {redact_url(remote)}'
    if not any(line.split('\t', 1)[-1] == ref for line in listed.stdout.splitlines()):
        return f'branch {branch!r} does not exist on {redact_url(remote)}; SRCREV {rev} cannot be on it'
    local = 'refs/suifleet-audit/branch'
    if subprocess.run([*git, 'fetch', '-q', remote, f'+{ref}:{local}'], env=env).returncode != 0:
        return f'could not fetch branch {branch!r} from {redact_url(remote)}'
    contained = subprocess.run([*git, 'merge-base', '--is-ancestor', rev, local], env=env)
    if contained.returncode == 1:
        return f'SRCREV {rev} is not on branch {branch!r}'
    if contained.returncode != 0:
        return f'could not check whether SRCREV {rev} is on branch {branch!r}'
    return None


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
    if recipe.parse_time:
        # SRC_URI as read here may not be what BitBake fetches after running it.
        raise ValueError(f'metadata that runs after parsing is not supported: {"; ".join(recipe.parse_time)}')
    if recipe.unmodelled or recipe.include_errors or recipe.expanded_keys or recipe.inline_python:
        # SRC_URI as read here may not be what BitBake would fetch.
        inline = [f'{item} inline Python' for item in recipe.inline_python]
        raise ValueError('unmodelled or unresolved metadata is not supported: '
                         f'{"; ".join(recipe.unmodelled + recipe.expanded_keys + recipe.include_errors + inline)}')
    plan = []
    for url, params in recipe.sources():
        scheme = url.split('://', 1)[0]
        if scheme == 'crate':
            continue
        if scheme == 'file':
            if unmodelled_file_params(params):
                raise ValueError(f'file:// source parameters the audit does not model: '
                                 f'{", ".join(unmodelled_file_params(params))}: {url}')
            name = recipe.expand(url[len('file://'):])
            problem = unsupported_unpack(PurePosixPath(name).name, params, extracts=NOTHING)
            if problem:
                raise ValueError(f'file:// source {problem}: {url}')
            source = file_source(recipe, name)
            # BitBake's FetchMethod.unpack copies file://a/b/c to subdir/a/b/c,
            # keeping the URL's intermediate directories (without leading or
            # trailing slashes), so the audit writes the same relative path.
            rel = PurePosixPath(params.get('subdir', ''), name.strip('/')).as_posix() if source else None
            plan.append((url, params, scheme, rel and (confined(unpack, rel, 'file:// subdir'), rel)))
        elif scheme == 'gitsm':
            raise ValueError(f'gitsm submodule inputs cannot be verified: {redact_url(url)}')
        elif scheme == 'git':
            if unmodelled_git_params(params):
                raise ValueError(f'git source parameters the audit does not model: '
                                 f'{", ".join(unmodelled_git_params(params))}: {redact_url(url)}')
            plan.append((url, params, scheme, confined(unpack, params.get('destsuffix', 'git'), 'destsuffix')))
        else:
            if unmodelled_https_params(params):
                raise ValueError(f'https source parameters the audit does not model: '
                                 f'{", ".join(unmodelled_https_params(params))}: {redact_url(url)}')
            problem = unsupported_unpack(download_name(url, params), params)
            if problem:
                raise ValueError(f'{problem}: {redact_url(url)}')
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
            remote = git_remote(url)
            env = anonymous_env(Path(workdir))
            for command in (['init', '-q', str(destination)], ['-C', str(destination), 'fetch', '-q', '--depth', '1', remote, rev],
                            ['-C', str(destination), 'checkout', '-q', '--detach', 'FETCH_HEAD']):
                subprocess.run(['git', '-c', 'credential.helper=', *command], env=env, check=True)
            actual = subprocess.check_output(['git', '-C', str(destination), 'rev-parse', 'HEAD'], env=env, text=True).strip()
            branch_error = None if params.get('nobranch') == '1' else git_branch_error(destination, remote, params.get('branch', ''), rev, env)
            fetched.append({'url': redact_url(url), 'revision': actual, 'branch': params.get('branch') if params.get('nobranch') != '1' else None,
                            'verified': actual == rev and branch_error is None})
            if actual != rev:
                errors.append(f'{recipe.path.name}: fetched {actual}, SRCREV is {rev}')
            if branch_error:
                errors.append(f'{recipe.path.name}: {branch_error}')
            continue
        name = download_name(url, params)
        target = downloads / name
        expected = checksum_for(recipe, params)
        actual = download(url, target)
        fetched.append({'url': redact_url(url), 'sha256': actual, 'verified': actual == expected, 'bytes': target.stat().st_size})
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


# BitBake parses every layer.conf into the base configuration that each recipe
# inherits, so any other statement in it (an assignment, :append, include,
# inherit, Python, ...) can change SRC_URI or the pins without appearing in a
# recipe. Only these layer registration forms, with these exact values, are
# accepted; the collection name comes from BBFILE_COLLECTIONS.
LAYER_CONF_FIXED = {
    ('BBPATH', '.='): (':${LAYERDIR}',),
    ('BBFILES', '+='): ('${LAYERDIR}/recipes-*/*/*.bb', '${LAYERDIR}/recipes-*/*/*.bbappend'),
}
LAYER_CONF_PER_COLLECTION = {
    'BBFILE_PATTERN': re.compile(r'^\^\$\{LAYERDIR\}/$'),
    'BBFILE_PRIORITY': re.compile(r'^[0-9]+$'),
    'LAYERVERSION': re.compile(r'^[0-9]+$'),
    'LAYERDEPENDS': re.compile(r'^[A-Za-z0-9_\-:.<>= ]*$'),
    'LAYERRECOMMENDS': re.compile(r'^[A-Za-z0-9_\-:.<>= ]*$'),
    'LAYERSERIES_COMPAT': re.compile(r'^[a-z ]*$'),
}
LAYER_CONF_STATEMENT = re.compile(r'^([A-Za-z0-9_\-]+)\s*(\.=|\+=|=)\s*"([^"]*)"$')


def layer_conf_errors(text):
    """Refuse every layer.conf statement that is not plain layer registration."""
    statements = [line.strip() for line in text.replace('\\\n', ' ').splitlines()]
    statements = [line for line in statements if line and not line.startswith('#')]
    collections = []
    for line in statements:
        match = LAYER_CONF_STATEMENT.match(line)
        if match and match.group(1) == 'BBFILE_COLLECTIONS' and match.group(2) == '+=':
            collections += match.group(3).split()
    errors = []
    for line in statements:
        match = LAYER_CONF_STATEMENT.match(line)
        accepted = False
        if match:
            name, op, value = match.groups()
            if (name, op) in LAYER_CONF_FIXED:
                accepted = value in LAYER_CONF_FIXED[(name, op)]
            elif name == 'BBFILE_COLLECTIONS':
                accepted = op == '+=' and len(value.split()) == 1 and re.fullmatch(r'[A-Za-z0-9\-]+', value) is not None
            elif op == '=' and '${' not in value.replace('${LAYERDIR}', ''):
                for prefix, pattern in LAYER_CONF_PER_COLLECTION.items():
                    suffix = name[len(prefix) + 1:] if name.startswith(prefix + '_') else None
                    # Without a BBFILE_COLLECTIONS line the name stays unchecked
                    # (as in a minimal fixture); it still cannot be an override.
                    if suffix and (suffix in collections or not collections) and pattern.match(value):
                        accepted = True
        if not accepted:
            errors.append(f'conf/layer.conf: {line!r} is not a layer registration statement the audit accepts; '
                          'BitBake applies layer.conf to every recipe, so its effect on sources cannot be audited')
    if len(collections) > 1:
        errors.append(f'conf/layer.conf: several BBFILE_COLLECTIONS {collections}; the audit supports one collection')
    return errors


def layer_registration_errors(text, recipes, appends):
    """Require the BBFILES registration that makes BitBake parse the audited files.

    The audit globs recipes and appends directly; without the matching
    registration BitBake would not parse them, so a clean report would describe
    files a standalone build never uses.
    """
    statements = [line.strip() for line in text.replace('\\\n', ' ').splitlines()]
    registered = set()
    for line in statements:
        match = LAYER_CONF_STATEMENT.match(line)
        if match and match.group(1) == 'BBFILES' and match.group(2) == '+=':
            registered.add(match.group(3))
    required = []
    if recipes:
        required.append(LAYER_CONF_FIXED[('BBFILES', '+=')][0])
    if appends:
        required.append(LAYER_CONF_FIXED[('BBFILES', '+=')][1])
    return [f'conf/layer.conf: BBFILES += "{value}" is missing; BitBake would not parse the audited '
            f'{"recipes" if value.endswith(".bb") else "appends"}, so the audit would not describe the build'
            for value in required if value not in registered]


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
    bbappends = sorted(layer.glob('recipes-*/*/*.bbappend'))
    bbfiles = sorted(layer.glob('recipes-*/*/*.bb'))
    conf_errors = layer_conf_errors(conf) + layer_registration_errors(conf, bbfiles, bbappends)
    report['errors'] += conf_errors
    series = re.search(r'^LAYERSERIES_COMPAT_\S+\s*=\s*"([^"]*)"', conf, re.M)
    report['series'] = series.group(1).split() if series else []
    recipes, static_errors = {}, {}
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
            'license_files': dict(license_md5s(recipe)), 'skip_guards': recipe.guards, 'sources': sources,
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
            if conf_errors or static_errors[name]:
                report['recipes'][name]['fetched'] = None
                report['recipes'][name]['fetch_skipped'] = ('layer configuration findings' if conf_errors
                                                            else 'static source findings')
                continue
            try:
                errors, fetched = fetch_recipe(recipe, workdir, cargo)
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                errors, fetched = [f'{recipe.path.name}: fetch/unpack failed: {error}'], []
            report['errors'] += errors
            report['recipes'][name]['fetched'] = fetched
    # Defence in depth: no URL in any diagnostic (including git command
    # lines from a failed subprocess) keeps userinfo, query or fragment.
    report = redact(report)
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
