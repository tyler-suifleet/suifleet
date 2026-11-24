# Yocto source distributions and pin audit (SF-306)

The meta-suifleet daemon recipe builds from an immutable, checksum-pinned
source archive rather than the core monorepo, so that a clean host can build
without private access, developer caches or a neighboring checkout.
[SF-295](https://github.com/tyler-suifleet/suifleet/issues/820) owns the recipe
pin lines, the public `tyler-suifleet/meta-suifleet` repository and Layer Index
submission. This page covers the tools for producing and auditing those inputs.
This is source/fetch evidence, not a BitBake build, physical-target or mainnet
acceptance.

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
`--paths-from FILE` rebuilds an archive from an existing file list, with the
same audit. Use it to reproduce a previously published archive.

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
- `file://` entries exist inside the layer, and `FILESEXTRAPATHS` doesn't point
  outside it. This rejects the pre-parse `${TOPDIR}` codec artifacts.
- The layer and `export-files.json` contain no runtime identity/state files.
- The coupled pins agree with each other. `PV` equals `SUIFLEET_CRATE_VERSION`.
  The codec `SUIFLEET_CODEC_SOURCE_REV` equals the daemon `SRCREV`. A commit
  named in an archive filename equals `SRCREV`.

`--repo` reads `SRCREV` from a Git checkout. It checks the Cargo.lock digest,
the crate version, the LICENSE md5 and that `suifleet-crates.inc` lists exactly
the lockfile's registry crates, versions and checksums. The crate include is
still regenerated with `bitbake -c update_crates suifleet`; this check confirms
the committed result matches.

`--fetch` downloads each non-crate source into a new directory. Downloads send
no Authorization header and don't read `.netrc`. Git uses an empty credential
helper, no global or system configuration and no prompts. Each SHA-256 is
checked before unpacking. Unpacking follows BitBake `subdir=`/`destsuffix=`
semantics, and members or links that escape the directory are refused. The
check then verifies the resulting `S`:

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
the repository layer has no source findings beyond the codec local-artifact
entries SF-295 is replacing.

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

## Validation record (2026-11-24)

Run on the WSL host by Claude (claude-opus-5-5), with git 2.34.1, GNU gzip
1.10, Python 3.10.12 and Rust 1.94.0.

- `package_edge_source.py --revision 212d989a --paths-from <published list>`
  reproduced the published SF-295 daemon archive byte for byte:
  `suifleet-edge-212d989af09f847e7f60941a6814b7e6715e997b.tar.gz`, SHA-256
  `0ec75c6b56b7e4a0fa40705ef3a28547a675b50aef6550f40a0d4a7957d7257d`, 132 files.
- **That published archive can't build.** It leaves out the workspace member
  `suifleet-edge/windows-runtime`. In a fresh unpack, `cargo metadata --offline`
  fails with `failed to load manifest for workspace member`. The cargo class
  `do_compile` loads the same workspace. This was reported in
  [SF-295](https://github.com/tyler-suifleet/suifleet/issues/820); the archive
  needs republishing under a new filename.
- The closure archive for 212d989a has 101 files, SHA-256
  `31a5e30c6e74a53111c6617fa132371de9a5d1ce36163eb042e754bb5ef41b4d`, and was
  identical across two builds. It includes `windows-runtime`, and `cargo
  metadata --offline --locked --no-deps` loads both workspace packages. It is
  not published.
- `check_layer_sources.py --fetch --cargo "cargo +1.94.0"` on a scratch copy of
  the SF-295 layer anonymously verified all three archives:
  - daemon `0ec75c6b…`;
  - codec `208d4b8f…`, whose five files, provenance, licenses and md5 match;
  - Node `e9e1930f…`.
  It reported only the missing workspace member and the PV mismatch (fixed
  here).
- On this branch's layer, anonymous Git fetch of the daemon at `b9bb2ef6…`
  passed every check: lock, 350 crates, LICENSE md5, workspace and includes,
  and cargo metadata. The static audit reports only the codec `${TOPDIR}`
  local-artifact findings, which SF-295 replaces.

No BitBake task execution was run for SF-306. The host had about 3 GB of free
memory, and three other Yocto builds were holding locks. The SF-295 standalone
builds reached `do_unpack` but not compilation. Compiling the recipe from a
corrected published archive remains outstanding.
