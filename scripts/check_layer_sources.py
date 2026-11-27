#!/usr/bin/env python3
"""Audit meta-suifleet source pins and, optionally, fetch them anonymously.

Static checks (always):
  * no AUTOREV; git sources use https and a 40-hex SRCREV;
  * https sources carry a SHA-256 and are immutable: no query strings or
    credentials, no branch/tag archives, no raw/codeload URL at a non-commit
    ref, and no expiring release-asset redirect hosts;
  * every crate:// entry is crate://crates.io/NAME/VERSION with no parameters
    (no other registry, inline checksum or name=) and has a SHA-256;
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
  * logical lines are joined as BitBake joins them (bitbake_lines): trailing
    whitespace is stripped, a final backslash concatenates the next line with
    nothing inserted, function bodies stay raw, and continuations BitBake
    refuses to parse are reported;
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
LICENSE md5 and the generated crate include (registries, names, versions and
effective checksums).

--fetch skips recipes with static findings. For the rest it first requires
the unpack directory to be a new plain PN name inside the scratch directory and
confines every subdir=/destsuffix= destination and S to it (plain relative
paths, resolved through symlinks), then downloads every
non-crate source with no credentials into a new directory, verifies its
SHA-256 before unpacking, re-checks each destination before writing, unpacks
it as BitBake would, creates loose files exclusively without following links
(file:// entries keep their URL directories, like BitBake; unpack=0
downloads; archive formats it cannot extract are refused). Every link in a
Git checkout and in the assembled unpack directory must resolve inside it
before any source byte is read; it then checks the resulting
S: license checksums, the daemon lockfile/version/crate include, workspace
members and transitive local path-dependency crates, include targets, and the codec's exact pinned file set and provenance.
--cargo additionally runs `cargo metadata --offline --locked --no-deps`.

The JSON report maps each recipe to its version, source revision, sources,
checksums and licenses. Parsing here is a source audit, not a BitBake build.
"""
import argparse
import ast
import bz2
import gzip
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
import zlib

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
# BitBake's function start (BBHandler.__func_start_regexp__): a function with
# no name, or named __anonymous, is anonymous Python whether or not it is
# marked python (ast.MethodNode).
FUNC_START = re.compile(r"(((?P<py>python(?=(\s|\()))|(?P<fr>fakeroot(?=\s)))\s*)*"
                        r"(?P<func>[\w\.\-\+\{\}\$:]+)?\s*\(\s*\)\s*{$")
# BitBake's def header and the lines it keeps in that Python function
# (BBHandler.__def_regexp__ and __python_func_regexp__).
PY_DEF = re.compile(r"def\s+(\w+).*:")
PY_DEF_BODY = re.compile(r"(\s+.*)|(^$)|(^#)")
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


# Only the compressions the supported host Python's tarfile decodes (gzip,
# bzip2, xz). Python 3.10 tarfile has no Zstandard support, so .tar.zst/.tzst
# fall through to unsupported_unpack and are refused statically instead of
# failing at extraction time.
TARBALL = re.compile(r'\.(tar(\.(gz|xz|bz2))?|tgz|tbz2|txz)$')
# Decoder failures a corrupt or mislabelled tarball raises while being read;
# reported as findings rather than escaping as a traceback. bz2 and gzip
# header failures are OSError, which callers already report.
ARCHIVE_DECODE_ERRORS = (tarfile.TarError, EOFError, zlib.error)
try:
    import lzma
    ARCHIVE_DECODE_ERRORS += (lzma.LZMAError,)
except ImportError:  # pragma: no cover - CPython built without lzma
    pass
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


def bitbake_lines(text, functions=True):
    """Yield (number, kind, line) for text as BitBake's parser feeds it.

    Each physical line loses its trailing whitespace. Outside function bodies a
    line ending in a backslash continues: BitBake drops the backslash and
    concatenates the next line, keeping that line's indentation and inserting
    nothing (BBHandler.feeder and ConfHandler.handle), so "a\\" followed by
    "b" is the single token "ab". Lines inside a shell or Python function and a
    def are kept raw, without continuation. number is the first physical line.
    kind is 'statement', 'start' (function header), 'body', 'end', 'def',
    'mixed-comment' (BitBake refuses a continuation that mixes comment and
    code) or 'unterminated' (a continuation at end of file, which BitBake
    refuses as unparsed).
    """
    physical = text.split('\n')
    if physical and physical[-1] == '':
        physical.pop()
    infunc = inpython = False
    residue, first = [], None
    for number, raw in enumerate(physical, 1):
        s = raw.rstrip()
        if infunc:
            infunc = s != '}'
            yield number, 'body' if infunc else 'end', s
            continue
        if inpython:
            if PY_DEF_BODY.match(s):
                yield number, 'def', s
                continue
            inpython = False
        if not residue:
            first = number
        lead = residue[0][:1] if residue else None
        if residue and ((s[:1] == '#') != (lead == '#')):
            yield first, 'mixed-comment', ''
            residue = []
            continue
        if s.endswith('\\'):
            residue.append(s[:-1])
            continue
        s = ''.join(residue) + s
        residue = []
        if not s or s[0] == '#':
            continue
        if functions and FUNC_START.match(s):
            infunc = True
            yield first, 'start', s
        elif functions and PY_DEF.match(s):
            inpython = True
            yield first, 'def', s
        else:
            yield first, 'statement', s
    if residue:
        yield first, 'unterminated', ''


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
        # Out-of-layer classes inherited, as (class, file).
        self.inherits = []
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
        # Line structure BitBake refuses to parse (see bitbake_lines).
        self.malformed = []
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
        # Inline Python is refused wherever BitBake could see it: in each
        # logical line as BitBake joins it, and (conservatively) in every
        # physical line with its continuations removed, including function
        # bodies, which BitBake keeps raw. Values are never reported.
        number = 1
        for logical in re.split(r'(?<!\\)\n', text):
            if INLINE_PYTHON.search(UNPACKDIR_EXPR.sub('', logical.replace('\\\n', ''))):
                self.inline_python.append(f'{self.rel(path)}:{number}')
            number += logical.count('\n') + 1
        lines, anonymous = [], None
        for number, kind, line in bitbake_lines(text):
            if (INLINE_PYTHON.search(UNPACKDIR_EXPR.sub('', line))
                    and f'{self.rel(path)}:{number}' not in self.inline_python):
                self.inline_python.append(f'{self.rel(path)}:{number}')
            if kind == 'start':
                match = FUNC_START.match(line)
                if (match.group('func') or '__anonymous') == '__anonymous':
                    anonymous = (line.rstrip('{').strip(), [])
            elif kind == 'body':
                if anonymous is not None:
                    anonymous[1].append(line)
            elif kind == 'end':
                if anonymous is not None:
                    self.anonymous_python(path, *anonymous)
                    anonymous = None
            elif kind == 'statement':
                lines.append(line.strip())
            elif kind == 'mixed-comment':
                self.malformed.append(f'{self.rel(path)}:{number}: a continuation mixing a comment and code')
            elif kind == 'unterminated':
                self.malformed.append(f'{self.rel(path)}:{number}: a continuation at end of file')
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
                    else:
                        self.inherits.append((name, path.name))
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
                'FILESEXTRAPATHS', 'FILESPATH', 'CARGO_SRC_DIR', 'CARGO_MANIFEST_PATH', 'CARGO_LOCK_PATH',
                'CARGO_LOCK_SRC_DIR', 'UNPACKDIR', 'WORKDIR')


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
    model per-configuration overrides, so it rejects these candidates unless
    they resolve to the same file the unqualified search selects. That holds
    even when the override subdirectory is itself a search root: its
    qualified candidate precedes every unqualified root.

    BitBake's local fetcher (bb.utils.which) takes the first candidate that
    exists, directories included, so a directory both counts as an override
    candidate and, when it comes first, is the selected source. The audit
    supports only regular files, so a selected directory is rejected rather
    than skipped in favour of a later same-named file."""
    layer = recipe.layer.resolve()
    directories = search_path(recipe)
    selected = next((directory / name for directory in directories
                     if (directory / name).exists() or (directory / name).is_symlink()), None)
    selected_resolved = selected.resolve() if selected is not None else None
    for directory in directories:
        if not directory.is_dir():
            continue
        for sub in sorted(directory.iterdir()):
            if not sub.is_dir():
                continue
            candidate = sub / name
            # An override subdirectory can also be a search root (for example
            # FILESEXTRAPATHS naming files/qemuarm64). Its qualified candidate
            # still precedes every unqualified root, so it is harmless only
            # when it resolves to the file the ordinary search selects.
            if (candidate.exists() or candidate.is_symlink()) and candidate.resolve() != selected_resolved:
                raise ValueError(f'file:// source {name} has an override-specific candidate {candidate} '
                                 f'that BitBake selects when FILESOVERRIDES contains {sub.name!r}; '
                                 'machine/distro-specific local files are not supported')
    if selected is not None:
        if layer not in selected_resolved.parents:
            raise ValueError(f'file:// source {name} resolves outside the layer or to a non-file: {selected_resolved}')
        if not selected_resolved.is_file():
            raise ValueError(f'file:// source {name} resolves outside the layer or to a non-file: {selected_resolved} '
                             '(BitBake selects the first existing candidate, directories included; '
                             'only regular local files are supported)')
        return selected_resolved
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
    errors += [f'{where}: {item} is metadata BitBake refuses to parse; the audit cannot describe its sources'
               for item in recipe.malformed]
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
            try:
                _, name, version = crate_source(url, params)
            except ValueError as error:
                errors.append(f'{where}: {error}')
                continue
            if not HEX64.match(checksum_for(recipe, params, f'{name}-{version}')):
                errors.append(f'{where}: crate {name} {version} lacks a SHA-256')
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


# The only crate registry the generated include and the audit model. BitBake's
# crate fetcher maps the crate:// host `crates.io` to static.crates.io; any
# other host (which may carry extra path segments) is fetched from that host.
CRATES_IO = 'crates.io'
CRATES_IO_LOCK_SOURCE = 'registry+https://github.com/rust-lang/crates.io-index'
CRATE_PART = re.compile(r'^[A-Za-z0-9_.+-]+$')


def crate_source(url, params):
    """Return (registry, name, version) for the only crate form the audit models:
    `crate://crates.io/NAME/VERSION` with no SRC_URI parameters, as generated by
    `bitbake -c update_crates`. BitBake honours an inline sha256sum or a name=
    parameter before the generated NAME-VERSION checksum flag, and derives the
    download host from the URL, so any other form could fetch or verify
    something other than what the audit compares with Cargo.lock."""
    shown = redact_url(url)
    parts = url.split('/')
    if len(parts) < 5:
        raise ValueError(f'crate source must be crate://HOST/NAME/VERSION: {shown}')
    registry, name, version = '/'.join(parts[2:-2]), parts[-2], parts[-1]
    if registry != CRATES_IO:
        raise ValueError(f'crate source registry {registry!r} is not {CRATES_IO}: {shown}')
    if not CRATE_PART.match(name) or not CRATE_PART.match(version) or '${' in url:
        raise ValueError(f'crate source has an unsupported name or version: {shown}')
    if params:
        raise ValueError(f'crate source parameters the audit does not model: {", ".join(sorted(params))}: {shown}')
    return registry, name, version


def lock_packages(lock):
    """Return ({(name, version): (registry, checksum)}, unsupported) for Cargo.lock.

    The whole lock is parsed as TOML (package_edge_source.parse_toml, which
    raises ValueError on anything outside its strict subset), so every TOML
    spelling of a row's keys is read as Cargo reads it; a source field can never
    be missed and mistaken for a source-less workspace package.

    Cargo package identity includes the source, so two rows may share a name and
    version. Only crates.io index rows enter the mapping; every row from another
    source, any repeated crates.io coordinate and any row whose name, version,
    source or checksum is not a plain string is listed in `unsupported` while
    reading, so no row can be overwritten or skipped before it is refused."""
    packager = load_packager()
    try:
        document = packager.parse_toml(lock)
    except ValueError as error:
        raise ValueError('Cargo.lock: ' + str(error).replace('Cargo.toml syntax', 'TOML syntax')) from None
    rows = document.get('package', [])
    if not isinstance(rows, packager._TableArray):
        raise ValueError('Cargo.lock: package is not an array of [[package]] tables')
    plain = lambda value: isinstance(value, str) and not isinstance(value, packager._Scalar)
    packages, unsupported = {}, []
    for index, row in enumerate(rows):
        name, version = row.get('name'), row.get('version')
        if not (plain(name) and plain(version)):
            unsupported.append(f'[[package]] #{index + 1} (name/version not plain strings)')
            continue
        if any(field in row and not plain(row[field]) for field in ('source', 'checksum')):
            unsupported.append(f'{name} {version} (source/checksum not plain strings)')
            continue
        source, key = row.get('source'), (name, version)
        if source == CRATES_IO_LOCK_SOURCE:
            if key in packages:
                unsupported.append(f'{name} {version} (duplicate crates.io entry)')
            else:
                packages[key] = (CRATES_IO, row.get('checksum', ''))
        elif source is not None:
            unsupported.append(f'{name} {version} ({redact_url(source)})')
    return packages, unsupported


def crate_include(recipe):
    """Return {(name, version): (registry, checksum)} as BitBake would fetch and
    verify each crate:// entry: the registry from the URL and the effective
    checksum (an inline sha256sum or name= parameter takes precedence over the
    generated flag). A form crate_source refuses, or a duplicate entry, is kept
    with an `unsupported:` registry so it can never compare equal to the lock."""
    entries = {}
    for url, params in recipe.sources():
        if not url.startswith('crate://'):
            continue
        parts = url.split('/')
        key = tuple(parts[-2:]) if len(parts) >= 5 else (url, '')
        try:
            registry, name, version = crate_source(url, params)
        except ValueError as error:
            entries[key] = (f'unsupported:{error}', '')
            continue
        if key in entries:
            entries[key] = (f'unsupported:duplicate crate entry {redact_url(url)}', '')
            continue
        entries[key] = (registry, checksum_for(recipe, params, f'{name}-{version}'))
    return entries


def plain_relative(rel, what):
    """ValueError unless rel is a non-empty relative path with no parent, absolute
    or unexpanded component, so joining it to a directory cannot leave it."""
    if (not rel or rel.startswith('/') or '${' in rel or '\0' in rel or '\\' in rel
            or '..' in PurePosixPath(rel).parts):
        raise ValueError(f'{what} {rel!r} is not a plain relative path')
    return rel


def license_md5s(recipe):
    """(path relative to S, md5) for each LIC_FILES_CHKSUM entry.

    Only file:// entries naming a plain relative path are supported; anything
    else raises ValueError, because the path is later joined to the unpacked
    S and must not name a host file outside it."""
    for entry in recipe.get('LIC_FILES_CHKSUM').split():
        url, *params = entry.split(';')
        options = dict(p.split('=', 1) for p in params if '=' in p)
        if set(options) - {'md5'}:
            raise ValueError(f'{recipe.path.name}: unsupported LIC_FILES_CHKSUM options {sorted(options)}')
        if not url.startswith('file://'):
            raise ValueError(f'{recipe.path.name}: LIC_FILES_CHKSUM entry {url!r} is not a file:// path relative to S')
        yield plain_relative(url[len('file://'):], f'{recipe.path.name}: LIC_FILES_CHKSUM file'), options.get('md5', '')


def license_errors(recipe):
    try:
        list(license_md5s(recipe))
    except ValueError as error:
        return [str(error)]
    return []


def cargo_src_dir(recipe):
    """The effective CARGO_SRC_DIR in canonical form; ValueError unless it is a
    plain relative path below S.

    BitBake and Cargo resolve "./crate", "crate/" and "crate//" to the same
    directory as "crate", so every spelling is normalized here once. Inventory,
    include-scan and Git-tree lookups then key on the same names the layout
    uses, instead of silently missing files under a noncanonical spelling."""
    what = f'{recipe.path.name}: CARGO_SRC_DIR'
    rel = plain_relative(recipe.get('CARGO_SRC_DIR', 'suifleet-edge'), what)
    canonical = PurePosixPath(rel).as_posix()
    if canonical == '.':
        raise ValueError(f'{what} {rel!r} names S itself; the audit requires the crate in a subdirectory of S')
    return canonical


def source_path(unpack, s_dir, rel, what):
    """s_dir/rel for a metadata-derived rel: lexically plain and, after resolving
    any symlinks, still inside the unpack root. Raises ValueError otherwise."""
    plain_relative(rel, what)
    return confined(unpack, os.path.relpath(Path(s_dir) / rel, Path(unpack).resolve()), what)


def license_file_errors(recipe, s_dir, unpack=None):
    """Compare each LIC_FILES_CHKSUM file under the unpacked S with its md5."""
    where, errors = recipe.path.name, []
    try:
        entries = list(license_md5s(recipe))
    except ValueError as error:
        return [str(error)]
    for rel, md5 in entries:
        try:
            path = source_path(unpack or s_dir, s_dir, rel, 'LIC_FILES_CHKSUM file')
        except ValueError as error:
            errors.append(f'{where}: {error}; not read')
            continue
        if not path.is_file() or hashlib.md5(path.read_bytes()).hexdigest() != md5:
            errors.append(f'{where}: license file {rel} does not match md5 {md5}')
    return errors


# The daemon and codec recipes are identified by PN, never by the pins this
# audit verifies: a recipe that dropped SUIFLEET_LOCK_SHA256 must fail the
# audit, not silently skip the Cargo.lock and layout checks.
DAEMON_PN = 'suifleet'
CODEC_PN = 'suifleet-walrus-codec'
NODE_PN = 'suifleet-node'
SHA256_HEX = re.compile(r'[0-9a-f]{64}')
COMMIT_HEX = re.compile(r'[0-9a-f]{40}')
DAEMON_PINS = {'SUIFLEET_LOCK_SHA256': SHA256_HEX, 'SUIFLEET_CRATE_VERSION': None, 'SRCREV': COMMIT_HEX}


CODEC_INSTALL = re.compile(r'^do_install\(\)\s*\{\n(?P<body>.*?)^\}', re.M | re.S)
CODEC_INSTALL_LOOP = re.compile(r'^\s*for f in (?P<names>[^;\n]+); do\s*$', re.M)
SHA256_HEX = re.compile(r'[0-9a-f]{64}')


def codec_install_files(recipe):
    """The files the codec recipe's do_install copies from S, or None.

    do_install names them in one literal loop, separately from the
    SUIFLEET_CODEC_SHA256 flags that do_verify_codec checks, so the audit
    requires a valid pin for each one rather than trusting the flags alone."""
    bodies = CODEC_INSTALL.findall(recipe.text)
    if len(bodies) != 1:
        return None
    loops = CODEC_INSTALL_LOOP.findall(bodies[0])
    if len(loops) != 1 or '$' in loops[0]:
        return None
    return loops[0].split()


def codec_install_errors(recipe):
    where, files = recipe.path.name, codec_install_files(recipe)
    if not files:
        return [f'{where}: codec do_install has no single literal "for f in ...; do" file list to audit']
    pins = recipe.flags.get('SUIFLEET_CODEC_SHA256', {})
    errors = []
    for name in files:
        sha = recipe.expand(pins[name]) if name in pins else ''
        if not SHA256_HEX.fullmatch(sha):
            errors.append(f'{where}: codec do_install file {name} has no lowercase SHA-256 SUIFLEET_CODEC_SHA256 pin')
    return errors


# suifleet-node's do_install copies exactly these files from S. The fetched
# audit checks that each exists and that the executable is an AArch64 ELF,
# because the archive SHA-256 and the license md5 alone do not prove that a
# checksum-correct archive contains the program the recipe installs.
NODE_INSTALL_INPUTS = ('LICENSE', 'bin/node')
NODE_INSTALL_REF = re.compile(r'\$\{S\}/(\S+)')
ELF_AARCH64 = 183
# Every A64 instruction is four bytes, at a four-byte-aligned address.
AARCH64_INSN = 4


ELF64_EHDR, ELF64_PHDR, PT_LOAD, PF_X = 64, 56, 1, 1
# Package version and epoch: OE publishes the Node package (and resolves the
# codec's RDEPENDS) from PKGV/PKGE, which default to PV/PE. The audit compares
# PV/PE, so it refuses anything that could make the package version differ.
PACKAGE_VERSION_VARS = ('PKGV', 'PKGE')


def elf_aarch64_problem(data):
    """Why data is not a minimal loadable ELF64 LE AArch64 executable, or None.

    Checks the whole 64-byte ELF header, that the program header table and
    at least one non-empty PT_LOAD segment lie inside the file, that every
    PT_LOAD has p_filesz <= p_memsz and a consistent alignment, and that
    e_entry is a four-byte-aligned address whose complete first instruction
    lies in the file-backed image of an executable (PF_X) PT_LOAD, so a
    truncated, header-only, unloadable or unrunnable file is not reported as
    the verified executable."""
    if len(data) < ELF64_EHDR:
        return f'{len(data)} bytes, shorter than the 64-byte ELF64 header'
    # e_ident: magic, ELFCLASS64, ELFDATA2LSB, EV_CURRENT.
    if data[:4] != b'\x7fELF' or data[4] != 2 or data[5] != 1 or data[6] != 1:
        return 'not an ELF64 little-endian identification'
    def u(offset, size):
        return int.from_bytes(data[offset:offset + size], 'little')
    if u(16, 2) not in (2, 3):
        return f'e_type {u(16, 2)} is neither ET_EXEC nor ET_DYN'
    if u(18, 2) != ELF_AARCH64:
        return f'e_machine {u(18, 2)} is not AArch64'
    if u(20, 4) != 1:
        return 'e_version is not EV_CURRENT'
    phoff, ehsize, phentsize, phnum = u(32, 8), u(52, 2), u(54, 2), u(56, 2)
    if ehsize != ELF64_EHDR or phentsize != ELF64_PHDR:
        return 'ELF64 header or program header size is wrong'
    if phnum == 0 or phoff < ELF64_EHDR or phoff + phnum * ELF64_PHDR > len(data):
        return 'program header table is missing or extends past the end of the file'
    loads, e_entry, entry_mapped = 0, u(24, 8), False
    for index in range(phnum):
        entry = phoff + index * ELF64_PHDR
        p_type, p_flags = u(entry, 4), u(entry + 4, 4)
        p_offset, p_vaddr = u(entry + 8, 8), u(entry + 16, 8)
        p_filesz, p_memsz, p_align = u(entry + 32, 8), u(entry + 40, 8), u(entry + 48, 8)
        if p_type != PT_LOAD:
            continue
        if p_offset + p_filesz > len(data):
            return f'PT_LOAD segment {index} extends past the end of the file'
        # The ELF specification requires a loadable segment's file image to fit
        # in its memory image, and its file offset and address to agree modulo a
        # power-of-two alignment; loaders refuse segments that do not.
        if p_filesz > p_memsz:
            return f'PT_LOAD segment {index} has p_filesz {p_filesz} larger than p_memsz {p_memsz}'
        if p_align > 1 and (p_align & (p_align - 1) or (p_vaddr - p_offset) % p_align):
            return f'PT_LOAD segment {index} alignment {p_align} is not a power of two congruent with its offset and address'
        loads += bool(p_filesz)
        # The kernel starts execution at e_entry (relative to the load bias for
        # ET_DYN, so in the same address space as p_vaddr); it must be backed by
        # file bytes of an executable segment, not .bss or a read-only mapping.
        # AArch64 instructions are four bytes and four-byte aligned, so the
        # whole first instruction must be in that file image.
        entry_mapped |= bool(p_flags & PF_X) and p_vaddr <= e_entry and e_entry + AARCH64_INSN <= p_vaddr + p_filesz
    if not loads:
        return 'no non-empty PT_LOAD segment'
    if e_entry % AARCH64_INSN:
        return f'e_entry {e_entry:#x} is not four-byte aligned, as AArch64 instructions must be'
    if not entry_mapped:
        return (f'e_entry {e_entry:#x} does not start a complete instruction inside the file-backed image of an '
                'executable PT_LOAD segment')
    return None


def node_package_version_errors(recipe):
    """Refuse metadata that can make the Node package version differ from PV/PE."""
    where = recipe.path.name
    assigned = {name for name in PACKAGE_VERSION_VARS if name in recipe.vars}
    assigned |= {base for base, *_ in recipe.overrides + recipe.unparsed if base in PACKAGE_VERSION_VARS}
    errors = [f'{where}: {name} is assigned; the package version must come from PV/PE so the audit can check '
              'it against the codec dependency' for name in sorted(assigned)]
    # Out-of-layer classes (gitpkgv, ...) can set PKGV/PKGE or PV, which this
    # reader does not see, so the Node recipe may inherit none.
    errors += [f'{where}: inherit {name} ({file}) can change the package version; the Node recipe must not inherit classes'
               for name, file in recipe.inherits]
    return errors


def node_install_errors(recipe):
    where = recipe.path.name
    bodies = CODEC_INSTALL.findall(recipe.text)
    if len(bodies) != 1:
        return [f'{where}: Node do_install is missing or defined more than once']
    refs = set(NODE_INSTALL_REF.findall(bodies[0]))
    if refs != set(NODE_INSTALL_INPUTS):
        return [f'{where}: Node do_install copies {sorted(refs)} from S; the audit verifies exactly '
                f'{list(NODE_INSTALL_INPUTS)}']
    return []


def node_errors(recipe, s_dir, unpack=None):
    """Check the unpacked Node release for the files suifleet-node installs."""
    where = recipe.path.name
    errors = license_file_errors(recipe, s_dir, unpack)
    for rel in NODE_INSTALL_INPUTS:
        if not (s_dir / rel).is_file():
            errors.append(f'{where}: do_install input {rel} is absent from the Node archive')
    node = s_dir / 'bin/node'
    if node.is_file():
        problem = elf_aarch64_problem(node.read_bytes())
        if problem:
            errors.append(f'{where}: bin/node is not a 64-bit little-endian AArch64 ELF executable ({problem})')
    return errors


# Variables that choose the Cargo manifest/lock BitBake verifies and builds
# (oe-core cargo_common and cargo-update-recipe-crates), with the file each must
# name relative to ${S}/${CARGO_SRC_DIR}. Their class defaults derive from
# CARGO_SRC_DIR, which daemon_errors audits; an explicit layer assignment must
# expand to that same file, or do_verify_lock would check a lockfile other than
# the audited crate graph.
CARGO_PATHS = {'CARGO_MANIFEST_PATH': 'Cargo.toml', 'CARGO_LOCK_PATH': 'Cargo.lock', 'CARGO_LOCK_SRC_DIR': ''}


def cargo_path_errors(recipe, where):
    errors = []
    try:
        src = cargo_src_dir(recipe)
    except ValueError as error:
        return [f'{error}; the audit reads the Cargo manifest and lock only inside S']
    for name, leaf in CARGO_PATHS.items():
        if recipe.raw(name) is None:
            continue
        expected = PurePosixPath(recipe.expand('${S}'), src, leaf).as_posix() if leaf else \
            PurePosixPath(recipe.expand('${S}'), src).as_posix()
        value = recipe.get(name)
        actual = PurePosixPath(value).as_posix() if value else ''
        # PurePosixPath drops a trailing "/" or "/.", but open() in
        # do_verify_lock then fails on a regular file, so a file-valued
        # setting must end in its file name exactly.
        if actual != expected or (leaf and value.rsplit('/', 1)[-1] != leaf):
            errors.append(f'{where}: {name} {value!r} is not ${{S}}/{src}{"/" + leaf if leaf else ""}; the audit '
                          f'verifies {src}/Cargo.lock and Cargo.toml, so the recipe must build and verify those files')
    return errors


def pin_errors(recipe):
    """Require the coupled pins of the daemon and codec recipes, keyed by PN.

    Pins set on any other recipe are refused because nothing would verify them."""
    pn, where, errors = recipe.get('PN'), recipe.path.name, []
    errors += license_errors(recipe)
    if pn == DAEMON_PN:
        errors += cargo_path_errors(recipe, where)
        for name, pattern in DAEMON_PINS.items():
            value = recipe.get(name)
            if not value:
                errors.append(f'{where}: daemon recipe lacks required {name}')
            elif pattern and not pattern.fullmatch(value):
                errors.append(f'{where}: daemon {name} {value!r} is not a lowercase {pattern.pattern} digest')
    elif recipe.get('SUIFLEET_LOCK_SHA256'):
        errors.append(f'{where}: SUIFLEET_LOCK_SHA256 is only verified for PN {DAEMON_PN!r}, not {pn!r}')
    if pn == CODEC_PN:
        value = recipe.get('SUIFLEET_CODEC_SOURCE_REV')
        if not value:
            errors.append(f'{where}: codec recipe lacks required SUIFLEET_CODEC_SOURCE_REV')
        elif not COMMIT_HEX.fullmatch(value):
            errors.append(f'{where}: codec SUIFLEET_CODEC_SOURCE_REV {value!r} is not a full lowercase commit')
        errors += codec_install_errors(recipe)
    elif recipe.get('SUIFLEET_CODEC_SOURCE_REV'):
        errors.append(f'{where}: SUIFLEET_CODEC_SOURCE_REV is only verified for PN {CODEC_PN!r}, not {pn!r}')
    if pn == NODE_PN:
        errors += node_install_errors(recipe)
        errors += node_package_version_errors(recipe)
    return errors


def daemon_errors(recipe, read, exists=None, listing=None):
    """Check coupled daemon pins with `read(relpath)` returning bytes or None.

    With `exists`, the layout is also audited; `listing(directory)` must then
    return every file path under that directory of the source layout."""
    errors = []
    # Every read below joins these metadata operands to the source root; refuse
    # an absolute or parent-relative operand before reading anything.
    try:
        src = cargo_src_dir(recipe)
        licenses = list(license_md5s(recipe))
    except ValueError as error:
        return [f'{error}; source checks not run']
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
    try:
        locked, unsupported = lock_packages(lock.decode())
    except (ValueError, UnicodeDecodeError) as error:
        errors.append(f'{recipe.path.name}: {src}/{error}' if isinstance(error, ValueError)
                      else f'{recipe.path.name}: {src}/Cargo.lock is not UTF-8')
        locked, unsupported = {}, []
    included = crate_include(recipe)
    if unsupported:
        errors.append(f'{recipe.path.name}: Cargo.lock has {len(unsupported)} packages from sources other than '
                      f'the crates.io registry that the crate include cannot pin, e.g. {unsupported[:3]}')
    for key, (registry, _) in sorted(included.items()):
        if registry.startswith('unsupported:'):
            errors.append(f'{recipe.path.name}: suifleet-crates.inc entry {key[0]} {key[1]}: {registry[len("unsupported:"):]}')
    missing = sorted(set(locked) - set(included))
    extra = sorted(set(included) - set(locked))
    both = set(locked) & set(included)
    # Refused include entries were already reported above.
    both = {k for k in both if not included[k][0].startswith('unsupported:')}
    registry_wrong = sorted(k for k in both if locked[k][0] != included[k][0])
    wrong = sorted(k for k in both if locked[k][0] == included[k][0] and locked[k][1] != included[k][1])
    for label, items in (('missing from', missing), ('not in Cargo.lock but in', extra),
                         ('registry differs in', registry_wrong), ('checksum differs in', wrong)):
        if items:
            errors.append(f'{recipe.path.name}: {len(items)} crates {label} suifleet-crates.inc, e.g. {items[:3]}; regenerate with bitbake -c update_crates')
    for rel, md5 in licenses:
        data = read(rel)
        if data is None or hashlib.md5(data).hexdigest() != md5:
            errors.append(f'{recipe.path.name}: license file {rel} does not match md5 {md5}')
    if exists is not None and version is not None:
        def read_manifest(directory):
            path = f'{src}/{directory}/Cargo.toml'
            return read(path).decode() if exists(path) else None
        try:
            declared = packager.declared_files(manifest.decode(), read_manifest, lambda rel: exists(f'{src}/{rel}'))
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


def codec_errors(recipe, s_dir, unpack=None):
    errors, pins = [], {k: recipe.expand(v) for k, v in recipe.flags.get('SUIFLEET_CODEC_SHA256', {}).items()}
    names = sorted(p.name for p in Path(s_dir).iterdir()) if Path(s_dir).is_dir() else []
    # do_install copies a fixed file list; each needs a pin and so a fetched file.
    install = codec_install_errors(recipe)
    if install:
        return install
    if names != sorted(pins):
        return [f'{recipe.path.name}: unpacked codec has {names}, recipe pins {sorted(pins)}']
    for name, sha in pins.items():
        if hashlib.sha256((Path(s_dir) / name).read_bytes()).hexdigest() != sha:
            errors.append(f'{recipe.path.name}: codec file {name} differs from its pin')
    documents = {}
    for name in ('provenance.json', 'manifest.json'):
        try:
            documents[name] = json.loads((Path(s_dir) / name).read_text())
        except (OSError, ValueError) as error:
            errors.append(f'{recipe.path.name}: codec {name} is not readable JSON: {error}')
            continue
        if not isinstance(documents[name], dict):
            errors.append(f'{recipe.path.name}: codec {name} is not a JSON object')
            del documents[name]
    if len(documents) != 2:
        return errors
    provenance, manifest = documents['provenance.json'], documents['manifest.json']
    # Mirror do_verify_codec in the recipe, which the build runs on the same files.
    if manifest.get('schema') != 1:
        errors.append(f'{recipe.path.name}: codec manifest schema {manifest.get("schema")!r} is not 1, '
                      'which do_verify_codec requires')
    files = manifest.get('files')
    if not isinstance(files, dict):
        errors.append(f'{recipe.path.name}: codec manifest has no "files" object for do_verify_codec')
    else:
        for name, entry in sorted(files.items()):
            # do_verify_codec calls entry.get('sha256'), so a non-object entry
            # fails the build; an unpinned name compares None with None there.
            if not isinstance(entry, dict):
                errors.append(f'{recipe.path.name}: codec manifest entry for {name} is not an object, '
                              'which do_verify_codec requires')
            elif name not in pins or entry.get('sha256') != pins[name]:
                errors.append(f'{recipe.path.name}: codec manifest and recipe disagree on {name}')
    if provenance.get('source_revision') != recipe.get('SUIFLEET_CODEC_SOURCE_REV'):
        errors.append(f'{recipe.path.name}: codec provenance revision {provenance.get("source_revision")} differs from SUIFLEET_CODEC_SOURCE_REV')
    if ' & '.join(provenance.get('licenses') or []) != recipe.get('LICENSE'):
        errors.append(f'{recipe.path.name}: codec provenance licenses differ from LICENSE')
    if manifest.get('node') != recipe.get('SUIFLEET_CODEC_NODE_VERSION'):
        errors.append(f'{recipe.path.name}: codec manifest Node version differs from SUIFLEET_CODEC_NODE_VERSION')
    errors += license_file_errors(recipe, Path(s_dir), unpack)
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


# The tarfile mode matching the command BitBake's FetchMethod.unpack selects
# from the local file name: 'tar xz' for .tar.gz/.tgz, 'bzip2 -dc' for
# .tar.bz2/.tbz2, 'xz -dc' for .tar.xz/.txz. Those commands fail when the
# bytes use another compression, so the audit must not autodetect one. A
# plain .tar goes to 'tar x -f', and GNU tar itself detects compression when
# reading a regular file, so it keeps autodetection.
TAR_MODES = (
    (re.compile(r'\.(tar\.gz|tgz)$'), 'r:gz'),
    (re.compile(r'\.(tar\.bz2|tbz2)$'), 'r:bz2'),
    (re.compile(r'\.(tar\.xz|txz)$'), 'r:xz'),
    (re.compile(r'\.tar$'), 'r:*'),
)


def tar_mode(name):
    for pattern, mode in TAR_MODES:
        if pattern.search(name):
            return mode
    raise ValueError(f'{name}: not a tarball name the audit extracts')


# Magic numbers tarfile's 'r:*' autodetection recognises, mapped to the
# compression it would select for a plain .tar.
COMPRESSION_MAGIC = ((b'\x1f\x8b', 'gz'), (b'BZh', 'bz2'), (b'\xfd7zXZ\x00', 'xz'))


def verify_xz_stream(archive, out=None):
    """Decode an .xz file as 'xz -dc' (XZ Utils) does, writing the decoded
    bytes of every stream to out when given.

    lzma.open ignores any bytes after a complete stream that fail to decode as
    another stream, and rejects the legal null stream padding after one, so it
    disagrees with the xz command BitBake runs in both directions. XZ Utils
    accepts one or more concatenated streams, each optionally followed by null
    stream padding whose length is a multiple of four, and rejects anything
    else: trailing garbage, misaligned padding, leading padding or a stream
    that ends early. Raises lzma.LZMAError or EOFError on such input."""
    with open(archive, 'rb') as data:
        decoder, streams, padding, pending = None, 0, 0, b''
        while True:
            if not pending and not (pending := data.read(1 << 20)):
                break
            if decoder is None:
                stripped = pending.lstrip(b'\0')
                if not streams and len(stripped) != len(pending):
                    raise lzma.LZMAError('null bytes before the first xz stream')
                padding += len(pending) - len(stripped)
                pending = stripped
                if not pending:
                    continue
                if padding % 4:
                    raise lzma.LZMAError(f'xz stream padding of {padding} bytes is not a multiple of four')
                padding, decoder = 0, lzma.LZMADecompressor(lzma.FORMAT_XZ)
            decoded = decoder.decompress(pending, 1 << 20)
            pending = b''
            while True:
                if out is not None:
                    out.write(decoded)
                if decoder.eof or decoder.needs_input:
                    break
                decoded = decoder.decompress(b'', 1 << 20)
            if decoder.eof:
                pending, decoder, streams = decoder.unused_data, None, streams + 1
    if decoder is not None:
        raise EOFError('file ends inside an xz stream')
    if not streams:
        raise lzma.LZMAError('no xz stream')
    if padding % 4:
        raise lzma.LZMAError(f'xz stream padding of {padding} bytes is not a multiple of four')


def stream_compression(archive, mode):
    """The compression the unpack command for mode applies to archive: the one
    the name selects, or for a plain .tar the one GNU tar detects ('' if none)."""
    compression = mode.partition(':')[2]
    if compression == '*':
        with open(archive, 'rb') as data:
            head = data.read(6)
        compression = next((kind for magic, kind in COMPRESSION_MAGIC if head.startswith(magic)), '')
    return compression


def verify_compressed_stream(archive, mode, out=None):
    """Decode the whole compressed stream through its end-of-stream marker.

    tarfile stops reading once it has the members it needs, so a stream cut
    short after the tar end-of-archive blocks (for example a gzip file missing
    its CRC/size trailer) still extracts. The decompressors BitBake runs (gzip,
    bzip2, xz) read to EOF and fail on such a stream, so the audit must too.
    For xz the decoded tar is written to out when given, so extraction reads
    exactly what 'xz -dc' produces. Raises tarfile.ReadError on any
    truncation, checksum or format failure."""
    compression = stream_compression(archive, mode)
    if not compression:
        return
    try:
        if compression == 'xz':
            verify_xz_stream(archive, out)
            return
        with {'gz': gzip.open, 'bz2': bz2.open}[compression](archive) as stream:
            while stream.read(1 << 20):
                pass
    except (OSError, EOFError, ValueError, *ARCHIVE_DECODE_ERRORS) as error:
        raise tarfile.ReadError(f'{compression} stream does not decode through its end: '
                                f'{type(error).__name__}: {error}') from error


def safe_extract(archive, destination, name=None, root=None):
    """Extract archive as BitBake would unpack a file called name (default:
    the archive's own name), using the decompression that name selects. The
    compressed stream must decode completely, as it must for BitBake. Every
    link under root (default: destination) must still resolve inside root
    afterwards; fetch passes the recipe's whole unpack directory."""
    destination = Path(destination).resolve()
    mode = tar_mode(name or Path(archive).name)
    with tempfile.TemporaryFile() as decoded:
        if stream_compression(archive, mode) == 'xz':
            # lzma.open, which tarfile uses for xz, stops at null stream padding
            # and ignores every later stream, while 'xz -dc' decodes them all.
            # Extract the tar that the xz-compatible decoder produced instead.
            verify_compressed_stream(archive, mode, decoded)
            decoded.seek(0)
            opened = tarfile.open(fileobj=decoded, mode='r:')
        else:
            verify_compressed_stream(archive, mode)
            opened = tarfile.open(archive, mode)
        with opened as tar:
            extract_members(tar, archive, destination, root)


def member_parts(archive, name):
    """The path components of a tar member name ('.' components dropped);
    ValueError for an absolute name or one with a '..' component."""
    if name.startswith('/') or '\0' in name:
        raise ValueError(f'{archive.name}: member escapes the unpack directory: {name}')
    parts = tuple(part for part in PurePosixPath(name).parts if part != '.')
    if '..' in parts:
        raise ValueError(f'{archive.name}: member escapes the unpack directory: {name}')
    return parts


def link_graph_errors(archive, members, destination):
    """Validate every member path and symlink target before anything is written.

    Resolution through links cannot be checked against the filesystem in
    advance, because the archive's own links (or links an earlier source
    unpacked) change what later paths resolve to. The check is therefore
    lexical over the archive's complete link graph plus what already exists
    on disk, and the graph is restricted so that the lexical answer is the
    real one: no member is written through a link, and no symlink target
    passes through or ends at a link. Every directory a target traverses must
    be a directory of this archive or a real directory already on disk, so a
    later source cannot turn it into a link. Hard links are not needed by any
    published layout and are refused."""
    seen, links, files, directories = set(), set(), set(), {()}
    entries = []
    for member in members:
        parts = member_parts(archive, member.name)
        if not parts:
            if not member.isdir():
                raise ValueError(f'{archive.name}: member has no name: {member.name!r}')
            continue
        if parts in seen:
            raise ValueError(f'{archive.name}: duplicate member: {member.name}')
        seen.add(parts)
        if member.islnk():
            raise ValueError(f'{archive.name}: hard link members are not supported: {member.name}')
        if not (member.issym() or member.isfile() or member.isdir()):
            raise ValueError(f'{archive.name}: unsupported member type: {member.name}')
        (links if member.issym() else files if member.isfile() else directories).add(parts)
        directories.update(parts[:i] for i in range(len(parts)))
        entries.append((member, parts))

    def on_disk_link(parts):
        return (destination.joinpath(*parts)).is_symlink() if parts else False

    def real_directory(parts):
        if parts in links or parts in files or on_disk_link(parts):
            return False
        if parts in directories:
            return True
        path = destination.joinpath(*parts)
        return path.is_dir() and not path.is_symlink()

    for member, parts in entries:
        for i in range(1, len(parts)):
            if parts[:i] in links or on_disk_link(parts[:i]):
                raise ValueError(f'{archive.name}: member is written through a link: {member.name}')
        if not member.issym():
            continue
        if not member.linkname or member.linkname.startswith('/') or '\0' in member.linkname:
            raise ValueError(f'{archive.name}: link escapes the unpack directory: {member.name}')
        current = list(parts[:-1])
        components = [part for part in PurePosixPath(member.linkname).parts if part != '.']
        for index, part in enumerate(components):
            if part == '..':
                if not current:
                    raise ValueError(f'{archive.name}: link escapes the unpack directory: {member.name}')
                current.pop()
                continue
            current.append(part)
            if index < len(components) - 1 and not real_directory(tuple(current)):
                raise ValueError(f'{archive.name}: link target passes through a link or a path that is '
                                 f'not a directory of the archive: {member.name} -> {member.linkname}')
        if tuple(current) in links or on_disk_link(tuple(current)):
            raise ValueError(f'{archive.name}: chained link: {member.name} -> {member.linkname}')
    return entries


def open_parent(destination, parts):
    """A directory fd for destination/parts[:-1], creating missing directories
    and never following a link on the way."""
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def directory_symlinks(root):
    """Every symlink under root (not following links) that resolves to a
    directory, relative to root."""
    root = Path(root)
    found = []
    for directory, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(directory) / name
            if path.is_symlink() and path.is_dir():
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def links_escaping(root):
    """Every symlink under root (not following links) whose full resolution
    leaves root."""
    root = Path(root).resolve()
    escaping = []
    for directory, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(directory) / name
            if path.is_symlink():
                resolved = Path(os.path.realpath(path))
                if root not in (resolved, *resolved.parents):
                    escaping.append(path.relative_to(root).as_posix())
    return escaping


def extract_members(tar, archive, destination, root=None):
    """Validate the archive's members and link graph, extract without following
    any link, then confirm every link under root (default destination) still
    resolves inside it."""
    destination = Path(destination).resolve()
    entries = link_graph_errors(archive, tar.getmembers(), destination)
    symlinks = []
    for member, parts in entries:
        if member.issym():
            symlinks.append((member, parts))
            continue
        fd = open_parent(destination, parts + (('',) if member.isdir() else ()))
        try:
            if member.isdir():
                continue
            out = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                          member.mode & 0o755 | 0o600, dir_fd=fd)
            with os.fdopen(out, 'wb') as handle, tar.extractfile(member) as source:
                shutil.copyfileobj(source, handle)
                os.fchmod(handle.fileno(), member.mode & 0o755 | 0o600)
        finally:
            os.close(fd)
    for member, parts in symlinks:
        fd = open_parent(destination, parts)
        try:
            os.symlink(member.linkname, parts[-1], dir_fd=fd)
        finally:
            os.close(fd)
    escaping = links_escaping(root or destination)
    if escaping:
        raise ValueError(f'{archive.name}: links resolve outside the unpack directory after extraction: '
                         f'{", ".join(escaping)}')


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
    if recipe.malformed:
        raise ValueError(f'metadata BitBake refuses to parse: {"; ".join(recipe.malformed)}')
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
        if scheme == 'git':
            # Resolve only the parent: the final component must be inspected and
            # created as declared. Resolving it too would follow a (dangling)
            # symlink an earlier archive left there, so the existence check and
            # the move would act on the link target instead of the declared path.
            rel = PurePosixPath(os.path.relpath(destination, unpack.resolve()))
            if rel.name in ('', '.', '..'):
                errors.append(f'{recipe.path.name}: {redact_url(url)}: git destination {rel.as_posix()!r} '
                              f'has no directory name; not fetched')
                continue
            try:
                parent = confined(unpack, rel.parent.as_posix(), 'unpack destination')
            except ValueError as error:
                errors.append(f'{recipe.path.name}: {redact_url(url)}: {error}; not fetched')
                continue
            destination = parent / rel.name
            rev = recipe.get(f'SRCREV_{params["name"]}') if 'name' in params else recipe.get('SRCREV')
            # Git never runs inside the unpack tree: an earlier archive could
            # have planted repository metadata (git/.git/config, hooks) at the
            # destination, and Git would honour it despite the isolated HOME
            # and disabled global/system config. A destination that already
            # exists is refused; the checkout is made in a fresh private
            # repository outside the unpack tree and only then moved into place.
            if os.path.lexists(destination):
                errors.append(f'{recipe.path.name}: {redact_url(url)}: git destination '
                              f'{destination.relative_to(unpack.resolve()).as_posix() or "."} already exists '
                              f'(created by an earlier source); not fetched')
                continue
            remote = git_remote(url)
            env = anonymous_env(Path(workdir))
            private = Path(tempfile.mkdtemp(prefix='git-', dir=downloads))
            env['GIT_CEILING_DIRECTORIES'] = str(downloads)
            for command in (['init', '-q', str(private)], ['-C', str(private), 'fetch', '-q', '--depth', '1', remote, rev],
                            ['-C', str(private), 'checkout', '-q', '--detach', 'FETCH_HEAD']):
                subprocess.run(['git', '-c', 'credential.helper=', *command], env=env, check=True)
            actual = subprocess.check_output(['git', '-C', str(private), 'rev-parse', 'HEAD'], env=env, text=True).strip()
            branch_error = None if params.get('nobranch') == '1' else git_branch_error(private, remote, params.get('branch', ''), rev, env)
            try:
                parent.mkdir(parents=True, exist_ok=True)
                # Re-check immediately before the move: rename() would replace
                # an empty directory created in the meantime, and the parent
                # must still resolve to the same confined directory.
                if os.path.lexists(destination) or confined(unpack, rel.parent.as_posix(),
                                                            'unpack destination') != parent:
                    raise ValueError(f'git destination {destination.relative_to(unpack.resolve()).as_posix()} '
                                     f'changed before the checkout was moved into place')
                os.rename(private, destination)
            except (OSError, ValueError) as error:
                return errors + [f'{recipe.path.name}: {redact_url(url)}: {error}'], fetched
            fetched.append({'url': redact_url(url), 'revision': actual, 'branch': params.get('branch') if params.get('nobranch') != '1' else None,
                            'verified': actual == rev and branch_error is None})
            if actual != rev:
                errors.append(f'{recipe.path.name}: fetched {actual}, SRCREV is {rev}')
            if branch_error:
                errors.append(f'{recipe.path.name}: {branch_error}')
            # A pinned commit can track symlinks to anything on the review host.
            # Apply the same unpack-root confinement as archive extraction, and
            # stop before a later source or check reads through such a link.
            escaping = links_escaping(unpack)
            if escaping:
                return errors + [f'{recipe.path.name}: {redact_url(url)}: links resolve outside the unpack '
                                 f'directory after checkout: {", ".join(escaping)}'], fetched
            continue
        # Re-resolve: an earlier archive may have created a symlink on the path.
        destination = confined(unpack, os.path.relpath(destination, unpack.resolve()), 'unpack destination')
        name = download_name(url, params)
        # Each source gets its own fresh staging directory. A name-keyed shared
        # directory would let another recipe's fetch of the same file (or an
        # earlier failed one) collide with or decide this source's result;
        # every source is downloaded and verified against its own pin.
        target = Path(tempfile.mkdtemp(prefix='source-', dir=downloads)) / name
        expected = checksum_for(recipe, params)
        actual = download(url, target)
        fetched.append({'url': redact_url(url), 'sha256': actual, 'verified': actual == expected, 'bytes': target.stat().st_size})
        if actual != expected:
            errors.append(f'{recipe.path.name}: {name} sha256 {actual} differs from pin {expected}')
            continue
        destination.mkdir(parents=True, exist_ok=True)
        if TARBALL.search(name) and params.get('unpack', '1') == '1':
            try:
                safe_extract(target, destination, name, unpack)
            except ARCHIVE_DECODE_ERRORS as error:
                errors.append(f'{recipe.path.name}: {name} could not be decoded as a tarball: '
                              f'{type(error).__name__}: {error}')
                continue
        else:
            try:
                write_new(unpack, os.path.relpath(destination / name, unpack.resolve()), target)
            except ValueError as error:
                errors.append(f'{recipe.path.name}: {error}')
    errors += fetched_state_errors(recipe, unpack)
    # Every source is assembled; refuse the tree before reading any manifest,
    # source or license bytes (or running Cargo) if any link leaves it.
    escaping = links_escaping(unpack)
    if escaping:
        return errors + [f'{recipe.path.name}: links resolve outside the unpack directory: '
                         f'{", ".join(escaping)}; source checks not run'], fetched
    try:
        s_dir = confined(unpack, os.path.relpath(s_dir, unpack.resolve()), 'S')
    except ValueError as error:
        return errors + [f'{recipe.path.name}: {error}'], fetched
    if not s_dir.is_dir():
        return errors + [f'{recipe.path.name}: S does not exist after unpack: {s_dir.relative_to(unpack)}'], fetched
    if recipe.get('PN') == DAEMON_PN:
        # Metadata-derived paths (CARGO_SRC_DIR, license files) are confined
        # to the unpack root after resolution before anything is read, listed
        # or handed to Cargo, so the audit never verifies a host-side file.
        try:
            src = cargo_src_dir(recipe)
            crate_dir = source_path(unpack, s_dir, src, 'CARGO_SRC_DIR')
            for rel in (f'{src}/Cargo.toml', f'{src}/Cargo.lock', *(rel for rel, _ in license_md5s(recipe))):
                source_path(unpack, s_dir, rel, 'source path')
        except ValueError as error:
            return errors + [f'{recipe.path.name}: {error}; source checks not run'], fetched
        # The compiler-input scan inventories S with rglob, which does not
        # descend through directory symlinks, while rustc, Cargo and the
        # existence checks follow them. A manifest-declared entry point reached
        # through such an alias would be read but never scanned, so the
        # daemon layout must be free of directory symlinks (the packager emits
        # no symlinks at all).
        aliases = directory_symlinks(s_dir)
        if aliases:
            errors.append(f'{recipe.path.name}: directory symlinks in the daemon source layout: '
                          f'{", ".join(aliases)}; the compiler-input scan requires a layout without them')
        refused = []

        def located(rel):
            try:
                return source_path(unpack, s_dir, rel, 'source path')
            except ValueError as error:
                refused.append(f'{recipe.path.name}: {error}; not read')
                return None

        def read(rel):
            path = located(rel)
            return path.read_bytes() if path is not None and path.is_file() else None

        def exists(rel):
            path = located(rel)
            return path is not None and path.is_file()
        errors += daemon_errors(recipe, read, exists,
                                lambda directory: [p.relative_to(s_dir).as_posix()
                                                   for p in source_path(unpack, s_dir, directory, 'source path').rglob('*')
                                                   if p.is_file()])
        errors += sorted(set(refused))
        packager = load_packager()
        files = {p.relative_to(s_dir).as_posix(): ('100644', 'blob', None) for p in s_dir.rglob('*') if p.is_file()}
        # Every .rs file is scanned, and so is every manifest-declared target
        # or build script and every include!/#[path] target whatever its
        # suffix, because Rust compiles each of them as code.
        roots = {p for p in files if p.endswith('.rs')}
        try:
            manifest = read(f'{src}/Cargo.toml').decode()

            def read_manifest(directory):
                data = read(f'{src}/{directory}/Cargo.toml')
                return data.decode() if data is not None else None
            roots |= {f'{src}/{rel}' for rel in packager.declared_files(manifest, read_manifest,
                                                                          lambda rel: f'{src}/{rel}' in files)
                      if f'{src}/{rel}' in files}
        except (OSError, ValueError, AttributeError, packager.MissingCrate):
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
                                    cwd=crate_dir, capture_output=True, text=True,
                                    env={**anonymous_env(Path(workdir)), 'CARGO_HOME': str(Path(workdir) / 'cargo-home'),
                                         'RUSTUP_HOME': os.environ.get('RUSTUP_HOME', str(Path.home() / '.rustup'))})
            if result.returncode:
                errors.append(f'{recipe.path.name}: cargo metadata failed: {result.stderr.strip().splitlines()[:3]}')
    elif recipe.get('PN') == CODEC_PN:
        errors += codec_errors(recipe, s_dir, unpack)
    elif recipe.get('PN') == NODE_PN:
        errors += node_errors(recipe, s_dir, unpack)
    else:
        errors += license_file_errors(recipe, s_dir, unpack)
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
    statements, errors = [], []
    for number, kind, line in bitbake_lines(text, functions=False):
        if kind == 'statement':
            statements.append(line.strip())
        elif kind == 'mixed-comment':
            # BitBake's ConfHandler refuses these, so the layer would not parse.
            errors.append(f'conf/layer.conf:{number}: a continuation mixing a comment and code; '
                          'BitBake refuses to parse it')
        elif kind == 'unterminated':
            errors.append(f'conf/layer.conf:{number}: a continuation at end of file; BitBake refuses to parse it')
        else:
            errors.append(f'conf/layer.conf:{number}: unexpected {kind} line {line!r}')
    statements = [line for line in statements if line and not line.startswith('#')]
    collections = []
    for line in statements:
        match = LAYER_CONF_STATEMENT.match(line)
        if match and match.group(1) == 'BBFILE_COLLECTIONS' and match.group(2) == '+=':
            collections += match.group(3).split()
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
    # A later plain assignment replaces an earlier one, so a repeated
    # registration variable would make the first value (and anything read from
    # it) differ from the effective configuration.
    assigned = {}
    for line in statements:
        match = LAYER_CONF_STATEMENT.match(line)
        if match and match.group(2) == '=':
            assigned[match.group(1)] = assigned.get(match.group(1), 0) + 1
    for name, count in assigned.items():
        if count > 1:
            errors.append(f'conf/layer.conf: {name} is assigned {count} times; only the last assignment takes '
                          'effect, so the audit refuses repeated registration variables')
    return errors


def layer_conf_series(text):
    """The effective LAYERSERIES_COMPAT of the registered collection.

    Read from BitBake's logical lines (continuations joined), with a later plain
    assignment replacing an earlier one, as BitBake applies them. Without a
    BBFILE_COLLECTIONS line any LAYERSERIES_COMPAT_<name> counts.
    """
    statements = [line.strip() for _, kind, line in bitbake_lines(text, functions=False) if kind == 'statement']
    matches = [LAYER_CONF_STATEMENT.match(line) for line in statements]
    matches = [m for m in matches if m]
    collections = [c for m in matches if m.group(1) == 'BBFILE_COLLECTIONS' and m.group(2) == '+='
                   for c in m.group(3).split()]
    series = []
    for match in matches:
        name, op, value = match.groups()
        if op == '=' and name.startswith('LAYERSERIES_COMPAT_'):
            if not collections or name[len('LAYERSERIES_COMPAT_'):] in collections:
                series = value.split()
    return series


def layer_registration_errors(text, recipes, appends):
    """Require the BBFILES registration that makes BitBake parse the audited files.

    The audit globs recipes and appends directly; without the matching
    registration BitBake would not parse them, so a clean report would describe
    files a standalone build never uses.
    """
    statements = [line.strip() for _, kind, line in bitbake_lines(text, functions=False) if kind == 'statement']
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
    report['series'] = layer_conf_series(conf)
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
        errors += pin_errors(recipe)
        static_errors[pn] = errors
        report['errors'] += errors
        report['recipes'][pn] = {
            'file': path.relative_to(layer).as_posix(),
            'appends': [a.relative_to(layer).as_posix() for a in appends], 'pv': recipe.get('PV'), 'license': recipe.get('LICENSE'),
            'license_files': None if license_errors(recipe) else dict(license_md5s(recipe)), 'skip_guards': recipe.guards, 'sources': sources,
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
    daemon, codec = recipes.get(DAEMON_PN), recipes.get(CODEC_PN)
    if daemon is None and (not only or DAEMON_PN in only):
        report['errors'].append(f'layer provides no daemon recipe with PN {DAEMON_PN!r}; its pins cannot be audited')
    if daemon and codec and daemon.get('SRCREV') != codec.get('SUIFLEET_CODEC_SOURCE_REV'):
        report['errors'].append(f'codec SUIFLEET_CODEC_SOURCE_REV {codec.get("SUIFLEET_CODEC_SOURCE_REV")} differs from daemon SRCREV {daemon.get("SRCREV")}')
    # The codec RDEPENDS on suifleet-node (= SUIFLEET_CODEC_NODE_VERSION); rootfs
    # assembly fails unless the layer packages exactly that Node version.
    node = recipes.get(NODE_PN)
    if codec and (not only or NODE_PN in only):
        wanted = codec.get('SUIFLEET_CODEC_NODE_VERSION')
        if not wanted:
            report['errors'].append(f'{codec.path.name}: codec recipe lacks required SUIFLEET_CODEC_NODE_VERSION')
        elif node is None:
            report['errors'].append(f'codec requires suifleet-node (= {wanted}), but the layer provides no {NODE_PN!r} recipe')
        elif node.get('PE') or node.get('PV') != wanted:
            report['errors'].append(f'{node.path.name}: suifleet-node PV {node.get("PV")!r}'
                                    f'{" with PE " + repr(node.get("PE")) if node.get("PE") else ""} does not satisfy '
                                    f'the codec dependency suifleet-node (= {wanted})')
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
            except (OSError, ValueError, subprocess.CalledProcessError, *ARCHIVE_DECODE_ERRORS) as error:
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
