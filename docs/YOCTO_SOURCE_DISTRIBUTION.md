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

The archive is `git archive --format=tar <commit> -- <paths> | gzip -n -6`. It
reads committed objects only: working-tree edits and untracked files can't
enter it. Contents sit at the archive root (recipes unpack with `subdir=`).
Ownership is root, timestamps are the commit time, and `gzip -n` stores no name
or time, so the same commit, git and gzip produce identical bytes. The output
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
  lives under `suifleet-edge/`. Cargo refuses a manifest whose declared target
  file is absent. A declared file that is missing, absolute or outside
  `suifleet-edge/`, a non-string target `path` and a `package.build` that is
  neither a string nor a boolean stop the build. Today the only declaration is
  `[[bin]] path = "src/main.rs"`, already under `src/`;
- each file named by a literal `include!`/`include_str!`/`include_bytes!` or a
  `#[path]` module attribute, followed transitively. Every macro delimiter
  (`(..)`, `[..]`, `{..}`), spacing, path prefix (`std::include!`), raw string
  and a `path` inside `cfg_attr(..)` is recognized. An include or `#[path]`
  whose argument is not one string literal without escapes (for example
  `include!(concat!(env!("OUT_DIR"), ..))`) stops the build instead of being
  skipped. The scan is lexical, so comments and strings count too.

`src/`, `examples/` and each local crate directory are packaged whole, so Rust
module files there are always present. Outside them only the declared files,
the default `build.rs` and their literal include/`#[path]` targets are
packaged. An implicit out-of-line module (`mod name;` without a literal
`#[path]`) in one of those files would make Cargo also read a sibling
`name.rs` or `name/mod.rs` that the closure does not include. The build stops
instead: give the module a `#[path]` or move the target under `src/`.
The same check applies to a file outside those directories that a file inside
them reaches through a literal include or `#[path]`, such as
`#[path = "../tools/entry.rs"]` in `src/main.rs`.
A `cfg_attr` path is packaged but still counts as implicit for this check. Inline `mod name { ... }` blocks are fine.

A missing workspace member, local path dependency, declared target file or include target, a path outside the
repository, a symlink or submodule, or a runtime identity/state file name
(`device.key`, `config.toml`, `*.key`, journals, databases and so on) stops the
build. Contents are not pattern-scanned for keys, because the redaction test
vectors deliberately contain fake ones. Build only from reviewed main, which
the [security CI](SECURITY_CI.md) secret scanning already covers.

Before writing, the builder reads back the tar stream `git archive` produced.
It must contain exactly the planned files and their directories, each with the
committed blob's bytes and executable bit. A committed `export-ignore` or
`export-subst` attribute that drops or rewrites a build input therefore stops
the build, instead of yielding an incomplete archive that reports success.

`--paths-from FILE` rebuilds an archive from an existing file list, with the
same audit. Use it to reproduce a previously published archive, or to keep the
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
- https sources have a SHA-256 (as a `SRC_URI[...]` flag or inline). They may
  not use a query string, a fragment or credentials. They may not use a GitHub
  branch/tag archive, a `raw.githubusercontent.com` or codeload URL at a
  non-commit ref, a `releases/latest` redirect or an expiring release-asset
  host.
- Every `crate://` entry has a SHA-256.
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
  BitBake tries every `FILESOVERRIDES` subdirectory (machine, distro,
  architecture) of each search directory before the unqualified ones, so a
  same-named file such as `files/qemuarm64/suifleet.service` replaces
  `files/suifleet.service` on that machine. The audit does not model
  per-configuration overrides, so any such candidate is rejected.
- Source-affecting variables take no conditional overrides. These are
  `SRC_URI` (and its flags), `SRCREV*`, `PV`, `PE`, `S`, `LICENSE`,
  `LIC_FILES_CHKSUM`, `FILESEXTRAPATHS`, `CARGO_SRC_DIR`, every `SUIFLEET_*`
  variable, and any variable they reference. Only a plain `:append` or
  `:prepend` is accepted. Machine, distro, class, `pn-` and `:remove`
  overrides are rejected, because the reader doesn't evaluate them. So are
  flagged appends and `?=`/`??=` appends. That way the audited assignment is
  the one BitBake uses in every configuration.
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
  standalone layer doesn't contain.
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
  named in an archive filename equals `SRCREV`.

`--repo` reads `SRCREV` from a Git checkout. It checks the Cargo.lock digest,
the crate version, the LICENSE md5 and that `suifleet-crates.inc` lists exactly
the lockfile's registry crates, versions and checksums. The crate include is
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

It then downloads each non-crate source into a new directory. Downloads send
no Authorization header and don't read `.netrc`. Git uses an empty credential
helper, no global or system configuration and no prompts. Each SHA-256 is
checked before unpacking. Unpacking follows BitBake `subdir=`/`destsuffix=`
semantics, and members or links that escape the directory are refused. The
check then verifies the resulting `S`:

- for every recipe, no runtime identity/state file name anywhere in the fetched
  and unpacked sources;
- for every recipe, the license file checksums;
- for the daemon, the lockfile digest, crate version, crate include, workspace
  members, transitive local path-dependency crates, manifest-declared target
  files, every include target and the same implicit-module refusal outside the
  directories packaged whole, plus `cargo metadata --offline --locked
  --no-deps` when `--cargo` is set;
- for the codec, the exact five pinned files, their digests, provenance
  revision, licenses and Node version.

The JSON report maps each recipe to its PV, crate version, source revision,
source URLs and SHA-256, license files and crate count, plus the advertised
`LAYERSERIES_COMPAT`. That report is the per-series version/source/license
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
