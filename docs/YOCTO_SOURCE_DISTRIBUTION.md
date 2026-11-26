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
- every `[workspace] members` directory (currently `windows-runtime`);
- each file named by a literal `include_str!`/`include_bytes!` or a `#[path]`
  module attribute, followed transitively.

A missing workspace member, a missing include target, a path outside the
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
written. Each destination is checked again just before writing, in case an
earlier source unpacked a symlink on the path.

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
  members and every include target, plus `cargo metadata --offline --locked
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

Not run for SF-306: a Scarthgap or Kirkstone build of these published pins
(SF-295 recorded Scarthgap/Wrynose builds from an earlier Git-sourced export),
an image build, executing the packaged daemon, physical-target acceptance,
Layer Index submission and mainnet acceptance. Physical and mainnet acceptance
remain in [#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159).

## Earlier record (2026-11-24, superseded archive)

The first published daemon archive (`suifleet-edge-212d989a….tar.gz`, SHA-256
`0ec75c6b…`, 132 files) left out the workspace member
`suifleet-edge/windows-runtime`, so `cargo metadata --offline` failed in a fresh
unpack. The builder reproduced it byte for byte, and the gap was reported in
[SF-295](https://github.com/tyler-suifleet/suifleet/issues/820). SF-295 then
published the corrected archive above under a new filename and merged its pins.
