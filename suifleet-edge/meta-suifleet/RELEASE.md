# Standalone release and index submission checklist

Publication is not performed by the export tool. A release operator needs explicit
authorization to create/publish a public repository and submit to the index.
Do not ship an export as customer-installable until the source prerequisites below
are publicly fetchable without credentials and their checks have passed.

## Source availability and synchronization

The current daemon recipe still fetches the core Git repository at immutable
SRCREV; a layer export alone does not make that repository public. Before public
layer release, either verify anonymous fetch of that exact revision or publish a
reviewed immutable source distribution. For the independent distribution option:

1. Export the complete source tree at SRCREV (including root LICENSE and the
   suifleet-edge directory), after a credential/private-state audit. Publish it
   at an immutable versioned HTTPS URL; preserve its SHA-256 and license texts.
   Do not use a mutable branch archive or a signed/expiring URL.
2. In a reviewed layer change replace only the Git entry in SRC_URI with that
   URL plus `;name=edge;subdir=git` for a tarball whose contents are at its root;
   set `SRC_URI[edge.sha256sum]` to the verified archive hash. Keep SRCREV as
   source provenance for build-info and keep S pointing at the unpacked git
   directory. Validate the actual archive layout in both unpack implementations.
   Never invent a URL/checksum or silently fall back to private credentials.
3. Verify Cargo.lock against SUIFLEET_LOCK_SHA256 and regenerate
   suifleet-crates.inc with `bitbake -c update_crates suifleet` at that source.
   Check SUIFLEET_CRATE_VERSION against Cargo.toml. Commit all coupled pins.
4. Build the codec at the identical revision with frozen pnpm dependencies using
   `scripts/package_walrus_codec.py` from the core repository. Publish the five
   immutable artifacts with all third-party notices. Update the codec recipe's
   source revision, five SHA-256 values, LICENSE and license md5 together. Either
   distribute these files for SUIFLEET_WALRUS_CODEC_DIR or implement and test
   checksum-pinned public fetch entries before advertising archive availability.
5. Run recipe builds, package QA, image inspection and yocto-check-layer against
   both pinned series; preserve compiler/layer revisions and artifact hashes.
   Check anonymous source/artifact downloads from a clean credential-free host.
   Never bundle runtime identities, config.toml, device.key, PINs or journals.

This publication step is outstanding; existing historical build evidence in the
core docs is not evidence that a public distribution is downloadable today.

## Reproducible export

From a reviewed, committed core checkout:

```
python3 scripts/export_yocto_layer.py --revision <full-reviewed-commit> --output /tmp/meta-suifleet.tar.gz
sha256sum /tmp/meta-suifleet.tar.gz
```

Only Git blobs in export-files.json are included; untracked and modified working
files cannot enter the archive. The root is meta-suifleet/, ownership/timestamps
are normalized, file modes retained and export-provenance.json records the core
commit plus every file digest. Repeat into another output and compare SHA-256.
Review additions to the explicit file list as release inputs. Extract into a
fresh directory; validate that exported layer, not the monorepo path.

## OpenEmbedded Layer Index submission worksheet

Reference: https://layers.openembedded.org/ and the Yocto layer guide at
https://docs.yoctoproject.org/dev/dev-manual/layers.html .

Current actual repository: https://github.com/tyler-suifleet/suifleet.git;
current development branch: main; current subdirectory:
suifleet-edge/meta-suifleet. Do not submit this restricted/unpublished source
configuration as an available standalone distribution.

After authorized standalone publication, fill the form using verified values:

| Field | Value / required verification |
| --- | --- |
| Name | meta-suifleet |
| Layer type | Software |
| Summary | SuiFleet edge client and isolated signing service |
| Repository | Actual authorized public standalone Git clone URL; not allocated yet |
| Web URL | Actual public repository browser URL |
| Subdirectory | Empty for exported repository root |
| Branch | Actual published branch for each validated series; none published yet |
| Dependencies | openembedded-core and meta-openembedded/meta-oe; verify index identities |
| Maintainer | Tyler Bush, tyler.jonathan.bush@gmail.com |
| License | MIT metadata; recipe payloads retain their own licenses |

Create scarthgap/wrynose branches only after each exact branch has build/check
receipts; do not submit master/main as proof of both. Verify remote IDs and
anonymous clone, then submit and record the real resulting listing URL and
maintainer acceptance in SF-230/#518. No repository or listing is claimed here.

`yocto-check-layer /absolute/path/meta-suifleet --dependency /path/meta-oe`
(with the release's build environment initialized) is a compatibility test,
not index publication. Include required Rust mixin and codec artifact path in
that environment. Keep any failures explicit. Optional Yocto Project Compatible
branding has a separate application/eligibility process; an index listing or
passing script grants no logo rights.
