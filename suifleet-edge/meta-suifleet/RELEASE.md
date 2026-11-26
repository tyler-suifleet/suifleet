# Release and Layer Index submission

## Source distributions

The recipes fetch SHA-256-pinned tarballs at immutable Git blob paths in
[the source-distributions branch](https://github.com/tyler-suifleet/meta-suifleet/tree/source-distributions).
The daemon archive contains its Cargo files, every Cargo workspace member
(including `suifleet-edge/windows-runtime`), examples, source, packaging
inputs, shared interfaces, test fixtures and root MIT license. The codec archive
contains the five files produced by the core repository's
`scripts/package_walrus_codec.py`, including third-party notices and
source provenance.

For updates, export committed files with `git archive` from reviewed core
main. Audit the exact file set for private state and credentials before
publishing. Extract the daemon archive in an empty directory and run
`cargo metadata --offline --locked --no-deps --manifest-path suifleet-edge/Cargo.toml`
to check the workspace from the archive alone. Build the codec from the same commit using that commit's frozen
pnpm lockfile and Node version. Normalize tar ownership and timestamps and
compress both archives with `gzip -n`. Publish under new filenames containing
the full source commit. Verify anonymous downloads and update the two archive
URLs and SHA-256 values, the source revision and version, the codec file
hashes and license metadata in the same change. Run BitBake
`-c update_crates suifleet` and verify the lock digest and generated include.

Build `suifleet`, `suifleet-node` and `suifleet-walrus-codec`, and run
`yocto-check-layer` against the standalone checkout on each advertised series.
Use a fresh build directory for validation and record the dependency commits,
compiler, task outcomes and package QA. Keep downstream image access policy in the separate integration layer. Enable
`INHERIT += "suifleet-core-fixes"` explicitly to reproduce the standalone
configuration described in README.md. Release branches require recipe and
layer-check evidence for each series at the published pin.

## Layer Index worksheet

Use the [OpenEmbedded Layer Index form](https://layers.openembedded.org/layerindex/submit/).

| Field | Value |
| --- | --- |
| Name | meta-suifleet |
| Layer type | Software |
| Summary | SuiFleet edge client and isolated signing service |
| Repository | https://github.com/tyler-suifleet/meta-suifleet.git |
| Web URL | https://github.com/tyler-suifleet/meta-suifleet |
| Subdirectory | Leave empty, the layer is at the repository root |
| Series branches | kirkstone, scarthgap and wrynose, after checking each remote ref and its build receipt |
| Dependencies | openembedded-core and meta-oe |
| Maintainer | Tyler Bush, tyler.jonathan.bush@gmail.com |
| License | MIT metadata, with recipe-specific payload licenses |

The CAPTCHA submission remains deferred. Record submission confirmation and the listing URL separately in
[SF-312](https://github.com/tyler-suifleet/suifleet/issues/838).
The Index entry and optional Yocto Project Compatible certification have
separate requirements.

## Core mirror

The core checkout retains a copy of the software-layer files used by its
packaging tests and native Linux installation instructions. Export committed
layer files with the core script
`python3 scripts/export_yocto_layer.py --revision <commit> --output <new-archive>`.
The script uses the explicit file inventory and emits core-commit and per-file
hash provenance. Diff release metadata against that inventory when updating
the downstream kas pins.

## Compatibility branches and maintenance

Main owns common metadata and the CI harness. Maintain `kirkstone`,
`scarthgap` and `wrynose` as protected release branches. Changes land via
reviewed PRs. Forward-port common changes to all supported branches and keep
series-specific fixes in their branches. Test the exact release commit before
tagging it. The matrix covers every series in `conf/layer.conf` with immutable
OE-Core, BitBake where separate, meta-oe and Rust mixin pins in `ci/pins.json`.
Changes to the series list update the matrix, pins, core kas profiles and
compatibility declaration together.

The standalone workflow checks public inputs and crate hashes, runs
`yocto-check-layer`, then fetches and builds the daemon, Node and codec IPKs
with license checks and SPDX output. It uses qemuarm64 and no downstream image
or login configuration. An optional manual workflow input runs the packaged
Node and the offline codec negative case in AArch64 QEMU user mode, which uses
the host kernel. Keep systemd boot evidence and physical-target acceptance in
their respective test records. For a local reproduction from the layer root,
run `python3 ci/sources.py` and `ci/build.sh <series> <new-build-directory> all`.
The CI job lists Ubuntu host prerequisites. Use a fresh build directory with
enough build disk for a Rust/LLVM toolchain. Local tests may use read-only
sstate mirrors, provided the receipt identifies them and lists the tasks run.
Record the compiler, exact dependency revisions and any skipped checks.

The core export is the source of shared layer files. For shared changes use one
core ticket, review the export there, then have the publication owner apply the
reviewed export to the standalone repository. Confirm the anonymous public
branches and export provenance before proposing an update to the core kas pins.
If the daemon or codec changes, keep the reviewed core source commit, published
archives, lock checksum, crate include and recipe/license pins together through
the source distribution procedure above. Use annotated release tags on tested
standalone commits. Record the exact layer commit, covered series, core commit,
archive source revisions and CI/build receipt in the tag or release notes.
Publish tags only after review and confirmation of the corresponding public
branch commits.

Use the core issue and its existing atomic claim for the export and its
publication handoff. For future standalone-only tickets, register the canonical
standalone checkout with its own namespace in the same atomic coordinator and
configure one coordinator project entry. Keep dispatch disabled until that
registration, the repository's protected branches, review routing and remote
identity have been verified. Link corresponding issues without duplicating the
active implementation claim. GitHub PR jobs use read-only checkout and need
neither publishing credentials nor a signing wallet. The public source branch,
compatibility branch publication, Layer Index submission and certification each
have independent receipts.
