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
[SF-295](https://github.com/tyler-suifleet/suifleet/issues/820).
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
