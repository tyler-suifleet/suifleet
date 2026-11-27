# Yocto source distributions and pin audit (SF-306)

The meta-suifleet daemon recipe builds from an immutable, checksum-pinned
source archive rather than the core monorepo, so that a clean host can build
without private access, developer caches or a neighboring checkout.
[SF-295](https://github.com/tyler-suifleet/suifleet/issues/820) owns the recipe
pin lines, the public `tyler-suifleet/meta-suifleet` repository and Layer Index
submission. This page covers the tools for producing and auditing those inputs,
and records the BitBake build of the published layout. None of this is
physical-target, Layer Index or mainnet acceptance.

## Building the daemon source archive

```
python3 scripts/package_edge_source.py --revision <reviewed-main-commit> \
    --output /tmp/new/suifleet-edge-<reviewed-main-commit>.tar.gz
python3 scripts/package_edge_source.py --revision <commit> --list   # review the file set
```

The archive is `git -c tar.umask=0002 archive --format=tar <commit> -- <paths> | gzip -n -6`.
It reads committed objects only: working-tree edits and untracked files can't
enter it. Contents sit at the archive root (recipes unpack with `subdir=`).
Ownership is root and timestamps are the commit time. Modes are 0664, or 0775
for executables and directories: the builder pins `tar.umask` to git's default
0002, which the published archives were built with, so a different user, system
or `GIT_CONFIG_*` setting can't change them. `gzip -n`
stores no name or time, so the same commit, git and gzip produce identical bytes. The output
file must not already exist. The JSON result records the commit, SHA-256, crate
version, Cargo.lock SHA-256, LICENSE md5 and tool versions.

The file set is the daemon build closure, computed from that commit's tree:

- the root `LICENSE` and `suifleet-edge/{Cargo.toml,Cargo.lock,config.example.toml}`;
- `suifleet-edge/src/` and `examples/`;
- every `[workspace] members` directory (currently `windows-runtime`). The
  container's Python 3.10 has no `tomllib`, so the packager parses the whole
  manifest with a strict reader for the TOML subset Cargo manifests use:
  tables, arrays of tables, dotted and quoted keys, single-line basic and
  literal strings, arrays and inline tables. It decodes keys and escapes
  before comparing them, so every TOML spelling of `workspace.members` is
  found (for example `"memb\u0065rs"` or `["worksp\u0061ce"]`). Syntax outside
  the subset, such as a multi-line string, and a `members` value that isn't
  an array of strings stop the build rather than dropping a member. The fetch
  audit uses the same reader and reports these as errors;
- every local crate reached through a `path` dependency, followed through each
  local crate's manifest, even when `members` doesn't list it. Cargo builds
  these either way. The packager reads `[dependencies]`,
  `[dev-dependencies]`, `[build-dependencies]` (with the underscore
  spellings), their `[target.*]` forms, `[workspace.dependencies]`, `[patch.*]`
  and `[replace]`. A path that leaves `suifleet-edge/`, an absolute path, a
  non-string `path` or a local crate without a `Cargo.toml` stops the build.
  Currently the only one is `windows-runtime`, which is also a member;
- every file a manifest (the root crate's or a local crate's) declares
  explicitly: a string `package.build` and the `path` of each `[lib]`,
  `[[bin]]`, `[[example]]`, `[[test]]` and `[[bench]]` target, wherever it
  lives under `suifleet-edge/`. A `[[test]]` or `[[bench]]` declared by name
  only uses the file Cargo infers, `tests/<name>.rs` or `tests/<name>/main.rs`
  (`benches/` for benchmarks); the other target kinds default under `src/` or
  `examples/`, which are packaged whole. Cargo refuses a manifest whose declared
  target file is absent, even for `cargo build --bin`. A name-only target with
  neither or both inferred files, or without a plain name, stops the build; its
  `tests/<name>/main.rs` falls under the implicit-module refusal below. A declared file that is missing, absolute or outside
  `suifleet-edge/`, a non-string target `path` and a `package.build` that is
  neither a string nor a boolean stop the build. Today the only declaration is
  `[[bin]] path = "src/main.rs"`, already under `src/`;
- each file named by a literal `include!`/`include_str!`/`include_bytes!` or a
  `#[path]` module attribute, followed transitively. Every macro delimiter
  (`(..)`, `[..]`, `{..}`), spacing, path prefix (`std::include!`), raw string
  and a `path` inside `cfg_attr(..)` is recognized. An include or `#[path]`
  whose argument is not one string literal without escapes (for example
  `include!(concat!(env!("OUT_DIR"), ..))`) stops the build instead of being
  skipped. Rust allows a comment wherever whitespace may appear, for example
  `include_str /* why */ !("..")` or `#[/* why */ path = ".."]`, so comments
  are blanked (string literals kept) before the include and `#[path]` forms
  are matched. References that appear only inside a comment are still
  packaged when they resolve. Renaming an include macro with `use` (for
  example `use std::include_str as embed;`) stops the build, because calls
  through the new name would not be seen. Rust
  compiles an `include!` or `#[path]` target as code whatever its file name,
  so that target is scanned for further includes too (`src/main.rs` →
  `tools/entry.inc` → `tools/helper.rs`). `include_str!`/`include_bytes!`
  targets are data and are not scanned unless named `.rs`. A
  manifest-declared target or build script is likewise scanned as a crate
  root whatever its suffix (`[[bin]] path = "tools/entry.inc"`).

`src/`, `examples/` and each local crate directory are packaged whole, so Rust
module files there are always present. Outside them only the declared files,
the default `build.rs` and their literal include/`#[path]` targets are
packaged. An implicit out-of-line module (`mod name;` without a literal
`#[path]`) in one of those files would make Cargo also read a sibling
`name.rs` or `name/mod.rs` that the closure does not include. The build stops
instead: give the module a `#[path]` or move the target under `src/`.
The same check applies to a file outside those directories that a file inside
them reaches through a literal include or `#[path]`, such as
`#[path = "../tools/entry.rs"]` in `src/main.rs`. It also applies to files
outside the crate directory, such as `#[path = "../../interfaces/helper.rs"]`:
a `mod child;` there would read `interfaces/child.rs`, which the archive
would not contain. Both the packager and the `--fetch` layout audit refuse it.
A `cfg_attr` path is packaged but still counts as implicit for this check. Inline `mod name { ... }` blocks are fine.
Comments are blanked here too, so `mod /* why */ name;` is still found and
`#[/* why */ path = ".."] mod name;` still counts as an explicit path. A
commented-out `mod name;` is ignored.

A literal include or `#[path]` may start with `..` to climb out of the
source file's directory, but a `..` after a named directory is refused, as in
`#[path = "../tools/anchor/../helper.rs"]`. Rust traverses `tools/anchor`
there, and the archive would contain only `tools/helper.rs`, so the path would
compile in the repository and fail after extraction. Write the path without
the detour (`../tools/helper.rs`). Both the packager and the `--fetch` layout
audit refuse it.

A `#[path]` is resolved against the directory of the file it appears in. That
holds only at file level. Inside an inline `mod name { ... }` block, rustc
resolves it against the module's own directory (`name/`, or `stem/name/` in a
file other than `mod.rs`, `lib.rs` or `main.rs`), and on an inline module it
names a directory. Neither case is modelled, so both stop the build and the
fetched-source audit everywhere, including under `src/`. Otherwise the wrong
file of the same name could be packaged, giving an archive that compiles in
the repository and fails after extraction. Declare such a module at file
level instead. The blocks are found structurally: comments, strings, raw
strings and char literals are masked first, so a `}` inside one cannot end a
block early. A file-level `#[path]` after a closed inline block is accepted,
for example the `#[cfg(test)] #[path = "poll_tests.rs"]` module in
`src/sui_client.rs`.

Module names may be Unicode identifiers, as in `mod café { ... }`. Rust also
treats U+0085, U+200E, U+200F, U+2028 and U+2029 as whitespace, which the
scanner does not model. Any Rust source containing one of those characters is
refused, even inside a comment or string. Use ASCII whitespace instead.

A missing workspace member, local path dependency, declared target file or include target, a path outside the
repository, a symlink or submodule, or a runtime identity/state file name
(`device.key`, `config.toml`, `*.key`, journals, databases and so on) stops the
build. Contents are not pattern-scanned for keys, because the redaction test
vectors deliberately contain fake ones. Build only from reviewed main, which
the [security CI](SECURITY_CI.md) secret scanning already covers.

Workspace members must be literal paths. Globs, `..` components and absolute
paths are refused. Spellings Cargo accepts, such as `./member/`, are
normalized to the canonical directory before the manifest lookup, so the
member is archived once under its canonical path.

Before writing, the builder reads back the tar stream `git archive` produced.
It must contain exactly the planned files and their directories, each with the
committed blob's bytes and exact mode. A committed `export-ignore` or
`export-subst` attribute that drops or rewrites a build input therefore stops
the build, instead of yielding an incomplete archive that reports success.

`--paths-from FILE` rebuilds an archive from an existing newline-separated
file list, with the same audit. The builder still computes the default build
closure and refuses a list that omits any of it, such as a manifest, the
lockfile, a workspace member or an include target. A list may contain more than
the closure. Use it to reproduce a previously published archive, or to keep the
broader published file set. The SF-295 archives also carry packaging inputs,
shared interfaces, test fixtures and docs (see the layer's `RELEASE.md`); the
default closure is the smaller set the recipe build needs.

The codec artifact is still produced by `scripts/package_walrus_codec.py`; see
[Yocto packaging](YOCTO_PACKAGING.md).

## Auditing the layer

```
python3 scripts/check_layer_sources.py --repo .                 # static + Git coherence
python3 scripts/check_layer_sources.py --repo . --fetch \
    --cargo "cargo +1.94.0"                                      # plus anonymous fetch
```

These static checks always run, on every recipe in the layer:

- No `AUTOREV`. Git sources use `protocol=https` and a 40-hex `SRCREV`.
- Git sources set either `branch=NAME` or `nobranch=1`, as BitBake requires.
  Under `--fetch`, the audit checks that the named branch exists on the remote
  and that `SRCREV` is reachable from it, as BitBake does. A source with
  several names or branches is refused.
- Git sources may use only `protocol`, `branch`, `nobranch`, `name` and
  `destsuffix`, the parameters whose fetch and checkout the audit reproduces.
  Other parameters, such as `subpath`, `nocheckout`, `bareclone`, `rev`, `tag`,
  `usehead` or `lfs`, change what BitBake fetches or unpacks, so they are
  refused statically and by the fetcher before anything is cloned.
- `gitsm://` sources are refused. The audit verifies only a top-level commit,
  not the submodules that `gitsm` also fetches. Use a checksum-pinned archive or
  a separate pinned `git://` source for each repository. Under `--fetch` the
  fetcher also refuses `gitsm` before it downloads anything.
- https sources have a SHA-256 (as a `SRC_URI[...]` flag or inline). They may
  not use a query string, a fragment or credentials. They may not use a GitHub
  branch/tag archive, a `raw.githubusercontent.com` or codeload URL at a
  non-commit ref, a `releases/latest` redirect or an expiring release-asset
  host. The report never repeats a rejected secret: every URL in it, including
  failed git command lines, is shown without userinfo, query or fragment
  (`<redacted>`).
- https sources may use only `name`, `sha256sum`, `subdir`, `unpack=0` or
  `unpack=1` and `downloadfilename`. Like BitBake, the audit saves the
  download under `downloadfilename` when that is set and chooses how to unpack
  it from that name's suffix. A `.bin` local name is copied as a loose file, not
  extracted. The audit extracts only tarballs (`.tar`, `.tar.gz`, `.tgz`,
  `.tar.xz`, `.txz`, `.tar.bz2`, `.tbz2`), the compressions the supported
  host Python's `tarfile` can decode. Any other name that BitBake would unpack,
  such as `.tar.zst`, `.tzst`, `.zip`, `.jar`, `.7z`, `.deb`, `.rpm`, `.tar.lz`
  or a bare `.gz`, is refused unless it has `unpack=0`. Like BitBake, the audit
  picks the decompression from the name: gzip for `.tar.gz` and `.tgz`, bzip2
  for `.tar.bz2` and `.tbz2`, xz for `.tar.xz` and `.txz`. A plain `.tar` is
  autodetected, as GNU `tar x -f` does. A pinned tarball that does not decode
  in the format its name claims (for example an uncompressed tar or Zstandard
  data saved as `.tar.gz`) is reported as a finding, not a traceback. The
  compressed stream must also decode through its end, as `gzip`, `bzip2` and
  `xz` require: a stream truncated after the tar members (such as a gzip file
  missing its CRC/size trailer) or one with a bad checksum is a finding even
  though its members can be read. An xz file is checked as `xz -dc` checks
  it: concatenated streams and null stream padding in multiples of four bytes
  are accepted, while any other bytes after a stream (which Python's `lzma`
  module silently ignores) are a finding. The audit then extracts the tar
  that this `xz -dc`-compatible decoder produced, so members in a stream after
  null padding (which Python's `tarfile` would silently drop) are unpacked and
  checked like the rest. With `unpack=0` it is
  copied as a loose file, as BitBake copies it. The local name must be a plain file name and, like the URL, must not
  name a commit other than `SRCREV`. BitBake uses `downloadfilename` literally
  (only the URL path is percent-decoded), so a `downloadfilename` containing a
  percent escape is refused rather than decoded. Other parameters, such as `striplevel`,
  `md5sum` or `localpath`, are refused statically and by the fetcher before
  anything is downloaded.
- Every `crate://` entry has the generated form `crate://crates.io/NAME/VERSION`
  and a SHA-256. BitBake derives the download host from the URL and lets an
  inline `sha256sum` or a `name=` parameter replace the generated
  `NAME-VERSION.sha256sum` flag, so another registry, any parameter and a
  duplicate entry are refused.
- `file://` entries resolve, after following symlinks, to regular files inside
  the layer, and `FILESEXTRAPATHS` doesn't point outside it. This rejects the
  pre-parse `${TOPDIR}` codec artifacts. No symlink in the layer may leave it.
  The reader searches BitBake's default `FILESPATH` order: `FILESEXTRAPATHS`
  entries first, then `${BP}`, `${BPN}` and `files` beside the recipe, so it
  checks the same file BitBake would pick. An immediate
  `FILESEXTRAPATHS:prepend := "${THISDIR}/...:"` uses the directory of the
  file that contains it; a deferred `${THISDIR}` is rejected. An explicit
  `FILESPATH` assignment is rejected, because it can replace that search
  order with directories outside the layer.
  Like BitBake, the fetcher copies `file://nested/NAME` to `subdir/nested/NAME`.
  It keeps the URL's intermediate directories, so a `LIC_FILES_CHKSUM` entry or
  `S` layout has to use that path. The audit only copies local files and never
  extracts them, so a local archive (any suffix BitBake unpacks) needs
  `unpack=0`. A `file://` entry may only use `subdir` and `unpack=0/1`. Other
  parameters are refused statically and by the fetcher before anything is
  downloaded. For example, BitBake reads an explicit `localpath` instead of the
  layer file, and `downloadfilename` or `striplevel` are not modelled either.
  BitBake tries every `FILESOVERRIDES` subdirectory (machine, distro,
  architecture) of each search directory before the unqualified ones, so a
  same-named file such as `files/qemuarm64/suifleet.service` replaces
  `files/suifleet.service` on that machine. The audit does not model
  per-configuration overrides, so any such candidate is rejected.
- Source-affecting variables take no conditional overrides. These are
  `SRC_URI` (and its flags), `SRCREV*`, `PV`, `PE`, `S`, `LICENSE`,
  `LIC_FILES_CHKSUM`, `FILESEXTRAPATHS`, `CARGO_SRC_DIR`, `CARGO_MANIFEST_PATH`,
  `CARGO_LOCK_PATH`, `CARGO_LOCK_SRC_DIR`, every `SUIFLEET_*` variable, and any variable they reference. Only a plain `:append` or
  `:prepend` is accepted. Machine, distro, class, `pn-` and `:remove`
  overrides are rejected, because the reader doesn't evaluate them. So are
  flagged appends and `?=`/`??=` appends. That way the audited assignment is
  the one BitBake uses in every configuration.
- The reader joins lines as BitBake does. It strips each physical line's
  trailing whitespace, then a final backslash joins the next line with its
  indentation and nothing inserted, so `file://a\` followed by `file://b` is
  the single token `file://afile://b`. Lines inside shell or Python function
  bodies stay raw, as BitBake keeps them. A continuation that mixes a comment
  and code, or ends the file, is refused, because BitBake refuses to parse it.
- Metadata that BitBake runs after parsing is refused, because it can rewrite
  `SRC_URI` and the other pins after the reader has read them. This covers
  anonymous Python functions (any function with no name or named
  `__anonymous`, such as `python () {`, `python __anonymous () {` or a bare
  `() {`, with or without `fakeroot`), `addhandler` event handlers, and `inherit` or
  `inherit_defer` of a class in this layer or of an unexpandable class name.
  The rule applies to the recipe, its includes and its bbappends. The refusal is
  static, and the fetcher repeats it before anything is downloaded. Named task
  functions such as `python do_verify_lock()` are not run during parsing and
  are still accepted, as is inheriting classes from other layers.
- The one accepted anonymous Python form is a skip guard built only from the
  reviewed SF-307 forms in `suifleet-node` and `suifleet-walrus-codec`.
  Skipping a recipe changes whether it is built, not which sources it pins.
  The audit parses the function body without running it. It does not try to
  prove arbitrary Python read-only; it accepts the body only if every
  statement matches this positive grammar:
  - `import os`;
  - a platform check, `if COND: raise bb.parse.SkipRecipe(MESSAGE)` with no
    `else`. COND compares a literal `d.getVar(NAME[, EXPAND])` or
    `d.getVarFlag(NAME, FLAG[, EXPAND])` with a string constant using `==`,
    `!=`, `in` or `not in`, optionally combined with `and`, `or` and `not`.
    MESSAGE is a string constant, optionally `%` literal reads;
  - the reviewed missing-local-input block, matched statement for statement
    (comments and the skip message text are not compared). It resolves the
    `file://` entries of `SRC_URI` with `bb.fetch2.Fetch(local, d,
    cache=False, localonly=True)`, calls that fetcher's local method search
    path (`localfile_searchpaths` or `localpaths`), records each candidate
    with `bb.parse.mark_dependency` and skips when an input is missing.

  Any other statement, call, receiver, name or binding is refused as before,
  even when it looks read-only. Accepted guards are listed per recipe as
  `skip_guards` in the JSON report, and the recipe is still fetched and
  verified. The model trusts those fixed BitBake calls not to rewrite pins; a
  new guard form must extend the grammar deliberately, with review.
- Inline Python (`${@...}`) is refused anywhere in a recipe, its includes and
  its bbappends, including flags, appends and task bodies, except the
  `SUIFLEET_UNPACKDIR` idiom `${@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}`.
  BitBake runs inline Python whenever a value containing it is expanded,
  directly or through a reference, for example by a skip guard's
  `d.getVar`, a `:=` assignment or signature generation, and that code could
  rewrite pins. Only the file and line are reported, never the value. The
  fetcher repeats the refusal before anything is downloaded. A guard's reads
  therefore reach only this checked metadata and the base layers the build
  already trusts.
- Directives the reader does not model are refused: `unset` (of a variable or
  a flag), `include_all`, `addfragments` and `addpylib`. They can remove or
  replace source pins or load further metadata. The refusal is static, and the
  fetcher repeats it before anything is downloaded.
- Assignments whose variable name contains a `${...}` reference are refused,
  in the recipe, its includes and its bbappends. BitBake expands such keys
  when it finalizes the datastore, so `SRC_${SUFFIX} = "file://x"` with
  `SUFFIX = "URI"` replaces `SRC_URI`. The reader does not model key
  expansion. Overrides such as `RDEPENDS:${PN}` keep a literal variable name
  and are still accepted. The refusal is static, and the fetcher repeats it
  before anything is downloaded. It names only the file and the variable
  name; the assigned value, which may carry credentials or a signed URL, is
  never copied into the report.
- `conf/layer.conf` may contain only layer registration. BitBake parses it
  into the base configuration that every recipe inherits, so an assignment
  there (for example `SRC_URI:append:pn-suifleet`) changes sources without
  appearing in any recipe. The audit accepts `BBPATH .= ":${LAYERDIR}"`, the
  two `BBFILES +=` recipe and bbappend patterns, one `BBFILE_COLLECTIONS`
  entry, and plain `=` assignments of `BBFILE_PATTERN`, `BBFILE_PRIORITY`,
  `LAYERVERSION`, `LAYERDEPENDS`, `LAYERRECOMMENDS` and `LAYERSERIES_COMPAT`
  for that collection. Every other statement is refused, including other
  variables, overrides, inline Python, `include`, `inherit` and Python
  blocks. The registration must also be present: the audit globs the
  recipes and bbappends itself, so it requires the `BBFILES +=` recipe
  pattern when the layer has recipes and the bbappend pattern when it has
  bbappends. Without them BitBake would not parse the audited files. A
  continuation in `layer.conf` that mixes a comment and code, or ends the
  file, is also a finding, because BitBake then refuses to parse the layer.
  A finding there skips the fetch of every recipe.
- The reader follows BitBake's ordering. Plain `:append` and `:prepend` apply
  after every assignment, even one that comes later in the file. `??=` is a
  weak default, used only when nothing else assigns the variable; each later
  `??=` replaces the previous weak default, while `?=` keeps the first.
  Flags such as `SRC_URI[name.sha256sum]` follow the same `=`, `:=`, `?=`,
  `+=`, `=+`, `.=` and `=.` rules, so a later `?=` doesn't replace a
  checksum and `:=` expands at that line. `??=` on a source flag is rejected,
  because its meaning differs between the supported BitBake releases.
- The reader accepts both double- and single-quoted values. An
  assignment-shaped line it can't read is reported if it names a
  source-affecting variable, rather than skipped.
- Every `require`/`include` target must be a file inside the layer, after
  following symlinks. An absolute or relative path outside the layer, a path
  with an unexpandable variable, or a target missing from the layer is
  reported and never read. BitBake would otherwise find such a file through
  `BBPATH` in another layer, so a clean audit could depend on metadata that a
  standalone layer doesn't contain. As in BitBake, the whole operand is
  expanded and every space-separated file name in it is included in order, so
  a valid first file doesn't hide a missing, unresolvable or out-of-layer
  second one. The fetcher refuses to run while any include finding remains.
- A variable named in a `require`/`include` operand chooses which metadata is
  read, so it is source-affecting. That includes every variable it referenced
  when the include was expanded. BitBake expands the operand with the active
  overrides, so `SOURCE_SETTINGS:qemuarm64` could select a different file
  with different sources on that machine. The audit evaluates no overrides,
  so it reports a conditional override of any such variable, and `--fetch`
  downloads nothing for that recipe. Unconditional variable-driven includes
  are read normally.
- A `:=` assignment stores the expanded text, but BitBake expanded it with
  the active overrides. So the audit keeps the variables each `:=` value
  (including a `:=` flag or `:append :=`) was expanded from. They stay
  source-affecting wherever the assigned variable is, for example
  `SOURCE_SETTINGS := "${SOURCE_CHOICE}.inc"` before `include ${SOURCE_SETTINGS}`.
  A conditional override of `SOURCE_CHOICE` is then reported.
- Each matching `.bbappend` (an exact name, or `name_%.bbappend`) is read after
  its recipe as one set of metadata, as BitBake does. So a variable assigned in
  the append affects the recipe's sources. A `.bbappend` that matches no
  recipe in the layer is rejected, because the audit can't see its target.
- Each PN comes from one recipe file. BitBake can select any of several
  versions of a PN (`PREFERRED_VERSION`), but the report, the pin cross-checks
  and the fetch directory are keyed by PN. A second recipe for the same PN,
  another version or an explicit `PN`, is therefore rejected and nothing is
  fetched for that PN.
- The layer and `export-files.json` contain no runtime identity/state files.
- The coupled pins agree with each other. `PV` equals `SUIFLEET_CRATE_VERSION`.
  The codec `SUIFLEET_CODEC_SOURCE_REV` equals the daemon `SRCREV`. A commit
  named in an archive filename equals `SRCREV`. When the codec is present, the
  layer must provide `suifleet-node` with `PV` equal to
  `SUIFLEET_CODEC_NODE_VERSION` and no `PE`, because the codec `RDEPENDS` on
  exactly that version. The `suifleet-node` `do_install` must copy exactly
  `${S}/bin/node` and `${S}/LICENSE`, the files the fetched check verifies.
  OE publishes the package version from `PKGV`/`PKGE`, which default to
  `PV`/`PE`, so the Node recipe may not assign either in any form (plain, weak,
  append, override or bbappend) and may not `inherit` a class, which could set
  them out of the audit's view.
- The daemon and codec recipes are found by PN (`suifleet` and
  `suifleet-walrus-codec`), not by the pins being verified. The layer must
  provide the daemon recipe, with a lowercase 64-hex `SUIFLEET_LOCK_SHA256`, a
  `SUIFLEET_CRATE_VERSION` and a 40-hex `SRCREV`. The codec recipe, when
  present, needs a 40-hex `SUIFLEET_CODEC_SOURCE_REV` and a lowercase 64-hex
  `SUIFLEET_CODEC_SHA256` pin for every file its `do_install` loop copies; that
  loop must be one literal `for f in ...; do` list. A missing or empty pin
  fails the audit instead of skipping the checks it would select. These pins on
  any other recipe are refused, because nothing would verify them.

The daemon recipe's `do_verify_lock` opens the effective `CARGO_LOCK_PATH`,
and Cargo builds from `CARGO_MANIFEST_PATH`. The core class defaults derive
both from `${S}/${CARGO_SRC_DIR}`, which is the directory the audit reads. If
the layer assigns `CARGO_LOCK_PATH`, `CARGO_MANIFEST_PATH` or
`CARGO_LOCK_SRC_DIR` (including through `??=`, `:append` or a referenced
variable), the expanded value must name `${S}/${CARGO_SRC_DIR}/Cargo.lock`,
`.../Cargo.toml` or that directory exactly. Any other value is refused, even a
missing file inside `S`, so the audit never certifies a lockfile other than the
one BitBake verifies.

`--repo` reads `SRCREV` from a Git checkout. It checks the Cargo.lock digest,
the crate version, the LICENSE md5 and that `suifleet-crates.inc` lists exactly
the lockfile's crates.io registry crates, versions and checksums. It compares
each entry's registry and effective checksum, as BitBake would fetch and verify
it. Cargo identifies a package by name, version and source, so every lockfile
row from a source other than the crates.io index is refused as it is read, even
when a crates.io row has the same name and version; a repeated crates.io row is
refused too. The whole lockfile is parsed as TOML with the packager's strict
reader, so a row's fields are read whatever their spacing, quoting or string
style. Syntax outside that reader, a `package` that is not an array of
`[[package]]` tables and a non-string name, version, source or checksum are
refused rather than skipped. `--fetch` does not download crates. The crate include is
still regenerated with `bitbake -c update_crates suifleet`; this check confirms
the committed result matches.

`--fetch` skips any recipe that has static findings. For the others, it first
checks every `subdir=`/`destsuffix=` destination and `S`. Each must be a plain
relative path that stays inside the recipe's unpack directory after following
symlinks. A failure stops that recipe before anything is downloaded or
written. The recipe's unpack directory itself must be a new, plain `PN` name
directly inside the scratch directory (no separators, `..`, or existing entry).
Each destination is checked again just before writing, in case an earlier
source unpacked a symlink on the path. A loose file (a `file://` entry, or an
https source that is not unpacked) is created exclusively without following
links: an existing file or link at the final name is reported, not written
through.

It then downloads each non-crate source into a new directory. Every HTTPS
source is staged in its own fresh download directory, so recipes that share
one pinned URL and SHA-256 each fetch, verify and unpack it independently, and
another recipe's fetch of the same file name (even a failed one) never decides
this source's result. Downloads send
no Authorization header and don't read `.netrc`. Git uses an empty credential
helper, no global or system configuration and no prompts. Each SHA-256 is
checked before unpacking. Unpacking follows BitBake `subdir=`/`destsuffix=`
semantics, and members or links that escape the directory are refused.

Resolution through links can't be checked in advance against the filesystem,
because the archive's own links, or links an earlier source unpacked, change
what later paths resolve to. Before writing anything, the audit therefore
validates each archive's complete link graph together with what is already on
disk. The graph is restricted so that the lexical result is the real one:

- No member is written through a link.
- No symlink target passes through a link or ends at one (no chains).
- Every directory a target traverses is a directory of the archive or a real
  directory already on disk.
- Hard links, duplicate member names and `..` member components are refused.
  The published archives contain none of them. Node's three `bin/` symlinks
  point to regular files.

Files and links are then created through directory descriptors opened with
`O_NOFOLLOW`, so a link swapped in after validation is never followed. After
each extraction, every symlink under the recipe's unpack directory is resolved
again and must stay inside it.

A pinned Git source can track symlinks too. After each checkout, and once more
after every source is assembled, every symlink under the unpack directory is
resolved and must stay inside it. Links that resolve inside the tree are
allowed. An escaping link stops the recipe before any manifest, source or
license byte is read or Cargo runs, so the result never depends on files on
the review host.

Git itself never runs inside the unpack directory. An earlier archive could
place repository metadata such as `git/.git/config` at a Git source's
destination, and Git honours repository-local configuration even with an
isolated `HOME` and no global or system configuration. A Git destination that
already exists is therefore refused and not fetched. Only the destination's
parent directory is resolved and confined; the final name is checked and created
as declared, so an existing symlink there (even a dangling one) counts as an
existing destination rather than being followed to its target. Otherwise the audit
initializes, fetches, checks out and verifies the branch in a fresh private
repository outside the unpack tree, then moves it into place.

The check then verifies the resulting `S`:

- for every recipe, no runtime identity/state file name anywhere in the fetched
  and unpacked sources;
- for every recipe, the license file checksums;
- for the daemon, the lockfile digest, crate version, crate include, workspace
  members, transitive local path-dependency crates, manifest-declared target
  files (including Cargo's inferred file for a name-only `[[test]]`/`[[bench]]`,
  which `cargo metadata --no-deps` does not check), every include target (followed from declared targets and through
  non-`.rs` code targets, whatever their suffix) and
  the same implicit-module refusal outside the
  directories packaged whole, plus `cargo metadata --offline --locked
  --no-deps` when `--cargo` is set;
- for the codec, the exact five pinned files (each file `do_install` copies
  must be pinned, so it must also be fetched), their digests, provenance
  revision, licenses and Node version, plus manifest schema 1 and manifest
  file hashes equal to the recipe pins, as `do_verify_codec` requires. Every
  manifest entry must be an object naming a pinned file;
- for Node, the `bin/node` and `LICENSE` that `do_install` copies, with
  `bin/node` a 64-bit little-endian AArch64 ELF executable: a complete 64-byte
  ELF64 header (`EV_CURRENT`, `ET_EXEC`/`ET_DYN`, standard header sizes), a
  program header table inside the file and at least one non-empty `PT_LOAD`
  segment. Every `PT_LOAD` must lie inside the file, have `p_filesz` no larger
  than `p_memsz`, and use a power-of-two alignment congruent with its offset
  and address. The archive
  SHA-256 and license checksum alone would accept an archive without the
  executable.

The JSON report maps each recipe to its PV, crate version, source revision,
source URLs and SHA-256, license files and crate count, plus the advertised
`LAYERSERIES_COMPAT`. The series are read from BitBake's logical lines, with
continuations joined, for the registered collection; a registration variable
assigned more than once in `layer.conf` is a finding, because only the last
assignment takes effect. That report is the per-series version/source/license
evidence. A listed series is only declared compatibility, until an actual
build on that series is recorded.

`make test-yocto-packaging` runs the offline tests
(`scripts/tests/test_layer_sources.py`). They include a regression check that
the repository layer has no source findings at all.

## Updating pins

1. Choose a reviewed main commit. Build the daemon archive and the codec
   artifact from that commit, and review `--list`.
2. Publish both archives under new filenames containing the full commit, at an
   immutable URL. A blob path at a commit is acceptable; a branch or tag path
   is not. Never overwrite a published file.
3. In one change, update the archive URLs and SHA-256 values, `SRCREV`,
   `SUIFLEET_CRATE_VERSION` (PV follows it), the lock digest, the regenerated
   crate include, the codec revision and hashes, and the license metadata.
4. Run `check_layer_sources.py --repo . --fetch --cargo …`. It must report no
   errors before a recipe build.
5. Run the BitBake fetch/unpack/compile/package checks on each advertised
   series. Record the results separately from this audit.

## Validation record (2026-11-26, published SF-295 pins)

Run on the WSL host by Claude (claude-opus-5-5) against merged main `e5c46c00`
plus this branch, with git 2.34.1, GNU gzip 1.10, Python 3.10.12 and Rust 1.94.0.
The layer pins the daemon and codec archives at
`tyler-suifleet/meta-suifleet` commit `b86509a1…`, both built from core commit
`b9bb2ef6210624d732ab2de2075ea1826e5300c1`.

- **Reproduction.** `package_edge_source.py --revision b9bb2ef6… --paths-from
  <published file list>` reproduced the published daemon archive byte for byte:
  144 files, SHA-256
  `4627f17b24b18de1eeb6d8254496ecadebf53540459fa76b16739e9fd6df5f30`. It
  includes `suifleet-edge/windows-runtime`. The default build closure for the
  same commit has 100 files, SHA-256 `6f8a587e…`; it is not published.
- **Anonymous source audit.** `check_layer_sources.py --repo . --fetch --cargo
  <rustup 1.94.0 cargo>`, run with an empty `HOME`, no GitHub tokens and no
  `.netrc`, reported `ok` with no findings. It verified:
  - daemon `4627f17b…` (792,958 bytes): Cargo.lock `c2dcebe1…`, crate version
    0.2.3, 350 crates matching `suifleet-crates.inc`, LICENSE md5
    `11afb1fb…`, the workspace members and include targets, and `cargo metadata
    --offline --locked --no-deps`;
  - codec `fae67d9c…`: its exact five pinned files, provenance revision
    `b9bb2ef6…`, licenses and Node version;
  - Node `e9e1930f…` (22.22.2, linux-arm64).
- **BitBake build of the published layout (Wrynose).** OE-Core `0f03c732`,
  BitBake 2.18.0, meta-oe `f28a144a`, `MACHINE = "qemuarm64"`, `DISTRO =
  "nodistro"` with systemd and usrmerge, `INHERIT += "suifleet-core-fixes"`,
  and this branch's layer, without the integration layer. The build directory
  was new. `DL_DIR` started empty, and `PREMIRRORS`/`MIRRORS` were cleared for
  `suifleet`, `suifleet-walrus-codec` and `suifleet-node`, so BitBake fetched
  their sources (the daemon archive from `raw.githubusercontent.com`, 350 crates
  from `static.crates.io`, the codec archive and Node) with wget and no
  credentials. Other recipes used existing sstate and downloads as read-only
  mirrors.
  - `bitbake -c unpack suifleet suifleet-walrus-codec suifleet-node`: exit 0.
  - `bitbake -c package_qa suifleet suifleet-walrus-codec suifleet-node`:
    exit 0, 1817 tasks attempted (1132 restored), all succeeded.
  - `suifleet` (`PKGE` 1, `PKGV` 0.2.3) ran `do_verify_lock`,
    `do_verify_toolchain` (rustc 1.94.1), `do_configure`, `do_compile` (the Cargo
    build), `do_install`, `do_package` and `do_package_qa`, with no QA warnings.
    The package holds an AArch64 `/usr/bin/suifleet`, the systemd units, the
    provisioning helper, `*.example` configuration files and `build-info`
    (`source_revision=b9bb2ef6…`, `lock_sha256=c2dcebe1…`,
    `crate_version=0.2.3`). It contains no device key, enrollment
    configuration or journal.
  - `suifleet-walrus-codec` ran `do_verify_codec`, `do_install`, `do_package`
    and `do_package_qa`. `suifleet-node` ran `do_install`, `do_package` and
    `do_package_qa`.

- **BitBake builds of the published layout (Scarthgap and Kirkstone).** Same
  method as Wrynose: a new build directory per series, an empty `DL_DIR`,
  `PREMIRRORS`/`MIRRORS` cleared for the three recipes, other recipes reading
  existing sstate and downloads as read-only mirrors. `MACHINE = "qemuarm64"`,
  `DISTRO = "poky"` with systemd and usrmerge, `INHERIT +=
  "suifleet-core-fixes"`, and this branch's layer without the integration
  layer. Every layer is at the full revision pinned in the series' kas profile.
  - Scarthgap (`kas/packaging.yml`): poky `a53cae3d`, BitBake 2.8.1, meta-oe
    `ae7dfb12`, meta-lts-mixins `scarthgap/rust` `eedcdc74`
    (rustc 1.98.1).
  - Kirkstone (`kas/packaging-kirkstone.yml`): poky `445a6223`, BitBake 2.0.0,
    meta-oe `ce8539c9`, meta-lts-mixins `kirkstone/rust` `6baa9afe`
    (rustc 1.92.0).
  - On both series, BitBake fetched the daemon archive from
    `raw.githubusercontent.com` (SHA-256 `4627f17b…`), 350 crates from
    `static.crates.io`, the codec archive (`fae67d9c…`) and Node
    (`e9e1930f…`), with wget and no credentials.
  - `bitbake -c unpack suifleet suifleet-walrus-codec suifleet-node`: exit 0 on
    both.
  - `bitbake -c package_qa suifleet suifleet-walrus-codec suifleet-node`: exit 0
    on both. Kirkstone attempted 1490 tasks (927 restored), all succeeded.
    The first Scarthgap attempt failed when the host's OOM killer stopped the
    daemon's LTO `rustc` while other agents were building. The rerun, with
    nothing changed, resumed at `do_compile` and succeeded (1733 tasks, 1728
    already done).
  - `suifleet` (`PKGV` 0.2.3) ran `do_verify_lock`, `do_verify_toolchain`,
    `do_compile`, `do_install`, `do_package` and `do_package_qa` on both, with
    no QA issues. Each package holds an AArch64 `/usr/bin/suifleet`, the same
    systemd units, provisioning helper and `*.example` files as Wrynose, and
    `build-info` with `source_revision=b9bb2ef6…`, `lock_sha256=c2dcebe1…` and
    `crate_version=0.2.3`. No device key, enrollment configuration or journal.
  - `suifleet-walrus-codec` ran `do_verify_codec` and `do_package_qa`, and
    `suifleet-node` ran `do_package_qa`, on both.

- **Packaged daemon runtime check (Wrynose, 2026-11-26).** The same Wrynose
  build directory, configuration and layer pins were rebuilt from scratch
  (`tmp/` and the local sstate had been deleted; `DL_DIR` was kept). `bitbake -c
  package_qa suifleet` exited 0 (1792 tasks, 1228 not rerun, all succeeded).
  The packaged `/usr/bin/suifleet` has SHA-256 `0528b5e1…`, identical to the
  first Wrynose build. A scratch root was assembled from the recipe's target
  sysroot with the `suifleet` package on top. `scripts/test_pi_binary.py
  --qemu <Wrynose qemu-native 10.2.0 qemu-aarch64> --rootfs <scratch root>`
  ran the packaged AArch64 binary in user-mode QEMU and passed, covering:
  - fresh identity provisioning, preservation of an existing key, address
    derivation and refusal of a corrupt key;
  - `--self-check` against the localnet fixture and the shipped
    `profiles/seal-testnet` manifest, which reported crate version 0.2.3 from
    `build-info`, plus refusal of unsupported protocol and client ranges;
  - `--key-holder` for both signer modes, and refusal of daemon self-update
    with the service signer.

  This executes the packaged binary's offline commands only. It is not a
  systemd service run, an image boot or a networked OTA.

These are package builds, not image builds. Kirkstone's runtime limits (no
`SO_PEERPIDFD`, weaker signer binding) are unchanged and recorded in
`docs/YOCTO_LEGACY_SERIES.md`. Any other Kirkstone standalone claims in
[YOCTO_STANDALONE_VALIDATION.md](YOCTO_STANDALONE_VALIDATION.md) (for example,
the task-signature comparison) are separate and still unrun.

Not run for SF-306: an image build, a systemd or networked run of the packaged
daemon, packaged runtime checks on Scarthgap and Kirkstone, physical-target acceptance, Layer Index submission and mainnet acceptance. Physical and mainnet acceptance
remain in [#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159).

## Earlier record (2026-11-24, superseded archive)

The first published daemon archive (`suifleet-edge-212d989a….tar.gz`, SHA-256
`0ec75c6b…`, 132 files) left out the workspace member
`suifleet-edge/windows-runtime`, so `cargo metadata --offline` failed in a fresh
unpack. The builder reproduced it byte for byte, and the gap was reported in
[SF-295](https://github.com/tyler-suifleet/suifleet/issues/820). SF-295 then
published the corrected archive above under a new filename and merged its pins.
