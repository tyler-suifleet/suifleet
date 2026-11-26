# meta-suifleet

Software layer for the SuiFleet Linux edge daemon, isolated signing service,
first-boot provisioning and optional keyless Walrus archive runtime.
Maintainer: Tyler Bush <tyler.jonathan.bush@gmail.com>.
Metadata is MIT licensed (COPYING.MIT); each recipe declares its payload licenses.

## Availability and supported configurations

The public standalone repository is https://github.com/tyler-suifleet/meta-suifleet.
Layer Index submission is deferred. The daemon package is version 0.2.3 at
`b9bb2ef6210624d732ab2de2075ea1826e5300c1`, the source pin used for the
integrated build validation. The daemon and optional codec fetch immutable
HTTPS archives with pinned SHA-256 checksums.

Declared series: kirkstone, scarthgap and wrynose, using only the full revisions
in the core repository's `kas/packaging-kirkstone.yml`, `kas/packaging.yml` and
`kas/packaging-wrynose.yml`. Kirkstone and Scarthgap require the pinned
meta-lts-mixins `kirkstone/rust` and `scarthgap/rust` toolchains respectively;
Wrynose uses core Rust. Kirkstone's 5.10/5.15 kernels lack `SO_PEERPIDFD`, so
its signer runs the weaker caller binding recorded in the core repository's
`docs/YOCTO_LEGACY_SERIES.md`; Dunfell has no usable Rust and is not supported. Rust >= 1.90 is checked by the recipe. Dependencies are
OE-Core (`core`) and meta-openembedded/meta-oe (`openembedded-layer`). Systemd,
usrmerge and a writable persistent root were used in the validated integrated
builds described below.
The archive runtime's prebuilt Node recipe supports Linux AArch64/glibc only.
See RELEASE.md for the validation and branch requirements.

Add this directory to BBLAYERS and run `bitbake suifleet`. Optional archive
packages are `suifleet-node` and `suifleet-walrus-codec`; the codec fetches its
pinned archive when built. They are not needed to parse the layer or build
`suifleet`. If a bbappend replaces the codec input with local `file://` files
that are absent, the codec recipe is skipped with a reason naming them, and
requesting it fails with that reason. Both recipes are skipped, with a reason,
on any target other than Linux AArch64 glibc. There is no musl, 32-bit or x86
fallback.

The separate, core-repository `meta-suifleet-integration` contains Pi image/access
policy, test components and a glibc usrmerge layout guard for its reused build
caches. Core kas profiles also include the board-independent `meta-suifleet-enrollment`
companion layer for automatic first-boot enrollment. See the
[customer Yocto guide](https://github.com/tyler-suifleet/suifleet/blob/main/docs/CUSTOMER_YOCTO.md). Generic consumers should select
their own BSP/distro and must not add the integration layer without reviewing its
policy.

### Opt-in OE-Core fixes

`classes/suifleet-core-fixes.bbclass` carries the OE-Core recipe fixes used by the
recorded builds; the layer never inherits it. If your distro hits these defects,
enable it explicitly in your distro configuration or local.conf:

```
INHERIT += "suifleet-core-fixes"
```

It changes only these recipes and suppresses no QA:

- `openssl`, all variants and declared series (including Kirkstone's OpenSSL
  3.0): `PARALLEL_MAKEINST = "-j1"`. This works around a recursive `install_sw`
  relink race. The setting does not change task signatures.
- `libcap`, Wrynose: passes the cross toolchain to the install-time `capsh`
  relink, which otherwise falls back to the host gcc.
- `xz`, Wrynose target: adds bash to RDEPENDS. If the generated `xzmore` uses
  `#!/bin/bash`, it is pointed at `${bindir}/bash` so it satisfies file-rdeps
  package QA. Pinned OE-Core normally generates `#!/bin/sh`, which makes the
  rewrite a no-op.

Each was still unfixed on OE-Core scarthgap, wrynose and master on 2026-11-24.
The integration layer inherits the same class; identical task signatures were
verified on the Scarthgap and Wrynose profiles only (no Kirkstone comparison has
been run). Validation, including Scarthgap and Wrynose builds
from the extracted export without the integration layer, is recorded in the core
repository's `docs/YOCTO_STANDALONE_VALIDATION.md`. Apart from the class, that
standalone configuration requires OE-Core, meta-oe, the Scarthgap Rust mixin, and
the systemd/usrmerge distro features listed above. It needs no other distro
setting. The earlier SF-309 Kirkstone recipe builds used the integration layer. Its
layer-check pass covered the export before the opt-in class. SF-307 later built
`suifleet`, `suifleet-node` and `suifleet-walrus-codec` on Kirkstone from the
export with the class, the Kirkstone Rust mixin and no integration layer, from the
public pins; see the core repository's `docs/validation/SF307.md`. See
[SF-295](https://github.com/tyler-suifleet/suifleet/issues/820) for standalone
validation of the public source distribution and RELEASE.md for release criteria.

Builds contain examples only: no device keys, enrollment configuration, signer
PINs or journals. Provision identities at runtime. Preserve /etc/suifleet and
/var/lib/suifleet across upgrades. Platform tools own OS installation, reboot and
firmware recovery; the daemon does not establish healthy boot.

## Contributions

Send patches through pull requests in the core SuiFleet repository and reference
the existing issue. Never include credentials or live
state. Keep source revision, crate include, lock digest and codec provenance in
sync; retain signing/identity boundaries and package QA. Supply exact recipe
build and yocto-check-layer logs for every declared series, artifact checksums and
limitations. Hardware, mainnet and production-provider acceptance are separate.
See RELEASE.md for export and submission procedure.
